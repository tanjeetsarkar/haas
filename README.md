# HaaS Distributed — FastAPI + Celery Canvas + Redis + PostgreSQL

Implementation of [docs/dockerized_celery_haas_spec.md](docs/dockerized_celery_haas_spec.md) (v3.0).

## Repository layout

* [app/](app/): FastAPI API, shared core modules, and Celery workers.
* [plugins/](plugins/): sample skills, native tools, and external endpoint manifests.
* [tests/](tests/): unit tests and Docker-backed end-to-end checks.
* [docs/](docs/): architecture specification and original design proposals.

## Run

```bash
cp .env.example .env            # set OPENROUTER_API_KEY
docker compose up --build -d    # API :8000, Flower :5555 (localhost only)

RUN_ID=$(curl -s -X POST localhost:8000/v1/runs -H 'Content-Type: application/json' -H 'X-Tenant-ID: fin-ops' -d '{
  "prompt": "Investigate Q3 revenue anomalies and prepare a refund batch if needed.",
  "secrets": {"ANALYTICS_DB_PATH": "/tmp/analytics.db", "BILLING_WEBHOOK_SECRET": "whsec_live_123"}
}' | jq -r .run_id)

curl -N -H 'X-Tenant-ID: fin-ops' "localhost:8000/v1/runs/$RUN_ID/stream"   # SSE (Last-Event-ID supported)
curl -X POST -H 'X-Tenant-ID: fin-ops' -H 'Content-Type: application/json' \
     -d '{"approved": true}' "localhost:8000/v1/runs/$RUN_ID/resume"         # HITL gate
```

All endpoints take `X-Tenant-ID` (default `default-tenant`). Tenant identity is **trusted from the header**;
run the API behind an authenticating gateway.

Set `FLOWER_BASIC_AUTH` in `.env` to protect the Flower UI. The sample billing endpoints
are placeholders; configure `BILLING_MCP_URL` and `BILLING_WEBHOOK_URL` for your services,
or use the mock stack below.

## Tests

Local development uses Python 3.12, matching the Docker image.

```bash
uv venv && uv pip install -e ".[dev]" && .venv/bin/pytest          # unit tests (no services needed)

# End-to-end against a deterministic mock OpenRouter + billing MCP/webhook server:
docker compose -f docker-compose.yml -f tests/e2e/docker-compose.e2e.yml up --build -d
.venv/bin/python tests/e2e/run_e2e.py

# Stop the mock stack (keeps data volumes):
docker compose -f docker-compose.yml -f tests/e2e/docker-compose.e2e.yml down
```

Local environment files, editor settings, generated packages, caches, and runtime databases
are ignored by Git. Keep real API keys and per-run secrets out of committed files.

The E2E run covers parallel chord fan-out across both tool queues, two approval gates, concurrent
double-approval, SSE resume, rejection, tool-error → planner routing, circuit breaker, step/USD budgets,
webhook delivery, tenant isolation, MCP `2026-07-28` wire format, HMAC signing and worker privilege separation.

## Spec validation: what changed and why

Documentation checked: Agent Skills spec (agentskills.io), OpenRouter usage accounting/models API,
MCP `2026-07-28` Streamable HTTP, Celery canvas, PyPI versions (all dependencies resolve on current releases).

| Area | Spec said | Implemented |
|---|---|---|
| Literals | Masked DSN/auth examples and a Markdown link in `openrouter_base_url` | Valid DSN/URL and bearer authentication headers |
| Packaging | `pip install .` on flat layout | `[build-system]` + `packages.find include=["app*"]` (setuptools otherwise rejects multiple top-level dirs) |
| SKILL.md | Top-level `version`, `required_tools`, `allowed_secrets` | Agent Skills only allows `name`, `description`, `license`, `compatibility`, `metadata` (string→string), `allowed-tools`. Custom keys live under `metadata` (space-separated lists); name must match the directory. Top-level keys still accepted as a fallback |
| OpenRouter | `usage: {include: true}` | Deprecated/no effect; `usage.cost` is always returned |
| MCP | Minimal headers | Adds required `Accept: application/json, text/event-stream`, `_meta` protocol version/client info/capabilities, handles JSON **or** SSE responses, surfaces JSON-RPC and `isError` results |
| Tool execution | Sync handler called inline (blocked the loop, timeout never fired) | Run via `asyncio.to_thread` under `wait_for` |
| Tool unlocking | Any registered tool callable | Only core tools + tools of active skills (enforced on the tool worker) |
| `read_skill_reference` | `startswith` path check (prefix bypass) | `is_relative_to` |
| SSRF | Literal/hostname check at registration | Also DNS-resolved check at call time, IPv4-mapped IPv6, webhook URLs validated; optional `SSRF_ALLOWED_HOSTS` for dev |
| Resume | Redis lock before authz, stale-status race | Tenant check first, atomic `UPDATE … WHERE status='input_required'` |
| Failures | LLM/aggregate exceptions left runs `running` forever | Retry transient errors, otherwise fail the run + `run_error` event + webhook; planner model rejecting tools falls back to primary |
| Schema init | DDL on every task | Only the API creates the schema, under a PG advisory lock |
| SSE | Stopped at any historic `input_required`; early exit with >50 buffered events | Stops only while still paused; drains before exit; tenant-checked |
| Compose | DB/Redis/Flower on all interfaces; tool worker not read-only | Bound to 127.0.0.1; `read_only: true` + tmpfs `/tmp`; one shared image; API health check |
| Spec naming | `aggregate_and_continue_task` (§2) vs `aggregate_step_task` (§5.8) | `aggregate_step_task` |

## Known gaps / caveats

* Ephemeral secrets are stored as plain JSON in Redis (spec text says "encrypted"); they are also passed to
  tool tasks via the broker, so Flower/Redis access should be restricted.
* "Idempotency" in the spec diagram (idempotency keys) is not implemented.
* Orphaned runs (worker crash mid-chord) are not reaped; no cancel endpoint.
* `deepseek/deepseek-r1` on step 1 depends on provider tool support; fallback to the primary model is automatic on HTTP 400/404.
* Verified against a mock OpenRouter only (no live API key was available); with the placeholder key, real OpenRouter returned 401 and the run failed cleanly.
