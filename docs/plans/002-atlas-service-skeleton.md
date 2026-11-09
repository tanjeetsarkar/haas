# 002 — Standalone Atlas service skeleton

- Status: done
- Type: slice
- Owner agent: plugin-engineer
- Epic: [001](001-atlas-extraction-sdk-epic.md)
- ADR: none — no core/client change

## Goal
Create an independently runnable, health-only service under
`services/atlas-explorer/`. This establishes deployment and test ownership without
changing tool behavior, SDK contracts or data. It is the smallest valuable slice:
operators can build and probe the future service while core remains unchanged.

## Verification and review evidence

- Verifier verdict: **GREEN**. Service tests: 3 passed; Atlas plugin tests:
  18 passed; ruff passed; pyright reported zero diagnostics.
- Compose smoke: exact `GET /healthz` response was HTTP 200 with
  `{"status":"ok"}`; container became healthy and ran as non-root.
- Boundary baseline unchanged: `atlas` 0 errors/0 warnings;
  `atlas-explorer` 134 errors/1 warning; overall 155 errors/9 warnings.
- An incidental full `pytest` run reported 492 passed, 16 failures, and 41
  skipped. The 16 failures were identified as pre-existing by diff-name
  comparison; they are not fixed or attributed to this slice.
- Reviewer verdict: **APPROVE**.

## Boundary classification

| Change | Core / Plugin / Client | Why (boundary test) |
|---|---|---|
| Service manifest, image, health endpoint, compose and tests | Plugin service | Would not exist if Atlas were deleted |
| README contract/deployment/data constraints | Plugin service | Describes only this service |
| Progress update to this plan | Plan | Records evidence; no runtime effect |

## Scope

**Allowed paths:** `services/atlas-explorer/**`, this plan's status/log.
**Out of scope:** `app/**`, core compose/deploy, `migrations/**`, all SDKs, `web/**`,
`plugins/**`, root dependency manifests/lockfiles, governance and other tests.
No tool switched, domain endpoint added, dependency added to core, credentials
provisioned, database connected, data copied, migration created or table dropped.

**Planned files (≤400 non-generated changed lines total; 0 migrations):**

- `services/atlas-explorer/README.md` — purpose, commands, configuration, target
  boundaries, health semantics and explicit behavior/data non-goals.
- `services/atlas-explorer/pyproject.toml` — Python ≥3.11 service-local package;
  FastAPI/Uvicorn runtime and pytest/httpx test dependencies locally declared.
  Service-local lockfile may be generated, never modify the root lockfile.
- `services/atlas-explorer/app/__init__.py`, `app/main.py` — service app factory
  or application, one `GET /healthz` route only (framework docs routes disabled).
- `services/atlas-explorer/Dockerfile` — install only service dependencies/code;
  unprivileged runtime, bind to container port 8080; no repo-root imports.
- `services/atlas-explorer/docker-compose.yml` — one distinctly named service,
  build context this directory, configurable host port (default 8081), HTTP health
  check and configurable external core network (`HAAS_CORE_NETWORK` required
  when joining core). No core services, DB credentials or secret defaults.
- `services/atlas-explorer/tests/test_health.py` — deterministic health contract
  and no-business-route tests, importable entirely from the service directory.

Use a unique installed Python package name in metadata; `app` is service-local
only. Root `app` shadowing must not make tests pass accidentally. Test from the
service working directory, not by inserting repo-root paths.

## Implementation sequence

1. Record baseline and protected working-tree paths before edits. If
   `services/atlas-explorer/` unexpectedly already exists, stop and replan.
2. Add the local package and health-only app. `GET /healthz` returns HTTP 200
   and exactly `{"status":"ok"}` without checking core, DB, secrets or network.
   It is process liveness, **not** domain readiness.
3. Add image and standalone compose. Network name is deployment configuration,
   not hardcoded into core. To boot without core, create a disposable external
   network and pass its name; never start core implicitly.
4. Add health/route tests and README run instructions. README sketches future
   service-owned DB/migrations and authenticated calls but implements none.
5. Run verification, record evidence, hand back for independent review.

## Acceptance criteria

- [x] Diff contains only allowed service files and this plan's progress update;
  ≤400 changed non-generated lines, 0 migrations. No existing file removed.
- [x] Service tests: health exact status/body, repeated probes and probe with
  no core/DB environment all pass; a representative business route is 404;
  OpenAPI/docs routes are disabled. No outbound request in the health handler.
- [x] Build/test from service directory succeeds without importing root `app`,
  `haas_sdk` internals or `haas_plugin_sdk` internals.
- [x] Compose config validates with an explicit external-network name; image
  boots on an empty disposable network and health becomes healthy within 60s.
  Curl receives HTTP 200 and the exact JSON above.
- [x] Container runs as non-root; image build context contains only service
  files; no core DSN, API key, token or Atlas business data in config/image.
- [x] README includes standalone build/test/curl commands, network configuration,
  joining core example, no-core-required health semantics, and “no behavior or
  data migrated; old tables retained; drops require explicit approval”.
- [x] Pre-implementation baseline: `--plugin atlas` 0 errors/0 warnings;
  `--plugin atlas-explorer` 134 errors/1 warning; overall 155 errors/9 warnings.
  These are baseline counts, not expected decreases: a health-only skeleton
  removes no coupling. Compare post-change counts for increases only.
- [x] Existing `.venv/bin/pytest tests/plugins/test_atlas_explorer.py -q` 
  introduces no new failures; all new service tests pass; protected TS file
  diff remains byte-for-byte unchanged.

## Verification rungs

Baseline commands run before implementation; repeat relevant commands afterward.
Use a service-local environment so root lockfiles and dependencies remain untouched.

1. **Rung 1:** `.venv/bin/ruff check services/atlas-explorer` from root.
2. **Rung 2 (extra service gate):** `.venv/bin/pyright services/atlas-explorer/app`
   using the service dependency environment; document environment invocation.
3. **Rung 3:** from `services/atlas-explorer`,
   `.venv/bin/python -m pytest tests/test_health.py -q` after installing service
   test dependencies into that local environment; from root,
   `.venv/bin/pytest tests/plugins/test_atlas_explorer.py -q`.
4. **Rung 5:** run checker with `--plugin atlas`, `--plugin atlas-explorer`,
   and without filter; retain counts and findings, not just exit status.
5. **Rung 11, bounded compose smoke:** create a uniquely named test network;
   `HAAS_CORE_NETWORK=<network> docker compose -f services/atlas-explorer/docker-compose.yml config`;
   same environment and file with `up --build -d --wait --wait-timeout 60`;
   `curl --fail http://localhost:8081/healthz`; inspect container user/health.
   Tear down only this compose project without `-v` and remove only the
   disposable network after confirming no unrelated containers use it.
6. **Diff gate:** `git diff --stat`, `git status --short`, inspect new untracked
   service files too; compare protected-file diff to pre-edit capture.

API snapshot/core full-suite/migration rungs are not applicable: no core API
changes, migrations or runtime coupling. If tooling/infrastructure is unavailable,
report not run and hand off incomplete verification; do not claim a pass.

## Baseline

```text
Fresh pre-implementation checks (2026-11-08):
`--plugin atlas` 0 errors/0 warnings; `--plugin atlas-explorer` 134 errors/1 warning;
overall 155 errors/9 warnings. These are baseline counts; no decreases are
expected from a health-only skeleton.
Ruff/pyright/service tests/Atlas tests/compose: not run.
`haas_sdk_ts/src/lib/atlasClient.ts` has a dirty user-owned limits change and
remains untouched and out of scope.
```

## Risks and rollback

- Empty skeleton mistaken for ready application → liveness-only endpoint and
  explicit README non-goals; no domain or “ready” claim.
- Docker network name/host-port collisions → configurable network/port and unique
  smoke-test project; no edits to core compose to work around a collision.
- Scope grows into DB/auth → stop and return to epic; persistence and service
  trust require their own plans/decisions.
- Rollback: remove/revert only newly added service files; stop only this service
  project without volume deletion. No core state changed; never reset user work.

## Decisions needed

1. `services/atlas-explorer`, health-only Python/FastAPI skeleton and independent
   compose: **approved by the user** with the recommended defaults.
2. Epic migration, compatibility and auth decisions remain open; this slice
   does not require their resolution and does not imply their approval.

## Log
- 2026-11-08 user: explicitly approved slice 002 with recommended defaults.
- 2026-11-08 architect: marked in progress and recorded fresh pre-implementation
  boundary baseline; no implementation or tests run.
- 2026-11-08 plugin-engineer: added the standalone health-only service, its
  service-local dependency/test configuration, Docker build-context exclusions,
  non-root image, external-network compose deployment, and deployment/non-goal
  documentation. No migrations.
  Evidence: `.venv/bin/ruff check services/atlas-explorer` passed;
  `.venv/bin/pyright services/atlas-explorer/app` reported 0 errors, warnings,
  or informations; from the service directory, `.venv/bin/python -m pytest -q`
  passed (3); `.venv/bin/pytest tests/plugins/test_atlas_explorer.py -q` passed
  (18). `HAAS_CORE_NETWORK=atlas-explorer-slice002-config docker compose -f
  services/atlas-explorer/docker-compose.yml config` passed. Docker
  `up --build -d --wait --wait-timeout 60` passed with network
  `atlas-explorer-002-smoke-20261108`; live probe returned HTTP 200 and exact
  `{"status":"ok"}`, and inspection reported `user=10001:10001
  health=healthy`. Rebuilt and repeated this smoke after adding `.dockerignore`
  on `atlas-explorer-002-final-smoke-20261108`, with the same result. Both
  projects were stopped without `-v`; after verifying no attached containers,
  both disposable networks were removed. Boundary checks remain `atlas` 0/0,
  `atlas-explorer` 134/1, overall 155/9 (errors/warnings), unchanged from
  baseline. The temporary service venv and generated test/package artifacts were
  removed. Status remains in-progress pending independent verification/review.
