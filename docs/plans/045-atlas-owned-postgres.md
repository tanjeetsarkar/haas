# 045 — Atlas-owned PostgreSQL topology

- Status: done — implementation and normal-Compose local activation approved and verified
- Type: slice
- Owner agent: plugin-engineer
- Epic: none
- ADR: none — Atlas service deployment/storage is extension-owned; no core change

## Goal

Make the Atlas Explorer service use a dedicated, persistent PostgreSQL instance
by default for local development, while retaining a documented external-managed
database option. This is a fresh provision: do not copy, inspect, compare, or
verify data in the existing Atlas database on core PostgreSQL. That database,
its volume, and the core stack remain untouched.

The change is limited to Atlas-owned deployment, provisioning, migration
permissions, tests, and operational documentation. Do not edit core compose,
core migrations, core runtime, or plugin code.

## Boundary classification

| Change | Core / Plugin / Client | Why (boundary test) |
|---|---|---|
| Atlas PostgreSQL, roles, private network, volume, compose configuration | Plugin service | Atlas-owned persistence and topology would disappear if Atlas were deleted. |
| Atlas role grants, bootstrap, and local migration commands | Plugin service | Applies only to the independently deployed Atlas database. |
| Atlas runbook and service README | Plugin service docs | Documents Atlas provisioning and local operations. |

## Scope

**Allowed paths:** `services/atlas-explorer/docker-compose.yml`,
`services/atlas-explorer/docker-compose.external-db.yml` (or a reviewed
equivalent override), `services/atlas-explorer/postgres/**`,
`services/atlas-explorer/migrations/001_workspace.up.sql`,
`services/atlas-explorer/tests/**`,
`services/atlas-explorer/README.md`,
`docs/atlas-explorer-local-runbook.md`, and this plan's status/log.
Do not edit other files; the architect updates `docs/plans/README.md` as plan
index maintenance.

**Out of scope:** core compose/network/volume/DB changes; core migrations;
any drop, delete, truncate, dump, restore, copy, or verification of the old
Atlas/core database or its data; any migration or rollback against that
database; production rollout; UI/API/plugin behavior; paid research; and
commits or unrelated core restarts.

**Planned files:**

- `services/atlas-explorer/docker-compose.yml` — default private database,
  persistent new named volume, role secret mounts, two-network service
  topology, loopback-only service port default `127.0.0.1:8081`.
- `services/atlas-explorer/docker-compose.external-db.yml` — explicit managed
  DB configuration that disables/skips the bundled PostgreSQL service. Prove
  the rendered external configuration does not start that service.
- `services/atlas-explorer/postgres/` — minimal first-boot role provisioning
  for bootstrap admin, migration owner, and distinct least-privilege runtime
  role. Consume local secret files; never embed passwords/DSNs or log them.
  Audit the PostgreSQL 16 initialized database's `public` schema and default
  ACLs explicitly; do not broaden them to make migrations work.
- `services/atlas-explorer/migrations/001_workspace.up.sql` — grant the
  runtime role only required schema/table permissions, including read-only
  migration-version access. Preserve forced RLS and transaction-local
  tenant/principal checks. Migration owner owns schema objects; runtime owns
  none. No migration runs on application startup.
- `services/atlas-explorer/tests/` — focused compose-topology and
  role/RLS/migration permission coverage, using only a disposable dedicated
  Atlas test DB or rendered Compose config.
- `services/atlas-explorer/README.md` and `docs/atlas-explorer-local-runbook.md`
  — exact fresh setup, external DB, explicit migration, safe config switch,
  smoke-check, and rollback instructions.
- `docs/plans/README.md` — add plan 045 row (architect only).

Keep the diff within the ~400-line slice budget, including tests/docs. If
implementation cannot meet this budget or needs core changes, stop and return
with a narrower proposal; do not widen scope.

## Required topology and safety contract

1. The default Atlas compose invocation starts Atlas-owned PostgreSQL on a
   private database network with a new persistent volume whose name cannot
   collide with core `pg_data`. The database has no published host port.
   PostgreSQL joins only that private network. Atlas service joins exactly the
   existing core external network and the private database network; use the
   documented existing network name explicitly. It must not join any other
   network. Bind Atlas HTTP only to loopback, default host port 8081.
2. The optional external-managed DB invocation is a clean, documented
   override/profile. The Atlas service connects to the configured external
   database, and the bundled PostgreSQL container is not started. Do not
   require users of this mode to start or expose the bundled DB.
3. Use three distinct identities: a bootstrap admin used only for fresh
   PostgreSQL initialization; a migration owner that owns Atlas objects and
   runs reviewed migrations explicitly; and a runtime role that is neither
   superuser, `BYPASSRLS`, nor owner. Grant the migration owner only
   CONNECT/CREATE on this dedicated database as required by the existing
   explicit runner; it owns the Atlas schema and its objects, not the database
   or the public schema. Runtime grants are limited to database CONNECT,
   Atlas-schema USAGE, workspace SELECT/INSERT/UPDATE, mutation SELECT/INSERT,
   tenant-usage SELECT/INSERT/UPDATE, and SELECT on `schema_migrations`. Keep
   `ENABLE` + `FORCE ROW LEVEL SECURITY` on all workspace data tables and
   default-deny tenant/principal predicates. Do not grant runtime DDL or
   migration writes. Verify neither PUBLIC nor either service role receives
   CREATE on `public`; do not rely on an assumed Docker-entrypoint ACL or
   blanket schema/default-privilege grants.
4. No secret, password, DSN, local token, or real credential goes in tracked
   source or command output. Use local secret files / trusted secret
   environment with restrictive permissions. `service.env` is an ignored
   local asset, not a source file: after independent review and before local
   switch, locate it without displaying values; make a mode-0600 rollback
   copy; change only the reviewed Atlas DB configuration; preserve every
   unrelated key and secret; verify the file permissions and changed key
   names without printing values. If its path/format or ignore status is
   uncertain, stop and ask rather than create, overwrite, or expose it.
5. Apply `001_workspace` only to the new empty Atlas database, using the
   migration-owner credential and a separately invoked reviewed migration
   command. Never attach the migration runner to core PostgreSQL. App startup
   remains migration-free.
6. This is **start afresh**: new workspace, mutation, and usage tables have
   zero rows before use. Do not copy/import old records, query old record
   counts, compare old/new schemas or contents, or claim old-data verification.
   Do not drop/delete/alter the old database, its Atlas schema, roles, volume,
   or data. Do not restart, recreate, or otherwise change any core service.
7. Rollback is config-only: preserve the ignored local config and its private
   backup; if the new path fails, stop and request review before restoring
   only Atlas settings. Never run migration `down`, remove volumes, or use
   `docker compose down -v`. Do not describe a rollback that reconnects to the
   old DB as harmless without explicit authorization.

## Acceptance criteria

- [x] Default `docker compose config` proves: Atlas service has only core
  external + private DB networks; PostgreSQL has only private DB network; no
  PostgreSQL host port; dedicated persistent volume; Atlas port defaults to
  loopback 8081.
- [x] External-DB compose command and rendered config are documented and
  tested; the bundled PostgreSQL service is not started in that mode.
- [x] Bootstrap/migration/runtime identities are distinct. Targeted
  disposable-DB verification proves runtime is non-owner, non-superuser,
  non-`BYPASSRLS`, has only listed grants, cannot write migration history,
  and workspace tables have forced RLS with deny-by-default unset context.
  Verify effective ACLs for database, `public`, `atlas_explorer`, and default
  privileges: migration owner has only required database CONNECT/CREATE;
  neither PUBLIC nor service roles can CREATE in `public`; runtime cannot
  create schema/tables or write migration history.
- [x] Migration remains explicit and reviewed; service startup does not
  invoke `app.migrate`. Existing migration applies only to the new empty
  database. No migration is run against the existing core DB.
- [x] Before local application, independent reviewer approves the exact diff,
  rendered local/external Compose topology, bootstrap SQL, migration grants,
  and secret handling. Local execution is limited to creating/starting the
  new Atlas DB, applying its reviewed migration, and switching/restarting
  only the Atlas service. No core stack operation or commit.
- [x] After that separately authorized local application, verify the **new**
  DB has migration version `001_workspace`, zero rows in workspaces,
  workspace mutations, and tenant usage; Atlas `/healthz` and `/readyz`
  succeed; runtime role/RLS checks pass; authenticated public-API workspace
  history is empty. Do not run a research action, paid model request, or
  external action. Do not query or verify old DB data.
- [x] Runbook preserves the old local config via private rollback copy,
  modifies only Atlas config after review, never prints secret values, and
  documents old DB non-interaction/no-drop guarantee.
- [x] `check_boundaries.py` remains at the historical 175 errors / 9 warnings
  or improves; no new Atlas-to-core dependency. No new failures versus the
  implementation-time baseline.

## Verification rungs

1. Before implementation, recapture the service test and boundary baselines;
   historical counts below are context only, not current verification.
2. Render both compose modes with `docker compose config` and assert topology
   and absence of bundled PostgreSQL in external mode.
3. Run focused Atlas service tests (existing DB tests plus new bootstrap /
   grants checks) against a disposable Atlas test database only; no test may
   use the old/core DB. Add a safe skip when disposable test DB credentials
   are absent.
4. After review and explicit local application, follow the new-database
   acceptance checks above. This operational step is not implied by test
   success and does not permit core restarts, old DB inspection, or paid runs.

## Baseline

```text
Historical user-reported baseline (2026-11-10; recapture before edits):
- repository boundary scan: 175 errors / 9 warnings
- Atlas service tests: 56 passed / 1 skipped
```

Architect did not rerun these historical checks. Implementation recorded
fresh counts before editing and introduced no new failures.

## Completion evidence

The following implementation, review and local-activation evidence was
reported on 2026-11-10:

- Independent topology review `ab1824` approved. The independent verifier
  reported 61 fresh-PostgreSQL tests passing. Follow-up reviews approved the
  migration-image packaging correction after checking both migration
  directions under `USER 10001`, and the explicit Compose API command fix.
- Final Atlas service verification: 63 passed, 1 skipped; type checks, Ruff
  and diff checks passed.
- Normal Compose activation is complete. Atlas API is a regular service
  (not a one-off container), healthy on `127.0.0.1:8081`; the private
  `atlas-postgres` service is healthy with no host port, using the persistent
  `atlas_explorer_postgres_data` volume. `001_workspace` is applied and the
  three application tables are empty. The authenticated catalog returned
  200 with an empty result; Atlas health and readiness returned 200. Core
  container IDs were unchanged and core readiness returned 200.
- Local secret handling was verified without recording values: ignored
  `postgres/secrets/` directory mode 0700, mounted secret files mode 0644,
  and a mode-0600 `service.env` backup. The previous Atlas DSN key names
  were removed from the active configuration.
- The previous Atlas/core database was never accessed, inspected, copied,
  compared, or changed. No paid research, external action, phases activation,
  core restart, or commit was part of this slice. Compose restart policy is
  the default; no automatic-restart guarantee is claimed.

## Risks and rollback

- Misresolved compose network or volume could couple Atlas to core persistence
  → assert exact rendered networks and use a unique Atlas volume; never run a
  destructive compose command or touch core `pg_data`.
- Runtime grants or RLS drift could expose tenant data or block service use
  → review exact grants and verify them on a disposable DB before local apply.
- Compose env-file edits can expose or overwrite existing credentials
  → use a private backup, change only Atlas keys, preserve unrelated values,
  and never display values. Unknown path/format is a stop condition.
- External mode could accidentally start an unused local DB
  → test the actual documented command/rendered profile before handoff.
- Rollback: stop; no volume deletion or migration downgrade. Restore only the
  Atlas config from the protected backup after reviewer/user approval. Keep
  the newly provisioned DB and volume intact pending a separate decision.

## Decisions needed

None. The user approved Atlas-owned PostgreSQL by default, external-managed
DB support, fresh empty storage, and no interaction with the existing core
PostgreSQL Atlas database. The previously unknown ignored `service.env`
configuration was located and safely switched with a private rollback backup;
no values are recorded.

## Log

- 2026-11-10 architect: approved topology recorded; documentation plan only.
  No runtime files, database, local secrets, services, or commits changed.
- 2026-11-10 architect: recorded user-reported independent approvals,
  verifier results, final service checks and authorized normal-Compose
  activation; marked implementation acceptance criteria done. No runtime
  commands or non-plan documentation files changed by this update. The
  requested runbook evidence refresh was not made because that file is
  outside the permitted edit paths.
