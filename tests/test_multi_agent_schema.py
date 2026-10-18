"""Multi-agent schema invariants (migration 0011) against a disposable PostgreSQL DB."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import asyncpg
import pytest

from app.core.database_context import tenant_context
from test_phase2_database import DSN, admit, db, service_store  # noqa: F401  (fixture re-export)

pytestmark = pytest.mark.skipif(
    not DSN, reason="Requires disposable PostgreSQL qualification DB"
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def _tree(store, slug: str, depth: int = 2) -> list[str]:
    """Admit a root and a chain of `depth` descendants; returns run ids root-first."""
    runs: list[str] = []
    for _ in range(depth + 1):
        run = f"r_{uuid4().hex}"
        await admit(
            store,
            slug,
            f"t_{uuid4().hex}",
            run,
            parent_run_id=runs[-1] if runs else None,
        )
        runs.append(run)
    return runs


async def _delegate(conn, slug: str, root: str, parent: str, child: str, depth: int):
    delegation = uuid4()
    caveats = {"tools": ["search"], "max_depth": 3}
    await conn.execute(
        "INSERT INTO delegations (tenant_id, delegation_id, root_run_id, parent_run_id, "
        "child_run_id, depth, mode, caveats, caveats_digest, budget_slice_usd) "
        "VALUES ($1,$2,$3,$4,$5,$6,'await',$7::jsonb,$8,0.05)",
        slug,
        delegation,
        root,
        parent,
        child,
        depth,
        json.dumps(caveats),
        _digest(json.dumps(caveats, sort_keys=True)),
    )
    return delegation


async def test_run_lineage_is_derived_from_parent_and_frozen(db):
    owner, store, slug, *_ = db
    root, child, grandchild = await _tree(store, slug)
    rows = {
        r["run_id"]: r
        for r in await owner.fetch(
            "SELECT run_id, root_run_id, depth FROM runs WHERE run_id = ANY($1)",
            [root, child, grandchild],
        )
    }
    assert (rows[root]["root_run_id"], rows[root]["depth"]) == (None, 0)
    assert (rows[child]["root_run_id"], rows[child]["depth"]) == (root, 1)
    assert (rows[grandchild]["root_run_id"], rows[grandchild]["depth"]) == (root, 2)
    async with store.connection() as conn:
        assert (
            await conn.fetchval("SELECT haas_run_root($1, $2)", slug, grandchild)
            == root
        )
        for statement in (
            "UPDATE runs SET depth = 7 WHERE run_id = $1",
            "UPDATE runs SET root_run_id = NULL WHERE run_id = $1",
            "UPDATE runs SET parent_run_id = NULL WHERE run_id = $1",
        ):
            with pytest.raises(asyncpg.PostgresError, match="lineage is immutable"):
                async with conn.transaction():
                    await conn.execute(statement, grandchild)
    # Callers cannot forge depth/root: the trigger recomputes them from the parent.
    forged = f"r_{uuid4().hex}"
    await owner.execute(
        "INSERT INTO runs (run_id, thread_id, tenant_id, traceparent, status, primary_model, "
        "planner_model, created_at, updated_at, parent_run_id, root_run_id, depth) "
        "SELECT $1, thread_id, tenant_id, 'qa', 'completed', 'm', 'm', 1, 1, $2, $2, 9 "
        "FROM runs WHERE run_id = $3",
        forged,
        child,
        root,
    )
    assert tuple(
        await owner.fetchrow(
            "SELECT root_run_id, depth FROM runs WHERE run_id = $1", forged
        )
    ) == (root, 2)


async def test_child_run_cannot_attach_to_foreign_tenant_parent(db):
    owner, store, slug, other_slug, *_ = db
    token = tenant_context.set(other_slug)
    try:
        (foreign_root,) = await _tree(store, other_slug, depth=0)
    finally:
        tenant_context.reset(token)
    with pytest.raises(asyncpg.ForeignKeyViolationError, match="not visible"):
        await admit(
            store,
            slug,
            f"t_{uuid4().hex}",
            f"r_{uuid4().hex}",
            parent_run_id=foreign_root,
        )
    # Even the schema owner (no RLS) cannot create a cross-tenant edge: the alias FK holds.
    with pytest.raises(asyncpg.PostgresError):
        await owner.execute(
            "INSERT INTO runs (run_id, thread_id, tenant_id, traceparent, status, "
            "primary_model, planner_model, created_at, updated_at, parent_run_id) "
            "SELECT $1, thread_id, $2, 'qa', 'completed', 'm', 'm', 1, 1, run_id "
            "FROM runs WHERE run_id = $3",
            f"r_{uuid4().hex}",
            slug,
            foreign_root,
        )


async def test_delegation_binding_and_settlement_rules(db):
    _, store, slug, *_ = db
    root, child, grandchild = await _tree(store, slug)
    async with store.connection() as conn:
        delegation = await _delegate(conn, slug, root, root, child, 1)
        await conn.execute(
            "UPDATE runs SET delegation_id = $2 WHERE run_id = $1", child, delegation
        )
        with pytest.raises(asyncpg.PostgresError, match="immutable once bound"):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE runs SET delegation_id = $2 WHERE run_id = $1",
                    child,
                    uuid4(),
                )
        # A delegation must describe a real parent → direct child edge at the right depth.
        for parent, target, depth in ((root, grandchild, 2), (child, grandchild, 1)):
            with pytest.raises(asyncpg.CheckViolationError, match="direct child"):
                async with conn.transaction():
                    await _delegate(conn, slug, root, parent, target, depth)
        # A run cannot claim a delegation that names a different child.
        with pytest.raises(asyncpg.CheckViolationError, match="does not delegate"):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE runs SET delegation_id = $2 WHERE run_id = $1",
                    grandchild,
                    delegation,
                )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE delegations SET budget_slice_usd = 99 WHERE delegation_id = $1",
                    delegation,
                )
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE delegations SET budget_returned_usd = 1 WHERE delegation_id = $1",
                    delegation,
                )
        await conn.execute(
            "UPDATE delegations SET status = 'settled', settled_at = now(), "
            "budget_returned_usd = 0.02 WHERE delegation_id = $1",
            delegation,
        )
        with pytest.raises(asyncpg.PostgresError, match="settled delegations"):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE delegations SET budget_returned_usd = 0.03 "
                    "WHERE delegation_id = $1",
                    delegation,
                )


async def test_mailbox_stays_inside_one_run_tree(db):
    _, store, slug, *_ = db
    root, child, _ = await _tree(store, slug)
    (other_root,) = await _tree(store, slug, depth=0)
    async with store.connection() as conn:
        message = uuid4()
        await conn.execute(
            "INSERT INTO agent_messages (tenant_id, message_id, root_run_id, from_run_id, "
            "to_run_id, kind, body_reference) VALUES ($1,$2,$3,$4,$5,'message',$6::jsonb)",
            slug,
            message,
            root,
            root,
            child,
            json.dumps({"inline": {"text": "hello"}}),
        )
        with pytest.raises(asyncpg.CheckViolationError, match="one run tree"):
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO agent_messages (tenant_id, message_id, root_run_id, "
                    "from_run_id, to_run_id, kind, body_reference) "
                    "VALUES ($1,$2,$3,$4,$5,'message','{}'::jsonb)",
                    slug,
                    uuid4(),
                    root,
                    other_root,
                    child,
                )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE agent_messages SET body_reference = '{}'::jsonb "
                    "WHERE message_id = $1",
                    message,
                )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM agent_messages WHERE message_id = $1", message
                )
        await conn.execute(
            "UPDATE agent_messages SET consumed_at = now() WHERE message_id = $1",
            message,
        )
        with pytest.raises(
            asyncpg.PostgresError, match="immutable once sent or consumed"
        ):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE agent_messages SET consumed_at = now() + interval '1 hour' "
                    "WHERE message_id = $1",
                    message,
                )


async def test_workspace_versions_require_compare_and_swap(db):
    _, store, slug, *_ = db
    root, child, _ = await _tree(store, slug)
    (other_root,) = await _tree(store, slug, depth=0)

    async def write(conn, version: int, *, run: str, path: str = "/notes/plan.md"):
        await conn.execute(
            "INSERT INTO workspace_entry_versions (tenant_id, workspace_id, path, version, "
            "written_by_run_id, content_digest, inline_content) VALUES ($1,$2,$3,$4,$5,$6,$7)",
            slug,
            workspace,
            path,
            version,
            run,
            _digest(str(version)),
            f"v{version}",
        )

    async with store.connection() as conn:
        workspace = uuid4()
        await conn.execute(
            "INSERT INTO workspaces (tenant_id, workspace_id, root_run_id) VALUES ($1,$2,$3)",
            slug,
            workspace,
            root,
        )
        with pytest.raises(asyncpg.CheckViolationError, match="not a root"):
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO workspaces (tenant_id, workspace_id, root_run_id) "
                    "VALUES ($1,$2,$3)",
                    slug,
                    uuid4(),
                    child,
                )
        await write(conn, 1, run=root)
        await write(conn, 2, run=child)
        for stale in (2, 4):
            with pytest.raises(asyncpg.SerializationError, match="CAS conflict"):
                async with conn.transaction():
                    await write(conn, stale, run=child)
        with pytest.raises(asyncpg.CheckViolationError, match="owning tree"):
            async with conn.transaction():
                await write(conn, 3, run=other_root)
        for bad_path in ("notes", "/a/../b", "/a//b", "/a/./b", "/a b"):
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await write(conn, 1, run=root, path=bad_path)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE workspace_entry_versions SET inline_content = 'x' "
                    "WHERE workspace_id = $1",
                    workspace,
                )
        assert (
            await conn.fetchval(
                "SELECT max(version) FROM workspace_entry_versions WHERE workspace_id = $1",
                workspace,
            )
            == 2
        )


async def test_tree_tables_are_tenant_isolated(db):
    owner, store, slug, other_slug, *_ = db
    token = tenant_context.set(other_slug)
    try:
        foreign_root, foreign_child = await _tree(store, other_slug, depth=1)
        async with store.connection() as conn:
            await _delegate(
                conn, other_slug, foreign_root, foreign_root, foreign_child, 1
            )
            await conn.execute(
                "INSERT INTO workspaces (tenant_id, workspace_id, root_run_id) "
                "VALUES ($1,$2,$3)",
                other_slug,
                uuid4(),
                foreign_root,
            )
            await conn.execute(
                "INSERT INTO agent_messages (tenant_id, message_id, root_run_id, "
                "from_run_id, to_run_id, kind, body_reference) "
                "VALUES ($1,$2,$3,$3,$4,'result','{}'::jsonb)",
                other_slug,
                uuid4(),
                foreign_root,
                foreign_child,
            )
    finally:
        tenant_context.reset(token)
    async with store.connection() as conn:
        for table in ("delegations", "agent_messages", "workspaces"):
            assert (
                await conn.fetchval(
                    f"SELECT count(*) FROM {table} WHERE root_run_id = $1", foreign_root
                )
                == 0
            )
        # Writes naming the other tenant are refused before RLS even evaluates.
        with pytest.raises(
            (asyncpg.ForeignKeyViolationError, asyncpg.InsufficientPrivilegeError)
        ):
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO workspaces (tenant_id, workspace_id, root_run_id) "
                    "VALUES ($1,$2,$3)",
                    other_slug,
                    uuid4(),
                    foreign_root,
                )
    async with service_store(owner) as service:
        async with service.pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM delegations WHERE root_run_id = $1",
                    foreign_root,
                )
                == 1
            )
    for table in ("delegations", "agent_messages", "workspaces", "agent_cards"):
        flags = await owner.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = $1",
            table,
        )
        assert tuple(flags) == (True, True), table


async def test_agent_cards_require_published_revision(db):
    owner, store, slug, other_slug, tenant, other, principal, policy = db
    revisions = {}
    for state in ("draft", "published"):
        revision = uuid4()
        await owner.execute(
            "INSERT INTO agent_revisions (tenant_id, revision_id, agent_name, version, state, "
            "configuration, digest, policy_revision_id, registry_fingerprint, created_by, "
            "published_at) VALUES ($1,$2,$3,1,$4,'{}'::jsonb,$5,$6,'qa',$7, "
            "CASE WHEN $4 = 'published' THEN now() END)",
            tenant,
            revision,
            f"card-{state}-{uuid4().hex[:8]}",
            state,
            _digest(str(revision)),
            policy,
            principal,
        )
        revisions[state] = revision
    card = json.dumps({"name": "researcher", "skills": [{"id": "search"}]})
    async with store.connection() as conn:
        with pytest.raises(asyncpg.CheckViolationError, match="published"):
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO agent_cards (tenant_id, agent_revision_id, card) "
                    "VALUES ($1,$2,$3::jsonb)",
                    tenant,
                    revisions["draft"],
                    card,
                )
        await conn.execute(
            "INSERT INTO agent_cards (tenant_id, agent_revision_id, card, exposure) "
            "VALUES ($1,$2,$3::jsonb,'public')",
            tenant,
            revisions["published"],
            card,
        )
        # The BEFORE trigger cannot see the foreign revision, so it refuses ahead of RLS.
        with pytest.raises(
            (asyncpg.CheckViolationError, asyncpg.InsufficientPrivilegeError)
        ):
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO agent_cards (tenant_id, agent_revision_id, card) "
                    "VALUES ($1,$2,'{}'::jsonb)",
                    other,
                    revisions["published"],
                )
    token = tenant_context.set(other_slug)
    try:
        async with store.connection() as conn:
            assert await conn.fetchval("SELECT count(*) FROM agent_cards") == 0
    finally:
        tenant_context.reset(token)
