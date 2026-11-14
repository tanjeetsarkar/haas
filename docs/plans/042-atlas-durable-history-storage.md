# 042 — Atlas-owned durable history, authentication and local migration

- Status: in-progress — implementation APPROVED, local migration/storage and draft recovery verified; whole-epic live research acceptance pending
- Type: epic — independently reviewed slices, each ≤400 changed lines and ≤1 migration
- Owner agent: plugin-engineer
- Epic: [038](038-atlas-guided-research-epic.md)
- ADR: none — extension-owned database and public identity consumption

## Binding implementation contracts

Atlas owns its PostgreSQL database, migrations, workspace catalog and mutation
journal. No core tables, migrations, internal imports or direct identity-table
reads. Prefer existing PostgreSQL/SQLAlchemy drivers; do not introduce a storage
framework or a new core dependency.

Every application row carries canonical verified `tenant_id`; enable AND FORCE
RLS. Runtime credentials must not be superuser, owner-bypass or BYPASSRLS.
Transaction-local tenant context comes only from public HaaS authentication,
never a browser tenant header. Fail closed if canonical tenant/principal cannot
be established. The local fixed-identity proxy is not a production auth boundary.

Persist pre-run drafts without a run ID. Mutations contain `mutation_id`,
`expected_revision`, `kind` and bounded `payload`; successful receipts contain
`workspace_id`, `mutation_id`, `revision` and server timestamp. Under a workspace
lock, same ID/same canonical body returns its original receipt; same ID/different
body conflicts. New stale revisions return 409 without applying any journal or
snapshot change. Commit the journal, materialized state and receipt atomically.

Persist operation intent before any run/action dispatch. An ambiguous dispatch
becomes `unknown`, never an automatic paid retry. Reload attaches only to a
recorded run ID; missing authoritative correlation requires explicit recovery.
Storage success never claims execution success.

## Authorization supersession

The user authorizes reviewed migration application and necessary feature
enablement only against the **existing local development stack**. This does not
authorize production/other shared databases, data deletion, safety bypass,
commits or pushes. The architect changes documentation only. Runtime owners
must identify the local target without printing credentials and capture evidence
before applying anything. No runtime application has occurred in this pass.

## Preflight

Inspect service dependencies/compose and public `/v1/me` response before coding.
If public authentication does not yield canonical tenant and principal, stop
for a generic identity contract; do not invent tenant mappings or use internals.
Public `/v1/me` exists in `app/main.py` and returns `tenant_id`, `principal_id`,
roles, credential type, environment and capabilities. It rejects unauthenticated
development-header-only requests. The public tenant identifier is a verified
string, not a documented canonical UUID: store its exact value as TEXT; do not
join to core UUID tables or invent a hash/UUID mapping.

## Evidence and dependency choice

The service currently has only FastAPI/uvicorn runtime dependencies, health-only
API and its own compose on an external network. Root dependencies already include
asyncpg and httpx, but those are **not installed in the service image by that
fact**. Add service-local `asyncpg>=0.30,<1` and `httpx>=0.28,<1` only: PostgreSQL
driver and authenticated public API verification require them. No ORM, Alembic,
Redis, new core dependency or core image reuse. Use a small service-local SQL
migration runner with ordered up/down SQL files and a fixed migration version.

Recommended simplest local topology: dedicated `atlas_explorer` database on
the existing local PostgreSQL instance, dedicated migration owner and runtime
role, no access to the core database. Use the existing service compose and
confirmed local network; core compose stays unchanged. Database/role provisioning
is a separate reviewed bootstrap, not a migration in core or permission to
reuse its superuser credential in Atlas. No default credentials in source.
If reachability/least-privilege provisioning cannot be established, stop and
present a dedicated Atlas PostgreSQL container alternative; do not silently
alter core networking or connect to a different database.

Runtime inspection was not possible with the available read/edit tools. No
running containers, active network, DB revision or role flags have been verified.
Do not treat the compose default network name as the running network.

## Production-safe authentication and UI proxy protocol

1. Backend accepts an end-user Bearer credential and optional tenant **selector**
   forwarded to a fixed trusted HaaS origin's `GET /v1/me`. HTTPS certificate
   verification, no redirects, no environment proxy inheritance, 2s timeout,
   64KiB response cap; explicitly configured loopback/private local-development
   HTTP is the only local exception. Never accept arbitrary verification URLs.
2. Verify on every request initially (no stale positive cache). 401 upstream →
   401; forbidden → 403; malformed identity, timeout or dependency failure →
   503 and zero DB work. Require nonempty verified IDs and allowed Atlas access
   capability policy; never treat caller-supplied roles or headers as identity.
   Record only verified IDs, status and request correlation, no credentials/body.
3. Use verified `tenant_id` + `principal_id` as DB context. Production direct
   Bearer clients are supported without a cookie/session dependency. A future
   production UI gateway must forward each caller's credential or authenticated
   session; the local configured submitter must never be its production identity.
4. Browser prefix `/__atlas/workspaces` maps only to fixed Atlas backend
   `/api/v1/workspaces` routes; preserve method/path/query with strict allowlist,
   request caps and JSON response. No arbitrary URL proxy, browser authorization
   override, tenant override, forwarded identity, Set-Cookie or credential echo.
   Existing core `/v1/*` and isolated approver routes stay separate.
5. Local loopback proxy may inject its existing configured submitter credential
   server-side. Backend still calls real `/v1/me`. Restrict loopback peer,
   Host/Origin and JSON mutation requests; reject cross-origin/null origins and
   arbitrary browser identity headers. This proves only local configured-user
   operation. Do not copy submitter credentials into approver requests.
6. Backend has no permissive CORS; same-origin proxy is the browser path.
   Production cookie authentication, if later added, requires explicit CSRF and
   session lifecycle design; do not silently add it here.

## API and storage protocol

All payloads strict/versioned, no tenant/owner writable fields. Unknown workspace
or another owner's workspace returns 404. V1 workspaces are private to the
verified creator within the verified tenant; no implicit tenant-wide sharing.

| Route | Request / response |
|---|---|
| POST `/api/v1/workspaces` | `{schema_version:1,mutation_id,title,draft}` → 201 `{workspace_id,revision:1,mutation_id,saved_at}`; idempotent create scoped to tenant+creator+mutation |
| GET `/api/v1/workspaces?cursor=&limit=` | creator-private catalog, stable updated_at/id descending cursor; limit default 20/max 100; no draft/tool bodies |
| GET `/api/v1/workspaces/{id}` | bounded materialized state + revision, no side effects |
| POST `/api/v1/workspaces/{id}/mutations` | `{schema_version:1,mutation_id,expected_revision,kind,payload}` → receipt; 409 `{code:"revision_conflict",current_revision}` or `mutation_id_reused`; do not echo private state on conflict |
| GET `/api/v1/workspaces/{id}/journal?after_revision=&limit=` | ordered committed meaningful changes + cursor, max 100; authorized owner only |

No delete route in this delivery: Start afresh creates a different workspace,
never destroys the previous one. Retain until user explicitly authorizes a
retention/deletion policy; enforce quotas instead of silently evicting data.
Provisional conservative limits: title 200 chars, draft 32k chars, mutation body
256KiB, materialized workspace 2MiB, 100 workspaces/creator, 10k journal entries
per workspace, 100MiB persisted JSON per tenant including receipts/journal.
Return 413 for body/state size and 429 for count/storage quota; remain dirty.
Enforce quota concurrency under a tenant-scoped transaction/advisory lock, not
race-prone read-then-insert checks. Metadata/bounded reference storage only;
never copy full provider output, credentials or sensitive patient data.

Application tables (all tenant_id TEXT NOT NULL, owner_principal_id TEXT NOT NULL):
- `workspaces`: composite tenant/workspace key, owner, title, schema version,
  materialized state JSONB, revision bigint, timestamps, create mutation digest.
- `workspace_mutations`: tenant/workspace/mutation unique key, owner, request
  digest, kind, bounded payload, assigned revision, original receipt/timestamp.
  Unique tenant/workspace/revision; same-tenant composite foreign key.
- No shared global graph/history table or foreign keys to core.

RLS policies use transaction-local settings for tenant AND principal, default
deny when absent; ENABLE and FORCE on both tables, including WITH CHECK.
Explicit WHERE tenant/owner remains required. Runtime role has only needed
SELECT/INSERT/UPDATE grants (journal immutable); migration role is separate.
Pool checkout/rollback tests prove no context leaks. Migration bookkeeping is
service schema metadata, not tenant application data; runtime cannot write it.

Mutation kinds: `draft_checkpoint`, `query_submitted`, `selection_changed`,
`branch_committed`, `comparison_changed`, `action_draft_changed`,
`operation_prepared`, `operation_bound`, `operation_outcome`.
Use an explicit per-kind strict schema, not arbitrary JSON replacement patches.
Draft checkpoint is debounced meaningful state (e.g. blur/idle), not keystrokes.
Graph references carry branch/card revision and provenance; preserve siblings.
Server validates branch ancestry and operation transitions; client cannot mark a
run/effect succeeded solely by arbitrary journal input.

Execution records contain operation ID, intent digest, exact application prompt,
selected public context, thread/run IDs when known, and states
`prepared/submitting/bound/unknown/terminal`. Strip credentials/private system
prompts before storage. Prepare commit precedes dispatch. Bind the run ID after
the public API response. Persist an idempotency key before createRun and reuse it
only where the public contract's documented scope/lifetime guarantees replay.
If process loss leaves no authoritative run correlation, mark unknown and block
resubmit. Workspace restore, journal replay and draft hydration dispatch nothing.
This plan does not invent exactly-once remote creation from a local transaction.

## Ordered bounded slices

Each row is a separately baselined, tested and reviewed slice ≤400 changed lines.
Do not combine all rows into one backend rewrite. Allowed paths for all rows:
`services/atlas-explorer/` only, with the narrower files below.

| Slice | Files / deliverable | Depends on |
|---|---|---|
| 042a | `app/identity.py`, strict API/storage models, tests, service pyproject: public verifier and owner/tenant context, no persistence activation | none |
| 042b | `migrations/001_workspace.{up,down}.sql`, `app/migrate.py`, `app/storage.py`, DB tests: one reversible migration, forced RLS and transactional idempotent mutation primitive | 042a |
| 042c | `app/workspaces.py`, `app/main.py`, API tests: catalog/create/get/mutations/journal, limits and truthful receipts | 042b |
| 042d | `client-ts/src/workspaceStorage.ts`, exports/tests and `web/dev/apiProxy.ts`/tests: typed protocol and fixed local proxy, no UI autosave | 042c |
| 042e | service `docker-compose.yml`, README and runtime-owner evidence: least-privilege local bootstrap/apply/readiness and restart/reload persistence proof | 042a–d review |

If models/tests alone exceed a row budget, stop and split that row before code.
No Atlas history activation until authenticated storage and local verification
pass. Existing artifact import/export remains intact throughout.

## Acceptance / verification

- [ ] Public real `/v1/me` proves authenticated IDs; header-only, forged tenant,
  wrong owner and upstream failure cause no unauthorized data access.
- [ ] Actual PG runtime-role tests: tenant A/B and principal A/B denial,
  missing context, forged owner insert/update, pool reuse, RLS FORCE flags and
  non-superuser/non-BYPASSRLS role. Mock SQL alone is insufficient.
- [ ] Duplicate create/mutation delivery returns same receipt; different payload
  same ID conflicts; concurrent revisions yield one winner; rollback writes
  neither journal nor snapshot; restart preserves pre-run drafts and receipts.
- [ ] Tenant quota is race-safe; unknown operations do not resend; cross-origin
  proxy requests fail; error/log capture contains no token or private payload.
- [ ] One migration upgrade→downgrade→upgrade on disposable DB, including refusal
  to downgrade with workspace data unless explicit export/deletion approval.
- [ ] Independent APPROVE for each row and no new boundary/lint/type/test failures.

Rungs 3/5 plus isolated DB integration. Engineer creates the named tests when
their row lands; nonexistent tests are not baseline successes:
```sh
.venv/bin/ruff check services/atlas-explorer/app services/atlas-explorer/tests
.venv/bin/pyright services/atlas-explorer/app
.venv/bin/pytest -q services/atlas-explorer/tests
# New targeted selections: test_identity.py, test_workspace_storage.py,
# test_workspace_api.py; DB suite uses a disposable dedicated Atlas test DB.
cd services/atlas-explorer/client-ts && npm test && npm run typecheck && npm run build
# From services/atlas-explorer/web:
npm test && npm run typecheck && npm run build
# From repository root:
python3 .github/skills/core-boundary-check/scripts/check_boundaries.py
git diff --check
```

Baseline: commands not run by architect; capture service-local tool/dependency
availability and actual failures before edits. Historical boundary count 175/9.

## Local migration/apply gate and rollback

Runtime owner records only nonsecret compose project/service IDs, network name,
database name, migration version, role safety booleans and local provenance.
Never print env files, DSNs, docker inspect environment, full compose config,
JWTs, keys or SQL credential statements. Confirm no production/shared target.
Back up existing Atlas data if any; review SQL and isolated round-trip first.
Use migration role for this database only, apply once, then runtime-role RLS
smoke tests through authenticated API. Restart only necessary local services.
Browser hard reload must restore draft, selected branch and recorded run ID
without submitting. Preserve core service/volumes and public artifact compatibility.

Rollback disables new UI storage activation and stops Atlas writes; retain DB
and data. Downgrade only reviewed migration on empty/disposable data; a populated
local database is not permission for destructive rollback. No volume deletion.

## Decisions and blockers

No outstanding product choices from approved 038 are reopened. Engineering
blockers: live local DB/network identity, role provisioning, service dependency
installation, public verifier connectivity, and documented run idempotency
recovery are unverified. If local least-privilege reuse is impossible:
1. Reuse existing local PG with isolated database/roles, or add a dedicated
   Atlas-owned local PG container? Recommend reuse if safe/reachable; ask user
   before changing topology rather than silently using core credentials.
2. Automatic expiry/deletion or retain-with-quotas? Recommend retain-with-quotas;
   obtain explicit retention/destruction approval before any purge.

## Log

- Final 2026-11-10 parent sync: independent service **56 passed / 1 DB test
  skipped**, disposable DB **2 passed previously**; dedicated local database
  remains migrated. Real default-browser draft server revision **3** verified
  by GET, then restored on hard reload. Start afresh/Continue clear/restore
  without run submission. These extend storage/draft recovery evidence, not
  full live research acceptance. Touched Python ruff clean; broad core lint
  F821 `Any` at `app/core/context.py:38` is unchanged pre-existing debt.
  Architect did not rerun checks or change runtime.

- 2026-11-10 latest parent integration report (supersedes earlier local-apply
  pending status): migration **001_workspace APPLIED** to dedicated
  `atlas_explorer` database on existing local harness PostgreSQL. Forced RLS
  and least-privilege runtime role verified; health/readiness **200**, public
  authenticated `/v1/me` **200**, proxy CRUD **201/200/200/200**. Core containers
  unchanged. Runtime environment file reported ignored by git and mode **0600**;
  no credential values or file contents are recorded here. Real browser draft
  persisted at server revision **3**. Architect did not rerun these checks or
  perform runtime changes. Remaining browser/drawer/history integration is
  tracked under 043; no full live research acceptance or paid runs claimed.

- 2026-11-10 integration update: parent reports final reviewer 59f32 APPROVE;
  service tests **56 passed / 1 skipped**, actual disposable DB **2 passed**,
  independent verifier 6e4a green, and final size-fix DB selection **2 passed**.
  Implementation is approved and DB-verified; the epic is not done because
  **existing-local-stack application has NOT occurred**. Local preflight,
  migration/apply and integration acceptance remain pending. Architect did not
  rerun checks or apply migrations; no activation is recorded.

- 2026-11-10 architect: contracts written; no DB, runtime or credential access.
