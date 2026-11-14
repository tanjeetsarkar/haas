# Local HaaS and Atlas Explorer runbook

This runbook describes the **local developer workflow** for the Atlas Explorer
web application, the HaaS API it calls for research, and the separate Atlas
workspace service that persists drafts and execution history. It is not a
production deployment guide. Bind every host port to loopback; do not use
production data or credentials. Start with the repository root as the current
directory unless a step says otherwise.

## 1. Components and ports

| Component | Local host endpoint | Purpose |
|---|---|---|
| HaaS API | `http://127.0.0.1:8000` | Public identity, research runs, execution events and history |
| Atlas workspace service | `http://127.0.0.1:8081` | Authenticated durable workspace API and receipts |
| Atlas submitter UI | `http://127.0.0.1:3000` | Main local development UI |
| Atlas approver UI | `http://127.0.0.1:4318` | Optional, independent approval review window |
| HaaS PostgreSQL | `127.0.0.1:5432` | Existing local HaaS database |
| HaaS Redis | `127.0.0.1:6379` | Existing local HaaS queue/cache |
| SearXNG | `127.0.0.1:8888` | Optional local web-research dependency |

The browser uses same-origin routes. The development server forwards `/v1`
requests to the fixed HaaS API on port 8000 and the limited
`/__atlas/workspaces` route to the workspace service on port 8081. The
development proxy reads credentials server-side from the repository-root
`.env.haas` file only; it does not read process/Vite environment variables or
put tokens in browser code. It is a loopback-only development convenience, not
a production authentication boundary.

Atlas workspace persistence is separate from HaaS core storage. By default it
uses a fresh Atlas-owned PostgreSQL 16 database, dedicated persistent volume
`atlas_explorer_postgres_data`, and private `atlas-explorer-db` network. Only
the Atlas API also joins the existing `harness_default` core network. The
database has no published host port. Do not point it at HaaS core PostgreSQL,
import core tables, or run core migrations for Atlas.

## 2. Prerequisites and credentials

- Docker Engine with Docker Compose v2.
- OpenSSL for generation of local role passwords.
- Node.js supported by the checked-in Vite toolchain (Node 20.19+ or 22.12+)
  and npm.
- The repository's local HaaS development environment and its approved local
  submitter credential. Live research also requires the configured HaaS model
  provider and research dependencies; a research request may incur provider
  cost.
- For the optional approver window, a separate local credential for an
  authorized approver identity in the same tenant.

Keep the repository's existing `.env` for HaaS services distinct from
`.env.haas`, which is read only by the local Atlas Vite proxy. Create/edit
`.env.haas` at the repository root using a trusted local editor or secret
manager. It must contain the local `HAAS_API_KEY` and `HAAS_TENANT` entries;
add `HAAS_APPROVER_KEY` only when using the independent approver window. Make
the file private (`chmod 600 .env.haas`). Do not print, paste, commit, or bundle
these values. The approver key must be different from the submitter key.

Keep all local services on loopback. Reserve ports 8000, 8081, 3000 and 4318
(and the ports required by the existing local HaaS stack). Atlas Compose binds
its API to `127.0.0.1:8081` by default; `ATLAS_EXPLORER_PORT` changes only the
numeric host port.

## 3. Existing HaaS stack prerequisite

The local HaaS development stack must already be running before Atlas
activation. Reuse it without restarting, recreating, or otherwise changing
core services. If it is stopped, pause Atlas activation: starting or changing
the HaaS stack is a separate operation requiring separate authorization, and
is not part of this Atlas workflow. Do not run the repository-root
`docker compose up` command as an Atlas setup step.

Check readiness and current container state without changing the stack:

```sh
curl --fail-with-body --show-error http://127.0.0.1:8000/readyz
docker compose \
  -f docker-compose.yml \
  -f docker-compose.dev.yml \
  -f plugins/web-research/docker-compose.yml \
  ps
```

Core `/readyz` covers core dependencies, not provider credentials, workers,
SearXNG search, Atlas workspace storage, or a successful research run. Never
infer provider readiness or research success from a health endpoint.

## 4. Check Atlas database state and start the workspace service

Atlas uses a fresh dedicated database, migration owner and distinct
least-privilege runtime role. The bootstrap admin initializes only a new
Atlas-owned PostgreSQL volume; `atlas_migrator` owns the Atlas schema and
executes migrations explicitly; `atlas_runtime` has only required data
permissions and read-only migration-version access. Neither service role owns
the database or `public` schema, and neither gets `CREATE` on `public`.
Workspace/mutation/usage tables retain ENABLE + FORCE RLS and default-deny
tenant/principal predicates. Do not reuse a core role or run Atlas migrations
against HaaS core PostgreSQL.

Routine API startup does not run migrations.

Only after independent review and explicit local authorization, create
ignored role passwords for a fresh local setup using the service helper. It
refuses to overwrite existing files and prints no values:

```sh
cd services/atlas-explorer
sh postgres/create-secrets.sh
```

Keep `postgres/secrets/` mode 0700. Its files are mode 0644 only so the
isolated PostgreSQL/API container UIDs can read their specifically mounted
secrets; other host users cannot traverse the private directory.

Start the new database and explicitly migrate it before starting the API:

```sh
docker compose up -d --wait atlas-postgres
docker compose --profile migrate run --rm --no-deps atlas-migrate
HAAS_CORE_NETWORK=harness_default \
ATLAS_HAAS_API_ORIGIN=http://haas-api:8000 \
ATLAS_ALLOW_LOCAL_HAAS_HTTP=true \
docker compose up --build -d --wait atlas-explorer
```

The database command initializes only the new Atlas volume; the migration job
uses the distinct `atlas_migrator` credential and applies `001_workspace`.
The runtime password is separate. Do not point either role at core PostgreSQL.
Do not copy, query, compare, migrate, or verify the previous Atlas/core
database or its records; the new workspace, mutation and usage tables start
empty. Never run migration `down`, remove a volume, or use
`docker compose down -v`.

This implementation did not inspect or edit any existing ignored local
configuration. Before any authorized local switch, locate the exact file,
confirm it is ignored and private without displaying values, and make a
mode-0600 rollback copy. Change only settings required by the reviewed Atlas
topology and preserve every unrelated key/secret. Do not copy or reuse a
previous Atlas database URL. If the file path/format or ignore status is
unclear, stop and request direction; rollback is config-only and must not
reconnect to the previous database.

For external-managed PostgreSQL, use a trusted secret manager or editor to
create ignored files `postgres/secrets/external_migration_database_url` and
`postgres/secrets/external_runtime_database_url` inside the mode-0700 secret
directory, with files mode 0644 for the container UID. They must target
`atlas_migrator` (schema owner) and `atlas_runtime` (least privilege).
Use this override only after independent review and explicit local
authorization:

```sh
docker compose \
  -f docker-compose.yml -f docker-compose.external-db.yml \
  --profile migrate run --rm --no-deps atlas-migrate
docker compose \
  -f docker-compose.yml -f docker-compose.external-db.yml \
  up --build -d --wait atlas-explorer
```

This mode gates bundled PostgreSQL behind a non-default profile and connects
the Atlas containers only to the existing external network; it neither starts
nor attaches to the private bundled database. Rendered Compose config contains
only secret-file paths, not DSN values. Apply equivalent role, ACL, RLS and
explicit-migration review to the managed database.

After starting the API, verify:

```sh
curl --fail-with-body --show-error http://127.0.0.1:8081/healthz
curl --fail-with-body --show-error http://127.0.0.1:8081/readyz
docker compose ps
```

`/healthz` is process liveness only. `/readyz` checks the configured Atlas
database, applied schema and runtime-role safety; neither endpoint proves
identity verification, provider, or research readiness. Workspace API calls
fail closed when storage or identity verification is unavailable.

## 5. Install/build the web packages when needed

Skip installation if the local dependencies are already present and correct.
Otherwise, use the checked-in package manifests and lockfiles; do not pack
temporary SDK archives, install from unreviewed sources, use offline fallbacks,
or skip the TypeScript build:

```sh
npm --prefix haas_sdk_ts ci
npm --prefix haas_sdk_ts run build
npm --prefix services/atlas-explorer/client-ts ci
npm --prefix services/atlas-explorer/client-ts run build
npm --prefix services/atlas-explorer/web ci
npm --prefix services/atlas-explorer/web run build
```

If `npm ci` cannot complete from the configured approved package source, stop
and use the repository's reviewed dependency-provisioning process. The web
build runs type checking and Vite bundling.

## 6. Run the UI and use Atlas

In a separate terminal, from the repository root:

```sh
npm --prefix services/atlas-explorer/web run dev
```

Open [http://127.0.0.1:3000](http://127.0.0.1:3000). The server binds to
loopback and uses strict port selection. Do not add a LAN-facing `--host`.
The Vite approver mode is a separate server and port; it does not change the
submitter session:

```sh
npm --prefix services/atlas-explorer/web run dev -- --mode approver
```

Open [http://127.0.0.1:4318](http://127.0.0.1:4318) only for an explicitly
authorized approver. Keep the two browser origins/windows distinct.

In the main UI:

1. Use a focused question and explicitly submit it. This starts research
   through HaaS and may incur provider cost; there is no supported free/live
   run guarantee.
   Atlas root and scoped requests ask for a maximum of 1,000 steps. This
   ceiling requires an explicitly authorized tenant policy with `max_steps=1000`
   and deployment of the updated API and worker images; older workers reject
   policy 1000. Activate the policy only after deployment. Other tenants'
   defaults remain unchanged. Root budgets remain $0.15/12 tool
   invocations and scoped budgets $0.10/8; the five-minute observation deadline
   still applies.
2. Review sources and distinguish reported facts from unavailable, estimated,
   or conflicting claims. Candidate identity facets wait for resolution.
3. Selecting a new entity, category or available facet runs one bounded
   scoped dive using the original question and selected ancestry. Sources and
   unavailable rows do not dispatch research. Returning to a cached branch
   reuses it; choose **Research again** explicitly to rerun it.
4. Open the execution drawer only when needed. It shows the submitted question,
   application prompt, selected context, public run/step/tool data and usage
   when available. Run history is loaded on demand, older history requires an
   explicit action, previews are bounded, and missing values stay unavailable
   rather than appearing as zero. The drawer does not poll or create runs.
5. Workspace drafts save automatically on meaningful edits/blur. **Saving**,
   **Saved**, failure and conflict states reflect server receipts; navigate
   only after the save completes. Hard reload/Continue restores saved workspace
   and only reattaches to an authoritative saved run binding. It must not
   silently start or duplicate research.
6. **Start afresh** starts a clean workspace view; it does not delete the
   durable workspace/history. Existing artifact import/export remains
   separate; the default durable workspace has no manual artifact snapshot
   save button.
7. Card actions inspect/export local sourced data. **Custom action** is a
   user-authored local draft saved to the workspace, not a model-generated
   action or external request.

The standalone approver page only reviews the actual server approval batch and
records an explicit approval decision. A separate approver identity and server
authorization are required. Approval does not enable workspace card actions or
external execution. Model-drafted actions are blocked by the server's
tool-free contract; external-action and phase-config holds remain disabled.
Do not remove or bypass those holds.

## 7. Persistence, uncertainty and recovery

The workspace API is owner/tenant scoped through the public HaaS identity
contract. The browser does not submit trusted tenant/owner identifiers or hold
credentials. A storage `503` is fail-closed: restore the service/database
configuration before trying again; do not fall back to browser storage or
claim that a draft was saved.

Common responses and safe handling:

| Response/code | Meaning and safe action |
|---|---|
| `404` workspace/operation not found | The resource is unknown or not visible to this identity. Confirm the selected account/workspace; do not try a different tenant or edit the database. |
| `409 revision_conflict` | The workspace changed since the edit. Use the UI's authorized reload/conflict flow; do not overwrite or merge by hand. |
| `409 mutation_id_reused` | A mutation ID was reused with a different body. Do not generate a replacement to force the write; resolve the UI's retained save item. |
| `409 operation_run_unknown` | No authoritative run binding exists. Treat execution as unresolved; do not submit again. |
| `409 operation_exists`, `operation_missing`, `operation_transition`, or another operation conflict | A recorded intent/state transition is inconsistent or unresolved. Keep the workspace, inspect its recorded status through the UI, and escalate rather than mutating storage manually. |
| `413` request/state/mutation too large | The request was rejected atomically. Reduce the edit/query through the UI and retry only after confirming the prior save state. |
| `429` workspace, journal or tenant storage quota | The service refused the write. Preserve the local session and follow the supported retention/support process; do not delete rows or recreate workspaces to evade a quota. |
| `503` storage unavailable/unsafe or identity unavailable | Fail closed. Restore the dedicated database/schema/runtime role or HaaS identity path. Do not retry paid research as a storage workaround. |

Workspace limits include 100 workspaces per owner, 10,000 mutation-journal
entries per workspace, 100 MiB persisted data per tenant, 2 MiB serialized
workspace state and 256 KiB serialized mutation payload. The workspace API
request body limit is 300 KiB. Failed constraint/quota writes roll back
atomically.

For save retries, retain and retry the exact mutation ID **and** body; an exact
replay returns its receipt. A new ID is not a safe retry for an uncertain
request. Prepared, submitting or unknown operations block new research. If
submission outcome is ambiguous, do not automatically resend a potentially
paid run; recover only through the recorded operation and authoritative public
run binding. An SSE/observation disconnect is not remote cancellation.
Explicit cancellation is available only through the UI when a known run ID is
available. Starting afresh, navigating away or hard reloading does not cancel a
remote run.

## 8. Logs, shutdown and troubleshooting

Inspect only the relevant local service logs; do not print secret files or
environment values:

```sh
# From services/atlas-explorer:
docker compose logs --tail=200 atlas-explorer

# From the repository root, for core dependencies:
docker compose \
  -f docker-compose.yml \
  -f docker-compose.dev.yml \
  -f plugins/web-research/docker-compose.yml \
  logs --tail=200 haas-api worker-orchestrator worker-tools searxng
```

Stop the UI with Ctrl+C in its terminal. Stop only the Atlas service from
`services/atlas-explorer`:

```sh
docker compose down
```

Stop the core stack separately only when you intend to stop the local HaaS
environment:

```sh
docker compose \
  -f docker-compose.yml \
  -f docker-compose.dev.yml \
  -f plugins/web-research/docker-compose.yml \
  down
```

Do not use `down -v` or other volume-removal commands. They can delete local
database/Redis data. Do not remove the shared external network.

| Symptom | Check |
|---|---|
| Core `/readyz` fails | Check the existing stack's `ps` and core PostgreSQL/Redis logs. This endpoint does not test Atlas. |
| Atlas `/healthz` works but `/readyz` fails | Check that the configured Atlas runtime secret targets the dedicated Atlas DB, `001_workspace` is applied, and runtime-role privileges/RLS are safe. Do not switch to core DB credentials. |
| Workspace API returns 503 | Verify the Atlas service is ready and HaaS `/v1/me` identity verification is reachable/configured. Keep research blocked until recovery. |
| UI cannot reach workspace API | Confirm the service is on `harness_default`, host port is loopback `8081`, and the UI is at `127.0.0.1:3000`. |
| UI authentication fails | Check only the local `.env.haas` file's presence/permissions and the matching local submitter identity/tenant. Never put a key in browser storage, the URL, or Vite environment variables. |
| Approver cannot load/decide | Confirm the approver server uses port 4318 and the separate authorized same-tenant approver credential. Do not substitute the submitter key or treat approval as action execution. |
| Save state is failed/conflict | Preserve the workspace. Restore service readiness, then use the UI's exact retry or authorized reload. Do not clear mutation IDs or edit DB rows. |
| Observation disconnected or run state unknown | Reattach only to recorded public run/thread correlation when available. Disconnect does not cancel or justify a new submission. |

## 9. Verification record and limits

Local verification recorded on 2026-11-10 (not rerun for this documentation
update) includes SDK 20, client 206, UI 229 and service 56 test passes, with one
service database-dependent test skipped; two disposable-database checks
passed. This historical record is not evidence for the new Atlas-owned
topology or the previous Atlas/core database. Plugin tests reported 24 passes
and doctor reported zero errors.
Scoped Ruff was clean; the global baseline remained 175 passes and 9 known
failures, unchanged. Browser checks covered authenticated catalog/workspace
CRUD, receipt-backed save and hard-reload restore, Start afresh/Continue with
zero research dispatches, and responsive layouts; a saved mutation reached
revision 3. This is local evidence, not a production guarantee.
Scoped Ruff was clean; the global baseline remained 175 passes and 9 known
failures, unchanged. Browser checks covered authenticated catalog/workspace
CRUD, receipt-backed save and hard-reload restore, Start afresh/Continue with
zero research dispatches, and responsive layouts; a saved mutation reached
revision 3. This is local evidence, not a production guarantee.

No paid live research journey or live SSE research run was performed as part
of that verification. Do not describe live provider execution, research
quality, approval-triggered action execution, or external actions as verified.
The run drawer and workspace UI consume public contracts; health/readiness
checks do not prove those end-to-end paths.

## 10. Related references

- [Atlas Explorer service operations](../services/atlas-explorer/README.md)
- [Atlas Explorer web UI](../services/atlas-explorer/web/README.md)
