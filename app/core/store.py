from __future__ import annotations

import json
import time
from typing import Any

import asyncpg
import redis.asyncio as aioredis

from app.core.config import settings

SCHEMA_LOCK_ID = 7_300_001

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS threads (
    thread_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    messages_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    traceparent TEXT NOT NULL,
    status TEXT NOT NULL,
    primary_model TEXT NOT NULL,
    planner_model TEXT NOT NULL,
    active_skills_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    pending_tool_calls_json JSONB,
    messages_checkpoint_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    last_step_had_error BOOLEAN NOT NULL DEFAULT FALSE,
    result_text TEXT,
    total_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    max_steps INTEGER NOT NULL DEFAULT 15,
    max_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0.30,
    step_count INTEGER NOT NULL DEFAULT 0,
    fingerprints_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    webhook_url TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS runs_thread_idx ON runs (thread_id);
"""


class ThreadOwnershipError(Exception):
    """The thread exists but belongs to a different tenant."""


def _loads(value: Any) -> Any:
    # asyncpg returns JSONB as `str` unless a codec is registered.
    return json.loads(value) if isinstance(value, str) else value


class DistributedStore:
    def __init__(self) -> None:
        self.pg_pool: asyncpg.Pool | None = None
        self.redis: aioredis.Redis | None = None

    async def init(self, *, ensure_schema: bool = False, pool_max: int = 15) -> None:
        self.pg_pool = await asyncpg.create_pool(
            dsn=settings.postgres_dsn, min_size=1, max_size=pool_max
        )
        self.redis = aioredis.from_url(settings.redis_url, decode_responses=True)

        if ensure_schema:
            # Several API replicas boot concurrently; CREATE TABLE IF NOT EXISTS is not
            # race-free in PostgreSQL, so serialise DDL with an advisory lock.
            async with self.pg_pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock($1)", SCHEMA_LOCK_ID
                    )
                    await conn.execute(SCHEMA_SQL)

    async def close(self) -> None:
        if self.pg_pool:
            await self.pg_pool.close()
        if self.redis:
            await self.redis.aclose()

    @property
    def pool(self) -> asyncpg.Pool:
        assert self.pg_pool is not None, "PostgreSQL pool not initialized"
        return self.pg_pool

    @property
    def rds(self) -> aioredis.Redis:
        assert self.redis is not None, "Redis client not initialized"
        return self.redis

    # --- Redis Ephemeral Vault & Idempotency Locks ---

    async def store_ephemeral_secrets(
        self, run_id: str, secrets: dict[str, str], ttl_sec: int = 3600
    ) -> None:
        if secrets:
            await self.rds.setex(f"haas:secrets:{run_id}", ttl_sec, json.dumps(secrets))

    async def get_scoped_secrets(
        self, run_id: str, allowed_keys: set[str]
    ) -> dict[str, str]:
        raw = await self.rds.get(f"haas:secrets:{run_id}")
        if not raw:
            return {}
        full: dict[str, str] = json.loads(raw)
        return {k: v for k, v in full.items() if k in allowed_keys}

    async def clear_ephemeral_secrets(self, run_id: str) -> None:
        await self.rds.delete(f"haas:secrets:{run_id}")

    async def acquire_state_lock(self, key: str, ttl_sec: int = 15) -> bool:
        return bool(await self.rds.set(f"haas:lock:{key}", "1", nx=True, ex=ttl_sec))

    async def release_state_lock(self, key: str) -> None:
        await self.rds.delete(f"haas:lock:{key}")

    # --- Redis Streams Real-Time Event Bus (XADD / XREAD) ---

    async def publish_event(
        self, run_id: str, event_type: str, payload: dict[str, Any]
    ) -> str:
        stream_key = f"haas:stream:{run_id}"
        entry_id = await self.rds.xadd(
            stream_key,
            {"event_type": event_type, "payload_json": json.dumps(payload)},
            maxlen=settings.redis_stream_max_len,
            approximate=True,
        )
        await self.rds.expire(stream_key, 86400)
        return str(entry_id)

    # --- PostgreSQL Thread & Run Persistence ---

    async def get_or_create_thread_messages(
        self, thread_id: str, tenant_id: str
    ) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO threads (thread_id, tenant_id, messages_json, updated_at) "
                "VALUES ($1, $2, '[]'::jsonb, $3) ON CONFLICT (thread_id) DO NOTHING",
                thread_id,
                tenant_id,
                time.time(),
            )
            row = await conn.fetchrow(
                "SELECT tenant_id, messages_json FROM threads WHERE thread_id = $1",
                thread_id,
            )
            assert row is not None
            if row["tenant_id"] != tenant_id:
                raise ThreadOwnershipError(thread_id)
            return _loads(row["messages_json"])

    async def create_run(
        self,
        *,
        run_id: str,
        thread_id: str,
        tenant_id: str,
        traceparent: str,
        primary_model: str,
        planner_model: str,
        initial_skills: list[str],
        initial_messages: list[dict[str, Any]],
        max_steps: int,
        max_cost_usd: float,
        webhook_url: str | None,
    ) -> None:
        now = time.time()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO runs (
                    run_id, thread_id, tenant_id, traceparent, status,
                    primary_model, planner_model, active_skills_json,
                    messages_checkpoint_json, max_steps, max_cost_usd,
                    webhook_url, created_at, updated_at
                ) VALUES (
                    $1, $2, $3, $4, 'running',
                    $5, $6, $7::jsonb,
                    $8::jsonb, $9, $10,
                    $11, $12, $13
                )
                """,
                run_id,
                thread_id,
                tenant_id,
                traceparent,
                primary_model,
                planner_model,
                json.dumps(sorted(initial_skills)),
                json.dumps(initial_messages),
                max_steps,
                max_cost_usd,
                webhook_url,
                now,
                now,
            )

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM runs WHERE run_id = $1", run_id)
        if not row:
            return None
        d = dict(row)
        d["active_skills"] = _loads(d["active_skills_json"])
        d["messages"] = _loads(d["messages_checkpoint_json"])
        d["fingerprints"] = _loads(d["fingerprints_json"])
        d["pending_tool_calls"] = (
            _loads(d["pending_tool_calls_json"])
            if d["pending_tool_calls_json"]
            else None
        )
        return d

    async def claim_resume(self, run_id: str, tenant_id: str) -> bool:
        """Atomically flips `input_required` -> `running`; exactly one caller can win."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE runs SET status = 'running', updated_at = $3 "
                "WHERE run_id = $1 AND tenant_id = $2 AND status = 'input_required' "
                "RETURNING run_id",
                run_id,
                tenant_id,
                time.time(),
            )
        return row is not None

    async def save_checkpoint(
        self,
        run_id: str,
        *,
        step_count: int,
        messages: list[dict[str, Any]],
        active_skills: set[str],
        fingerprints: list[str],
        cost_delta_usd: float = 0.0,
        last_step_had_error: bool = False,
        status: str = "running",
        pending_tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE runs
                SET step_count = $2,
                    messages_checkpoint_json = $3::jsonb,
                    active_skills_json = $4::jsonb,
                    fingerprints_json = $5::jsonb,
                    total_cost_usd = total_cost_usd + $6,
                    last_step_had_error = $7,
                    status = $8,
                    pending_tool_calls_json = $9::jsonb,
                    updated_at = $10
                WHERE run_id = $1
                """,
                run_id,
                step_count,
                json.dumps(messages),
                json.dumps(sorted(active_skills)),
                json.dumps(fingerprints[-20:]),
                cost_delta_usd,
                last_step_had_error,
                status,
                json.dumps(pending_tool_calls)
                if pending_tool_calls is not None
                else None,
                time.time(),
            )

    async def finalize_run(
        self,
        run_id: str,
        thread_id: str,
        status: str,
        result_text: str,
        final_messages: list[dict[str, Any]],
        cost_delta_usd: float = 0.0,
    ) -> None:
        now = time.time()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE runs
                SET status = $2,
                    result_text = $3,
                    messages_checkpoint_json = $4::jsonb,
                    total_cost_usd = total_cost_usd + $5,
                    pending_tool_calls_json = NULL,
                    updated_at = $6
                WHERE run_id = $1
                """,
                run_id,
                status,
                result_text,
                json.dumps(final_messages),
                cost_delta_usd,
                now,
            )
            if status == "completed":
                await conn.execute(
                    "UPDATE threads SET messages_json = $2::jsonb, updated_at = $3 WHERE thread_id = $1",
                    thread_id,
                    json.dumps(final_messages),
                    now,
                )
        await self.clear_ephemeral_secrets(run_id)
