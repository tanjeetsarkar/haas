"""Real distinct authenticated approver and bound held decision, never dispatch."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from app.core.identity import AuthenticatedIdentity
from app.core.workflow import IdempotencyConflictError
from test_phase2_database import DSN, db as db
from test_run_intent_workflow import running

pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


async def pending(db, monkeypatch):
    owner, store, slug, _, tenant, *_ = db
    _, registry, lease = await running(db, monkeypatch)
    await store.prepare_intent_approval(lease)
    approver = uuid4()
    await owner.execute(
        "INSERT INTO principals(principal_id,issuer,subject,kind) VALUES($1,'https://qa.invalid',$2,'human')",
        approver, str(approver),
    )
    await owner.execute(
        "INSERT INTO memberships(tenant_id,principal_id,role) VALUES($1,$2,'approver')", tenant, approver,
    )
    batch = await owner.fetchrow("SELECT * FROM approval_batches WHERE run_id=$1", lease.run_id)
    identity = AuthenticatedIdentity(
        tenant_id=slug, principal_id=str(approver), issuer="https://qa.invalid",
        subject=str(approver), roles=frozenset({"approver"}), credential_type="oidc",
    )
    return registry, {
        "identity": identity, "run_id": lease.run_id, "batch_id": str(batch["batch_id"]),
        "batch_digest": batch["batch_digest"], "approved": True,
        "request_id": str(uuid4()), "idempotency_key": uuid4().hex,
    }


@pytest.mark.parametrize("approved", [True, False])
async def test_real_distinct_approver_records_bound_decision_but_run_stays_held(db, monkeypatch, approved):
    owner, store, *_ = db
    _, decision = await pending(db, monkeypatch)
    decision["approved"] = approved
    first = await store.record_held_intent_decision(**decision)
    replay = await store.record_held_intent_decision(**{**decision, "request_id": str(uuid4())})
    assert not first["replayed"] and replay["replayed"]
    saved = await store.get_run(decision["run_id"])
    assert saved["status"] == "input_required" and saved["next_action"] == "none"
    persisted = await owner.fetchrow(
        "SELECT * FROM approval_decisions WHERE run_id=$1", decision["run_id"],
    )
    assert str(persisted["decided_by"]) == decision["identity"].principal_id
    assert persisted["batch_digest"] == decision["batch_digest"]
    assert await owner.fetchval(
        "SELECT count(*) FROM approval_decisions WHERE run_id=$1", decision["run_id"],
    ) == 1
    assert await owner.fetchval(
        "SELECT count(*) FROM outbox_messages WHERE run_id=$1", decision["run_id"],
    ) == 0


@pytest.mark.parametrize("change", [
    "self", "foreign", "role", "revoked", "principal", "digest", "batch", "expiry",
    "arguments", "plugin", "policy", "secrets", "invocation_step", "preview",
])
async def test_unbound_ineligible_or_secret_mutating_decision_has_zero_writes(db, monkeypatch, change):
    owner, store, _, _, tenant, _, submitter, old_policy = db
    registry, decision = await pending(db, monkeypatch)
    if change == "self":
        await owner.execute(
            "INSERT INTO memberships(tenant_id,principal_id,role) VALUES($1,$2,'approver')", tenant, submitter,
        )
        decision["identity"] = replace(decision["identity"], principal_id=str(submitter))
    elif change == "foreign":
        decision["identity"] = replace(decision["identity"], tenant_id=db[3])
    elif change == "role":
        decision["identity"] = replace(decision["identity"], roles=frozenset({"submitter"}))
    elif change in ("revoked", "principal"):
        from uuid import UUID
        if change == "revoked":
            await owner.execute(
                "UPDATE memberships SET status='revoked' WHERE principal_id=$1",
                UUID(decision["identity"].principal_id),
            )
        else:
            await owner.execute(
                "UPDATE principals SET status='revoked' WHERE principal_id=$1",
                UUID(decision["identity"].principal_id),
            )
    elif change == "digest":
        decision["batch_digest"] = "0" * 64
    elif change == "batch":
        decision["batch_id"] = str(uuid4())
    elif change == "expiry":
        await owner.execute(
            "UPDATE approval_batches SET created_at=now()-interval '2 seconds',"
            "expires_at=now()-interval '1 second' WHERE run_id=$1", decision["run_id"],
        )
    elif change == "invocation_step":
        await owner.execute(
            "UPDATE tool_invocations SET step_number=2 WHERE run_id=$1", decision["run_id"],
        )
    elif change == "preview":
        await owner.execute(
            "UPDATE approval_batches SET tool_calls=jsonb_set(tool_calls,'{0,id}','\"altered\"') "
            "WHERE run_id=$1", decision["run_id"],
        )
    elif change == "arguments":
        await owner.execute("UPDATE tool_invocations SET arguments='{}' WHERE run_id=$1", decision["run_id"])
    elif change == "plugin":
        registry.plugins["fake"].digest = "b" * 64
    elif change == "policy":
        await owner.execute("UPDATE tenants SET active_policy_revision_id=$2 WHERE tenant_id=$1", tenant, old_policy)
    else:
        decision["secrets"] = {"synthetic": "not-live"}
    with pytest.raises(ValueError):
        await store.record_held_intent_decision(**decision)
    for table in ("approval_decisions", "outbox_messages"):
        assert await owner.fetchval(f"SELECT count(*) FROM {table} WHERE run_id=$1", decision["run_id"]) == 0


async def test_competing_decisions_one_wins_and_replay_rechecks_revoked_approver(db, monkeypatch):
    from uuid import UUID

    owner, store, *_ = db
    _, decision = await pending(db, monkeypatch)
    results = await asyncio.gather(
        store.record_held_intent_decision(**decision),
        store.record_held_intent_decision(**{**decision, "approved": False}),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, IdempotencyConflictError) for result in results) == 1
    await owner.execute(
        "UPDATE memberships SET status='revoked' WHERE principal_id=$1", UUID(decision["identity"].principal_id),
    )
    with pytest.raises(ValueError, match="restriction.approver_required"):
        await store.record_held_intent_decision(**decision)


async def test_audit_failure_rolls_back_decision_batch_and_invocation_atomically(db, monkeypatch):
    owner, store, *_ = db
    _, decision = await pending(db, monkeypatch)

    async def broken(*args, **kwargs):
        raise ValueError("synthetic audit failure")

    monkeypatch.setattr(store, "_insert_audit_event", broken)
    with pytest.raises(ValueError, match="synthetic audit"):
        await store.record_held_intent_decision(**decision)
    assert await owner.fetchval(
        "SELECT count(*) FROM approval_decisions WHERE run_id=$1", decision["run_id"],
    ) == 0
    assert await owner.fetchval(
        "SELECT status FROM approval_batches WHERE run_id=$1", decision["run_id"],
    ) == "pending"
    assert await owner.fetchval(
        "SELECT status FROM tool_invocations WHERE run_id=$1", decision["run_id"],
    ) == "awaiting_approval"
