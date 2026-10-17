# Distributed FastAPI + Celery Canvas + Redis Harness-as-a-Service (HaaS)
## Production Architecture & Build Specification (v3.0)

> Historical build specification, not a production-readiness guarantee. For verified
> current behavior, complete SDD/DFD/ERD diagrams, and the proposed authenticated
> multi-tenant architecture, see the [HaaS design set](design/README.md).
> In particular, tenant authentication, exactly-once effects and sandbox isolation
> must not be inferred from this specification's original topology labels.

* **Architecture:** Stateless FastAPI API Tier + Redis 7 (Broker, Chord Coordinator, & SSE Streams) + Celery 5.4 Canvas Workers (`q_orchestrator`, `q_tools_fast`, `q_tools_sandbox`) + PostgreSQL 16 (Durable Threads, Checkpoints, & Cost Ledger) + OpenRouter Gateway
* **Extension Standards:** 3-Tier Progressive Disclosure (`agentskills.io` `SKILL.md` + `references/`), Native Python `@registry.tool`, and Stateless MCP HTTP (`2026-07-28`)
* **License & Stack Cost:** 100% Open-Source Containers ($0 software licensing; pay-per-token or free-tier models via OpenRouter)

---

## 1. Distributed System Topology & Queue Isolation

```text
                        +---------------------------------------+
                        |   External Clients / Microservices    |
                        |  (POST /v1/runs, SSE Stream, Resume)  |
                        +-------------------+-------------------+
                                            |
                                            v
                        +---------------------------------------+
                        |       haas-api (FastAPI Replicas)     |
                        |  - Stateless HTTP + Resumable SSE     |
                        |  - Idempotency & Tenant Auth          |
                        +------+-------------------------+------+
                               |                         |
         Dispatches Signatures |                         | XREAD BLOCK (SSE)
         & Reads Checkpoints   |                         | & XADD Events
                               v                         v
+-------------------------------------+           +-------------------------------------+
|   PostgreSQL 16 (Durable Storage)   |           |       Redis 7 (Hot Backbone)        |
|  - threads (multi-turn history)     |           |  - Celery Broker & Chord Locks      |
|  - runs (checkpoints, HITL state,   |           |  - Redis Streams: haas:stream:{id}  |
|    OpenRouter USD cost ledger)      |           |  - Hot Run State Cache & Locks      |
+------------------+------------------+           +------+-----------------------+------+
                   ^                                     |                       |
                   | Checkpoints                         | Queue:                | Queue:
                   | & Cost Ledger                       | q_orchestrator        | q_tools_fast /
                   |                                     v                       | q_tools_sandbox
                   |              +-------------------------------+              |
                   +------------> |     worker-orchestrator       |              |
                                  |  - Holds OPENROUTER_API_KEY   |              |
                                  |  - Runs `llm_step_task`       |              |
                                  |  - Runs `aggregate_step_task` |              |
                                  |  - Never runs plugin code     |              |
                                  +---------------+---------------+              |
                                                  |                              |
                                   Dispatches     |                              |
                                   Canvas Chord   +------------------------------+
                                                                                 |
                                                                                 v
                                                  +-------------------------------------+
                                                  |            worker-tools             |
                                                  |  - NO LLM API Keys in Environment   |
                                                  |  - cap_drop: [ALL], read-only root  |
                                                  |  - Executes Python, HTTP & MCP Tools|
                                                  |  - Receives scoped `allowed_secrets`|
                                                  +-------------------------------------+
```

---

## 2. How Celery Canvas Drives the Agent Execution Loop

Instead of a blocking `while` loop, every agent run is a **dynamic Directed Acyclic Graph (DAG)** orchestrated via Celery Canvas primitives:

1. **Step Initiation (`llm_step_task` on `q_orchestrator`):**
   * Loads the current checkpoint (`messages`, `active_skills`, `step_count`, `total_cost_usd`).
   * Builds the deterministically sorted tool schema list (only `is_core=True` meta-tools + tools unlocked by `active_skills` to preserve OpenRouter KV-caches).
   * Calls OpenRouter using **Dual-Model Routing**: uses `planner_model` (e.g., `deepseek/deepseek-r1`) on Step 1 or immediately after a tool error, and `primary_model` (e.g., `deepseek/deepseek-chat-v3-0324`) on standard execution steps.
2. **Branching Decision inside `llm_step_task`:**
   * **Branch A — Terminal Response (`tool_calls` is empty):** Saves final state to PostgreSQL, publishes `run_completed` to Redis Stream `haas:stream:{run_id}`, and optionally chains `deliver_webhook_task.s(...)` on `q_orchestrator`.
   * **Branch B — Human-in-the-Loop Gate (`any(tool.requires_approval)`):** Saves the checkpoint with `status = "input_required"`, publishes `input_required` to the Redis Stream, and exits cleanly. The run consumes zero CPU/RAM while paused.
   * **Branch C — Parallel Tool Execution via Celery `chord`:**
     When the LLM emits $N$ tool calls ($tc_1, \dots, tc_N$), the orchestrator builds a Celery `chord`:
     ```python
     header = group(
         execute_tool_task.s(run_id, tc, scoped_secrets).set(queue=resolve_tool_queue(tc))
         for tc in tool_calls
     )
     body = aggregate_and_continue_task.s(run_id).set(queue="q_orchestrator")
     chord(header)(body)
     ```
3. **Fan-In & Continuation (`aggregate_and_continue_task` on `q_orchestrator`):**
   * Receives the list of tool observations in the exact order of `tool_calls`.
   * Applies Tier-1 Head/Tail truncation (spilling oversized outputs to `/tmp/haas_scratchpad`), appends `role="tool"` messages to the checkpoint, verifies step and USD cost budgets, and dispatches `llm_step_task.s(run_id).set(queue="q_orchestrator")` for `step + 1`.

---

## 3. Repository File Structure

```text
haas-distributed/
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── .env.example
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI API, Redis Stream SSE, MRTR HITL Resume
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py            # Environment & Queue routing configuration
│   │   ├── context.py           # Scoped RunContext & SSRF / Secret guards
│   │   ├── store.py             # Asyncpg PostgreSQL Store + Redis Streams Bus
│   │   ├── plugins.py           # 3-Tier SKILL.md, Python Tools, Stateless MCP HTTP
│   │   └── llm.py               # OpenRouter Client, Dual-Model Router, JSON Repair
│   └── worker/
│       ├── __init__.py
│       ├── celery_app.py        # Celery configuration, queues, reliability settings
│       └── tasks.py             # Canvas tasks: llm_step, execute_tool, aggregate_step
└── plugins/
    ├── skills/
    │   └── revenue-ops/
    │       ├── SKILL.md
    │       └── references/
    │           └── billing_metrics.md
    ├── tools/
    │   └── analytics_tools.py
    └── manifests/
        └── mcp_and_webhooks.json
```

---

## 4. Docker & Compose Infrastructure

### 4.1 `Dockerfile` (Shared Multi-Role Image with Non-Root User)

```dockerfile
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install system dependencies and create non-root execution user
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 haas && useradd -u 10001 -g haas -m -s /bin/bash haas

COPY pyproject.toml ./
COPY app ./app
COPY plugins ./plugins

RUN pip install --upgrade pip && pip install . \
    && mkdir -p /tmp/haas_scratchpad \
    && chown -R haas:haas /app /tmp/haas_scratchpad

USER haas

EXPOSE 8000
```

### 4.2 `docker-compose.yml` (Privilege-Separated Worker Topology)

```yaml
services:
  redis:
    image: redis:7.4-alpine
    command: redis-server --appendonly yes --maxmemory 512mb --maxmemory-policy noeviction
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5
    volumes:
      - redis_data:/data

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: haas
      POSTGRES_PASSWORD: haas_dev_password
      POSTGRES_DB: haas_db
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U haas -d haas_db"]
      interval: 5s
      timeout: 3s
      retries: 5
    volumes:
      - pg_data:/var/lib/postgresql/data

  haas-api:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
    ports:
      - "8000:8000"
    environment:
      POSTGRES_DSN: postgresql://haas:haas_dev_password@postgres:5432/haas_db
      REDIS_URL: redis://redis:6379/0
      CELERY_BROKER_URL: redis://redis:6379/1
      CELERY_RESULT_BACKEND: redis://redis:6379/2
      PLUGINS_DIR: /app/plugins
    volumes:
      - ./plugins:/app/plugins:ro
      - scratchpad_vol:/tmp/haas_scratchpad
    depends_on:
      redis:
        condition: service_healthy
      postgres:
        condition: service_healthy

  # Orchestrator Worker: Holds OPENROUTER_API_KEY, never runs arbitrary tool code
  worker-orchestrator:
    build: .
    command: >
      celery -A app.worker.celery_app worker
      -Q q_orchestrator
      -c 8
      --loglevel=INFO
      -n orchestrator@%h
    environment:
      POSTGRES_DSN: postgresql://haas:haas_dev_password@postgres:5432/haas_db
      REDIS_URL: redis://redis:6379/0
      CELERY_BROKER_URL: redis://redis:6379/1
      CELERY_RESULT_BACKEND: redis://redis:6379/2
      OPENROUTER_API_KEY: ${OPENROUTER_API_KEY:-sk-or-v1-replace-me}
      DEFAULT_PRIMARY_MODEL: ${DEFAULT_PRIMARY_MODEL:-deepseek/deepseek-chat-v3-0324}
      DEFAULT_PLANNER_MODEL: ${DEFAULT_PLANNER_MODEL:-deepseek/deepseek-r1}
      PLUGINS_DIR: /app/plugins
    volumes:
      - ./plugins:/app/plugins:ro
      - scratchpad_vol:/tmp/haas_scratchpad
    depends_on:
      redis:
        condition: service_healthy
      postgres:
        condition: service_healthy

  # Isolated Tool Worker: NO OpenRouter API key, dropped Linux capabilities
  worker-tools:
    build: .
    command: >
      celery -A app.worker.celery_app worker
      -Q q_tools_fast,q_tools_sandbox
      -c 8
      --loglevel=INFO
      -n tools@%h
    environment:
      REDIS_URL: redis://redis:6379/0
      CELERY_BROKER_URL: redis://redis:6379/1
      CELERY_RESULT_BACKEND: redis://redis:6379/2
      PLUGINS_DIR: /app/plugins
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    volumes:
      - ./plugins:/app/plugins:ro
      - scratchpad_vol:/tmp/haas_scratchpad
    depends_on:
      redis:
        condition: service_healthy

  # Optional Real-Time Celery Canvas Monitor
  flower:
    build: .
    command: celery -A app.worker.celery_app flower --port=5555
    ports:
      - "5555:5555"
    environment:
      CELERY_BROKER_URL: redis://redis:6379/1
      CELERY_RESULT_BACKEND: redis://redis:6379/2
    depends_on:
      - redis

volumes:
  redis_data:
  pg_data:
  scratchpad_vol:
```

---

## 5. Complete Application Codebase

### 5.1 `pyproject.toml`

```toml
[project]
name = "haas-distributed"
version = "3.0.0"
description = "Distributed FastAPI + Celery Canvas + Redis Harness-as-a-Service"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "pydantic>=2.10.0",
    "pydantic-settings>=2.6.0",
    "celery[redis]>=5.4.0",
    "redis>=5.2.0",
    "asyncpg>=0.30.0",
    "httpx>=0.28.0",
    "pyyaml>=6.0.2",
    "flower>=2.0.1",
]
```

---

### 5.2 Configuration (`app/core/config.py`)

```python
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Distributed HaaS Platform"
    postgres_dsn: str = "postgresql://haas:haas_dev_password@localhost:5432/haas_db"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    plugins_dir: Path = Path("./plugins")
    scratchpad_dir: Path = Path("/tmp/haas_scratchpad")

    # OpenRouter Settings (Only required on worker-orchestrator)
    openrouter_api_key: str = "sk-or-v1-missing"
    openrouter_base_url: str = "[https://openrouter.ai/api/v1](https://openrouter.ai/api/v1)"
    default_primary_model: str = "deepseek/deepseek-chat-v3-0324"
    default_planner_model: str = "deepseek/deepseek-r1"

    # Guardrails & Budgets
    default_max_steps: int = 15
    default_max_cost_usd: float = 0.30
    per_tool_timeout_sec: float = 25.0
    max_tool_output_chars: int = 4000
    max_identical_tool_calls: int = 2
    redis_stream_max_len: int = 1000


settings = Settings()
```

---

### 5.3 Scoped Execution Context & SSRF Protection (`app/core/context.py`)

```python
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from urllib.parse import urlparse
import httpx


class ToolRemediationError(Exception):
    """Expected domain error with actionable self-correction instructions for the LLM."""


class SecurityViolationError(Exception):
    """Raised on unauthorized secret access, path traversal, or SSRF attempts."""


@dataclass
class RunContext:
    """
    Passed into tool handlers declaring `ctx: RunContext`.
    Constructed inside the isolated tool worker with ONLY the secrets allowed by active skills.
    """
    run_id: str
    thread_id: str
    tenant_id: str
    traceparent: str
    active_skills: set[str] = field(default_factory=set)
    allowed_secrets: set[str] = field(default_factory=set)
    _scoped_secrets: dict[str, str] = field(default_factory=dict, repr=False)
    http_client: httpx.AsyncClient | None = field(default=None, repr=False)

    def get_secret(self, key: str) -> str:
        if key not in self.allowed_secrets:
            raise SecurityViolationError(
                f"Access denied: Secret '{key}' is not declared in any active skill's `allowed_secrets`."
            )
        val = self._scoped_secrets.get(key)
        if not val:
            raise ToolRemediationError(
                f"Secret '{key}' was not supplied in the run's secret vault by the caller."
            )
        return val


def validate_safe_outbound_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SecurityViolationError(f"Invalid scheme '{parsed.scheme}'. Only http/https permitted.")
    host = parsed.hostname or ""
    if host in ("localhost", "metadata.google.internal", "redis", "postgres"):
        raise SecurityViolationError(f"SSRF Guard: Outbound calls to internal host '{host}' are blocked.")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise SecurityViolationError(f"SSRF Guard: Outbound calls to private IP '{ip}' are blocked.")
    except ValueError:
        pass
    return url
```

---

### 5.4 PostgreSQL Durable Store + Redis Streams Event Bus (`app/core/store.py`)

Combines **PostgreSQL (`asyncpg`)** for acid-compliant multi-turn threads, checkpoints, and cost accounting with **Redis Streams (`XADD` / `XREAD`)** for instant, resumable SSE streaming and encrypted ephemeral secret storage (`haas:secrets:{run_id}`).

```python
from __future__ import annotations

import json
import time
from typing import Any
import asyncpg
import redis.asyncio as aioredis

from app.core.config import settings


class DistributedStore:
    def __init__(self) -> None:
        self.pg_pool: asyncpg.Pool | None = None
        self.redis: aioredis.Redis | None = None

    async def init(self) -> None:
        self.pg_pool = await asyncpg.create_pool(dsn=settings.postgres_dsn, min_size=2, max_size=15)
        self.redis = aioredis.from_url(settings.redis_url, decode_responses=True)

        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
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
                """
            )

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

    async def store_ephemeral_secrets(self, run_id: str, secrets: dict[str, str], ttl_sec: int = 3600) -> None:
        if secrets:
            await self.rds.setex(f"haas:secrets:{run_id}", ttl_sec, json.dumps(secrets))

    async def get_scoped_secrets(self, run_id: str, allowed_keys: set[str]) -> dict[str, str]:
        raw = await self.rds.get(f"haas:secrets:{run_id}")
        if not raw:
            return {}
        full: dict[str, str] = json.loads(raw)
        return {k: v for k, v in full.items() if k in allowed_keys}

    async def clear_ephemeral_secrets(self, run_id: str) -> None:
        await self.rds.delete(f"haas:secrets:{run_id}")

    async def acquire_state_lock(self, key: str, ttl_sec: int = 15) -> bool:
        return bool(await self.rds.set(f"haas:lock:{key}", "1", nx=True, ex=ttl_sec))

    # --- Redis Streams Real-Time Event Bus (XADD / XREAD) ---

    async def publish_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> str:
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
            row = await conn.fetchrow(
                "SELECT messages_json FROM threads WHERE thread_id = $1 AND tenant_id = $2",
                thread_id,
                tenant_id,
            )
            if row:
                return json.loads(row["messages_json"])
            await conn.execute(
                "INSERT INTO threads (thread_id, tenant_id, messages_json, updated_at) VALUES ($1, $2, '[]'::jsonb, $3)",
                thread_id,
                tenant_id,
                time.time(),
            )
            return []

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
            d["active_skills"] = json.loads(d["active_skills_json"])
            d["messages"] = json.loads(d["messages_checkpoint_json"])
            d["fingerprints"] = json.loads(d["fingerprints_json"])
            d["pending_tool_calls"] = (
                json.loads(d["pending_tool_calls_json"]) if d["pending_tool_calls_json"] else None
            )
            return d

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
                json.dumps(pending_tool_calls) if pending_tool_calls is not None else None,
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
```

---

### 5.5 Pluggable Skill & Tool Registry (`app/core/plugins.py`)

Adds **Queue Routing metadata (`queue="q_tools_fast" | "q_tools_sandbox"`)** per tool so Celery automatically dispatches heavy/untrusted tools to sandboxed workers and lightweight API tools to fast workers.

```python
from __future__ import annotations

import hashlib
import hmac
import importlib.metadata
import importlib.util
import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml
from pydantic import BaseModel, Field, create_model

from app.core.context import (
    RunContext,
    ToolRemediationError,
    validate_safe_outbound_url,
)


@dataclass
class RegisteredTool:
    name: str
    description: str
    args_schema: type[BaseModel]
    handler: Callable[..., Awaitable[Any] | Any]
    injects_context: bool
    requires_approval: bool = False
    is_core: bool = False
    queue: str = "q_tools_fast"  # "q_tools_fast" or "q_tools_sandbox"

    def to_openai_schema(self) -> dict[str, Any]:
        schema = self.args_schema.model_json_schema()
        schema.pop("title", None)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }


@dataclass
class RegisteredSkill:
    name: str
    description: str
    instructions_markdown: str
    root_dir: Path
    version: str = "1.0.0"
    required_tools: list[str] = field(default_factory=list)
    allowed_secrets: list[str] = field(default_factory=list)
    available_references: list[str] = field(default_factory=list)


class PluginRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, RegisteredTool] = {}
        self.skills: dict[str, RegisteredSkill] = {}

    def tool(
        self,
        *,
        name: str,
        description: str,
        args_schema: type[BaseModel],
        requires_approval: bool = False,
        is_core: bool = False,
        sandbox: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            sig = inspect.signature(fn)
            self.tools[name] = RegisteredTool(
                name=name,
                description=description,
                args_schema=args_schema,
                handler=fn,
                injects_context="ctx" in sig.parameters,
                requires_approval=requires_approval,
                is_core=is_core,
                queue="q_tools_sandbox" if sandbox else "q_tools_fast",
            )
            return fn
        return decorator

    def load_skill_directory(self, skill_dir: Path) -> None:
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            return
        raw = skill_md.read_text(encoding="utf-8")
        if not raw.startswith("---"):
            raise ValueError(f"{skill_md} missing YAML frontmatter")
        _, fm_raw, body = raw.split("---", 2)
        meta = yaml.safe_load(fm_raw) or {}

        name = str(meta["name"]).strip()
        refs_dir = skill_dir / "references"
        refs = (
            sorted(p.name for p in refs_dir.glob("*") if p.is_file())
            if refs_dir.exists()
            else []
        )
        self.skills[name] = RegisteredSkill(
            name=name,
            description=str(meta["description"]).strip()[:1024],
            instructions_markdown=body.strip(),
            root_dir=skill_dir.resolve(),
            version=str(meta.get("version", "1.0.0")),
            required_tools=list(meta.get("required_tools", [])),
            allowed_secrets=list(meta.get("allowed_secrets", [])),
            available_references=refs,
        )

    def register_external_manifest(self, manifest: dict[str, Any]) -> None:
        name = manifest["name"]
        description = manifest["description"]
        protocol = manifest.get("protocol", "http")
        endpoint_url = validate_safe_outbound_url(manifest["endpoint_url"])
        secret_key = manifest.get("secret_key")
        requires_approval = bool(manifest.get("requires_approval", False))

        props = manifest.get("parameters", {}).get("properties", {})
        req_fields = set(manifest.get("parameters", {}).get("required", []))
        type_map = {"string": str, "integer": int, "number": float, "boolean": bool, "object": dict, "array": list}
        field_defs: dict[str, Any] = {}

        for p_name, p_spec in props.items():
            py_t = type_map.get(p_spec.get("type", "string"), Any)
            default = ... if p_name in req_fields else p_spec.get("default", None)
            field_defs[p_name] = (py_t, Field(default, description=p_spec.get("description", "")))

        DynamicSchema = create_model(f"{name}_Args", **field_defs)

        async def _external_handler(ctx: RunContext, args: BaseModel) -> str:
            assert ctx.http_client is not None
            payload_dict = args.model_dump(exclude_none=True)
            headers = {"Content-Type": "application/json", "traceparent": ctx.traceparent}

            if secret_key:
                secret_val = ctx.get_secret(secret_key)
                sig = hmac.new(
                    secret_val.encode(),
                    json.dumps(payload_dict, sort_keys=True).encode(),
                    hashlib.sha256,
                ).hexdigest()
                headers["Authorization"] = f"Bearer {secret_val}"
                headers["X-HaaS-Signature-256"] = f"sha256={sig}"

            if protocol == "mcp_http":
                headers["MCP-Protocol-Version"] = "2026-07-28"
                headers["Mcp-Method"] = "tools/call"
                headers["Mcp-Name"] = name
                body = {
                    "jsonrpc": "2.0",
                    "id": ctx.run_id,
                    "method": "tools/call",
                    "params": {
                        "name": name,
                        "arguments": payload_dict,
                        "_meta": {"traceparent": ctx.traceparent},
                    },
                }
            else:
                body = payload_dict

            resp = await ctx.http_client.post(endpoint_url, headers=headers, json=body, timeout=20.0)
            resp.raise_for_status()
            return resp.text

        self.tools[name] = RegisteredTool(
            name=name,
            description=description,
            args_schema=DynamicSchema,
            handler=_external_handler,
            injects_context=True,
            requires_approval=requires_approval,
            queue="q_tools_fast",
        )

    def discover_all(self, plugins_dir: Path) -> None:
        skills_path = plugins_dir / "skills"
        if skills_path.exists():
            for child in sorted(skills_path.iterdir()):
                if child.is_dir():
                    self.load_skill_directory(child)

        tools_path = plugins_dir / "tools"
        if tools_path.exists():
            for py_file in sorted(tools_path.glob("*.py")):
                if py_file.name.startswith("_"):
                    continue
                spec = importlib.util.spec_from_file_location(py_file.stem, py_file)
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    if hasattr(mod, "register"):
                        mod.register(self)

        manifests_path = plugins_dir / "manifests"
        if manifests_path.exists():
            for jf in sorted(manifests_path.glob("*.json")):
                items = json.loads(jf.read_text(encoding="utf-8"))
                for item in (items if isinstance(items, list) else [items]):
                    self.register_external_manifest(item)

        for ep in importlib.metadata.entry_points(group="haas.plugins"):
            ep.load()(self)

    def resolve_allowed_secrets(self, active_skills: set[str]) -> set[str]:
        allowed: set[str] = set()
        for s_name in active_skills:
            skill = self.skills.get(s_name)
            if skill:
                allowed.update(skill.allowed_secrets)
        return allowed

    def resolve_sorted_tool_schemas(self, active_skills: set[str]) -> list[dict[str, Any]]:
        unlocked = {name for name, t in self.tools.items() if t.is_core}
        for s_name in active_skills:
            skill = self.skills.get(s_name)
            if skill:
                unlocked.update(skill.required_tools)
        return [
            self.tools[name].to_openai_schema()
            for name in sorted(unlocked)
            if name in self.tools
        ]


registry = PluginRegistry()


# --- Core Built-in Meta-Tools ---

class ActivateSkillArgs(BaseModel):
    skill_names: list[str] = Field(..., description="Skill names from the catalog to activate.")


@registry.tool(
    name="activate_skill",
    description="Activates one or more domain skills, returning their Markdown SOP and unlocking their tools.",
    args_schema=ActivateSkillArgs,
    is_core=True,
)
async def activate_skill(ctx: RunContext, args: ActivateSkillArgs) -> str:
    blocks: list[str] = []
    missing: list[str] = []
    for name in args.skill_names:
        skill = registry.skills.get(name)
        if not skill:
            missing.append(name)
            continue
        ctx.active_skills.add(name)
        refs_info = (
            f"\nBundled Tier-3 References (call `read_skill_reference`): {skill.available_references}"
            if skill.available_references
            else ""
        )
        blocks.append(
            f"=== ACTIVATED SKILL: {skill.name} (v{skill.version}) ===\n"
            f"Unlocked Tools: {', '.join(skill.required_tools) or 'None'}{refs_info}\n\n"
            f"{skill.instructions_markdown}"
        )
    if missing:
        avail = ", ".join(sorted(registry.skills.keys()))
        raise ToolRemediationError(f"Unknown skill(s): {missing}. Available: [{avail}]")
    return "\n\n".join(blocks)


class ReadSkillReferenceArgs(BaseModel):
    skill_name: str = Field(..., description="Already activated skill name.")
    filename: str = Field(..., description="File inside the skill's references/ folder.")


@registry.tool(
    name="read_skill_reference",
    description="Reads a Tier-3 reference document from an activated skill's `references/` directory.",
    args_schema=ReadSkillReferenceArgs,
    is_core=True,
)
async def read_skill_reference(ctx: RunContext, args: ReadSkillReferenceArgs) -> str:
    if args.skill_name not in ctx.active_skills:
        raise ToolRemediationError(f"Skill '{args.skill_name}' is not active. Call `activate_skill` first.")
    skill = registry.skills[args.skill_name]
    ref_dir = (skill.root_dir / "references").resolve()
    target = (ref_dir / args.filename).resolve()
    if not str(target).startswith(str(ref_dir)) or not target.is_file():
        raise ToolRemediationError(
            f"Reference '{args.filename}' not found. Available: {skill.available_references}"
        )
    return target.read_text(encoding="utf-8")
```

---

### 5.6 OpenRouter Dual-Model Client & JSON Repair (`app/core/llm.py`)

```python
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
import httpx


def repair_tool_json(raw: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
    return json.loads(cleaned) if cleaned else {}


@dataclass
class CompletionResult:
    assistant_message: dict[str, Any]
    model_used: str
    cost_usd: float
    prompt_tokens: int
    cached_tokens: int


class OpenRouterGateway:
    def __init__(self, api_key: str, base_url: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    @staticmethod
    def select_model_for_step(
        step: int,
        last_step_had_error: bool,
        primary_model: str,
        planner_model: str,
    ) -> str:
        """
        Architect/Editor Dual-Model Routing:
        Uses the reasoning/planner model on Step 1 and immediately following any tool error;
        uses the fast/cost-effective execution model for standard tool-calling turns.
        """
        if step == 1 or last_step_had_error:
            return planner_model
        return primary_model

    async def chat_completion(
        self,
        client: httpx.AsyncClient,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        traceparent: str,
    ) -> CompletionResult:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "usage": {"include": True},
        }
        if tools:
            payload["tools"] = tools

        resp = await client.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "https://haas.internal",
                "X-Title": "FastAPI-Celery-HaaS",
                "traceparent": traceparent,
            },
            json=payload,
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()

        choice = data["choices"][0]["message"]
        usage = data.get("usage") or {}
        return CompletionResult(
            assistant_message=choice,
            model_used=model,
            cost_usd=float(usage.get("cost") or 0.0),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            cached_tokens=int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
        )
```

---

### 5.7 Celery Application & Reliability Configuration (`app/worker/celery_app.py`)

Configured with `task_acks_late=True`, `worker_prefetch_multiplier=1`, and three isolated queues (`q_orchestrator`, `q_tools_fast`, `q_tools_sandbox`) so long-running or sandboxed tasks never block fast tasks.

```python
from celery import Celery
from celery.signals import worker_process_init
from kombu import Queue

from app.core.config import settings
from app.core.plugins import registry

celery_app = Celery(
    "haas_worker",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Critical for AI Agents: Never hoard tasks; acknowledge only after step completion
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    # Chord & Result Expiration
    result_expires=3600,
    chord_unlock_max_retries=100,
    broker_transport_options={
        "visibility_timeout": 600,  # 10 min safety margin
    },
    task_queues=(
        Queue("q_orchestrator"),
        Queue("q_tools_fast"),
        Queue("q_tools_sandbox"),
    ),
    task_default_queue="q_orchestrator",
)


@worker_process_init.connect
def init_worker_plugins(**kwargs) -> None:
    """Loads all SKILL.md, Python Tools, and MCP manifests when each worker process forks."""
    settings.plugins_dir.mkdir(parents=True, exist_ok=True)
    settings.scratchpad_dir.mkdir(parents=True, exist_ok=True)
    registry.discover_all(settings.plugins_dir)
```

---

### 5.8 Celery Canvas Agent Workflow Tasks (`app/worker/tasks.py`)

Implements the three core Canvas tasks:
1. `llm_step_task` (`q_orchestrator`): Performs 1 reasoning step and dispatches a `chord(group(tools), aggregate_step_task)` when tools are requested.
2. `execute_tool_task` (`q_tools_fast` or `q_tools_sandbox`): Runs inside the privilege-dropped tool worker container with scoped secrets and Pydantic boundary validation.
3. `aggregate_step_task` (`q_orchestrator`): Chord callback that collects parallel tool observations, bounds outputs, updates `active_skills`, checks budgets, and chains the next `llm_step_task`.

```python
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from typing import Any

import httpx
from celery import chord, group
from pydantic import ValidationError

from app.core.config import settings
from app.core.context import RunContext, SecurityViolationError, ToolRemediationError
from app.core.llm import OpenRouterGateway, repair_tool_json
from app.core.plugins import registry
from app.core.store import DistributedStore
from app.worker.celery_app import celery_app

gateway = OpenRouterGateway(settings.openrouter_api_key, settings.openrouter_base_url)


def _run_async(coro: Any) -> Any:
    """Executes an async coroutine cleanly inside a Celery worker process."""
    return asyncio.run(coro)


def _bound_observation(tool_name: str, call_id: str, raw_text: str) -> str:
    limit = settings.max_tool_output_chars
    if len(raw_text) <= limit:
        return raw_text
    half = (limit - 400) // 2
    safe_id = "".join(c for c in f"{tool_name}_{call_id}" if c.isalnum() or c in ("_", "-"))[:48]
    spill_file = settings.scratchpad_dir / f"{safe_id}.txt"
    spill_file.write_text(raw_text, encoding="utf-8")
    omitted = len(raw_text) - (half * 2)
    return (
        f"{raw_text[:half]}\n\n"
        f"... [HARNESS TRUNCATED {omitted:,} CHARS — Full output at {spill_file}] ...\n\n"
        f"{raw_text[-half:]}"
    )


def _fingerprint(name: str, raw_args: Any) -> str:
    canonical = json.dumps({"n": name, "a": raw_args}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@celery_app.task(name="haas.llm_step_task", bind=True, queue="q_orchestrator", max_retries=2)
def llm_step_task(self, run_id: str) -> dict[str, Any]:
    return _run_async(_async_llm_step(self, run_id))


async def _async_llm_step(task_self: Any, run_id: str) -> dict[str, Any]:
    store = DistributedStore()
    await store.init()
    try:
        run = await store.get_run(run_id)
        if not run or run["status"] != "running":
            return {"status": "aborted", "reason": "Run not in running state"}

        step = int(run["step_count"]) + 1
        max_steps = int(run["max_steps"])
        if step > max_steps:
            msg = f"Exceeded max_steps budget ({max_steps})."
            await store.finalize_run(run_id, run["thread_id"], "failed", msg, run["messages"])
            await store.publish_event(run_id, "run_error", {"error": msg})
            return {"status": "failed", "reason": msg}

        active_skills = set(run["active_skills"])
        messages: list[dict[str, Any]] = run["messages"]
        fingerprints: list[str] = run["fingerprints"]

        chosen_model = gateway.select_model_for_step(
            step=step,
            last_step_had_error=bool(run["last_step_had_error"]),
            primary_model=run["primary_model"],
            planner_model=run["planner_model"],
        )
        tools_payload = registry.resolve_sorted_tool_schemas(active_skills)

        await store.publish_event(
            run_id,
            "step_started",
            {
                "step": step,
                "model": chosen_model,
                "active_skills": sorted(active_skills),
                "unlocked_tools_count": len(tools_payload),
            },
        )

        async with httpx.AsyncClient() as http_client:
            completion = await gateway.chat_completion(
                http_client,
                model=chosen_model,
                messages=messages,
                tools=tools_payload,
                traceparent=run["traceparent"],
            )

        assistant_msg = completion.assistant_message
        messages.append(assistant_msg)
        new_total_cost = float(run["total_cost_usd"]) + completion.cost_usd

        # Case 1: Model finished without tool calls
        tool_calls = assistant_msg.get("tool_calls") or []
        if not tool_calls:
            final_text = assistant_msg.get("content") or ""
            await store.finalize_run(
                run_id,
                run["thread_id"],
                "completed",
                final_text,
                messages,
                cost_delta_usd=completion.cost_usd,
            )
            await store.publish_event(
                run_id,
                "run_completed",
                {
                    "output": final_text,
                    "steps_used": step,
                    "total_cost_usd": round(new_total_cost, 6),
                    "cached_tokens": completion.cached_tokens,
                },
            )
            if run["webhook_url"]:
                deliver_webhook_task.apply_async(
                    args=[run["webhook_url"], {"run_id": run_id, "status": "completed", "output": final_text}],
                    queue="q_orchestrator",
                )
            return {"status": "completed", "run_id": run_id}

        # Case 2: Check USD budget before executing tools
        if new_total_cost > float(run["max_cost_usd"]):
            err = f"Exceeded max_cost_usd budget (${new_total_cost:.4f} >${run['max_cost_usd']:.4f})."
            await store.finalize_run(
                run_id, run["thread_id"], "failed", err, messages, cost_delta_usd=completion.cost_usd
            )
            await store.publish_event(run_id, "run_error", {"error": err})
            return {"status": "failed", "reason": err}

        # Case 3: Human-in-the-Loop (HITL) Gate — Suspend Canvas Workflow (0 Worker Hold)
        approval_needed = [
            tc for tc in tool_calls
            if (t := registry.tools.get(tc["function"]["name"])) and t.requires_approval
        ]
        if approval_needed:
            await store.save_checkpoint(
                run_id,
                step_count=step,
                messages=messages,
                active_skills=active_skills,
                fingerprints=fingerprints,
                cost_delta_usd=completion.cost_usd,
                status="input_required",
                pending_tool_calls=tool_calls,
            )
            await store.publish_event(
                run_id,
                "input_required",
                {
                    "step": step,
                    "pending_tool_calls": [
                        {"id": tc["id"], "name": tc["function"]["name"], "arguments": tc["function"].get("arguments")}
                        for tc in tool_calls
                    ],
                    "resume_endpoint": f"/v1/runs/{run_id}/resume",
                },
            )
            return {"status": "input_required", "run_id": run_id}

        # Case 4: Parallel Tool Fan-Out via Celery Canvas `chord`
        await store.save_checkpoint(
            run_id,
            step_count=step,
            messages=messages,
            active_skills=active_skills,
            fingerprints=fingerprints,
            cost_delta_usd=completion.cost_usd,
            status="running",
        )

        allowed_secret_keys = registry.resolve_allowed_secrets(active_skills)
        scoped_secrets = await store.get_scoped_secrets(run_id, allowed_secret_keys)

        tool_signatures = []
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            reg_tool = registry.tools.get(fn_name)
            target_queue = reg_tool.queue if reg_tool else "q_tools_fast"

            await store.publish_event(
                run_id,
                "tool_dispatched",
                {"tool_call_id": tc["id"], "tool": fn_name, "queue": target_queue},
            )
            tool_signatures.append(
                execute_tool_task.s(
                    run_id=run_id,
                    thread_id=run["thread_id"],
                    tenant_id=run["tenant_id"],
                    traceparent=run["traceparent"],
                    active_skills=sorted(active_skills),
                    allowed_secrets=sorted(allowed_secret_keys),
                    scoped_secrets=scoped_secrets,
                    tool_call=tc,
                ).set(queue=target_queue)
            )

        callback_sig = aggregate_step_task.s(run_id=run_id).set(queue="q_orchestrator")
        chord(group(tool_signatures))(callback_sig)
        return {"status": "dispatched_chord", "tool_count": len(tool_signatures)}
    finally:
        await store.close()


@celery_app.task(name="haas.execute_tool_task", bind=True)
def execute_tool_task(
    self,
    *,
    run_id: str,
    thread_id: str,
    tenant_id: str,
    traceparent: str,
    active_skills: list[str],
    allowed_secrets: list[str],
    scoped_secrets: dict[str, str],
    tool_call: dict[str, Any],
) -> dict[str, Any]:
    """
    Runs on `worker-tools` (`q_tools_fast` or `q_tools_sandbox`).
    Completely isolated from OpenRouter credentials and PostgreSQL.
    """
    return _run_async(
        _async_execute_tool(
            run_id=run_id,
            thread_id=thread_id,
            tenant_id=tenant_id,
            traceparent=traceparent,
            active_skills=set(active_skills),
            allowed_secrets=set(allowed_secrets),
            scoped_secrets=scoped_secrets,
            tool_call=tool_call,
        )
    )


async def _async_execute_tool(
    *,
    run_id: str,
    thread_id: str,
    tenant_id: str,
    traceparent: str,
    active_skills: set[str],
    allowed_secrets: set[str],
    scoped_secrets: dict[str, str],
    tool_call: dict[str, Any],
) -> dict[str, Any]:
    tc_id = tool_call["id"]
    fn_name = tool_call["function"]["name"]
    raw_args = tool_call["function"].get("arguments", "{}")
    fp = _fingerprint(fn_name, raw_args)

    tool = registry.tools.get(fn_name)
    if not tool:
        return {
            "tool_call_id": tc_id,
            "name": fn_name,
            "fingerprint": fp,
            "is_error": True,
            "activated_skills": sorted(active_skills),
            "content": (
                f"Error: Tool '{fn_name}' does not exist or is not unlocked. "
                f"Call `activate_skill` first to unlock domain tools."
            ),
        }

    try:
        parsed_args = repair_tool_json(raw_args)
        validated_args = tool.args_schema.model_validate(parsed_args)
    except (json.JSONDecodeError, ValidationError) as exc:
        return {
            "tool_call_id": tc_id,
            "name": fn_name,
            "fingerprint": fp,
            "is_error": True,
            "activated_skills": sorted(active_skills),
            "content": f"Schema Validation Failed for '{fn_name}': {exc}. Fix arguments and retry.",
        }

    async with httpx.AsyncClient() as http_client:
        ctx = RunContext(
            run_id=run_id,
            thread_id=thread_id,
            tenant_id=tenant_id,
            traceparent=traceparent,
            active_skills=active_skills,
            allowed_secrets=allowed_secrets,
            _scoped_secrets=scoped_secrets,
            http_client=http_client,
        )
        try:
            res = tool.handler(ctx, validated_args) if tool.injects_context else tool.handler(validated_args)
            if inspect.iscoroutine(res):
                raw_out = await asyncio.wait_for(res, timeout=settings.per_tool_timeout_sec)
            else:
                raw_out = await asyncio.wait_for(
                    asyncio.to_thread(lambda: res), timeout=settings.per_tool_timeout_sec
                )
            bounded = _bound_observation(fn_name, tc_id, str(raw_out))
            return {
                "tool_call_id": tc_id,
                "name": fn_name,
                "fingerprint": fp,
                "is_error": False,
                "activated_skills": sorted(ctx.active_skills),
                "content": bounded,
            }
        except asyncio.TimeoutError:
            return {
                "tool_call_id": tc_id,
                "name": fn_name,
                "fingerprint": fp,
                "is_error": True,
                "activated_skills": sorted(ctx.active_skills),
                "content": f"Timeout Error: '{fn_name}' exceeded {settings.per_tool_timeout_sec}s.",
            }
        except (ToolRemediationError, SecurityViolationError) as exc:
            return {
                "tool_call_id": tc_id,
                "name": fn_name,
                "fingerprint": fp,
                "is_error": True,
                "activated_skills": sorted(ctx.active_skills),
                "content": f"Tool Error: {exc}",
            }
        except Exception as exc:
            return {
                "tool_call_id": tc_id,
                "name": fn_name,
                "fingerprint": fp,
                "is_error": True,
                "activated_skills": sorted(ctx.active_skills),
                "content": f"Runtime Error in '{fn_name}' ({type(exc).__name__}): {exc}",
            }


@celery_app.task(name="haas.aggregate_step_task", queue="q_orchestrator")
def aggregate_step_task(tool_outcomes: list[dict[str, Any]], run_id: str) -> dict[str, Any]:
    """
    Celery Chord Callback:
    Merges parallel tool outputs, enforces the identical-call circuit breaker,
    checkpoints the updated state, and triggers the next `llm_step_task`.
    """
    return _run_async(_async_aggregate_step(tool_outcomes, run_id))


async def _async_aggregate_step(
    tool_outcomes: list[dict[str, Any]], run_id: str
) -> dict[str, Any]:
    store = DistributedStore()
    await store.init()
    try:
        run = await store.get_run(run_id)
        if not run or run["status"] != "running":
            return {"status": "aborted"}

        messages: list[dict[str, Any]] = run["messages"]
        active_skills: set[str] = set(run["active_skills"])
        fingerprints: list[str] = run["fingerprints"]
        any_error = False

        for outcome in tool_outcomes:
            active_skills.update(outcome.get("activated_skills", []))
            fp = outcome["fingerprint"]
            fingerprints.append(fp)

            window = settings.max_identical_tool_calls + 1
            if len(fingerprints) >= window and all(x == fp for x in fingerprints[-window:]):
                content = (
                    f"Circuit Breaker Triggered: Identical call to '{outcome['name']}' "
                    f"repeated {window} times consecutively. Change parameters or strategy."
                )
                is_err = True
            else:
                content = outcome["content"]
                is_err = bool(outcome["is_error"])

            any_error = any_error or is_err
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": outcome["tool_call_id"],
                    "name": outcome["name"],
                    "content": content,
                }
            )
            await store.publish_event(
                run_id,
                "tool_result",
                {
                    "tool_call_id": outcome["tool_call_id"],
                    "tool": outcome["name"],
                    "is_error": is_err,
                    "preview": content[:300],
                },
            )

        await store.save_checkpoint(
            run_id,
            step_count=int(run["step_count"]),
            messages=messages,
            active_skills=active_skills,
            fingerprints=fingerprints,
            last_step_had_error=any_error,
            status="running",
        )

        # Dispatch next turn onto q_orchestrator
        llm_step_task.apply_async(args=[run_id], queue="q_orchestrator")
        return {"status": "continued", "run_id": run_id}
    finally:
        await store.close()


@celery_app.task(name="haas.deliver_webhook_task", queue="q_orchestrator", max_retries=3)
def deliver_webhook_task(webhook_url: str, payload: dict[str, Any]) -> None:
    with httpx.Client(timeout=10.0) as client:
        client.post(webhook_url, json=payload)
```

---

### 5.9 FastAPI Transport Layer with Redis Streams SSE (`app/main.py`)

```python
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from celery import chord, group
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.plugins import registry
from app.core.store import DistributedStore
from app.worker.tasks import aggregate_step_task, execute_tool_task, llm_step_task

store = DistributedStore()


def build_tier1_system_prompt() -> str:
    catalog = [
        f"- `{name}`: {skill.description}"
        for name, skill in sorted(registry.skills.items())
    ]
    catalog_block = "\n".join(catalog) if catalog else "(No domain skills installed)"
    return (
        "You are an autonomous execution agent running inside a deterministic distributed harness.\n"
        "1. Review the Domain Skills Catalog below. When a task matches a skill, call "
        "`activate_skill` FIRST to load its Standard Operating Procedure and unlock its tools.\n"
        "2. Never guess parameters or schema names. Follow remediation errors carefully.\n\n"
        "## Domain Skills Catalog (Tier 1)\n"
        f"{catalog_block}"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.plugins_dir.mkdir(parents=True, exist_ok=True)
    registry.discover_all(settings.plugins_dir)
    await store.init()
    yield
    await store.close()


app = FastAPI(title=settings.app_name, version="3.0.0", lifespan=lifespan)


class CreateRunRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    thread_id: str | None = None
    preload_skills: list[str] = Field(default_factory=list)
    primary_model: str | None = None
    planner_model: str | None = None
    max_steps: int = Field(default=settings.default_max_steps, ge=1, le=50)
    max_cost_usd: float = Field(default=settings.default_max_cost_usd, gt=0.0, le=10.0)
    webhook_url: str | None = None
    secrets: dict[str, str] = Field(default_factory=dict)


class ResumeRunRequest(BaseModel):
    approved: bool
    rejection_reason: str | None = None
    secrets: dict[str, str] = Field(default_factory=dict)


@app.post("/v1/runs", status_code=202)
async def create_run(
    req: CreateRunRequest,
    x_tenant_id: str = Header(default="default-tenant"),
    traceparent: str | None = Header(default=None),
) -> dict[str, Any]:
    run_id = f"run_{uuid.uuid4().hex[:14]}"
    thread_id = req.thread_id or f"thd_{uuid.uuid4().hex[:12]}"
    trace_id = traceparent or f"00-{uuid.uuid4().hex}-{uuid.uuid4().hex[:16]}-01"

    history = await store.get_or_create_thread_messages(thread_id, x_tenant_id)
    if not history:
        history.append({"role": "system", "content": build_tier1_system_prompt()})
    history.append({"role": "user", "content": req.prompt})

    await store.store_ephemeral_secrets(run_id, req.secrets)
    await store.create_run(
        run_id=run_id,
        thread_id=thread_id,
        tenant_id=x_tenant_id,
        traceparent=trace_id,
        primary_model=req.primary_model or settings.default_primary_model,
        planner_model=req.planner_model or settings.default_planner_model,
        initial_skills=req.preload_skills,
        initial_messages=history,
        max_steps=req.max_steps,
        max_cost_usd=req.max_cost_usd,
        webhook_url=req.webhook_url,
    )
    await store.publish_event(run_id, "run_queued", {"run_id": run_id, "thread_id": thread_id})

    # Dispatch Step 1 to Celery Orchestrator Queue
    llm_step_task.apply_async(args=[run_id], queue="q_orchestrator")

    return {
        "run_id": run_id,
        "thread_id": thread_id,
        "status": "running",
        "stream_url": f"/v1/runs/{run_id}/stream",
    }


@app.post("/v1/runs/{run_id}/resume", status_code=202)
async def resume_run(
    run_id: str,
    req: ResumeRunRequest,
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    """
    Resumes a paused `input_required` run using an atomic Redis lock so two operators
    cannot double-approve the same tool batch.
    """
    if not await store.acquire_state_lock(f"resume:{run_id}", ttl_sec=15):
        raise HTTPException(status_code=409, detail="Resume already in progress for this run.")

    run = await store.get_run(run_id)
    if not run or run["tenant_id"] != x_tenant_id:
        raise HTTPException(status_code=404, detail="Run not found.")
    if run["status"] != "input_required":
        raise HTTPException(status_code=409, detail=f"Run status is '{run['status']}', not 'input_required'.")

    if req.secrets:
        await store.store_ephemeral_secrets(run_id, req.secrets)

    pending_calls: list[dict[str, Any]] = run["pending_tool_calls"] or []
    active_skills = set(run["active_skills"])
    messages = run["messages"]

    if not req.approved:
        reason = req.rejection_reason or "Rejected by human operator."
        for tc in pending_calls:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": tc["function"]["name"],
                    "content": f"Action Denied by Human Operator: {reason}",
                }
            )
        await store.save_checkpoint(
            run_id,
            step_count=int(run["step_count"]),
            messages=messages,
            active_skills=active_skills,
            fingerprints=run["fingerprints"],
            last_step_had_error=True,
            status="running",
            pending_tool_calls=None,
        )
        await store.publish_event(run_id, "hitl_rejected", {"reason": reason})
        llm_step_task.apply_async(args=[run_id], queue="q_orchestrator")
        return {"run_id": run_id, "status": "running", "action": "rejected_and_resumed"}

    # Operator approved: fan out the pending tools via Celery Canvas `chord`
    await store.save_checkpoint(
        run_id,
        step_count=int(run["step_count"]),
        messages=messages,
        active_skills=active_skills,
        fingerprints=run["fingerprints"],
        status="running",
        pending_tool_calls=None,
    )
    allowed_keys = registry.resolve_allowed_secrets(active_skills)
    scoped_secrets = await store.get_scoped_secrets(run_id, allowed_keys)

    sigs = [
        execute_tool_task.s(
            run_id=run_id,
            thread_id=run["thread_id"],
            tenant_id=run["tenant_id"],
            traceparent=run["traceparent"],
            active_skills=sorted(active_skills),
            allowed_secrets=sorted(allowed_keys),
            scoped_secrets=scoped_secrets,
            tool_call=tc,
        ).set(queue=registry.tools[tc["function"]["name"]].queue if tc["function"]["name"] in registry.tools else "q_tools_fast")
        for tc in pending_calls
    ]
    callback = aggregate_step_task.s(run_id=run_id).set(queue="q_orchestrator")
    chord(group(sigs))(callback)

    await store.publish_event(run_id, "hitl_approved", {"dispatched_tools": len(sigs)})
    return {"run_id": run_id, "status": "running", "action": "approved_and_dispatched"}


@app.get("/v1/runs/{run_id}/stream")
async def stream_run_events(
    run_id: str,
    last_event_id: str = Header(default="0-0", alias="Last-Event-ID"),
) -> StreamingResponse:
    """
    Zero-polling SSE endpoint powered by Redis Streams (`XREAD BLOCK`).
    Works seamlessly across multiple FastAPI container replicas.
    """
    stream_key = f"haas:stream:{run_id}"

    async def sse_generator() -> AsyncGenerator[str, None]:
        cursor = last_event_id if "-" in last_event_id else "0-0"
        while True:
            entries = await store.rds.xread({stream_key: cursor}, count=50, block=5000)
            if entries:
                _, messages = entries[0]
                for entry_id, fields in messages:
                    cursor = entry_id
                    ev_type = fields.get("event_type", "message")
                    payload = fields.get("payload_json", "{}")
                    yield f"id: {entry_id}\nevent: {ev_type}\ndata: {payload}\n\n"
                    if ev_type in ("run_completed", "run_error", "input_required"):
                        return

            run = await store.get_run(run_id)
            if not run or run["status"] in ("completed", "failed", "cancelled", "input_required"):
                break

    return StreamingResponse(sse_generator(), media_type="text/event-stream")


@app.get("/v1/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    run = await store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found.")
    return {
        "run_id": run["run_id"],
        "thread_id": run["thread_id"],
        "status": run["status"],
        "step_count": run["step_count"],
        "total_cost_usd": round(float(run["total_cost_usd"]), 6),
        "active_skills": run["active_skills"],
        "pending_tool_calls": run["pending_tool_calls"],
        "result": run["result_text"],
    }


@app.get("/v1/plugins")
async def get_plugins() -> dict[str, Any]:
    return {
        "skills": [
            {
                "name": s.name,
                "version": s.version,
                "description": s.description,
                "required_tools": s.required_tools,
                "allowed_secrets": s.allowed_secrets,
                "references": s.available_references,
            }
            for s in sorted(registry.skills.values(), key=lambda x: x.name)
        ],
        "tools": [
            {"name": t.name, "queue": t.queue, "requires_approval": t.requires_approval}
            for t in sorted(registry.tools.values(), key=lambda x: x.name)
        ],
    }
```

---

## 6. Deployment & Verification Commands

### 1. Launch the Full Distributed Stack
```bash
export OPENROUTER_API_KEY="sk-or-v1-your-key"
docker compose up --build -d
```

### 2. Inspect Celery Workers & Canvas Topology in Flower
Open `http://localhost:5555` to view `orchestrator@...` (`q_orchestrator`) and `tools@...` (`q_tools_fast`, `q_tools_sandbox`) worker pools in real time.

### 3. Submit a Multi-Tool Task & Stream Events Over Redis Streams SSE
```bash
# Submit task
RUN_ID=$(curl -s -X POST http://localhost:8000/v1/runs \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: fin-ops" \
  -d '{
    "prompt": "Investigate Q3 revenue anomalies and prepare a refund batch if needed.",
    "secrets": {"ANALYTICS_DB_PATH": "/tmp/analytics.db", "BILLING_WEBHOOK_SECRET": "whsec_live_123"}
  }' | jq -r .run_id)

# Stream real-time events via Redis Streams XREAD (supports Last-Event-ID reconnection)
curl -N -H "Last-Event-ID: 0-0" "http://localhost:8000/v1/runs/${RUN_ID}/stream"
```