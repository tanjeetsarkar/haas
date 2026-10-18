# HaaS Distributed — FastAPI + Celery Canvas + Redis + PostgreSQL

Implementation of [docs/dockerized_celery_haas_spec.md](docs/dockerized_celery_haas_spec.md) (v3.0).

## System design

The [HaaS design set](docs/design/README.md) documents the verified implementation and
the proposed production multi-tenant service:

* [Software design description (SDD)](docs/design/sdd.md)
* [Architecture and deployment diagrams](docs/design/architecture.md)
* [Data-flow diagrams (DFD) and execution sequences](docs/design/data-flows.md)
* [Entity-relationship diagrams (ERD) and data lifecycle](docs/design/data-model.md)
* [Integration patterns, API contracts and usage](docs/design/integrations-usage.md)
* [Threat model and security acceptance criteria](docs/design/threat-model.md)
* [Operations, reliability and migration plan](docs/design/operations-migration.md)
* [Plugin marketplace](docs/design/marketplace.md), [multi-agent collaboration (HACP)](docs/design/multi-agent.md)
  and [developer platform](docs/design/developer-platform.md) (TARGET product extensions)
* [Terminal experience](docs/design/tui-experience.md) (TARGET phased TUI workbench,
  marketplace, multi-agent and operator journeys)

The **[production-readiness roadmap](docs/roadmap.md)** is the living, dependency-ordered checklist
that tracks progress toward these targets; decisions are indexed in [docs/adr/](docs/adr/README.md).

Current behavior and target requirements are explicitly separated. Authenticated
tenancy, RBAC and transaction-scoped PostgreSQL RLS are implemented foundations.
Durable dispatch recovery, worker-time revocation, mediated egress and per-run
sandboxing remain production requirements, not guarantees of the current Compose stack.

## Deterministic local development

The `haas dev` stack is isolated from the regular Compose project, uses a deterministic mock
model and billing service, binds published ports to loopback, and does not use real provider
credentials:

```bash
uv pip install -e ".[tui]"      # once; installs the `haas` and TUI commands
haas dev up
haas dev status
haas dev seed                  # prints one-time admin/submitter and approver keys
haas chat --url http://127.0.0.1:18000 --tenant local-dev
# In the TUI, run /login and paste the admin/submitter key.
haas dev down                  # preserves local database/Redis volumes
```

The default local ports are API `18000`, PostgreSQL `15432`, Redis `16379`, Flower `15555`,
and the mock service `19000`. Override them with `HAAS_DEV_API_PORT`,
`HAAS_DEV_POSTGRES_PORT`, `HAAS_DEV_REDIS_PORT`, `HAAS_DEV_FLOWER_PORT`, and
`HAAS_DEV_MOCK_PORT`. The seed operation is restricted to `HAAS_PROFILE=dev` and a local
PostgreSQL host. It creates an active `local-dev` tenant, a tenant-admin/submitter principal,
a separate approver principal, a mock-only policy, and two hashed, expiring test keys. Both
clear credentials are printed once and are never written to profiles. The separate approver
preserves the no-self-approval rule; use the admin/submitter key to create a run and the
approver key to inspect/approve its pending action in the TUI with `/approve <run-id>` or
through the API. The queued run ID is displayed in the chat. Use
`haas dev seed --rotate-key` to revoke and replace both keys.

## Repository layout

* [app/](app/): FastAPI API, shared core modules, and Celery workers.
* [haas_cli/](haas_cli/): `haas-chat` (Textual chat TUI) and `haas-plugin` (scaffold, list/inspect, doctor, migrate plugin packs).
* [plugins/](plugins/): plugin packs (`plugins/<id>/` bundling skills, native tools and endpoint manifests) and [authoring templates](plugins/_templates/README.md).
* [tests/](tests/): unit tests (core, CLI/TUI, plugins) and Docker-backed end-to-end checks.
* [docs/](docs/): architecture specification, [plugin architecture](docs/plugin_architecture.md), original design proposals and the [TUI usage guide](docs/tui_usage.md).

## Run

```bash
cp .env.example .env            # set OPENROUTER_API_KEY
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build -d
# API :8000, Flower :5555, Redis :6379 and PostgreSQL :5432 (localhost only)

RUN_ID=$(curl -s -X POST localhost:8000/v1/runs -H 'Content-Type: application/json' -H 'X-Tenant-ID: fin-ops' -d '{
  "prompt": "Investigate Q3 revenue anomalies and prepare a refund batch if needed.",
  "secrets": {"ANALYTICS_DB_PATH": "/tmp/analytics.db", "BILLING_WEBHOOK_SECRET": "whsec_live_123"}
}' | jq -r .run_id)

curl -N -H 'X-Tenant-ID: fin-ops' "localhost:8000/v1/runs/$RUN_ID/stream"   # SSE (Last-Event-ID supported)
curl -X POST -H 'X-Tenant-ID: fin-ops' -H 'Content-Type: application/json' \
     -d '{"approved": true}' "localhost:8000/v1/runs/$RUN_ID/resume"         # HITL gate
```

The development profile accepts `X-Tenant-ID` (default `default-tenant`) as a local
selector. It is **not authentication**. The Phase 1 identity foundation now verifies
provider-neutral OIDC JWTs or tenant-scoped API keys against active tenant memberships,
and role-based action checks protect run, plugin, tenant administration and audit routes.
Approvers are separated from submitters; tenant policy revisions, membership/key
administration and append-only audit are available through the API and `haas` clients.
Phase 1 identity/authority and Phase 2 database/revision/thread controls are qualified,
but this is not a production/GA claim: worker-time policy revocation, mediated egress and
other later-phase controls remain tracked in the roadmap. Production API startup requires complete HTTPS OIDC
configuration and the production encryption key; authenticated runs also require an active
tenant policy revision.
Production also requires separate non-owner API and execution-service PostgreSQL logins,
forced RLS and completed legacy cutover. Do not point runtime services at the migration
owner account. The [Phase 2 operator runbook](docs/design/operations-migration.md#phase-2-operator-runbook)
documents provisioning, draining, mapping, quarantine and cutover; automatic migrations
deliberately refuse active or paused legacy runs.
The base Compose file does not publish Redis/PostgreSQL ports or start Flower; the dev
overlay adds loopback-only ports and Flower. Do not combine the dev overlay with production.

## Chat TUI

```bash
uv pip install -e ".[tui]"      # once
haas-chat                       # talks to http://localhost:8000 (HAAS_URL / --url to change)
haas-chat --preload weather     # activate a skill from the first message
```

Full navigation guide (screen layout, keys, slash commands, approvals, secrets, recipes, troubleshooting):
[docs/tui_usage.md](docs/tui_usage.md).
Verified identities with tenant-admin or auditor roles also get an Admin workspace for
authorized membership, API-key, policy, scoped-plugin and audit workflows. The unified
`haas` CLI currently includes `chat`, `profile`, `admin`, and `dev` commands; other CLI
surfaces remain roadmap work.

Phase 2 workflows are keyboard-first commands in Chat:
`/agent draft` opens a structured JSON form, `/agents` browses revisions,
`/agent validate ID` then `/agent publish ID` publishes immutable configuration, and
`/agent use ID` pins subsequent runs. `/agent diff ID ID` compares configuration,
policy permissions and registry fingerprints. `/threads [search]`, `/threads next`,
`/history [offset]` and `/branch VERSION` browse or explicitly branch committed history.
See the [terminal guide](docs/tui_usage.md#agent-revisions-and-thread-history) for roles
and conflict recovery. These use scoped `/v1/agents` and `/v1/threads` APIs through the
shared Python client, not database access. `/v1/capabilities` and `/v1/me` advertise
identity-scoped actions and supported event schemas; durable replay is explicitly false.

## Plugins: skills and tools

Each capability is a **plugin pack** `plugins/<id>/` with a static `plugin.json` plus its `skills/`, `tools/` and
`endpoints/`. Packs can be allowed/denied by operators, skills can be limited per tenant or run, and a skill is only
offered when all its tools exist. Design and rationale: [docs/plugin_architecture.md](docs/plugin_architecture.md).

Sample packs (each has a `SKILL.md`, references where useful, tools and unit tests):

| Pack / skill | Tools | Needs |
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
haas-plugin new plugin my-pack --tools my_tool --secrets MY_TOKEN --category ops
haas-plugin new tool my_tool --plugin my-pack --skill my-pack [--approval] [--sandbox]
haas-plugin new endpoint my_remote_tool --plugin my-pack --protocol mcp_http --secret MY_TOKEN
haas-plugin doctor              # run before restarting (also: list, inspect <id>, migrate)
docker compose restart haas-api worker-orchestrator worker-tools
```

Authoring guide: [plugins/_templates/README.md](plugins/_templates/README.md).

Operator controls (identical for the API and every worker; see [.env.example](.env.example)):
`PLUGINS_ALLOW`, `PLUGINS_DENY`, `PLUGINS_EXTRA_DIRS`, `PLUGINS_STRICT`, `SKILLS_PROMPT_MAX_CHARS`,
`TENANT_SKILL_ALLOWLISTS` applies to development header-tenancy only. Authenticated tenants instead require an
active immutable policy revision: exact model/provider grants, plugin digest and skill grants, webhook host
grants, budget ceilings and retention class. `POST /v1/runs` accepts an optional `skills` list that can only
narrow those grants; authenticated `GET /v1/plugins` returns only the scoped catalog. Use `haas admin`
or the TUI Admin workspace to author policy revisions and administer members/API keys; authenticated
tenants without an active revision are deliberately denied run admission. Human membership grants
require a configured OIDC issuer. Upgrading from the flat
`plugins/skills|tools|manifests` layout: run `haas-plugin migrate` once.

Set `FLOWER_BASIC_AUTH` in `.env` to protect the Flower UI. The sample billing endpoints
are placeholders; configure `BILLING_MCP_URL` and `BILLING_WEBHOOK_URL` for your services,
or use the mock stack below.

## Tests

Local development uses Python 3.12, matching the Docker image.

```bash
uv venv && uv pip install -e ".[dev]" && .venv/bin/pytest          # unit tests (no services needed)

# End-to-end against deterministic mock OpenRouter + billing MCP/webhook services:
docker compose -f docker-compose.yml -f tests/e2e/docker-compose.e2e.yml up --build -d
.venv/bin/python tests/e2e/run_e2e.py

# Stop the mock stack (keeps data volumes):
docker compose -f docker-compose.yml -f tests/e2e/docker-compose.e2e.yml down
```

`pytest` also covers the CLI/TUI (`tests/cli`, Textual pilot tests against an in-memory API) and every sample
plugin (`tests/plugins`, HTTP mocked). Rebuild the images using the same Compose file list after changing `app/`;
`plugins/` is bind-mounted, so plugin changes only need a restart.

Phase 2 qualification additionally requires a **disposable PostgreSQL 16 database**.
Migrate it with the operator login, then set `HAAS_TEST_POSTGRES_DSN` to run
[the database suite](tests/test_phase2_database.py), or the full suite with coverage:

```bash
POSTGRES_DSN="$DISPOSABLE_POSTGRES_DSN" .venv/bin/alembic upgrade head
HAAS_TEST_POSTGRES_DSN="$DISPOSABLE_POSTGRES_DSN" .venv/bin/pytest \
  --cov=app --cov=haas_cli --cov-fail-under=75
```

The database tests create/drop roles and perform legacy cutover. Never use a customer
database. Without that variable they skip; CI includes them on Python 3.11/3.12 so the
75% coverage gate measures real database paths rather than excluding those paths.

`python -m scripts.update_openapi_snapshot` deliberately refreshes the checked-in
[/v1 OpenAPI snapshot](docs/openapi-v1.json). CI fails if the snapshot is stale.

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
| Plugin packaging | Flat type-based `skills/ tools/ manifests/` dirs linked by name strings | `plugins/<id>/plugin.json` packs, static manifest, policy, readiness, isolation ([details](docs/plugin_architecture.md)) |
| Plugin loading | Tool modules executed without being registered in `sys.modules` | Registered first; otherwise pydantic cannot resolve `Literal`/other typing imports under `from __future__ import annotations` |
| SSRF | Literal/hostname check at registration | Also DNS-resolved check at call time, IPv4-mapped IPv6, webhook URLs validated; optional `SSRF_ALLOWED_HOSTS` for dev |
| Resume | Redis lock before authz, stale-status race | Tenant check first, atomic `UPDATE … WHERE status='input_required'` |
| Failures | LLM/aggregate exceptions left runs `running` forever | Retry transient errors, otherwise fail the run + `run_error` event + webhook; planner model rejecting tools falls back to primary |
| Schema init | DDL on every task | Separate advisory-locked Alembic migration job; runtime API/workers never create schema |
| SSE | Stopped at any historic `input_required`; early exit with >50 buffered events | Stops only while still paused; drains before exit; tenant-checked |
| Compose | DB/Redis/Flower on all interfaces; tool worker not read-only | Bound to 127.0.0.1; `read_only: true` + tmpfs `/tmp`; one shared image; API health check |
| Spec naming | `aggregate_and_continue_task` (§2) vs `aggregate_step_task` (§5.8) | `aggregate_step_task` |

## Known gaps / caveats

* Cancellation is cooperative through the API/TUI; `Esc` only detaches. It does not
  terminate an external effect or sandbox process already in progress.
* `deepseek/deepseek-r1` as the step-1 planner may emit tool calls as plain text on some providers; override with `/planner` or `HAAS_PLANNER_MODEL`.
* `data-analyst` datasets live on the shared scratchpad volume (per tenant + thread) and are not garbage-collected.
* Ephemeral Redis secrets are AES-GCM encrypted, but scoped values are still passed to
  tool tasks via the broker. Restrict Flower/Redis access; invocation credential brokering
  remains later-phase work.
* "Idempotency" in the spec diagram (idempotency keys) is not implemented.
* A stale-run reaper releases orphaned thread claims, but durable crash recovery,
  worker lease fencing and effect reconciliation remain Phase 3 work.
* `deepseek/deepseek-r1` on step 1 depends on provider tool support; fallback to the primary model is automatic on HTTP 400/404.
* Verified against a mock OpenRouter only (no live API key was available); with the placeholder key, real OpenRouter returned 401 and the run failed cleanly.
