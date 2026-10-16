# HaaS Distributed — FastAPI + Celery Canvas + Redis + PostgreSQL

Implementation of [docs/dockerized_celery_haas_spec.md](docs/dockerized_celery_haas_spec.md) (v3.0).

## Repository layout

* [app/](app/): FastAPI API, shared core modules, and Celery workers.
* [haas_cli/](haas_cli/): `haas-chat` (Textual chat TUI) and `haas-plugin` (scaffold/validate plugins).
* [plugins/](plugins/): skills, native tools, external endpoint manifests and [authoring templates](plugins/_templates/README.md).
* [tests/](tests/): unit tests (core, CLI/TUI, plugins) and Docker-backed end-to-end checks.
* [docs/](docs/): architecture specification, original design proposals and the [TUI usage guide](docs/tui_usage.md).

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

## Chat TUI

```bash
uv pip install -e ".[tui]"      # once
haas-chat                       # talks to http://localhost:8000 (HAAS_URL / --url to change)
haas-chat --preload weather     # activate a skill from the first message
```

Full navigation guide (screen layout, keys, slash commands, approvals, secrets, recipes, troubleshooting):
[docs/tui_usage.md](docs/tui_usage.md).

## Plugins: skills and tools

Sample plugins (each has a `SKILL.md`, references where useful, tools and unit tests):

| Skill | Tools | Needs |
|---|---|---|
| `github-ops` | list/get issues, list/get PRs, CI status, **create issue\***, **add comment\*** | `GITHUB_TOKEN` |
| `web-research` | `web_search`, `web_fetch_page`, `web_extract_links` | `BRAVE_API_KEY` for search only |
| `weather` | `get_weather_forecast` (Open-Meteo) | nothing |
| `incident-triage` | `analyze_log_text`, `classify_incident_severity` | nothing |
| `data-analyst` | `dataset_load`, `dataset_list`, `dataset_profile`, `dataset_query` (read-only SQL) | nothing |
| `plugin-creator` | advisory skill that drafts new plugins | nothing |
| `revenue-ops` | original demo (analytics DB, MCP, webhook) | see above |

\* approval-gated. Create your own from the templates:

```bash
haas-plugin new skill my-skill --tools my_tool --secrets MY_TOKEN
haas-plugin new tool my_tool --skill my-skill [--approval] [--sandbox]
haas-plugin new manifest my_remote_tool --protocol mcp_http --secret MY_TOKEN
haas-plugin validate            # run before restarting
docker compose restart haas-api worker-orchestrator worker-tools
```

Authoring guide: [plugins/_templates/README.md](plugins/_templates/README.md).

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

`pytest` also covers the CLI/TUI (`tests/cli`, Textual pilot tests against an in-memory API) and every sample
plugin (`tests/plugins`, HTTP mocked). Rebuild the images (`docker compose up --build -d`) after changing `app/`;
`plugins/` is bind-mounted, so plugin changes only need a restart.

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
| Plugin loading | Tool modules executed without being registered in `sys.modules` | Registered first; otherwise pydantic cannot resolve `Literal`/other typing imports under `from __future__ import annotations` |
| SSRF | Literal/hostname check at registration | Also DNS-resolved check at call time, IPv4-mapped IPv6, webhook URLs validated; optional `SSRF_ALLOWED_HOSTS` for dev |
| Resume | Redis lock before authz, stale-status race | Tenant check first, atomic `UPDATE … WHERE status='input_required'` |
| Failures | LLM/aggregate exceptions left runs `running` forever | Retry transient errors, otherwise fail the run + `run_error` event + webhook; planner model rejecting tools falls back to primary |
| Schema init | DDL on every task | Only the API creates the schema, under a PG advisory lock |
| SSE | Stopped at any historic `input_required`; early exit with >50 buffered events | Stops only while still paused; drains before exit; tenant-checked |
| Compose | DB/Redis/Flower on all interfaces; tool worker not read-only | Bound to 127.0.0.1; `read_only: true` + tmpfs `/tmp`; one shared image; API health check |
| Spec naming | `aggregate_and_continue_task` (§2) vs `aggregate_step_task` (§5.8) | `aggregate_step_task` |

## Known gaps / caveats

* `haas-chat` cannot cancel a run (no cancel endpoint); `Esc` only detaches the UI.
* `deepseek/deepseek-r1` as the step-1 planner may emit tool calls as plain text on some providers; override with `/planner` or `HAAS_PLANNER_MODEL`.
* `data-analyst` datasets live on the shared scratchpad volume (per tenant + thread) and are not garbage-collected.
* Ephemeral secrets are stored as plain JSON in Redis (spec text says "encrypted"); they are also passed to
  tool tasks via the broker, so Flower/Redis access should be restricted.
* "Idempotency" in the spec diagram (idempotency keys) is not implemented.
* Orphaned runs (worker crash mid-chord) are not reaped; no cancel endpoint.
* `deepseek/deepseek-r1` on step 1 depends on provider tool support; fallback to the primary model is automatic on HTTP 400/404.
* Verified against a mock OpenRouter only (no live API key was available); with the placeholder key, real OpenRouter returned 401 and the run failed cleanly.
