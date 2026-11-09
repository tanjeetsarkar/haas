"""Pending denial regressions; no activation of held phase runs."""

import asyncio
from uuid import uuid4

import asyncpg
import pytest
from fastapi import HTTPException

from app.core.run_phases import PhaseConfig
from app.core.store import StoreConflictError, StoreNotFoundError
from test_phase2_database import DSN, admit, db as db

pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


async def counts(owner, slug):
    return tuple(await owner.fetchrow(
        "SELECT (SELECT count(*) FROM runs WHERE tenant_id=$1),"
        "(SELECT count(*) FROM delegations WHERE tenant_id=$1),"
        "(SELECT count(*) FROM outbox_messages WHERE tenant_id=$1),"
        "(SELECT count(*) FROM tool_invocations WHERE tenant_id=$1)", slug,
    ))


async def phase_parent(store, slug):
    parent = f"phase-parent-{uuid4().hex}"
    await admit(store, slug, parent, parent, phase_config=PhaseConfig())
    return parent


@pytest.mark.parametrize("terminal", [False, True])
async def test_phase_parent_child_admission_denied_before_any_write(db, terminal):
    owner, store, slug, *_ = db
    parent = await phase_parent(store, slug)
    if terminal:
        await owner.execute("UPDATE runs SET status='completed' WHERE run_id=$1", parent)
    target = f"target-{uuid4().hex}"
    await store.get_or_create_thread_messages(target, slug)
    before = await counts(owner, slug)
    with pytest.raises(StoreConflictError, match="phase.delegation_unavailable"):
        await admit(store, slug, target, uuid4().hex, parent_run_id=parent)
    assert await counts(owner, slug) == before


async def test_phase_parent_mesh_denied_before_revision_lookup_or_writes(db, monkeypatch):
    import app.main  # noqa: F401
    from app.api import agent_mesh
    from app.api.deps import PrincipalInfo

    owner, store, slug, _, _, _, principal, _ = db
    parent = await phase_parent(store, slug)
    monkeypatch.setattr(agent_mesh, "store", store)
    before = await counts(owner, slug)
    request = agent_mesh.DelegationCreateRequest(
        parent_run_id=parent, child_agent_revision_id="invalid-before-lookup",
    )
    with pytest.raises(HTTPException) as denied:
        await agent_mesh.create_delegation(
            request, PrincipalInfo(tenant_id=slug, principal_id=str(principal), roles=["tenant_admin"]),
        )
    assert denied.value.status_code == 409
    assert denied.value.detail == "phase.delegation_unavailable"
    assert await counts(owner, slug) == before


async def test_phase_parent_mesh_hides_foreign_tenant(db, monkeypatch):
    import app.main  # noqa: F401
    from app.api import agent_mesh
    from app.api.deps import PrincipalInfo

    _, store, slug, other_slug, _, _, principal, _ = db
    parent = await phase_parent(store, slug)
    monkeypatch.setattr(agent_mesh, "store", store)
    with pytest.raises(HTTPException) as foreign:
        await agent_mesh.create_delegation(
            agent_mesh.DelegationCreateRequest(
                parent_run_id=parent, child_agent_revision_id="invalid-before-lookup",
            ),
            PrincipalInfo(tenant_id=other_slug, principal_id=str(principal), roles=["tenant_admin"]),
        )
    assert foreign.value.status_code == 404


async def test_parent_check_preserves_legacy_and_foreign_isolation(db):
    _, store, slug, other_slug, *_ = db
    legacy = f"legacy-parent-{uuid4().hex}"
    await admit(store, slug, legacy, legacy)
    async with store.connection() as conn:
        await store._check_parent_authority(conn, legacy, slug)
        with pytest.raises(StoreNotFoundError):
            await store._check_parent_authority(conn, legacy, other_slug)


@pytest.mark.parametrize("terminal", ["completed", "cancelled"])
async def test_terminal_phase_history_allows_ordinary_continuation_and_branch(db, terminal):
    owner, store, slug, *_ = db
    phase = await phase_parent(store, slug)
    await owner.execute("UPDATE runs SET status=$2 WHERE run_id=$1", phase, terminal)
    await owner.execute("UPDATE threads SET active_run_id=NULL WHERE thread_id=$1", phase)
    branch = await store.branch_thread(
        phase, slug, expected_history_version=0, actor_principal_id=None, request_id=str(uuid4()),
    )
    await admit(store, slug, phase, uuid4().hex)
    await admit(store, slug, branch["thread_id"], uuid4().hex)


@pytest.mark.parametrize("route", ["delegation", "cross_thread_child"])
async def test_derivation_waits_for_inflight_phase_insert_on_source_thread(db, monkeypatch, route):
    import app.main  # noqa: F401
    from app.api import agent_mesh
    from app.api.deps import PrincipalInfo

    owner, store, slug, *_ = db
    source, target = uuid4().hex, uuid4().hex
    await admit(store, slug, source, source)
    template = await phase_parent(store, slug)
    await store.get_or_create_thread_messages(target, slug)
    monkeypatch.setattr(agent_mesh, "store", store)
    writer = await asyncpg.connect(DSN)
    transaction = writer.transaction()
    await transaction.start()
    task = None
    before = await counts(owner, slug)
    try:
        await writer.execute("SELECT 1 FROM threads WHERE thread_id=$1 FOR UPDATE", source)
        # Use actual admitted phase data: DB triggers independently reject
        # malformed configuration and orphan phase state.
        await writer.execute(
            "INSERT INTO runs SELECT (jsonb_populate_record(NULL::runs,to_jsonb(r)||"
            "jsonb_build_object('run_id',$2::text,'run_key',gen_random_uuid(),"
            "'status','completed','thread_id',$3::text))).* "
            "FROM runs r WHERE run_id=$1", template, uuid4().hex, source,
        )
        if route == "delegation":
            task = asyncio.create_task(agent_mesh.create_delegation(
                agent_mesh.DelegationCreateRequest(
                    parent_run_id=source, child_agent_revision_id="invalid-before-lookup",
                ), PrincipalInfo(tenant_id=slug, principal_id=str(db[6]), roles=["tenant_admin"]),
            ))
        else:
            task = asyncio.create_task(admit(store, slug, target, uuid4().hex, parent_run_id=source))

        async def waiting():
            while not await owner.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE wait_event_type='Lock' "
                "AND query LIKE 'WITH RECURSIVE parents AS%')",
            ):
                if task.done():
                    await task
                    pytest.fail("derivation bypassed source admission lock")
                await asyncio.sleep(.02)

        await asyncio.wait_for(waiting(), 10)
        await transaction.commit()
        if route == "delegation":
            with pytest.raises(HTTPException) as denied:
                await asyncio.wait_for(task, 10)
            assert denied.value.status_code == 409
            assert denied.value.detail == "phase.delegation_unavailable"
        else:
            with pytest.raises(StoreConflictError, match="phase.delegation_unavailable"):
                await asyncio.wait_for(task, 10)
        assert await counts(owner, slug) == (before[0] + 1, *before[1:])
    finally:
        if writer.is_in_transaction():
            await transaction.rollback()
        await writer.close()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
