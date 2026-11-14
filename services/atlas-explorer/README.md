# Atlas Explorer service

This independently buildable service owns Atlas workspaces and mutation
history. It verifies caller identity through the public HaaS `/v1/me` API,
stores workspaces in its separately provisioned PostgreSQL database, and does
not import or access HaaS core internals or tables. The Atlas web UI uses this
service for durable drafts and workspace history; it uses the public HaaS API
for research and run data. For the complete local workflow, see the
[local runbook](../../docs/atlas-explorer-local-runbook.md).

## Standalone backend smoke check

The normal local workflow uses this service's Compose file, the existing
`harness_default` network and the dedicated Atlas database as described in
the [local runbook](../../docs/atlas-explorer-local-runbook.md). This
standalone process command is for a developer who has already provisioned the
database, runtime role and identity configuration; a liveness-only response
does not make the workspace API ready.

From this directory:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
curl --fail http://127.0.0.1:8080/healthz
.venv/bin/python -m pytest -q
```

`/healthz` returns HTTP 200 and exactly `{"status":"ok"}`; it is process
liveness only. `/readyz` checks the configured Atlas database, applied schema,
and runtime-role safety. Workspace API requests fail closed unless the public
identity verifier is configured and confirms authenticated tenant/principal
IDs plus `run.submit`.

## Workspace API and authentication

The backend exposes `POST`/`GET /api/v1/workspaces`,
`GET /api/v1/workspaces/{workspace_id}`,
`POST /api/v1/workspaces/{workspace_id}/mutations`, and
`GET /api/v1/workspaces/{workspace_id}/journal`. Requests carry no writable
tenant or owner fields. Authentication is forwarded to the fixed trusted
`ATLAS_HAAS_API_ORIGIN` `/v1/me` endpoint on each request; verification follows
no redirects, inherits no environment proxy, has a two-second timeout and a
64 KiB response bound. Only verified IDs become tenant/owner context.

Set `ATLAS_HAAS_API_ORIGIN` to the trusted HaaS origin. HTTPS is required except
for explicitly enabled local development (`ATLAS_ALLOW_LOCAL_HAAS_HTTP=true`)
with loopback/private-network resolution. The service has no permissive CORS.
The development Vite proxy maps only `/__atlas/workspaces` routes to this API
and injects its configured local submitter credential server-side; this is not
a production authentication boundary.

## PostgreSQL topology

Default Compose starts a fresh, persistent PostgreSQL 16 database on private
`atlas-explorer-db`, without a host DB port. Atlas joins that network plus
`harness_default`; its API binds to loopback `127.0.0.1:8081`. The external DB
override omits the bundled database and private network.

Atlas activation requires the local HaaS stack to already be running. Reuse it
without restarting or changing core services. If it is stopped, pause and
obtain separate authorization to start it; core startup is not part of Atlas
activation.

The database has separate bootstrap-admin, migration-owner (`atlas_migrator`)
and runtime (`atlas_runtime`) roles. Runtime is neither owner, superuser nor
`BYPASSRLS`; its table grants are limited to workspace SELECT/INSERT/UPDATE,
mutation SELECT/INSERT, usage SELECT/INSERT/UPDATE, and migration-version
SELECT. Forced RLS and transaction-local default-deny tenant/principal
predicates remain in the migration. Role passwords are local ignored files;
Compose mounts only the secret required by each process. API startup never
runs migrations.

After review and explicit local authorization, follow the
[local runbook](../../docs/atlas-explorer-local-runbook.md) for role-secret
generation, default/external startup, explicit migration, readiness checks and
config-only rollback. Do not inspect, copy, compare, migrate, or verify the
previous Atlas/core database. Never remove the new volume or use
`docker compose down -v`.

The service remains unprivileged. `/healthz` is liveness-only; `/readyz` checks
the configured database, migration and runtime-role safety.

## Explicit non-goals

This fresh-start setup does not inspect, copy, compare, migrate, or verify
anything in the previous Atlas/core database; that database remains untouched.
Workspace storage does not dispatch research or external actions, does not
claim run success, and has no workspace delete route. Existing public artifact
import/export remains separate.
