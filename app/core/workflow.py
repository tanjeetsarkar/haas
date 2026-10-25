"""Durable workflow persistence (SDD §5.3/§5.4, ADR-09).

PostgreSQL is the source of truth: every orchestration transition is a fenced compare-and-set
on ``runs.lease_epoch``/``runs.state_version`` that writes its events, outbox messages, tool
invocations and ledger rows in the same transaction. Redis and Celery only carry wakeups and
messages that are safe to duplicate.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from app.core.config import settings
from app.core.envelope import sha256_hex

log = logging.getLogger("haas.workflow")

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
ACTIVE_STATUSES = ("queued", "running", "input_required", "cancelling")
TERMINAL_EVENTS = frozenset({"run_completed", "run_error", "run_cancelled"})

# Stable, documented `run_error.code` values (run-events.v1); `error` stays human-readable.
_ERROR_CODES = (
    ("Exceeded max_steps budget", "budget.max_steps"),
    ("Exceeded max_cost_usd budget", "budget.max_cost"),
    ("Exceeded max_tool_invocations budget", "budget.max_tool_invocations"),
    ("Run deadline exceeded", "budget.deadline"),
    ("Approval expired", "approval.expired"),
    ("Run exceeded its recovery attempts", "recovery.exhausted"),
    ("Orchestrator step failed", "execution.failed"),
)
RUN_ERROR_CODES = frozenset(code for _, code in _ERROR_CODES) | {"run.failed"}
WEBHOOK_SCHEMA = "run-terminal-webhook.v1"


def run_error_code(message: str) -> str:
    """Stable machine code for a `run_error` message (matched on the harness-owned prefix)."""
    text = message.strip()
    for prefix, code in _ERROR_CODES:
        if text.startswith(prefix):
            return code
    return "run.failed"


def _with_error_code(payload: dict[str, Any]) -> dict[str, Any]:
    if "code" in payload:
        return payload
    return {**payload, "code": run_error_code(str(payload.get("error", "")))}


OUTCOME_STATUSES = frozenset({"succeeded", "failed", "unknown", "cancelled"})
UNKNOWN_OUTCOME = (
    "Tool outcome unknown: the invocation was dispatched but no result was recorded "
    "before the timeout. The harness did not retry it because the side effect may "
    "already have happened. Verify externally before repeating it."
)

Publisher = Callable[[dict[str, Any]], Awaitable[None]]
CancelHook = Callable[[str], Awaitable[None]]

TENANT_SCOPE = (
    "COALESCE(r.tenant_uuid::text, r.tenant_id) "
    "= COALESCE(resolve_haas_tenant_uuid($2)::text, $2)"
)


class LeaseLostError(Exception):
    """The run lease or state version changed; the caller must stop without writing."""


class IdempotencyConflictError(Exception):
    """The idempotency key was already used for a different request."""


class IdempotentReplay(Exception):
    def __init__(self, response: dict[str, Any]) -> None:
        super().__init__("idempotent replay")
        self.response = response


@dataclass
class Lease:
    tenant_id: str
    run_id: str
    thread_id: str
    epoch: int
    version: int
    owner: str


@dataclass
class StepTicket:
    step: int
    attempt: int
    model: str
    reserved_usd: Decimal
    attempt_key: str


@dataclass
class Effects:
    outbox: list[UUID] = field(default_factory=list)
    wake: set[str] = field(default_factory=set)
    cancelled: set[str] = field(default_factory=set)
    clear_secrets: set[str] = field(default_factory=set)


def loads(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def decode_run(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["active_skills"] = loads(d["active_skills_json"])
    d["messages"] = loads(d["messages_checkpoint_json"])
    d["fingerprints"] = loads(d["fingerprints_json"])
    d["skill_allowlist"] = (
        loads(d["skill_allowlist_json"])
        if d.get("skill_allowlist_json") is not None
        else None
    )
    d["pending_tool_calls"] = (
        loads(d["pending_tool_calls_json"]) if d["pending_tool_calls_json"] else None
    )
    return d


def dec(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def batch_digest(run_id: str, step: int, tool_calls: list[dict[str, Any]]) -> str:
    return sha256_hex(
        {
            "run_id": run_id,
            "step": step,
            "calls": [
                {
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "arguments": tc["function"].get("arguments") or "",
                }
                for tc in tool_calls
            ],
        }
    )


def effect_key(
    tenant_id: str, run_id: str, step: int, ordinal: int, call_id: str
) -> str:
    return sha256_hex(
        {"t": tenant_id, "r": run_id, "s": step, "o": ordinal, "c": call_id}
    )


def _utc(ts: float | None = None) -> datetime:
    return datetime.fromtimestamp(time.time() if ts is None else ts, tz=timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


class WorkflowMixin:
    """Mixed into ``DistributedStore``; relies on ``connection()``, ``rds`` and audit helpers."""

    outbox_publisher: Publisher | None = None
    cancel_hooks: list[CancelHook] = []

    # The host store provides these.
    connection: Any
    rds: Any
    _insert_audit_event: Any
    clear_ephemeral_secrets: Any

    # ------------------------------------------------------------------ primitives

    async def _append_events(
        self,
        conn: Any,
        fx: Effects,
        tenant_id: str,
        run_id: str,
        events: list[tuple[str, dict[str, Any]]],
    ) -> int:
        if not events:
            return 0
        last = await conn.fetchval(
            "UPDATE runs SET event_sequence = event_sequence + $3 "
            "WHERE tenant_id = $1 AND run_id = $2 RETURNING event_sequence",
            tenant_id,
            run_id,
            len(events),
        )
        first = int(last) - len(events) + 1
        events = [
            (kind, _with_error_code(payload) if kind == "run_error" else payload)
            for kind, payload in events
        ]
        await conn.executemany(
            "INSERT INTO run_events (tenant_id, run_id, sequence, event_id, type, payload) "
            "VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
            [
                (tenant_id, run_id, first + i, uuid4(), kind, _json(payload))
                for i, (kind, payload) in enumerate(events)
            ],
        )
        fx.wake.add(run_id)
        return int(last)

    async def _enqueue(
        self,
        conn: Any,
        fx: Effects,
        *,
        tenant_id: str,
        run_id: str,
        destination: str,
        payload: dict[str, Any],
        dedup_key: str,
    ) -> None:
        message_id = await conn.fetchval(
            "INSERT INTO outbox_messages "
            "(message_id, tenant_id, run_id, destination, payload, dedup_key) "
            "VALUES ($1, $2, $3, $4, $5::jsonb, $6) "
            "ON CONFLICT (dedup_key) DO NOTHING RETURNING message_id",
            uuid4(),
            tenant_id,
            run_id,
            destination,
            _json(payload),
            dedup_key,
        )
        if message_id is not None:
            fx.outbox.append(message_id)

    async def _enqueue_orchestrate(
        self, conn: Any, fx: Effects, row: Any, action: str, version: int
    ) -> None:
        await self._enqueue(
            conn,
            fx,
            tenant_id=row["tenant_id"],
            run_id=row["run_id"],
            destination="orchestrator",
            payload={
                "action": action,
                "run_id": row["run_id"],
                "expected_state_version": version,
            },
            dedup_key=f"orch:{row['run_id']}:{version}:{action}",
        )

    async def _lock_run(
        self, conn: Any, run_id: str, tenant_id: str | None = None
    ) -> Any:
        """Lock thread then run, the global lock order shared with admission and cancel."""
        if tenant_id is None:
            thread_id = await conn.fetchval(
                "SELECT thread_id FROM runs r WHERE r.run_id = $1", run_id
            )
        else:
            thread_id = await conn.fetchval(
                f"SELECT thread_id FROM runs r WHERE r.run_id = $1 AND {TENANT_SCOPE}",
                run_id,
                tenant_id,
            )
        if thread_id is None:
            return None
        await conn.execute(
            "SELECT 1 FROM threads WHERE thread_id = $1 FOR UPDATE", thread_id
        )
        return await conn.fetchrow(
            "SELECT * FROM runs WHERE run_id = $1 FOR UPDATE", run_id
        )

    async def _fenced(self, conn: Any, lease: Lease) -> Any:
        row = await self._lock_run(conn, lease.run_id)
        if (
            row is None
            or row["status"] != "running"
            or row["lease_epoch"] != lease.epoch
            or row["state_version"] != lease.version
        ):
            raise LeaseLostError(lease.run_id)
        return row

    async def _after_commit(self, fx: Effects) -> None:
        rds = getattr(self, "redis", None)
        if rds is not None:
            for run_id in fx.wake:
                try:
                    key = f"haas:wake:{run_id}"
                    await rds.xadd(key, {"w": "1"}, maxlen=50, approximate=True)
                    await rds.expire(key, 86_400)
                except Exception:  # Wakeups are advisory; PostgreSQL holds the events.
                    log.warning("event wakeup failed for %s", run_id, exc_info=True)
            for run_id in fx.cancelled:
                try:
                    await rds.setex(f"haas:cancelled:{run_id}", 604_800, "1")
                except Exception:
                    log.warning("cancel signal failed for %s", run_id, exc_info=True)
        for run_id in fx.clear_secrets:
            try:
                await self.clear_ephemeral_secrets(run_id)
            except Exception:
                log.warning("secret cleanup failed for %s", run_id, exc_info=True)
        for run_id in fx.cancelled:
            for hook in self.cancel_hooks:
                try:
                    await hook(run_id)
                except Exception:
                    log.warning("cancel hook failed for %s", run_id, exc_info=True)
        if fx.outbox:
            try:
                await self.relay_outbox(ids=fx.outbox)
            except Exception:  # The reconciler sweep retries pending outbox rows.
                log.warning("outbox fast path failed", exc_info=True)

    async def _finalize(
        self,
        conn: Any,
        fx: Effects,
        row: Any,
        status: str,
        result_text: str,
        *,
        messages: list[dict[str, Any]] | None = None,
        events: list[tuple[str, dict[str, Any]]] | None = None,
        cost_delta: Decimal = Decimal(0),
        batch_status: str = "cancelled",
        step_count: int | None = None,
    ) -> None:
        run_id, tenant_id = row["run_id"], row["tenant_id"]
        final_messages = (
            messages if messages is not None else loads(row["messages_checkpoint_json"])
        )
        await conn.execute(
            """
            UPDATE runs SET status = $2, result_text = $3, messages_checkpoint_json = $4::jsonb,
                total_cost_usd = total_cost_usd + $5::numeric,
                pending_tool_calls_json = NULL, lease_owner = NULL, lease_expires_at = NULL,
                next_action = 'none', budget_reserved_usd = 0,
                step_count = COALESCE($7, step_count),
                state_version = state_version + 1, updated_at = $6
            WHERE run_id = $1
            """,
            run_id,
            status,
            result_text,
            _json(final_messages),
            cost_delta,
            time.time(),
            step_count,
        )
        await conn.execute(
            "UPDATE usage_ledger SET settlement_status = 'uncertain', settled_at = now() "
            "WHERE tenant_id = $1 AND run_id = $2 AND settlement_status = 'reserved'",
            tenant_id,
            run_id,
        )
        await conn.execute(
            "UPDATE run_steps SET status = 'abandoned', completed_at = now() "
            "WHERE tenant_id = $1 AND run_id = $2 AND status = 'model_running'",
            tenant_id,
            run_id,
        )
        await conn.execute(
            "UPDATE approval_batches SET status = $3, resolved_at = now() "
            "WHERE tenant_id = $1 AND run_id = $2 AND status = 'pending'",
            tenant_id,
            run_id,
            batch_status,
        )
        await conn.execute(
            "UPDATE tool_invocations SET status = 'cancelled', completed_at = now() "
            "WHERE tenant_id = $1 AND run_id = $2 "
            "AND status IN ('awaiting_approval', 'approved')",
            tenant_id,
            run_id,
        )
        await conn.execute(
            "UPDATE tool_invocations SET status = 'unknown', completed_at = now(), "
            "outcome = jsonb_build_object('content', $3::text, 'is_error', true) "
            "WHERE tenant_id = $1 AND run_id = $2 AND status = 'dispatched'",
            tenant_id,
            run_id,
            UNKNOWN_OUTCOME,
        )
        commit_history = status == "completed"
        thread = await conn.fetchrow(
            "SELECT active_run_id, history_version FROM threads WHERE thread_id = $1",
            row["thread_id"],
        )
        if (
            commit_history
            and thread is not None
            and thread["active_run_id"] == run_id
            and row["thread_history_version"] is not None
            and thread["history_version"] != row["thread_history_version"]
        ):
            commit_history = False
        await conn.execute(
            "UPDATE threads SET active_run_id = NULL, "
            "messages_json = CASE WHEN $4 THEN $2::jsonb ELSE messages_json END, "
            "history_version = history_version + CASE WHEN $4 THEN 1 ELSE 0 END, "
            "updated_at = $3 WHERE thread_id = $1 AND active_run_id = $5",
            row["thread_id"],
            _json(final_messages),
            time.time(),
            commit_history,
            run_id,
        )
        await self._append_events(conn, fx, tenant_id, run_id, events or [])
        if row["webhook_url"]:
            body: dict[str, Any] = {
                "schema": WEBHOOK_SCHEMA,
                "run_id": run_id,
                "status": status,
            }
            if status == "completed":
                body["output"] = result_text or ""
            else:
                body["error"] = result_text or f"Run {status}."
                if status == "failed":
                    body["code"] = run_error_code(body["error"])
            await self._enqueue(
                conn,
                fx,
                tenant_id=tenant_id,
                run_id=run_id,
                destination="webhook",
                payload={"url": row["webhook_url"], "body": body},
                dedup_key=f"webhook:{run_id}:terminal",
            )
        fx.clear_secrets.add(run_id)
        if status == "cancelled":
            fx.cancelled.add(run_id)

    # ------------------------------------------------------------------ outbox relay

    async def relay_outbox(
        self, *, ids: list[UUID] | None = None, limit: int = 100
    ) -> int:
        publisher = self.outbox_publisher
        if publisher is None:
            return 0
        sent = 0
        async with self.connection() as conn:
            rows = await conn.fetch(
                """
                SELECT message_id, tenant_id, run_id, destination, payload, attempts
                FROM outbox_messages
                WHERE state = 'pending' AND next_attempt_at <= now()
                  AND ($1::uuid[] IS NULL OR message_id = ANY($1::uuid[]))
                ORDER BY created_at LIMIT $2
                FOR UPDATE SKIP LOCKED
                """,
                ids,
                limit,
            )
            for row in rows:
                message = dict(row)
                message["payload"] = loads(message["payload"])
                try:
                    await publisher(message)
                except Exception as exc:
                    log.warning("outbox publish failed for %s", row["message_id"], exc_info=True)
                    attempts = int(row["attempts"]) + 1
                    await conn.execute(
                        "UPDATE outbox_messages SET attempts = $2::int, last_error = $3, "
                        "state = CASE WHEN $2::int >= $4::int THEN 'dead' ELSE 'pending' END, "
                        "next_attempt_at = now() + make_interval(secs => $5::float8) "
                        "WHERE message_id = $1",
                        row["message_id"],
                        attempts,
                        f"{type(exc).__name__}: {exc}"[:500],
                        settings.outbox_max_attempts,
                        float(min(2**attempts, 300)),
                    )
                    continue
                await conn.execute(
                    "UPDATE outbox_messages SET state = 'dispatched', "
                    "attempts = attempts + 1, dispatched_at = now(), last_error = NULL "
                    "WHERE message_id = $1",
                    row["message_id"],
                )
                sent += 1
        return sent

    # ------------------------------------------------------------------ admission

    async def find_admission(
        self, tenant_id: str, principal_key: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        async with self.connection() as conn:
            row = await conn.fetchrow(
                "SELECT request_digest, response FROM admission_requests r "
                f"WHERE r.principal_key = $1 AND {TENANT_SCOPE} "
                "AND r.operation = 'run.create' AND r.idempotency_key = $3 "
                "AND r.expires_at > now()",
                principal_key,
                tenant_id,
                idempotency_key,
            )
        if row is None:
            return None
        return {
            "request_digest": row["request_digest"],
            "response": loads(row["response"]),
        }

    # ------------------------------------------------------------------ leases

    async def acquire_lease(
        self,
        *,
        message_id: str,
        run_id: str,
        action: str,
        expected_state_version: int,
        owner: str,
    ) -> tuple[Lease, dict[str, Any]] | None:
        fx = Effects()
        result: tuple[Lease, dict[str, Any]] | None = None
        async with self.connection() as conn:
            row = await self._lock_run(conn, run_id)
            if row is None:
                return None
            fresh = await conn.fetchval(
                "INSERT INTO consumer_inbox (consumer, tenant_id, message_id, run_id) "
                "VALUES ('orchestrator', $1, $2, $3) ON CONFLICT DO NOTHING RETURNING 1",
                row["tenant_id"],
                message_id,
                run_id,
            )
            if (
                not fresh
                or row["status"] != "queued"
                or row["state_version"] != expected_state_version
                or row["next_action"] != action
            ):
                return None
            tenant_status = (
                await conn.fetchval(
                    "SELECT status FROM tenants WHERE tenant_id = $1",
                    row["tenant_uuid"],
                )
                if row["tenant_uuid"] is not None
                else "active"
            )
            if tenant_status != "active":
                await self._finalize(
                    conn,
                    fx,
                    row,
                    "cancelled",
                    "Run cancelled: tenant is not active.",
                    events=[
                        (
                            "run_cancelled",
                            {"run_id": run_id, "reason": "tenant_inactive"},
                        )
                    ],
                )
            elif row["deadline_at"] is not None and row["deadline_at"] <= _utc():
                message = "Run deadline exceeded."
                await self._finalize(
                    conn,
                    fx,
                    row,
                    "failed",
                    message,
                    events=[("run_error", {"error": message})],
                )
            else:
                updated = await conn.fetchrow(
                    """
                    UPDATE runs SET status = 'running', lease_epoch = lease_epoch + 1,
                        lease_owner = $2,
                        lease_expires_at = now() + make_interval(secs => $3),
                        state_version = state_version + 1, updated_at = $4
                    WHERE run_id = $1 RETURNING *
                    """,
                    run_id,
                    owner,
                    float(settings.lease_ttl_sec),
                    time.time(),
                )
                run = decode_run(updated)
                result = (
                    Lease(
                        tenant_id=row["tenant_id"],
                        run_id=run_id,
                        thread_id=row["thread_id"],
                        epoch=int(updated["lease_epoch"]),
                        version=int(updated["state_version"]),
                        owner=owner,
                    ),
                    run,
                )
        await self._after_commit(fx)
        return result

    async def _bump(
        self,
        conn: Any,
        lease: Lease,
        assignments: str,
        *args: Any,
        release: bool = False,
    ) -> None:
        lease_sql = (
            "lease_owner = NULL, lease_expires_at = NULL"
            if release
            else "lease_expires_at = now() + make_interval(secs => "
            f"{float(settings.lease_ttl_sec)})"
        )
        sep = ", " if assignments else ""
        version = await conn.fetchval(
            f"UPDATE runs SET {assignments}{sep}{lease_sql}, "
            "state_version = state_version + 1, updated_at = "
            f"{time.time()} WHERE run_id = $1 RETURNING state_version",
            lease.run_id,
            *args,
        )
        lease.version = int(version)

    async def fail_with_lease(self, lease: Lease, message: str) -> bool:
        fx = Effects()
        try:
            async with self.connection() as conn:
                row = await self._fenced(conn, lease)
                await self._finalize(
                    conn,
                    fx,
                    row,
                    "failed",
                    message,
                    events=[("run_error", {"error": message})],
                )
        except LeaseLostError:
            return False
        await self._after_commit(fx)
        return True

    # ------------------------------------------------------------------ steps

    async def _uncertain_cost(self, conn: Any, row: Any) -> Decimal:
        return dec(
            await conn.fetchval(
                "SELECT COALESCE(sum(cost_usd), 0) FROM usage_ledger "
                "WHERE tenant_id = $1 AND run_id = $2 AND unit = 'model_usd' "
                "AND settlement_status = 'uncertain'",
                row["tenant_id"],
                row["run_id"],
            )
        )

    async def begin_step(
        self, lease: Lease, *, step: int, model: str, estimate_usd: Decimal
    ) -> StepTicket | None:
        fx = Effects()
        ticket: StepTicket | None = None
        async with self.connection() as conn:
            row = await self._fenced(conn, lease)
            stale = await conn.fetchval(
                "WITH s AS (UPDATE usage_ledger SET settlement_status = 'uncertain', "
                "settled_at = now() WHERE tenant_id = $1 AND run_id = $2 "
                "AND unit = 'model_usd' AND settlement_status = 'reserved' RETURNING cost_usd) "
                "SELECT COALESCE(sum(cost_usd), 0) FROM s",
                row["tenant_id"],
                row["run_id"],
            )
            await conn.execute(
                "UPDATE run_steps SET status = 'abandoned', completed_at = now() "
                "WHERE tenant_id = $1 AND run_id = $2 AND status = 'model_running'",
                row["tenant_id"],
                row["run_id"],
            )
            reserved = max(dec(row["budget_reserved_usd"]) - dec(stale), Decimal(0))
            spent = dec(row["total_cost_usd"]) + await self._uncertain_cost(conn, row)
            max_cost = dec(row["max_cost_usd"])
            if step > int(row["max_steps"]):
                message = f"Exceeded max_steps budget ({row['max_steps']})."
                await self._finalize(
                    conn,
                    fx,
                    row,
                    "failed",
                    message,
                    events=[("run_error", {"error": message})],
                )
            elif spent + reserved + estimate_usd > max_cost:
                message = (
                    f"Exceeded max_cost_usd budget before model dispatch "
                    f"(${spent + reserved + estimate_usd:.4f} > ${max_cost:.4f})."
                )
                await self._finalize(
                    conn,
                    fx,
                    row,
                    "failed",
                    message,
                    events=[("run_error", {"error": message})],
                )
            else:
                attempt = int(
                    await conn.fetchval(
                        "SELECT COALESCE(max(attempt), 0) + 1 FROM run_steps "
                        "WHERE tenant_id = $1 AND run_id = $2 AND step_number = $3",
                        row["tenant_id"],
                        row["run_id"],
                        step,
                    )
                )
                key = f"{row['run_id']}:step:{step}:attempt:{attempt}"
                await conn.execute(
                    "INSERT INTO run_steps (tenant_id, run_id, step_number, attempt, "
                    "lease_epoch, model, status, reserved_usd) "
                    "VALUES ($1, $2, $3, $4, $5, $6, 'model_running', $7)",
                    row["tenant_id"],
                    row["run_id"],
                    step,
                    attempt,
                    lease.epoch,
                    model,
                    estimate_usd,
                )
                await conn.execute(
                    "INSERT INTO usage_ledger (entry_id, tenant_id, run_id, "
                    "source_attempt_key, unit, quantity, cost_usd, settlement_status, "
                    "pricing_revision) VALUES ($1, $2, $3, $4, 'model_usd', $5, $5, "
                    "'reserved', $6)",
                    uuid4(),
                    row["tenant_id"],
                    row["run_id"],
                    key,
                    estimate_usd,
                    settings.pricing_revision,
                )
                await self._bump(
                    conn,
                    lease,
                    "budget_reserved_usd = $2::numeric",
                    reserved + estimate_usd,
                )
                ticket = StepTicket(step, attempt, model, estimate_usd, key)
        await self._after_commit(fx)
        return ticket

    async def emit_step_started(self, lease: Lease, payload: dict[str, Any]) -> None:
        fx = Effects()
        async with self.connection() as conn:
            await self._fenced(conn, lease)
            await self._append_events(
                conn, fx, lease.tenant_id, lease.run_id, [("step_started", payload)]
            )
        await self._after_commit(fx)

    async def _settle(
        self,
        conn: Any,
        row: Any,
        ticket: StepTicket,
        *,
        cost_usd: float,
        cost_reported: bool,
        prompt_tokens: int,
        cached_tokens: int,
        step_status: str,
        error: str | None = None,
        completion_tokens: int = 0,
    ) -> tuple[Decimal, Decimal]:
        """Settle the reservation; returns (reported cost delta, remaining reservation)."""
        cost = dec(cost_usd) if cost_reported else ticket.reserved_usd
        await conn.execute(
            "UPDATE usage_ledger SET settlement_status = $3, cost_usd = $4, quantity = $4, "
            "settled_at = now() WHERE tenant_id = $1 AND source_attempt_key = $2 "
            "AND unit = 'model_usd' AND settlement_status = 'reserved'",
            row["tenant_id"],
            ticket.attempt_key,
            "settled" if cost_reported else "uncertain",
            cost,
        )
        for unit, quantity in (
            ("prompt_tokens", prompt_tokens),
            ("completion_tokens", completion_tokens),
        ):
            if quantity:
                await conn.execute(
                    "INSERT INTO usage_ledger (entry_id, tenant_id, run_id, "
                    "source_attempt_key, unit, quantity, settlement_status, "
                    "pricing_revision, settled_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, 'settled', $7, now()) "
                    "ON CONFLICT DO NOTHING",
                    uuid4(),
                    row["tenant_id"],
                    row["run_id"],
                    ticket.attempt_key,
                    unit,
                    quantity,
                    settings.pricing_revision,
                )
        await conn.execute(
            "UPDATE run_steps SET status = $5, cost_usd = $6, cost_reported = $7, "
            "prompt_tokens = $8, cached_tokens = $9, error = $10, completed_at = now(), "
            "completion_tokens = $11 "
            "WHERE tenant_id = $1 AND run_id = $2 AND step_number = $3 AND attempt = $4",
            row["tenant_id"],
            row["run_id"],
            ticket.step,
            ticket.attempt,
            step_status,
            cost,
            cost_reported,
            prompt_tokens,
            cached_tokens,
            error,
            completion_tokens,
        )
        remaining = max(
            dec(row["budget_reserved_usd"]) - ticket.reserved_usd, Decimal(0)
        )
        return (dec(cost_usd) if cost_reported else Decimal(0)), remaining

    async def fail_step(self, lease: Lease, ticket: StepTicket, message: str) -> None:
        fx = Effects()
        try:
            async with self.connection() as conn:
                row = await self._fenced(conn, lease)
                await self._settle(
                    conn,
                    row,
                    ticket,
                    cost_usd=0,
                    cost_reported=False,
                    prompt_tokens=0,
                    cached_tokens=0,
                    step_status="failed",
                    error=message[:2000],
                )
                await self._finalize(
                    conn,
                    fx,
                    row,
                    "failed",
                    message,
                    events=[("run_error", {"error": message})],
                )
        except LeaseLostError:
            return
        await self._after_commit(fx)

    async def commit_step(
        self,
        lease: Lease,
        ticket: StepTicket,
        *,
        cost_usd: float,
        cost_reported: bool,
        prompt_tokens: int,
        cached_tokens: int,
        messages: list[dict[str, Any]],
        outcome: dict[str, Any],
        completion_tokens: int = 0,
    ) -> str:
        """Atomically settle the model attempt and apply exactly one branch.

        ``outcome["kind"]`` is ``complete`` | ``approval`` | ``dispatch``. Returns the branch
        actually applied (budget and quota guards may turn it into ``failed``).
        """
        fx = Effects()
        kind = outcome["kind"]
        async with self.connection() as conn:
            row = await self._fenced(conn, lease)
            step_status = {
                "complete": "completed",
                "approval": "waiting_approval",
                "dispatch": "tools_dispatched",
            }[kind]
            delta, reserved = await self._settle(
                conn,
                row,
                ticket,
                cost_usd=cost_usd,
                cost_reported=cost_reported,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached_tokens,
                step_status=step_status,
            )
            total = dec(row["total_cost_usd"]) + delta
            spent = total + await self._uncertain_cost(conn, row)
            max_cost = dec(row["max_cost_usd"])
            calls: list[dict[str, Any]] = outcome.get("tool_calls") or []
            used = int(row["tool_invocations_used"])
            if kind == "complete":
                text = outcome["text"]
                await self._finalize(
                    conn,
                    fx,
                    row,
                    "completed",
                    text,
                    messages=messages,
                    cost_delta=delta,
                    step_count=ticket.step,
                    events=[
                        (
                            "run_completed",
                            {
                                "output": text,
                                "steps_used": ticket.step,
                                "total_cost_usd": round(float(total), 6),
                                "cached_tokens": cached_tokens,
                            },
                        )
                    ],
                )
            elif spent > max_cost:
                kind = "failed"
                message = (
                    f"Exceeded max_cost_usd budget (${spent:.4f} > ${max_cost:.4f})."
                )
                await self._finalize(
                    conn,
                    fx,
                    row,
                    "failed",
                    message,
                    messages=messages,
                    cost_delta=delta,
                    step_count=ticket.step,
                    events=[("run_error", {"error": message})],
                )
            elif used + len(calls) > int(row["max_tool_invocations"]):
                kind = "failed"
                message = f"Exceeded max_tool_invocations budget ({row['max_tool_invocations']})."
                await self._finalize(
                    conn,
                    fx,
                    row,
                    "failed",
                    message,
                    messages=messages,
                    cost_delta=delta,
                    step_count=ticket.step,
                    events=[("run_error", {"error": message})],
                )
            elif kind == "approval":
                await self._create_approval(
                    conn, fx, lease, row, ticket.step, calls, outcome["invocations"]
                )
                await self._bump(
                    conn,
                    lease,
                    "status = 'input_required', step_count = $2, "
                    "messages_checkpoint_json = $3::jsonb, pending_tool_calls_json = $4::jsonb, "
                    "total_cost_usd = total_cost_usd + $5::numeric, "
                    "budget_reserved_usd = $6::numeric, next_action = 'none'",
                    ticket.step,
                    _json(messages),
                    _json(calls),
                    delta,
                    reserved,
                    release=True,
                )
            else:
                await self._insert_invocations(
                    conn, lease, row, ticket.step, outcome["invocations"], "dispatched"
                )
                await self._dispatch(conn, fx, lease, row, outcome["invocations"])
                await self._bump(
                    conn,
                    lease,
                    "step_count = $2, messages_checkpoint_json = $3::jsonb, "
                    "total_cost_usd = total_cost_usd + $4::numeric, "
                    "budget_reserved_usd = $5::numeric, next_action = 'none', "
                    "pending_tool_calls_json = NULL, "
                    "tool_invocations_used = tool_invocations_used + $6",
                    ticket.step,
                    _json(messages),
                    delta,
                    reserved,
                    len(outcome["invocations"]),
                    release=True,
                )
        await self._after_commit(fx)
        return kind if kind != "complete" else "completed"

    async def _insert_invocations(
        self,
        conn: Any,
        lease: Lease,
        row: Any,
        step: int,
        invocations: list[dict[str, Any]],
        status: str,
        batch_id: UUID | None = None,
    ) -> None:
        await conn.executemany(
            "INSERT INTO tool_invocations (invocation_id, tenant_id, run_id, step_number, "
            "ordinal, tool_call_id, tool_name, queue, arguments, arguments_digest, "
            "effect_key, status, lease_epoch, approval_batch_id) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)",
            [
                (
                    UUID(inv["invocation_id"]),
                    row["tenant_id"],
                    row["run_id"],
                    step,
                    inv["ordinal"],
                    inv["tool_call_id"],
                    inv["tool_name"],
                    inv["queue"],
                    inv["arguments"],
                    inv["arguments_digest"],
                    inv["effect_key"],
                    status,
                    lease.epoch,
                    batch_id,
                )
                for inv in invocations
            ],
        )

    async def _dispatch(
        self,
        conn: Any,
        fx: Effects,
        lease: Lease,
        row: Any,
        invocations: list[dict[str, Any]],
    ) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        for inv in invocations:
            await conn.execute(
                "UPDATE tool_invocations SET status = 'dispatched', dispatched_at = now(), "
                "lease_epoch = $2 WHERE invocation_id = $1",
                UUID(inv["invocation_id"]),
                lease.epoch,
            )
            await self._enqueue(
                conn,
                fx,
                tenant_id=row["tenant_id"],
                run_id=row["run_id"],
                destination="tool",
                payload={
                    "queue": inv["queue"],
                    "envelope": inv["envelope"],
                    "secret_keys": inv["secret_keys"],
                },
                dedup_key=f"tool:{inv['effect_key']}:{lease.epoch}",
            )
            events.append(
                (
                    "tool_dispatched",
                    {
                        "tool_call_id": inv["tool_call_id"],
                        "tool": inv["tool_name"],
                        "queue": inv["queue"],
                        "invocation_id": inv["invocation_id"],
                        "effect_key": inv["effect_key"],
                    },
                )
            )
        await self._append_events(conn, fx, row["tenant_id"], row["run_id"], events)

    async def _create_approval(
        self,
        conn: Any,
        fx: Effects,
        lease: Lease,
        row: Any,
        step: int,
        calls: list[dict[str, Any]],
        invocations: list[dict[str, Any]],
    ) -> None:
        batch_id = uuid4()
        digest = batch_digest(row["run_id"], step, calls)
        expires = _utc(time.time() + settings.approval_ttl_sec)
        pending = [
            {
                "id": tc["id"],
                "name": tc["function"]["name"],
                "arguments": tc["function"].get("arguments"),
                "requires_approval": inv.get("requires_approval", False),
                "queue": inv["queue"],
                "allowed_secrets": inv["secret_keys"],
            }
            for tc, inv in zip(calls, invocations, strict=True)
        ]
        await conn.execute(
            "INSERT INTO approval_batches (batch_id, tenant_id, run_id, step_number, "
            "batch_digest, run_state_version, tool_calls, expires_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)",
            batch_id,
            row["tenant_id"],
            row["run_id"],
            step,
            digest,
            lease.version + 1,
            _json(pending),
            expires,
        )
        await self._insert_invocations(
            conn, lease, row, step, invocations, "awaiting_approval", batch_id
        )
        await self._append_events(
            conn,
            fx,
            row["tenant_id"],
            row["run_id"],
            [
                (
                    "input_required",
                    {
                        "step": step,
                        "batch_id": str(batch_id),
                        "batch_digest": digest,
                        "expires_at": expires.isoformat(),
                        "pending_tool_calls": [
                            {k: p[k] for k in ("id", "name", "arguments")}
                            for p in pending
                        ],
                        "resume_endpoint": f"/v1/runs/{row['run_id']}/resume",
                        "decision_endpoint": f"/v1/approvals/{batch_id}/decision",
                    },
                )
            ],
        )

    # ------------------------------------------------------------------ tool outcomes

    async def record_tool_outcome(
        self,
        *,
        run_id: str,
        invocation_id: str,
        status: str,
        outcome: dict[str, Any],
    ) -> bool:
        if status not in OUTCOME_STATUSES:
            raise ValueError(f"Unsupported tool outcome status {status!r}.")
        fx = Effects()
        async with self.connection() as conn:
            row = await self._lock_run(conn, run_id)
            if row is None:
                return False
            fresh = await conn.fetchval(
                "INSERT INTO consumer_inbox (consumer, tenant_id, message_id, run_id) "
                "VALUES ('tool_outcome', $1, $2, $3) ON CONFLICT DO NOTHING RETURNING 1",
                row["tenant_id"],
                invocation_id,
                run_id,
            )
            if not fresh:
                return False
            content = str(outcome.get("content", ""))
            updated = await conn.fetchval(
                "UPDATE tool_invocations SET status = $3, outcome = $4::jsonb, preview = $5, "
                "completed_at = now() WHERE invocation_id = $1 AND run_id = $2 "
                "AND status = 'dispatched' RETURNING 1",
                UUID(invocation_id),
                run_id,
                status,
                _json(outcome),
                content[:300],
            )
            if updated:
                await self._after_outcome(conn, fx, row)
        await self._after_commit(fx)
        return bool(updated)

    async def _after_outcome(self, conn: Any, fx: Effects, row: Any) -> None:
        outstanding = await conn.fetchval(
            "SELECT count(*) FROM tool_invocations WHERE tenant_id = $1 AND run_id = $2 "
            "AND status = 'dispatched'",
            row["tenant_id"],
            row["run_id"],
        )
        if outstanding:
            return
        if row["status"] == "cancelling":
            await self._finalize(
                conn,
                fx,
                row,
                "cancelled",
                "Run cancelled.",
                events=[("run_cancelled", {"run_id": row["run_id"]})],
            )
        elif row["status"] == "running" and row["lease_owner"] is None:
            version = await conn.fetchval(
                "UPDATE runs SET status = 'queued', next_action = 'aggregate', "
                "state_version = state_version + 1, updated_at = $2 "
                "WHERE run_id = $1 RETURNING state_version",
                row["run_id"],
                time.time(),
            )
            await self._enqueue_orchestrate(conn, fx, row, "aggregate", int(version))

    async def load_step_outcomes(self, lease: Lease, step: int) -> list[dict[str, Any]]:
        async with self.connection() as conn:
            await self._fenced(conn, lease)
            rows = await conn.fetch(
                "SELECT invocation_id, ordinal, tool_call_id, tool_name, arguments, status, "
                "outcome FROM tool_invocations WHERE tenant_id = $1 AND run_id = $2 "
                "AND step_number = $3 ORDER BY ordinal",
                lease.tenant_id,
                lease.run_id,
                step,
            )
        result = []
        for r in rows:
            item = dict(r)
            item["invocation_id"] = str(item["invocation_id"])
            item["outcome"] = loads(item["outcome"]) or {}
            result.append(item)
        return result

    async def commit_aggregate(
        self,
        lease: Lease,
        *,
        messages: list[dict[str, Any]],
        active_skills: list[str],
        fingerprints: list[str],
        had_error: bool,
        events: list[tuple[str, dict[str, Any]]],
    ) -> None:
        fx = Effects()
        async with self.connection() as conn:
            await self._fenced(conn, lease)
            await self._append_events(conn, fx, lease.tenant_id, lease.run_id, events)
            await self._bump(
                conn,
                lease,
                "messages_checkpoint_json = $2::jsonb, active_skills_json = $3::jsonb, "
                "fingerprints_json = $4::jsonb, last_step_had_error = $5, next_action = 'step'",
                _json(messages),
                _json(sorted(active_skills)),
                _json(fingerprints[-20:]),
                had_error,
            )
        await self._after_commit(fx)

    # ------------------------------------------------------------------ approvals

    async def load_decided_batch(self, lease: Lease) -> dict[str, Any] | None:
        async with self.connection() as conn:
            row = await self._fenced(conn, lease)
            batch = await conn.fetchrow(
                "SELECT b.batch_id, b.status, b.step_number, d.reason "
                "FROM approval_batches b JOIN approval_decisions d ON d.batch_id = b.batch_id "
                "WHERE b.tenant_id = $1 AND b.run_id = $2 AND b.step_number = $3 "
                "AND b.status IN ('approved', 'rejected') "
                "ORDER BY b.created_at DESC LIMIT 1",
                row["tenant_id"],
                row["run_id"],
                row["step_count"],
            )
            if batch is None:
                return None
            invocations = await conn.fetch(
                "SELECT invocation_id, ordinal, tool_call_id, tool_name, queue, arguments, "
                "arguments_digest, effect_key, status FROM tool_invocations "
                "WHERE approval_batch_id = $1 ORDER BY ordinal",
                batch["batch_id"],
            )
        return {
            "batch_id": str(batch["batch_id"]),
            "status": batch["status"],
            "step": batch["step_number"],
            "reason": batch["reason"],
            "invocations": [
                {**dict(i), "invocation_id": str(i["invocation_id"])}
                for i in invocations
            ],
        }

    async def dispatch_approved(
        self, lease: Lease, invocations: list[dict[str, Any]]
    ) -> str:
        fx = Effects()
        kind = "dispatch"
        async with self.connection() as conn:
            row = await self._fenced(conn, lease)
            if int(row["tool_invocations_used"]) + len(invocations) > int(
                row["max_tool_invocations"]
            ):
                kind = "failed"
                message = f"Exceeded max_tool_invocations budget ({row['max_tool_invocations']})."
                await self._finalize(
                    conn,
                    fx,
                    row,
                    "failed",
                    message,
                    events=[("run_error", {"error": message})],
                )
            else:
                await self._dispatch(conn, fx, lease, row, invocations)
                await self._bump(
                    conn,
                    lease,
                    "next_action = 'none', "
                    "tool_invocations_used = tool_invocations_used + $2",
                    len(invocations),
                    release=True,
                )
        await self._after_commit(fx)
        return kind

    async def decide_approval(
        self,
        tenant_id: str,
        *,
        run_id: str | None,
        batch_id: str | None,
        batch_digest_value: str | None,
        approved: bool,
        reason: str | None,
        actor_principal_id: str | None,
        request_id: str,
        idempotency_key: str | None,
        require_binding: bool,
    ) -> dict[str, Any]:
        """Record exactly one decision for the pending batch and queue its orchestration."""
        from app.core.store import StoreConflictError, StoreNotFoundError

        fx = Effects()
        decision = "approved" if approved else "rejected"
        async with self.connection() as conn:
            # Tenant before thread/run: the same lock order as admission and suspension.
            audit_tenant: UUID | None = None
            if actor_principal_id is not None:
                audit_tenant = await conn.fetchval(
                    "SELECT tenant_id FROM tenants WHERE (slug = $1 OR tenant_id::text = $1) "
                    "AND status = 'active' FOR SHARE",
                    tenant_id,
                )
                if audit_tenant is None:
                    raise StoreConflictError("Tenant is not active.")
            if run_id is None:
                if batch_id is None:
                    raise StoreNotFoundError("Approval batch not found.")
                run_id = await conn.fetchval(
                    f"SELECT run_id FROM approval_batches r WHERE r.batch_id = $1::uuid "
                    f"AND {TENANT_SCOPE}",
                    batch_id,
                    tenant_id,
                )
                if run_id is None:
                    raise StoreNotFoundError("Approval batch not found.")
            row = await self._lock_run(conn, run_id, tenant_id)
            if row is None:
                raise StoreNotFoundError("Run not found.")
            if idempotency_key is not None:
                prior = await conn.fetchrow(
                    "SELECT batch_id, decision FROM approval_decisions "
                    "WHERE tenant_id = $1 AND run_id = $2 AND idempotency_key = $3",
                    row["tenant_id"],
                    run_id,
                    idempotency_key,
                )
                if prior is not None:
                    if prior["decision"] != decision or (
                        batch_id is not None and str(prior["batch_id"]) != batch_id
                    ):
                        raise IdempotencyConflictError(
                            "Idempotency-Key was used for a different decision."
                        )
                    return {
                        "run_id": run_id,
                        "batch_id": str(prior["batch_id"]),
                        "decision": decision,
                        "replayed": True,
                    }
            if row["status"] != "input_required":
                raise StoreConflictError(
                    f"Run status is '{row['status']}', not 'input_required'."
                )
            batch = await conn.fetchrow(
                "SELECT * FROM approval_batches WHERE tenant_id = $1 AND run_id = $2 "
                "AND status = 'pending'",
                row["tenant_id"],
                run_id,
            )
            if batch is None:
                raise StoreConflictError("No pending approval batch for this run.")
            if require_binding and (batch_id is None or batch_digest_value is None):
                raise StoreConflictError(
                    "Approval decisions must bind batch_id and batch_digest."
                )
            if batch_id is not None and str(batch["batch_id"]) != batch_id:
                raise StoreConflictError("Approval batch is stale; refresh and review.")
            if (
                batch_digest_value is not None
                and batch["batch_digest"] != batch_digest_value
            ):
                raise StoreConflictError("Approval batch digest does not match.")
            if batch["expires_at"] <= _utc():
                raise StoreConflictError("Approval batch expired.")
            await conn.execute(
                "INSERT INTO approval_decisions (decision_id, batch_id, tenant_id, run_id, "
                "decided_by, decision, batch_digest, idempotency_key, reason, request_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                uuid4(),
                batch["batch_id"],
                row["tenant_id"],
                run_id,
                UUID(actor_principal_id) if actor_principal_id else None,
                decision,
                batch["batch_digest"],
                idempotency_key,
                reason,
                UUID(request_id),
            )
            await conn.execute(
                "UPDATE approval_batches SET status = $2, resolved_at = now() "
                "WHERE batch_id = $1",
                batch["batch_id"],
                decision,
            )
            await conn.execute(
                "UPDATE tool_invocations SET status = $2 WHERE approval_batch_id = $1 "
                "AND status = 'awaiting_approval'",
                batch["batch_id"],
                "approved" if approved else "denied",
            )
            version = await conn.fetchval(
                "UPDATE runs SET status = 'queued', next_action = 'decision', "
                "pending_tool_calls_json = NULL, state_version = state_version + 1, "
                "updated_at = $2 WHERE run_id = $1 RETURNING state_version",
                run_id,
                time.time(),
            )
            calls = loads(batch["tool_calls"])
            event = (
                (
                    "hitl_approved",
                    {
                        "batch_id": str(batch["batch_id"]),
                        "dispatched_tools": len(calls),
                    },
                )
                if approved
                else (
                    "hitl_rejected",
                    {
                        "batch_id": str(batch["batch_id"]),
                        "reason": reason or "Rejected by human operator.",
                    },
                )
            )
            await self._append_events(conn, fx, row["tenant_id"], run_id, [event])
            await self._enqueue_orchestrate(conn, fx, row, "decision", int(version))
            if audit_tenant is not None and actor_principal_id is not None:
                await self._insert_audit_event(
                    conn,
                    tenant_id=audit_tenant,
                    actor_principal_id=UUID(actor_principal_id),
                    action="run.approved" if approved else "run.rejected",
                    target_type="run",
                    target_id=run_id,
                    request_id=UUID(request_id),
                    details={
                        "batch_id": str(batch["batch_id"]),
                        "batch_digest": batch["batch_digest"],
                        "pending_tool_count": len(calls),
                    },
                )
        await self._after_commit(fx)
        return {
            "run_id": run_id,
            "batch_id": str(batch["batch_id"]),
            "decision": decision,
            "replayed": False,
        }

    async def list_approval_batches(
        self, tenant_id: str, *, status: str | None, limit: int
    ) -> list[dict[str, Any]]:
        async with self.connection() as conn:
            rows = await conn.fetch(
                "SELECT r.batch_id, r.run_id, r.step_number, r.batch_digest, r.status, "
                "r.tool_calls, r.created_at, r.expires_at, r.resolved_at, "
                "d.decision, d.decided_by, d.reason, d.decided_at, "
                "runs.submitted_by_principal_id, runs.thread_id "
                "FROM approval_batches r "
                "JOIN runs ON runs.tenant_id = r.tenant_id AND runs.run_id = r.run_id "
                "LEFT JOIN approval_decisions d ON d.batch_id = r.batch_id "
                f"WHERE ($1::text IS NULL OR r.status = $1) AND {TENANT_SCOPE} "
                "ORDER BY r.created_at DESC LIMIT $3",
                status,
                tenant_id,
                limit,
            )
        return [_batch_view(row) for row in rows]

    async def pending_approval_batch(self, run_id: str) -> dict[str, Any] | None:
        async with self.connection() as conn:
            row = await conn.fetchrow(
                "SELECT batch_id, batch_digest, expires_at FROM approval_batches "
                "WHERE run_id = $1 AND status = 'pending'",
                run_id,
            )
        if row is None:
            return None
        return {
            "batch_id": str(row["batch_id"]),
            "batch_digest": row["batch_digest"],
            "expires_at": row["expires_at"].isoformat(),
        }

    async def get_approval_batch(
        self, tenant_id: str, batch_id: str
    ) -> dict[str, Any] | None:
        async with self.connection() as conn:
            row = await conn.fetchrow(
                "SELECT r.batch_id, r.run_id, r.step_number, r.batch_digest, r.status, "
                "r.tool_calls, r.created_at, r.expires_at, r.resolved_at, "
                "d.decision, d.decided_by, d.reason, d.decided_at, "
                "runs.submitted_by_principal_id, runs.thread_id "
                "FROM approval_batches r "
                "JOIN runs ON runs.tenant_id = r.tenant_id AND runs.run_id = r.run_id "
                "LEFT JOIN approval_decisions d ON d.batch_id = r.batch_id "
                f"WHERE r.batch_id = $1::uuid AND {TENANT_SCOPE}",
                batch_id,
                tenant_id,
            )
        return _batch_view(row) if row is not None else None

    # ------------------------------------------------------------------ cancellation

    async def _cancel_locked(
        self, conn: Any, fx: Effects, row: Any, reason: str
    ) -> str:
        if row["status"] in TERMINAL_STATUSES:
            return "terminal"
        if row["status"] == "cancelling":
            return "cancelling"
        outstanding = await conn.fetchval(
            "SELECT count(*) FROM tool_invocations WHERE tenant_id = $1 AND run_id = $2 "
            "AND status = 'dispatched'",
            row["tenant_id"],
            row["run_id"],
        )
        await conn.execute(
            "UPDATE runs SET lease_epoch = lease_epoch + 1 WHERE run_id = $1",
            row["run_id"],
        )
        children = await conn.fetch(
            "SELECT run_id FROM runs WHERE tenant_id = $1 AND parent_run_id = $2 "
            "AND status = ANY($3::text[])",
            row["tenant_id"],
            row["run_id"],
            list(ACTIVE_STATUSES),
        )
        for child in children:
            child_row = await self._lock_run(conn, child["run_id"])
            if child_row is not None:
                await self._cancel_locked(conn, fx, child_row, "parent_cancelled")
        if outstanding and row["status"] == "running":
            await conn.execute(
                "UPDATE runs SET status = 'cancelling', lease_owner = NULL, "
                "lease_expires_at = NULL, next_action = 'none', "
                "state_version = state_version + 1, updated_at = $2 WHERE run_id = $1",
                row["run_id"],
                time.time(),
            )
            await self._append_events(
                conn,
                fx,
                row["tenant_id"],
                row["run_id"],
                [
                    (
                        "run_cancelling",
                        {"run_id": row["run_id"], "outstanding_tools": outstanding},
                    )
                ],
            )
            fx.cancelled.add(row["run_id"])
            return "cancelling"
        await self._finalize(
            conn,
            fx,
            row,
            "cancelled",
            "Run cancelled.",
            events=[("run_cancelled", {"run_id": row["run_id"], "reason": reason})],
        )
        return "cancelled"

    async def request_cancel(self, run_id: str, tenant_id: str) -> str | None:
        """Fenced cancellation; returns cancelled | cancelling | terminal, or None if absent."""
        fx = Effects()
        async with self.connection() as conn:
            row = await self._lock_run(conn, run_id, tenant_id)
            if row is None:
                return None
            outcome = await self._cancel_locked(conn, fx, row, "requested")
        await self._after_commit(fx)
        return outcome

    async def _cancel_tenant_runs(
        self, conn: Any, fx: Effects, tenant_uuid: UUID
    ) -> int:
        rows = await conn.fetch(
            "SELECT run_id FROM runs WHERE tenant_uuid = $1 AND status = ANY($2::text[]) "
            "AND parent_run_id IS NULL ORDER BY run_id",
            tenant_uuid,
            list(ACTIVE_STATUSES),
        )
        count = 0
        for item in rows:
            row = await self._lock_run(conn, item["run_id"])
            if row is not None and await self._cancel_locked(
                conn, fx, row, "tenant_suspended"
            ) in {"cancelled", "cancelling"}:
                count += 1
        return count

    # ------------------------------------------------------------------ events & inspection

    async def list_run_events(
        self, run_id: str, *, after: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        async with self.connection() as conn:
            rows = await conn.fetch(
                "SELECT sequence, event_id, schema_version, type, payload, occurred_at "
                "FROM run_events WHERE run_id = $1 AND sequence > $2 "
                "ORDER BY sequence LIMIT $3",
                run_id,
                after,
                limit,
            )
        return [
            {
                "sequence": int(r["sequence"]),
                "event_id": str(r["event_id"]),
                "schema_version": r["schema_version"],
                "type": r["type"],
                "run_id": run_id,
                "occurred_at": r["occurred_at"].isoformat(),
                "data": loads(r["payload"]),
            }
            for r in rows
        ]

    async def list_run_steps(self, run_id: str) -> dict[str, Any]:
        async with self.connection() as conn:
            steps = await conn.fetch(
                "SELECT step_number, attempt, lease_epoch, model, status, reserved_usd, "
                "cost_usd, cost_reported, prompt_tokens, completion_tokens, cached_tokens, "
                "error, started_at, completed_at FROM run_steps WHERE run_id = $1 "
                "ORDER BY step_number, attempt",
                run_id,
            )
            invocations = await conn.fetch(
                "SELECT invocation_id, step_number, ordinal, tool_call_id, tool_name, queue, "
                "arguments_digest, effect_key, status, attempt, lease_epoch, "
                "approval_batch_id, preview, created_at, dispatched_at, completed_at "
                "FROM tool_invocations WHERE run_id = $1 ORDER BY step_number, ordinal",
                run_id,
            )
        return {
            "steps": [_jsonable(dict(s)) for s in steps],
            "invocations": [_jsonable(dict(i)) for i in invocations],
        }

    async def run_usage(self, run_id: str) -> dict[str, Any]:
        async with self.connection() as conn:
            rows = await conn.fetch(
                "SELECT source_attempt_key, unit, quantity, cost_usd, settlement_status, "
                "pricing_revision, created_at, settled_at FROM usage_ledger "
                "WHERE run_id = $1 ORDER BY created_at, unit",
                run_id,
            )
        return {"entries": [_jsonable(dict(r)) for r in rows]}

    async def run_budget(self, run: dict[str, Any]) -> dict[str, Any]:
        async with self.connection() as conn:
            rows = await conn.fetch(
                "SELECT settlement_status, COALESCE(sum(cost_usd), 0) AS cost "
                "FROM usage_ledger WHERE run_id = $1 AND unit = 'model_usd' "
                "GROUP BY settlement_status",
                run["run_id"],
            )
        by_status = {r["settlement_status"]: dec(r["cost"]) for r in rows}
        settled = by_status.get("settled", Decimal(0))
        uncertain = by_status.get("uncertain", Decimal(0))
        reserved = dec(run.get("budget_reserved_usd"))
        max_cost = dec(run["max_cost_usd"])
        deadline = run.get("deadline_at")
        return {
            "max_cost_usd": float(max_cost),
            "settled_usd": float(settled),
            "uncertain_usd": float(uncertain),
            "reserved_usd": float(reserved),
            "remaining_usd": float(
                max(max_cost - settled - uncertain - reserved, Decimal(0))
            ),
            "max_steps": int(run["max_steps"]),
            "steps_used": int(run["step_count"]),
            "max_tool_invocations": int(run.get("max_tool_invocations") or 0),
            "tool_invocations_used": int(run.get("tool_invocations_used") or 0),
            "deadline_at": deadline.isoformat() if deadline is not None else None,
        }

    # ------------------------------------------------------------------ reconciler

    async def reconcile(self) -> dict[str, int]:
        """Repair every crash boundary; safe to run concurrently and repeatedly."""
        counts = {
            "approvals_expired": 0,
            "deadlines": 0,
            "unknown_tools": 0,
            "recovered": 0,
            "requeued": 0,
            "ledger_uncertain": 0,
        }
        fx = Effects()
        async with self.connection() as conn:
            for item in await conn.fetch(
                "SELECT DISTINCT run_id FROM approval_batches "
                "WHERE status = 'pending' AND expires_at <= now() LIMIT 100"
            ):
                row = await self._lock_run(conn, item["run_id"])
                if row is not None and row["status"] == "input_required":
                    message = "Approval expired before a decision was recorded."
                    await self._finalize(
                        conn,
                        fx,
                        row,
                        "failed",
                        message,
                        batch_status="expired",
                        events=[
                            ("approval_expired", {"run_id": row["run_id"]}),
                            ("run_error", {"error": message}),
                        ],
                    )
                    counts["approvals_expired"] += 1
        await self._after_commit(fx)

        fx = Effects()
        async with self.connection() as conn:
            for item in await conn.fetch(
                "SELECT run_id FROM runs WHERE status = ANY($1::text[]) "
                "AND deadline_at <= now() LIMIT 100",
                list(ACTIVE_STATUSES),
            ):
                row = await self._lock_run(conn, item["run_id"])
                if row is None or row["status"] in TERMINAL_STATUSES:
                    continue
                if row["status"] == "cancelling":
                    await self._finalize(
                        conn,
                        fx,
                        row,
                        "cancelled",
                        "Run cancelled.",
                        events=[("run_cancelled", {"run_id": row["run_id"]})],
                    )
                else:
                    message = "Run deadline exceeded."
                    await self._finalize(
                        conn,
                        fx,
                        row,
                        "failed",
                        message,
                        events=[("run_error", {"error": message})],
                    )
                counts["deadlines"] += 1
        await self._after_commit(fx)

        fx = Effects()
        async with self.connection() as conn:
            stale = await conn.fetch(
                "SELECT invocation_id, run_id FROM tool_invocations "
                "WHERE status = 'dispatched' "
                "AND dispatched_at < now() - make_interval(secs => $1) LIMIT 200",
                float(settings.tool_outcome_timeout_sec),
            )
            touched: dict[str, None] = {}
            for item in stale:
                touched.setdefault(item["run_id"], None)
            for run_id in touched:
                row = await self._lock_run(conn, run_id)
                if row is None:
                    continue
                marked = await conn.fetchval(
                    "WITH u AS (UPDATE tool_invocations SET status = 'unknown', "
                    "completed_at = now(), preview = left($3, 300), "
                    "outcome = jsonb_build_object('content', $3::text, 'is_error', true) "
                    "WHERE tenant_id = $1 AND run_id = $2 AND status = 'dispatched' "
                    "AND dispatched_at < now() - make_interval(secs => $4) RETURNING 1) "
                    "SELECT count(*) FROM u",
                    row["tenant_id"],
                    run_id,
                    UNKNOWN_OUTCOME,
                    float(settings.tool_outcome_timeout_sec),
                )
                if marked:
                    counts["unknown_tools"] += int(marked)
                    await self._after_outcome(conn, fx, row)
        await self._after_commit(fx)

        fx = Effects()
        async with self.connection() as conn:
            for item in await conn.fetch(
                """
                SELECT r.run_id FROM runs r
                WHERE (r.status = 'running' AND r.lease_owner IS NOT NULL
                       AND r.lease_expires_at < now())
                   OR (r.status = 'queued'
                       AND r.updated_at < $1
                       AND NOT EXISTS (SELECT 1 FROM outbox_messages o
                           WHERE o.run_id = r.run_id AND o.state = 'pending'))
                LIMIT 100
                """,
                time.time() - settings.stall_timeout_sec,
            ):
                row = await self._lock_run(conn, item["run_id"])
                if row is None or row["status"] not in {"running", "queued"}:
                    continue
                expired_lease = (
                    row["status"] == "running"
                    and row["lease_owner"] is not None
                    and row["lease_expires_at"] < _utc()
                )
                stalled_queue = (
                    row["status"] == "queued"
                    and float(row["updated_at"])
                    < time.time() - settings.stall_timeout_sec
                )
                if not (expired_lease or stalled_queue) or row["next_action"] == "none":
                    continue
                if int(row["recovery_attempts"]) >= settings.max_recovery_attempts:
                    message = (
                        "Run exceeded its recovery attempts after worker failures."
                    )
                    await self._finalize(
                        conn,
                        fx,
                        row,
                        "failed",
                        message,
                        events=[("run_error", {"error": message})],
                    )
                    counts["recovered"] += 1
                    continue
                version = await conn.fetchval(
                    "UPDATE runs SET status = 'queued', lease_owner = NULL, "
                    "lease_expires_at = NULL, recovery_attempts = recovery_attempts + 1, "
                    "lease_epoch = lease_epoch + CASE WHEN status = 'running' THEN 1 ELSE 0 END, "
                    "state_version = state_version + 1, updated_at = $2 "
                    "WHERE run_id = $1 RETURNING state_version",
                    row["run_id"],
                    time.time(),
                )
                await self._append_events(
                    conn,
                    fx,
                    row["tenant_id"],
                    row["run_id"],
                    [
                        (
                            "run_recovering",
                            {
                                "action": row["next_action"],
                                "attempt": int(row["recovery_attempts"]) + 1,
                                "reason": "lease_expired"
                                if expired_lease
                                else "queue_stalled",
                            },
                        )
                    ],
                )
                await self._enqueue_orchestrate(
                    conn, fx, row, row["next_action"], int(version)
                )
                counts["requeued" if stalled_queue else "recovered"] += 1
        await self._after_commit(fx)

        async with self.connection() as conn:
            result = await conn.execute(
                "UPDATE usage_ledger l SET settlement_status = 'uncertain', settled_at = now() "
                "FROM runs r WHERE r.tenant_id = l.tenant_id AND r.run_id = l.run_id "
                "AND l.settlement_status = 'reserved' "
                "AND r.status IN ('completed', 'failed', 'cancelled')"
            )
            counts["ledger_uncertain"] = int(result.split()[-1])
            await conn.execute(
                "DELETE FROM consumer_inbox "
                "WHERE processed_at < now() - make_interval(secs => $1)",
                float(settings.inbox_retention_sec),
            )
            await conn.execute(
                "DELETE FROM admission_requests WHERE expires_at < now()"
            )
        return counts


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in d.items():
        if isinstance(value, Decimal):
            out[key] = float(value)
        elif isinstance(value, datetime):
            out[key] = value.isoformat()
        elif isinstance(value, UUID):
            out[key] = str(value)
        else:
            out[key] = value
    return out


def _batch_view(row: Any) -> dict[str, Any]:
    d = _jsonable(dict(row))
    d["tool_calls"] = loads(row["tool_calls"])
    expires = row["expires_at"]
    d["expired"] = row["status"] == "pending" and expires <= _utc()
    d["decision"] = (
        {
            "decision": row["decision"],
            "decided_by": str(row["decided_by"]) if row["decided_by"] else None,
            "reason": row["reason"],
            "decided_at": row["decided_at"].isoformat(),
        }
        if row["decision"]
        else None
    )
    for key in ("decided_by", "reason", "decided_at"):
        d.pop(key, None)
    return d


def deadline_from_now(seconds: int) -> datetime:
    return _utc() + timedelta(seconds=seconds)
