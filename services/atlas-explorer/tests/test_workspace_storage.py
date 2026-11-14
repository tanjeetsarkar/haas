"""Forced-RLS and transactional workspace storage checks.

The database test is opt-in and accepts only explicitly named disposable
``atlas_test_*`` databases with separate migration/runtime roles.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest
from fastapi import HTTPException

from app.identity import VerifiedIdentity
from app.migrate import migrate
from app.models import MAX_MUTATION_BYTES, WorkspaceCreate, WorkspaceMutation
from app.storage import WorkspaceStore

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def test_migration_contract_keeps_forced_rls_and_refuses_populated_downgrade() -> None:
    upgrade = (MIGRATIONS / "001_workspace.up.sql").read_text()
    downgrade = (MIGRATIONS / "001_workspace.down.sql").read_text()
    assert upgrade.count("ENABLE ROW LEVEL SECURITY") == 3
    assert upgrade.count("FORCE ROW LEVEL SECURITY") == 3
    assert "current_setting('atlas.tenant_id', true)" in upgrade
    assert "current_setting('atlas.principal_id', true)" in upgrade
    assert "owner_principal_id = nullif" in upgrade
    assert "UPDATE atlas_explorer.workspace_tenant_usage" in upgrade
    assert "INSERT INTO atlas_explorer.workspace_tenant_usage" in upgrade
    assert "VALUES (tenant, '__tenant_usage__', delta)" in upgrade
    assert "IF NOT FOUND AND delta >= 0" in upgrade
    assert "CONSTRAINT workspace_state_size" in upgrade
    assert "CONSTRAINT workspace_mutation_payload_size" in upgrade
    assert "refusing to downgrade populated Atlas workspace storage" in downgrade


def _required_test_urls() -> tuple[str, str] | None:
    if os.getenv("ATLAS_RUN_DISPOSABLE_DB_TESTS") != "true":
        return None
    migration_url = os.getenv("ATLAS_TEST_MIGRATION_DATABASE_URL")
    runtime_url = os.getenv("ATLAS_TEST_RUNTIME_DATABASE_URL")
    if not migration_url or not runtime_url:
        pytest.fail("Disposable DB tests require separate migration/runtime DSNs.")
    return migration_url, runtime_url


async def _assert_runtime_role_permissions(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        grants = await conn.fetchrow(
            "SELECT current_user AS role, r.rolsuper, r.rolbypassrls, "
            "has_database_privilege(current_user, current_database(), 'CONNECT') AS db_connect, "
            "has_database_privilege(current_user, current_database(), 'CREATE') AS db_create, "
            "has_database_privilege('atlas_migrator', current_database(), 'CONNECT') AS migrator_db_connect, "
            "has_database_privilege('atlas_migrator', current_database(), 'CREATE') AS migrator_db_create, "
            "has_database_privilege('atlas_migrator', current_database(), 'TEMP') AS migrator_db_temp, "
            "NOT EXISTS (SELECT 1 FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba "
            "WHERE d.datname=current_database() AND r.rolname='atlas_migrator') "
            "AS migrator_does_not_own_database, "
            "has_schema_privilege(current_user, 'atlas_explorer', 'USAGE') AS schema_usage, "
            "has_schema_privilege(current_user, 'atlas_explorer', 'CREATE') AS schema_create, "
            "has_schema_privilege(current_user, 'public', 'CREATE') AS public_create, "
            "has_schema_privilege('atlas_migrator', 'public', 'CREATE') AS migrator_public_create, "
            "NOT EXISTS (SELECT 1 FROM pg_namespace n CROSS JOIN LATERAL "
            "aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a "
            "WHERE n.nspname='public' AND a.grantee=0 AND a.privilege_type='CREATE') "
            "AS public_public_create, "
            "(SELECT pg_get_userbyid(n.nspowner)='atlas_migrator' "
            "FROM pg_namespace n WHERE n.nspname='atlas_explorer') AS migration_owns_schema, "
            "NOT EXISTS (SELECT 1 FROM pg_default_acl d CROSS JOIN LATERAL "
            "aclexplode(d.defaclacl) a "
            "WHERE d.defaclrole=(SELECT oid FROM pg_roles WHERE rolname='atlas_migrator') "
            "AND d.defaclnamespace=(SELECT oid FROM pg_namespace WHERE nspname='atlas_explorer') "
            "AND a.grantee IN (0, (SELECT oid FROM pg_roles WHERE rolname='atlas_runtime'))) "
            "AS default_acl_is_private, "
            "has_table_privilege(current_user, 'atlas_explorer.workspaces', 'SELECT') AS workspace_select, "
            "has_table_privilege(current_user, 'atlas_explorer.workspaces', 'INSERT') AS workspace_insert, "
            "has_table_privilege(current_user, 'atlas_explorer.workspaces', 'UPDATE') AS workspace_update, "
            "has_table_privilege(current_user, 'atlas_explorer.workspaces', 'DELETE') AS workspace_delete, "
            "has_table_privilege(current_user, 'atlas_explorer.workspaces', 'TRUNCATE') AS workspace_truncate, "
            "has_table_privilege(current_user, 'atlas_explorer.workspace_mutations', 'SELECT') AS mutation_select, "
            "has_table_privilege(current_user, 'atlas_explorer.workspace_mutations', 'INSERT') AS mutation_insert, "
            "has_table_privilege(current_user, 'atlas_explorer.workspace_mutations', 'UPDATE') AS mutation_update, "
            "has_table_privilege(current_user, 'atlas_explorer.workspace_mutations', 'DELETE') AS mutation_delete, "
            "has_table_privilege(current_user, 'atlas_explorer.workspace_tenant_usage', 'SELECT') AS usage_select, "
            "has_table_privilege(current_user, 'atlas_explorer.workspace_tenant_usage', 'INSERT') AS usage_insert, "
            "has_table_privilege(current_user, 'atlas_explorer.workspace_tenant_usage', 'UPDATE') AS usage_update, "
            "has_table_privilege(current_user, 'atlas_explorer.workspace_tenant_usage', 'DELETE') AS usage_delete, "
            "has_table_privilege(current_user, 'atlas_explorer.schema_migrations', 'SELECT') AS version_select, "
            "has_table_privilege(current_user, 'atlas_explorer.schema_migrations', 'INSERT') AS version_insert, "
            "has_table_privilege(current_user, 'atlas_explorer.schema_migrations', 'UPDATE') AS version_update, "
            "has_table_privilege(current_user, 'atlas_explorer.schema_migrations', 'DELETE') AS version_delete, "
            "(SELECT count(*)=3 AND bool_and(pg_get_userbyid(c.relowner)='atlas_migrator') "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='atlas_explorer' AND c.relkind='r' "
            "AND c.relname IN ('workspaces','workspace_mutations','workspace_tenant_usage')) "
            "AS migration_owns_tables "
            "FROM pg_roles r WHERE r.rolname=current_user"
        )
        assert grants["role"] == "atlas_runtime"
        assert not grants["rolsuper"] and not grants["rolbypassrls"]
        assert grants["db_connect"] and not grants["db_create"]
        assert grants["migrator_db_connect"] and grants["migrator_db_create"]
        assert not grants["migrator_db_temp"] and grants["migrator_does_not_own_database"]
        assert grants["schema_usage"] and not grants["schema_create"]
        assert not grants["public_create"] and not grants["migrator_public_create"]
        assert grants["public_public_create"] and grants["migration_owns_schema"]
        assert grants["default_acl_is_private"]
        assert grants["workspace_select"] and grants["workspace_insert"] and grants["workspace_update"]
        assert not grants["workspace_delete"] and not grants["workspace_truncate"]
        assert grants["mutation_select"] and grants["mutation_insert"]
        assert not grants["mutation_update"] and not grants["mutation_delete"]
        assert grants["usage_select"] and grants["usage_insert"] and grants["usage_update"]
        assert not grants["usage_delete"]
        assert grants["version_select"]
        assert not grants["version_insert"] and not grants["version_update"] and not grants["version_delete"]
        assert grants["migration_owns_tables"]
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO atlas_explorer.schema_migrations(version) VALUES('runtime-write-check')"
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("CREATE TABLE atlas_explorer.runtime_ddl_check(id integer)")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_disposable_postgres_rls_transactions_and_migration_roundtrip() -> None:
    configured = _required_test_urls()
    if configured is None:
        pytest.skip("Set ATLAS_RUN_DISPOSABLE_DB_TESTS=true with disposable Atlas test roles.")
    migration_url, runtime_url = configured
    probe = await asyncpg.connect(migration_url)
    try:
        database_identity = await probe.fetchrow(
            "SELECT current_database() AS name, inet_server_addr()::text AS address, "
            "inet_server_port() AS port"
        )
        database_name = database_identity["name"]
        if not database_name.startswith("atlas_test_"):
            pytest.fail("Refusing DB integration tests outside an atlas_test_* database.")
        runtime_probe = await asyncpg.connect(runtime_url)
        try:
            runtime_identity = await runtime_probe.fetchrow(
                "SELECT current_database() AS name, inet_server_addr()::text AS address, "
                "inet_server_port() AS port"
            )
        finally:
            await runtime_probe.close()
        if (
            runtime_identity["name"] != database_name
            or runtime_identity["address"] != database_identity["address"]
            or runtime_identity["port"] != database_identity["port"]
            or database_identity["address"] is None
        ):
            pytest.fail("Migration and runtime test roles must target the same disposable database.")
        has_workspace_table = await probe.fetchval(
            "SELECT to_regclass('atlas_explorer.workspaces') IS NOT NULL"
        )
        if not has_workspace_table:
            populated = False
        else:
            async with probe.transaction():
                for table in ("workspaces", "workspace_mutations", "workspace_tenant_usage"):
                    await probe.execute(
                        f"ALTER TABLE atlas_explorer.{table} NO FORCE ROW LEVEL SECURITY"
                    )
                    await probe.execute(f"ALTER TABLE atlas_explorer.{table} DISABLE ROW LEVEL SECURITY")
                populated = await probe.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM atlas_explorer.workspaces) "
                    "OR EXISTS(SELECT 1 FROM atlas_explorer.workspace_mutations)"
                )
                for table in ("workspaces", "workspace_mutations", "workspace_tenant_usage"):
                    await probe.execute(f"ALTER TABLE atlas_explorer.{table} ENABLE ROW LEVEL SECURITY")
                    await probe.execute(f"ALTER TABLE atlas_explorer.{table} FORCE ROW LEVEL SECURITY")
    finally:
        await probe.close()
    if populated:
        pytest.fail("Disposable Atlas test database must be empty before a round trip.")

    await migrate("up", migration_url)
    await _assert_runtime_role_permissions(runtime_url)
    workspace_store = await WorkspaceStore.connect(runtime_url)
    tenant_a = VerifiedIdentity("042-test-tenant-a", "042-test-principal-a")
    tenant_b = VerifiedIdentity("042-test-tenant-b", "042-test-principal-b")
    owner_b = VerifiedIdentity("042-test-tenant-a", "042-test-principal-b")
    try:
        created = WorkspaceCreate(
            schema_version=1,
            mutation_id="create-042",
            title="Pre-run draft",
            draft="saved before run",
        )
        receipt = await workspace_store.create(tenant_a, created)
        replay = await workspace_store.create(tenant_a, created)
        assert replay == receipt
        with pytest.raises(HTTPException) as create_reuse:
            await workspace_store.create(tenant_a, created.model_copy(update={"title": "different"}))
        assert create_reuse.value.status_code == 409
        workspace_id = UUID(receipt["workspace_id"])

        with pytest.raises(HTTPException) as foreign_tenant:
            await workspace_store.get(tenant_b, workspace_id)
        with pytest.raises(HTTPException) as foreign_owner:
            await workspace_store.get(owner_b, workspace_id)
        assert foreign_tenant.value.status_code == foreign_owner.value.status_code == 404

        mutation = WorkspaceMutation(
            schema_version=1,
            mutation_id="checkpoint-042",
            expected_revision=1,
            kind="draft_checkpoint",
            payload={"draft": "edited draft"},
        )
        saved = await workspace_store.mutate(tenant_a, workspace_id, mutation)
        assert await workspace_store.mutate(tenant_a, workspace_id, mutation) == saved
        # A shrinking materialized draft must subtract bytes without tripping
        # the tenant usage table's nonnegative CHECK constraint.
        shrink = WorkspaceMutation(
            schema_version=1,
            mutation_id="shrink-042",
            expected_revision=2,
            kind="draft_checkpoint",
            payload={"draft": ""},
        )
        shrunk_receipt = await workspace_store.mutate(tenant_a, workspace_id, shrink)
        assert shrunk_receipt["revision"] == 3
        async with workspace_store.transaction(tenant_a) as conn:
            usage_after_shrink = await conn.fetchval(
                "SELECT persisted_bytes FROM atlas_explorer.workspace_tenant_usage WHERE tenant_id=$1",
                tenant_a.tenant_id,
            )
            assert usage_after_shrink > 0
        changed_body = mutation.model_copy(update={"payload": {"draft": "different"}})
        with pytest.raises(HTTPException) as mutation_reuse:
            await workspace_store.mutate(tenant_a, workspace_id, changed_body)
        assert mutation_reuse.value.status_code == 409
        before_stale = await workspace_store.journal(tenant_a, workspace_id, 0, 100)
        stale = shrink.model_copy(update={"mutation_id": "stale-042"})
        with pytest.raises(HTTPException) as conflict:
            await workspace_store.mutate(tenant_a, workspace_id, stale)
        assert conflict.value.status_code == 409
        assert (await workspace_store.journal(tenant_a, workspace_id, 0, 100)) == before_stale

        async def competing(mutation_id: str) -> object:
            candidate = WorkspaceMutation(
                schema_version=1,
                mutation_id=mutation_id,
                expected_revision=3,
                kind="draft_checkpoint",
                payload={"draft": mutation_id},
            )
            try:
                return await workspace_store.mutate(tenant_a, workspace_id, candidate)
            except HTTPException as error:
                return error

        concurrent = await asyncio.gather(competing("race-one"), competing("race-two"))
        assert sum(not isinstance(result, HTTPException) for result in concurrent) == 1
        assert sum(isinstance(result, HTTPException) and result.status_code == 409 for result in concurrent) == 1

        boundary_receipt = await workspace_store.create(
            tenant_a,
            WorkspaceCreate(
                schema_version=1,
                mutation_id="create-size-boundary-042",
                title="Size boundary",
                draft="",
            ),
        )
        boundary_id = UUID(boundary_receipt["workspace_id"])
        payload_prefix = len(json.dumps(
            {"selection": {"blob": ""}}, separators=(",", ":"), ensure_ascii=False
        ).encode())
        payload_at_limit = WorkspaceMutation(
            schema_version=1,
            mutation_id="payload-size-boundary-042",
            expected_revision=1,
            kind="selection_changed",
            payload={"selection": {"blob": "x" * (MAX_MUTATION_BYTES - payload_prefix)}},
        )
        with pytest.raises(HTTPException) as payload_too_large:
            await workspace_store.mutate(tenant_a, boundary_id, payload_at_limit)
        assert payload_too_large.value.status_code == 413
        assert payload_too_large.value.detail == {"code": "mutation_payload_too_large"}
        assert (await workspace_store.get(tenant_a, boundary_id))["revision"] == 1
        assert (await workspace_store.journal(tenant_a, boundary_id, 1, 10))["items"] == []

        # Build a compact-valid state just below 2 MiB whose PostgreSQL jsonb
        # text form crosses the DB constraint by its normalized whitespace.
        seed_blob = "x" * 261_900
        for index in range(8):
            await workspace_store.mutate(
                tenant_a,
                boundary_id,
                WorkspaceMutation(
                    schema_version=1,
                    mutation_id=f"state-seed-{index}-042",
                    expected_revision=index + 1,
                    kind="branch_committed",
                    payload={"branch": {"blob": seed_blob}},
                ),
            )
        boundary_state = (await workspace_store.get(tenant_a, boundary_id))["state"]
        branch_prefix = len(json.dumps(
            {"branch": {"blob": ""}}, separators=(",", ":"), ensure_ascii=False
        ).encode())
        low, high = 0, MAX_MUTATION_BYTES - branch_prefix
        async with workspace_store.transaction(tenant_a) as conn:
            while low < high:
                middle = (low + high) // 2
                candidate = dict(boundary_state)
                candidate["branches"] = [*boundary_state["branches"], {"blob": "x" * middle}]
                candidate_json = json.dumps(candidate, separators=(",", ":"), ensure_ascii=False)
                pg_size = await conn.fetchval(
                    "SELECT octet_length(($1::text)::jsonb::text)", candidate_json
                )
                if pg_size > 2 * 1024 * 1024:
                    high = middle
                else:
                    low = middle + 1
        final_state = dict(boundary_state)
        final_state["branches"] = [*boundary_state["branches"], {"blob": "x" * low}]
        assert len(json.dumps(final_state, separators=(",", ":"), ensure_ascii=False).encode()) <= 2 * 1024 * 1024
        final_state_json = json.dumps(final_state, separators=(",", ":"), ensure_ascii=False)
        async with workspace_store.transaction(tenant_a) as conn:
            assert await conn.fetchval(
                "SELECT octet_length(($1::text)::jsonb::text)", final_state_json
            ) > 2 * 1024 * 1024
        final_payload = {"branch": {"blob": "x" * low}}
        assert len(json.dumps(final_payload, separators=(",", ":"), ensure_ascii=False).encode()) <= MAX_MUTATION_BYTES
        with pytest.raises(HTTPException) as state_too_large:
            await workspace_store.mutate(
                tenant_a,
                boundary_id,
                WorkspaceMutation(
                    schema_version=1,
                    mutation_id="state-size-boundary-042",
                    expected_revision=9,
                    kind="branch_committed",
                    payload=final_payload,
                ),
            )
        assert state_too_large.value.status_code == 413
        assert state_too_large.value.detail == {"code": "workspace_too_large"}
        assert (await workspace_store.get(tenant_a, boundary_id))["revision"] == 9
        assert (await workspace_store.journal(tenant_a, boundary_id, 9, 10))["items"] == []

        async with workspace_store.transaction(tenant_a) as conn:
            assert await conn.fetchval("SELECT count(*) FROM atlas_explorer.workspaces") == 2
        async with workspace_store.transaction(owner_b) as conn:
            assert await conn.fetchval("SELECT count(*) FROM atlas_explorer.workspaces") == 0
        async with workspace_store.pool.acquire() as conn:
            tenant_context = await conn.fetchval("SELECT current_setting('atlas.tenant_id', true)")
            principal_context = await conn.fetchval("SELECT current_setting('atlas.principal_id', true)")
            assert tenant_context in (None, "")
            assert principal_context in (None, "")
            async with conn.transaction():
                await conn.execute("SELECT set_config('atlas.tenant_id',$1,true)", tenant_a.tenant_id)
                await conn.execute("SELECT set_config('atlas.principal_id',$1,true)", tenant_a.principal_id)
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with conn.transaction():
                        await conn.execute(
                            "INSERT INTO atlas_explorer.workspaces "
                            "(tenant_id,workspace_id,owner_principal_id,title,schema_version,state,revision,"
                            "create_mutation_id,create_digest) VALUES($1,gen_random_uuid(),'forged-owner',"
                            "'forged',1,'{}',1,'forged','0000000000000000000000000000000000000000000000000000000000000000')",
                            tenant_a.tenant_id,
                        )
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with conn.transaction():
                        await conn.execute(
                            "UPDATE atlas_explorer.workspaces SET owner_principal_id='forged-owner' "
                            "WHERE tenant_id=$1 AND owner_principal_id=$2",
                            tenant_a.tenant_id, tenant_a.principal_id,
                        )

        # Removing a second workspace must subtract its receipts/snapshot while
        # retaining the first; this exercises negative trigger deltas in PG.
        extra_receipt = await workspace_store.create(
            tenant_a,
            WorkspaceCreate(
                schema_version=1,
                mutation_id="create-delete-042",
                title="Disposable",
                draft="some data to remove",
            ),
        )
        extra_workspace_id = UUID(extra_receipt["workspace_id"])
        async with workspace_store.transaction(tenant_a) as conn:
            usage_before_delete = await conn.fetchval(
                "SELECT persisted_bytes FROM atlas_explorer.workspace_tenant_usage WHERE tenant_id=$1",
                tenant_a.tenant_id,
            )
        cleanup_conn = await asyncpg.connect(migration_url)
        try:
            async with cleanup_conn.transaction():
                await cleanup_conn.execute(
                    "SELECT set_config('atlas.tenant_id',$1,true)", tenant_a.tenant_id
                )
                await cleanup_conn.execute(
                    "SELECT set_config('atlas.principal_id',$1,true)", tenant_a.principal_id
                )
                await cleanup_conn.execute(
                    "DELETE FROM atlas_explorer.workspace_mutations WHERE tenant_id=$1 AND workspace_id=$2",
                    tenant_a.tenant_id, extra_workspace_id,
                )
                await cleanup_conn.execute(
                    "DELETE FROM atlas_explorer.workspaces WHERE tenant_id=$1 AND workspace_id=$2",
                    tenant_a.tenant_id, extra_workspace_id,
                )
                usage_after_delete = await cleanup_conn.fetchval(
                    "SELECT persisted_bytes FROM atlas_explorer.workspace_tenant_usage WHERE tenant_id=$1",
                    tenant_a.tenant_id,
                )
                assert 0 < usage_after_delete < usage_before_delete
        finally:
            await cleanup_conn.close()

        migration_conn = await asyncpg.connect(migration_url)
        try:
            flags = await migration_conn.fetch(
                "SELECT c.relrowsecurity,c.relforcerowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='atlas_explorer' "
                "AND c.relname IN ('workspaces','workspace_mutations','workspace_tenant_usage')"
            )
            assert len(flags) == 3 and all(row["relrowsecurity"] and row["relforcerowsecurity"] for row in flags)
            with pytest.raises(asyncpg.PostgresError):
                await migrate("down", migration_url)
        finally:
            await migration_conn.close()
    finally:
        await workspace_store.close()
        # Delete only the explicit test identities/data in the disposable DB.
        cleanup = await asyncpg.connect(migration_url)
        try:
            for identity in (tenant_a, tenant_b, owner_b):
                async with cleanup.transaction():
                    await cleanup.execute("SELECT set_config('atlas.tenant_id',$1,true)", identity.tenant_id)
                    await cleanup.execute("SELECT set_config('atlas.principal_id',$1,true)", identity.principal_id)
                    await cleanup.execute(
                        "DELETE FROM atlas_explorer.workspace_mutations WHERE tenant_id=$1 AND owner_principal_id=$2",
                        identity.tenant_id, identity.principal_id,
                    )
                    await cleanup.execute(
                        "DELETE FROM atlas_explorer.workspaces WHERE tenant_id=$1 AND owner_principal_id=$2",
                        identity.tenant_id, identity.principal_id,
                    )
        finally:
            await cleanup.close()
        await migrate("down", migration_url)
        await migrate("up", migration_url)
        await _assert_runtime_role_permissions(runtime_url)
