"""Restricted derivation denies before writes; legacy lineage remains supported."""

import asyncio
from uuid import uuid4

import asyncpg
import pytest

from app.core.run_restrictions import RunAuthority
from app.core.store import StoreConflictError, StoreNotFoundError
from app.core.workflow import decode_run
from test_phase2_database import DSN, admit, db as db
from test_run_authority_storage import bound

pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


async def restricted(db, kind="empty"):
    owner, store, slug, _, tenant, _, principal, policy = db
    run = "restricted_" + uuid4().hex
    authority = {
        "empty": RunAuthority(tools=()), "null": RunAuthority(),
        "intent": bound(tenant, principal, policy, run),
    }[kind]
    await admit(
        store, slug, run, run, authority=authority,
        submitted_by_principal_id=str(principal), policy_revision_id=str(policy),
        registry_fingerprint="registry",
    )
    return run, authority


async def snapshot(owner, slug):
    return tuple(await owner.fetchrow(
        "SELECT (SELECT count(*) FROM runs WHERE tenant_id=$1),"
        "(SELECT count(*) FROM threads WHERE tenant_id=$1),"
        "(SELECT count(*) FROM outbox_messages WHERE tenant_id=$1),"
        "(SELECT count(*) FROM approval_decisions WHERE tenant_id=$1)", slug,
    ))


@pytest.mark.parametrize("kind", ["empty", "null", "intent"])
@pytest.mark.parametrize("terminal", [False, True])
async def test_restricted_branch_continuation_child_and_resume_deny_without_writes(db, kind, terminal):
    owner, store, slug, *_ = db
    parent, authority = await restricted(db, kind)
    if terminal:
        await owner.execute("UPDATE runs SET status='completed' WHERE run_id=$1", parent)
        await owner.execute("UPDATE threads SET active_run_id=NULL WHERE thread_id=$1", parent)
    target = "child_" + uuid4().hex
    await store.get_or_create_thread_messages(target, slug)
    before = await snapshot(owner, slug)
    with pytest.raises(StoreConflictError, match="restriction.derivation_unavailable"):
        await store.branch_thread(
            parent, slug, expected_history_version=0, actor_principal_id=None,
            request_id=str(uuid4()),
        )
    for thread, parent_id in ((parent, None), (target, parent)):
        with pytest.raises(StoreConflictError, match="restriction.derivation_unavailable"):
            await admit(store, slug, thread, uuid4().hex, parent_run_id=parent_id)
    for key in (None, "replayed-decision"):
        with pytest.raises(StoreConflictError, match="restriction.resume_unavailable"):
            await store.decide_approval(
                slug, run_id=parent, batch_id=None, batch_digest_value=None,
                approved=True, reason=None, actor_principal_id=None,
                request_id=str(uuid4()), idempotency_key=key, require_binding=False,
            )
    assert await snapshot(owner, slug) == before
    # Restart/checkpoint decoding has no route to a new, unrestricted authority.
    saved = await owner.fetchrow("SELECT * FROM runs WHERE run_id=$1", parent)
    assert decode_run(saved)["authority"] == authority


async def test_legacy_branch_and_child_still_work_and_foreign_parent_is_hidden(db):
    owner, store, slug, other_slug, *_ = db
    parent, child = uuid4().hex, uuid4().hex
    await admit(store, slug, parent, parent)
    branch = await store.branch_thread(
        parent, slug, expected_history_version=0, actor_principal_id=None, request_id=str(uuid4()),
    )
    await admit(store, slug, branch["thread_id"], child, parent_run_id=parent)
    assert (await store.get_run(child))["parent_run_id"] == parent
    async with store.connection() as conn:
        for tenant in (other_slug,):
            with pytest.raises(StoreNotFoundError):
                await store._check_parent_authority(conn, parent, tenant)
        with pytest.raises(StoreNotFoundError):
            await store._check_parent_authority(conn, "missing", slug)
    assert await owner.fetchval("SELECT authority_json FROM runs WHERE run_id=$1", child) is None


async def test_preexisting_branch_and_child_cannot_launder_restricted_ancestor(db):
    owner, store, slug, *_ = db
    parent, _ = await restricted(db)
    branch, old_child, target = (uuid4().hex for _ in range(3))
    for thread in (branch, target):
        await store.get_or_create_thread_messages(thread, slug)
    await owner.execute(
        "UPDATE threads SET parent_thread_id=$2,parent_history_version=0 WHERE thread_id=$1",
        branch, parent,
    )
    await owner.execute(
        "INSERT INTO runs SELECT (jsonb_populate_record(NULL::runs,to_jsonb(r)||"
        "jsonb_build_object('run_id',$2::text,'run_key',gen_random_uuid(),"
        "'parent_run_id',$1::text,'authority_json',NULL,'status','completed'))).* "
        "FROM runs r WHERE run_id=$1", parent, old_child,
    )
    before = await snapshot(owner, slug)
    with pytest.raises(StoreConflictError, match="restriction.derivation_unavailable"):
        await admit(store, slug, branch, uuid4().hex)
    with pytest.raises(StoreConflictError, match="restriction.derivation_unavailable"):
        await admit(store, slug, target, uuid4().hex, parent_run_id=old_child)
    with pytest.raises(StoreConflictError, match="restriction.derivation_unavailable"):
        await store.branch_thread(
            branch, slug, expected_history_version=0, actor_principal_id=None,
            request_id=str(uuid4()),
        )
    assert await snapshot(owner, slug) == before


async def test_delegation_route_denies_before_revision_lookup_or_writes(db, monkeypatch):
    # Main owns router loading; phase engineer's request gate is independent.
    import app.main  # noqa: F401
    from app.api import agent_mesh
    from app.api.deps import PrincipalInfo
    from fastapi import HTTPException

    owner, store, slug, *_ = db
    parent, _ = await restricted(db, "intent")
    monkeypatch.setattr(agent_mesh, "store", store)
    before = await snapshot(owner, slug)
    with pytest.raises(HTTPException) as error:
        await agent_mesh.create_delegation(
            agent_mesh.DelegationCreateRequest(
                parent_run_id=parent, child_agent_revision_id="invalid-before-lookup",
                child_agent_config={"tools": None},
            ), PrincipalInfo(tenant_id=slug, principal_id=str(db[6]), roles=["tenant_admin"]),
        )
    assert error.value.status_code == 409
    assert await snapshot(owner, slug) == before
    with pytest.raises(HTTPException) as foreign:
        await agent_mesh.create_delegation(
            agent_mesh.DelegationCreateRequest(
                parent_run_id=parent, child_agent_revision_id="invalid-before-lookup",
            ), PrincipalInfo(tenant_id=db[3], principal_id=str(db[6]), roles=["tenant_admin"]),
        )
    assert foreign.value.status_code == 404


async def test_malformed_authority_and_legacy_parent_on_restricted_thread_cannot_derive(db):
    owner, store, slug, *_ = db
    legacy, corrupted = uuid4().hex, uuid4().hex
    await admit(store, slug, legacy, legacy)
    await owner.execute(
        "INSERT INTO runs SELECT (jsonb_populate_record(NULL::runs,to_jsonb(r)||"
        "jsonb_build_object('run_id',$2::text,'run_key',gen_random_uuid(),"
        "'status','completed','authority_json','{}'::jsonb))).* "
        "FROM runs r WHERE run_id=$1", legacy, corrupted,
    )
    before = await snapshot(owner, slug)
    async with store.connection() as conn:
        for parent in (legacy, corrupted):
            with pytest.raises(StoreConflictError, match="restriction.derivation_unavailable"):
                await store._check_parent_authority(conn, parent, slug)
    assert await snapshot(owner, slug) == before


async def test_branch_waits_for_inflight_restricted_admission_and_then_denies(db):
    owner, store, slug, *_ = db
    source = uuid4().hex
    await admit(store, slug, source, source)
    # Actual uncommitted restricted INSERT under admission's thread lock:
    # branch must wait and inspect committed authority, not its earlier snapshot.
    writer = await asyncpg.connect(DSN)
    transaction = writer.transaction()
    await transaction.start()
    task = None
    try:
        await writer.execute("SELECT 1 FROM threads WHERE thread_id=$1 FOR UPDATE", source)
        await writer.execute(
            "INSERT INTO runs SELECT (jsonb_populate_record(NULL::runs,to_jsonb(r)||"
            "jsonb_build_object('run_id',$2::text,'run_key',gen_random_uuid(),"
            "'status','completed','authority_json','{\"version\":1,\"tools\":[]}'::jsonb))).* "
            "FROM runs r WHERE run_id=$1", source, uuid4().hex,
        )
        task = asyncio.create_task(store.branch_thread(
            source, slug, expected_history_version=0, actor_principal_id=None,
            request_id=str(uuid4()),
        ))

        async def waiting():
            while not await owner.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE wait_event_type='Lock' "
                "AND query LIKE 'WITH RECURSIVE parents AS%')",
            ):
                if task.done():
                    await task
                await asyncio.sleep(.02)

        await asyncio.wait_for(waiting(), 10)
        await transaction.commit()
        with pytest.raises(StoreConflictError, match="restriction.derivation_unavailable"):
            await asyncio.wait_for(task, 10)
    finally:
        if writer.is_in_transaction():
            await transaction.rollback()
        await writer.close()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("route", ["delegation", "cross_thread_child"])
async def test_derivation_waits_for_authority_insert_on_parent_source_thread(db, monkeypatch, route):
    import app.main  # noqa: F401
    from app.api import agent_mesh
    from app.api.deps import PrincipalInfo
    from fastapi import HTTPException

    owner, store, slug, *_ = db
    source, target = uuid4().hex, uuid4().hex
    await admit(store, slug, source, source)
    await store.get_or_create_thread_messages(target, slug)
    monkeypatch.setattr(agent_mesh, "store", store)
    writer = await asyncpg.connect(DSN)
    transaction = writer.transaction()
    await transaction.start()
    task = None
    before = await snapshot(owner, slug)
    try:
        await writer.execute("SELECT 1 FROM threads WHERE thread_id=$1 FOR UPDATE", source)
        await writer.execute(
            "INSERT INTO runs SELECT (jsonb_populate_record(NULL::runs,to_jsonb(r)||"
            "jsonb_build_object('run_id',$2::text,'run_key',gen_random_uuid(),"
            "'status','completed','authority_json','{\"version\":1,\"tools\":[]}'::jsonb))).* "
            "FROM runs r WHERE run_id=$1", source, uuid4().hex,
        )
        if route == "delegation":
            task = asyncio.create_task(agent_mesh.create_delegation(
                agent_mesh.DelegationCreateRequest(
                    parent_run_id=source, child_agent_revision_id="invalid-before-lookup",
                ), PrincipalInfo(tenant_id=slug, principal_id=str(db[6]), roles=["tenant_admin"]),
            ))
        else:
            task = asyncio.create_task(admit(
                store, slug, target, uuid4().hex, parent_run_id=source,
            ))

        async def waiting():
            while not await owner.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE wait_event_type='Lock' "
                "AND query LIKE 'WITH RECURSIVE parents AS%')",
            ):
                if task.done():
                    await task
                    pytest.fail("derivation did not wait for source admission")
                await asyncio.sleep(.02)

        await asyncio.wait_for(waiting(), 10)
        await transaction.commit()
        if route == "delegation":
            with pytest.raises(HTTPException) as error:
                await asyncio.wait_for(task, 10)
            assert error.value.status_code == 409
        else:
            with pytest.raises(StoreConflictError, match="restriction.derivation_unavailable"):
                await asyncio.wait_for(task, 10)
        after = await snapshot(owner, slug)
        assert after == (before[0] + 1, *before[1:])  # Only the writer's authority INSERT.
    finally:
        if writer.is_in_transaction():
            await transaction.rollback()
        await writer.close()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
