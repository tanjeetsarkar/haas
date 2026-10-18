"""Run with HAAS_TEST_POSTGRES_DSN against a disposable, migrated PostgreSQL 16 DB."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock
from uuid import UUID, uuid4
from urllib.parse import urlsplit

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.core.database_context import tenant_context
from app.core.identity import AuthenticatedIdentity
from app.core.store import DistributedStore, StoreConflictError
from app.core.tenant_policy import TenantPolicyRules, policy_digest
from app.phase2_migration import backfill, cutover, quarantine, map_tenant

DSN = os.environ.get("HAAS_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="Requires disposable PostgreSQL qualification DB"
)


@pytest_asyncio.fixture
async def db():
    owner = await asyncpg.connect(DSN)
    tenant, other, principal, policy = (uuid4() for _ in range(4))
    slug = f"qa-{tenant.hex[:12]}"
    other_slug = f"qa-{other.hex[:12]}"
    role = f"qa_{uuid4().hex}"
    await owner.execute(
        f"CREATE ROLE {role} LOGIN PASSWORD 'phase2-test-only' IN ROLE haas_tenant_app"
    )
    for identity, name in ((tenant, slug), (other, other_slug)):
        await owner.execute(
            "INSERT INTO tenants (tenant_id, slug, status) VALUES ($1,$2,'active')",
            identity,
            name,
        )
    await owner.execute(
        "INSERT INTO principals (principal_id,issuer,subject,kind) VALUES ($1,'https://qa.invalid',$2,'human')",
        principal,
        str(principal),
    )
    await owner.execute(
        "INSERT INTO memberships (tenant_id,principal_id,role) VALUES ($1,$2,'tenant_admin')",
        tenant,
        principal,
    )
    rules = TenantPolicyRules(
        model_providers=["openrouter"], models=["model-a", "model-b"]
    )
    await owner.execute(
        "INSERT INTO tenant_policy_revisions (policy_revision_id,tenant_id,version,digest,rules) "
        "VALUES ($1,$2,1,$3,$4::jsonb)",
        policy,
        tenant,
        policy_digest(rules),
        json.dumps(rules.model_dump(mode="json", exclude_none=True)),
    )
    await owner.execute(
        "UPDATE tenants SET active_policy_revision_id = $2 WHERE tenant_id = $1",
        tenant,
        policy,
    )
    store = DistributedStore()
    store.pg_pool = await asyncpg.create_pool(
        DSN,
        user=role,
        password="phase2-test-only",
        min_size=1,
        max_size=2,
    )
    store.redis = AsyncMock()
    token = tenant_context.set(slug)
    try:
        yield owner, store, slug, other_slug, tenant, other, principal, policy
    finally:
        tenant_context.reset(token)
        await store.close()
        await owner.execute(
            "UPDATE runs SET status = 'cancelled' WHERE tenant_uuid = $1 "
            "AND status IN ('running','input_required')",
            tenant,
        )
        await owner.execute(
            "UPDATE threads SET active_run_id = NULL WHERE tenant_uuid = $1", tenant
        )
        await owner.execute(f"DROP OWNED BY {role}")
        await owner.execute(f"DROP ROLE {role}")
        await owner.close()


async def admit(store, slug, thread, run, **kwargs):
    history = await store.get_or_create_thread_messages(thread, slug)
    await store.create_run(
        run_id=run,
        thread_id=thread,
        tenant_id=slug,
        traceparent="qa",
        primary_model="model-a",
        planner_model="model-b",
        initial_skills=[],
        initial_messages=[*history, {"role": "user", "content": run}],
        max_steps=3,
        max_cost_usd=0.3,
        webhook_url=None,
        **kwargs,
    )


async def test_rls_default_deny_foreign_rows_and_pool_reuse(db):
    owner, store, slug, other_slug, *_ = db
    thread, foreign_thread = f"t_{uuid4().hex}", f"t_{uuid4().hex}"
    await store.get_or_create_thread_messages(thread, slug)
    await owner.execute(
        "INSERT INTO threads (thread_id,tenant_id,updated_at) VALUES ($1,$2,1)",
        foreign_thread,
        other_slug,
    )
    async with store.connection() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM threads WHERE thread_id = $1", foreign_thread
            )
            == 0
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO threads (thread_id,tenant_id,updated_at) VALUES ($1,$2,1)",
                    f"t_{uuid4().hex}",
                    other_slug,
                )
    # Directly reuse the same pooled connection after transaction commit and rollback.
    async with store.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM threads") == 0
        assert await conn.fetchval("SELECT count(*) FROM tenants") == 0
        assert not await conn.fetchval("SELECT current_setting('haas.tenant',true)")
    token = tenant_context.set(other_slug)
    try:
        async with store.connection() as conn:
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM threads WHERE thread_id = $1", thread
                )
                == 0
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM threads WHERE thread_id = $1", foreign_thread
                )
                == 1
            )
    finally:
        tenant_context.reset(token)
    with pytest.raises(RuntimeError, match="rollback"):
        async with store.connection():
            raise RuntimeError("rollback")
    async with store.pool.acquire() as conn:
        assert not await conn.fetchval("SELECT current_setting('haas.tenant',true)")


async def test_thread_race_branch_stale_completion_cancel_and_reaper(db):
    owner, store, slug, _, _, _, principal, _ = db
    thread = f"t_{uuid4().hex}"
    await store.get_or_create_thread_messages(thread, slug)
    results = await asyncio.gather(
        admit(store, slug, thread, "r_" + uuid4().hex),
        admit(store, slug, thread, "r_" + uuid4().hex),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, StoreConflictError) for result in results) == 1
    state = await store.get_thread(thread, slug)
    active = state["active_run_id"]
    with pytest.raises(ValueError, match="Drain active"):
        await cutover(owner)
    messages = [{"role": "assistant", "content": "committed"}]
    assert await store.finalize_run(active, thread, "completed", "committed", messages)
    assert (await store.get_thread(thread, slug))["history_version"] == 1
    with pytest.raises(StoreConflictError):
        await admit(store, slug, thread, "r_" + uuid4().hex, expected_history_version=0)
    branch = await store.branch_thread(
        thread,
        slug,
        expected_history_version=1,
        actor_principal_id=str(principal),
        request_id=str(uuid4()),
    )
    assert (await store.get_thread(branch["thread_id"], slug))["messages"] == messages
    second = "r_" + uuid4().hex
    await admit(store, slug, thread, second, expected_history_version=1)
    assert not await store.finalize_run(active, thread, "completed", "stale", [])
    assert (await store.get_thread(thread, slug))["messages"] == messages
    assert await store.cancel_run(second, slug)
    third = "r_" + uuid4().hex
    await admit(store, slug, thread, third)
    assert any(row["run_id"] == third for row in await store.reap_orphaned_runs(10**12))
    assert (await store.get_thread(thread, slug))["active_run_id"] is None


async def test_revision_lifecycle_audit_immutability_and_pinning(db):
    _, store, slug, _, _, _, principal, policy = db
    configuration = {"primary_model": "model-a", "planner_model": "model-b"}
    revision = await store.create_agent_revision(
        slug,
        agent_name="qa",
        configuration=configuration,
        digest=policy_digest(configuration),
        policy_revision_id=str(policy),
        registry_fingerprint="qa",
        actor_principal_id=str(principal),
        request_id=str(uuid4()),
    )
    revision_id = revision["revision_id"]
    for target in ("validated", "published"):
        result = await store.transition_agent_revision(
            slug,
            revision_id,
            target=target,
            actor_principal_id=str(principal),
            request_id=str(uuid4()),
            policy_revision_id=str(policy),
            registry_fingerprint="qa",
        )
        assert result["state"] == target
    async with store.connection() as conn:
        with pytest.raises(asyncpg.RaiseError, match="immutable"):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE agent_revisions SET configuration = '{}'::jsonb "
                    "WHERE revision_id = $1",
                    UUID(revision_id),
                )
    run, thread = "r_" + uuid4().hex, "t_" + uuid4().hex
    await admit(
        store,
        slug,
        thread,
        run,
        agent_revision_id=revision_id,
        policy_revision_id=str(policy),
        registry_fingerprint="qa",
    )
    assert str((await store.get_run(run))["agent_revision_id"]) == revision_id
    actions = [
        event["action"] for event in await store.list_audit_events(slug, limit=20)
    ]
    assert {"agent.drafted", "agent.validated", "agent.published"} <= set(actions)


async def test_legacy_backfill_quarantine_and_cutover(db):
    owner, store, slug, _, tenant, other, principal, _ = db
    unknown = "legacy_" + uuid4().hex
    thread = "t_" + uuid4().hex
    await owner.execute(
        "INSERT INTO threads (thread_id,tenant_id,updated_at) VALUES ($1,$2,1)",
        thread,
        unknown,
    )
    assert (await backfill(owner, 1))[
        "threads"
    ] == 0  # Already synchronized, still unmapped.
    with pytest.raises(ValueError, match="Unresolved"):
        await cutover(owner)
    with pytest.raises(ValueError, match="administrator"):
        await map_tenant(owner, unknown, tenant, uuid4(), "invalid actor")
    await owner.execute(
        "UPDATE principals SET status = 'suspended' WHERE principal_id = $1", principal
    )
    with pytest.raises(ValueError, match="administrator"):
        await map_tenant(owner, unknown, tenant, principal, "Disabled actor")
    await owner.execute(
        "UPDATE principals SET status = 'active' WHERE principal_id = $1", principal
    )
    await map_tenant(owner, unknown, tenant, principal, "Approved legacy QA tenant")
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM audit_events WHERE tenant_id = $1 "
            "AND action = 'migration.tenant_mapped'",
            tenant,
        )
        == 1
    )
    await owner.execute(
        "INSERT INTO memberships (tenant_id, principal_id, role) VALUES ($1,$2,'tenant_admin')",
        other,
        principal,
    )
    with pytest.raises(ValueError, match="conflicts"):
        await map_tenant(owner, slug, other, principal, "Conflicting mapping")
    assert (await backfill(owner, 1))["threads"] == 1
    assert (
        await owner.fetchval(
            "SELECT tenant_uuid FROM threads WHERE thread_id = $1", thread
        )
        == tenant
    )
    orphan = "t_" + uuid4().hex
    await owner.execute(
        "INSERT INTO threads (thread_id,tenant_id,updated_at) VALUES ($1,$2,1)",
        orphan,
        "unmapped_" + uuid4().hex,
    )
    await quarantine(owner, "threads", orphan, principal, "Unmapped test fixture")
    async with store.connection() as conn:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.fetch("SELECT * FROM phase2_quarantine")
    await cutover(owner)
    assert (
        await owner.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'runs' AND column_name = 'total_cost_usd'"
        )
        == "numeric"
    )


async def test_api_revision_journey_policy_denials_and_history(db, monkeypatch):
    import app.main as main

    _, store, slug, other_slug, _, _, principal, _ = db
    role = "tenant_admin"

    async def authenticate(_credential, tenant):
        return AuthenticatedIdentity(
            tenant_id=tenant,
            principal_id=str(principal),
            issuer="https://qa.invalid",
            subject="qa",
            roles=frozenset({role}),
            credential_type="oidc",
        )

    from unittest.mock import Mock

    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "llm_step_task", Mock())
    monkeypatch.setattr(main.identity_authenticator, "authenticate", authenticate)
    headers = {"Authorization": "Bearer test-only", "X-Tenant-ID": slug}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="http://qa",
        headers=headers,
    ) as client:
        draft = await client.post(
            "/v1/agents/revisions",
            json={
                "agent_name": "api-qa",
                "configuration": {
                    "primary_model": "model-a",
                    "planner_model": "model-b",
                },
            },
        )
        assert draft.status_code == 201, draft.text
        revision_id = draft.json()["revision_id"]
        path = f"/v1/agents/revisions/{revision_id}"
        assert (await client.post(path + "/publish")).status_code == 409
        assert (await client.post(path + "/validate")).status_code == 200
        assert (await client.post(path + "/publish")).status_code == 200
        assert (await client.post(path + "/publish")).status_code == 409
        assert (
            await client.post(
                "/v1/runs",
                json={
                    "prompt": "qa",
                    "agent_revision_id": revision_id,
                    "max_steps": 2,
                },
            )
        ).status_code == 422
        created = await client.post(
            "/v1/runs",
            json={
                "prompt": "qa",
                "agent_revision_id": revision_id,
            },
        )
        assert created.status_code == 202, created.text
        thread = created.json()["thread_id"]
        assert (
            await client.post(
                "/v1/runs",
                json={
                    "prompt": "racing",
                    "thread_id": thread,
                    "agent_revision_id": revision_id,
                },
            )
        ).status_code == 409
        history = await client.get(f"/v1/threads/{thread}")
        assert history.status_code == 200 and history.json()["active_run_id"]
        branch = await client.post(
            f"/v1/threads/{thread}/branches", json={"expected_history_version": 0}
        )
        assert branch.status_code == 201, branch.text
        stale = await client.get(f"/v1/threads/{thread}", params={"history_version": 1})
        assert stale.status_code == 409
        assert (
            await client.get(path, headers={"X-Tenant-ID": other_slug})
        ).status_code == 404
        for denied_role in ("auditor", "operator", "break_glass", "approver"):
            role = denied_role
            assert (await client.get("/v1/threads")).status_code == 403
            assert (await client.get("/v1/agents/revisions")).status_code == 403
            assert (await client.post(path + "/publish")).status_code == 403
        role = "submitter"
        metadata = (await client.get("/v1/capabilities")).json()
        assert metadata["version"] == "1"
        assert "run.submit" in metadata["actions"]
        assert "tenant.admin" not in metadata["actions"]
        assert (await client.post(path + "/publish")).status_code == 403


async def test_real_api_key_identity_and_admin_queries_under_rls(db, monkeypatch):
    import app.main as main
    from unittest.mock import Mock

    _, store, slug, other_slug, _, _, principal, _ = db
    thread = "legacy_" + uuid4().hex
    await store.get_or_create_thread_messages(thread, slug)
    credential = "hk_test_" + uuid4().hex + uuid4().hex
    await store.create_tenant_api_key(
        tenant_selector=slug,
        principal_id=str(principal),
        credential=credential,
        name="rls-qualification",
        expires_in_days=1,
        actor_principal_id=str(principal),
        request_id=str(uuid4()),
    )
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "llm_step_task", Mock())
    monkeypatch.setattr(main.identity_authenticator, "store", store)
    headers = {"Authorization": f"Bearer {credential}", "X-Tenant-ID": slug}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(main.app), base_url="http://qa"
    ) as client:
        identity = await client.get("/v1/me", headers=headers)
        assert identity.status_code == 200, identity.text
        assert identity.json()["principal_id"] == str(principal)
        members = await client.get("/v1/admin/memberships", headers=headers)
        assert members.status_code == 200, members.text
        assert len(members.json()["items"]) == 1
        threads = await client.get("/v1/threads", headers=headers)
        assert threads.status_code == 200, threads.text
        assert any(row["thread_id"] == thread for row in threads.json()["items"])
        created = await client.post(
            "/v1/runs",
            headers=headers,
            json={
                "prompt": "Continue legacy thread",
                "thread_id": thread,
                "primary_model": "model-a",
                "planner_model": "model-b",
                "max_steps": 2,
            },
        )
        assert created.status_code == 202, created.text
        run_path = "/v1/runs/" + created.json()["run_id"]
        assert (await client.get(run_path, headers=headers)).status_code == 200
        assert (
            await client.post(run_path + "/cancel", headers=headers)
        ).status_code == 202
        history = await client.get(f"/v1/threads/{thread}", headers=headers)
        assert history.status_code == 200, history.text
        branch = await client.post(
            f"/v1/threads/{thread}/branches",
            headers=headers,
            json={"expected_history_version": 0},
        )
        assert branch.status_code == 201, branch.text
        foreign = await client.get(
            "/v1/me", headers={**headers, "X-Tenant-ID": other_slug}
        )
        assert foreign.status_code in {401, 403}
        assert (await client.get("/v1/me", headers=headers)).status_code == 200


async def test_operator_cli_reports_and_refuses_unstopped_cutover(
    db, monkeypatch, capsys
):
    from argparse import Namespace

    from app import phase2_migration
    from app.core.config import settings

    monkeypatch.setattr(settings, "haas_profile", "prod")
    monkeypatch.setattr(settings, "migration_postgres_dsn", None)
    with pytest.raises(ValueError, match="MIGRATION_POSTGRES_DSN"):
        await phase2_migration.execute(Namespace(operation="report"))
    monkeypatch.setattr(settings, "migration_postgres_dsn", DSN)
    await phase2_migration.execute(Namespace(operation="report"))
    rows = json.loads(capsys.readouterr().out)
    assert {row["relation"] for row in rows} == {"runs", "threads"}
    await phase2_migration.execute(Namespace(operation="backfill", batch_size=1))
    output = capsys.readouterr().out.splitlines()
    assert set(json.loads(output[0])) == {"runs", "threads"}
    assert len(json.loads(output[1])) == 2
    with pytest.raises(ValueError, match="Stop admission"):
        await phase2_migration.execute(
            Namespace(operation="cutover", admission_stopped=False)
        )


async def test_production_bootstrap_uses_operator_dsn(db, monkeypatch):
    from app.bootstrap_tenant import bootstrap
    from app.core.config import settings

    owner, *_ = db
    slug = "bootstrap-" + uuid4().hex[:12]
    monkeypatch.setattr(settings, "haas_profile", "prod")
    monkeypatch.setattr(settings, "bootstrap_tenant_slug", slug)
    monkeypatch.setattr(settings, "bootstrap_admin_issuer", "https://qa.invalid")
    monkeypatch.setattr(settings, "bootstrap_admin_subject", str(uuid4()))
    monkeypatch.setattr(settings, "oidc_issuer", "https://qa.invalid")
    monkeypatch.setattr(settings, "migration_postgres_dsn", None)
    with pytest.raises(ValueError, match="MIGRATION_POSTGRES_DSN"):
        await bootstrap()
    monkeypatch.setattr(settings, "migration_postgres_dsn", DSN)
    result = await bootstrap()
    assert result["tenant_slug"] == slug
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM memberships WHERE tenant_id = $1 "
            "AND principal_id = $2 AND role = 'tenant_admin'",
            UUID(result["tenant_id"]),
            UUID(result["principal_id"]),
        )
        == 1
    )


async def test_production_login_rejects_owner_and_service_api_identity(db, monkeypatch):
    from app.core.config import settings

    owner, store, *_ = db
    await cutover(owner)
    monkeypatch.setattr(settings, "haas_profile", "prod")
    monkeypatch.setattr(settings, "postgres_dsn", DSN)
    rejected = DistributedStore()
    with pytest.raises(RuntimeError, match="non-owner"):
        await asyncio.wait_for(rejected.init(pool_max=1), timeout=5)
    async with store.pool.acquire() as conn:
        role = await conn.fetchval("SELECT current_user")
    address = urlsplit(DSN)
    monkeypatch.setattr(
        settings,
        "postgres_dsn",
        address._replace(
            netloc=f"{role}:phase2-test-only@{address.netloc.rsplit('@', 1)[-1]}"
        ).geturl(),
    )
    api = DistributedStore()
    await api.init(pool_max=1)
    await api.close()
    await owner.execute(f"GRANT haas_execution_service TO {role}")
    try:
        with pytest.raises(RuntimeError, match="non-owner"):
            await asyncio.wait_for(DistributedStore().init(pool_max=1), timeout=5)
        service = DistributedStore()
        await service.init(pool_max=1, service=True)
        await service.close()
    finally:
        await owner.execute(f"REVOKE haas_execution_service FROM {role}")
