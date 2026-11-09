"""Deterministic branch and actual pending approval persistence, never dispatch."""

import json
import copy
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core import deterministic_intent
from app.core.envelope import verify_envelope
from app.core.workflow import Lease, LeaseLostError, batch_digest
from app.worker import tasks
from test_held_native_agent import native_setup
from test_phase2_database import DSN, db as db

pg = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


async def test_worker_intent_branches_before_model_selection_or_run_field_access(monkeypatch):
    from app.core.run_restrictions import ExactIntent, IntentContext, RunAuthority

    def forbidden(*args, **kwargs):
        pytest.fail("intent reached model selection")

    monkeypatch.setattr(tasks.gateway, "select_model_for_step", forbidden)
    authority = RunAuthority(tools=("act",), intent=ExactIntent(
        tool_name="act", arguments_json="{}", context=IntentContext(
            tenant_id=str(uuid4()), submitter_id=str(uuid4()), run_id="run",
            policy_revision=str(uuid4()), registry_fingerprint="r",
            plugin_id="fake", plugin_digest="a" * 64, destination="native",
        ),
    ))
    store = AsyncMock()
    store.prepare_intent_approval.return_value = {"status": "input_required"}
    lease = Lease("t", "run", "thread", 1, 0, "worker")
    assert await tasks._step(store, lease, {
        "authority_json": authority.model_dump_json(),
    }) == {"status": "input_required"}
    store.prepare_intent_approval.assert_awaited_once_with(lease)
    store.begin_step.assert_not_called()


def test_versioned_intent_digest_binds_proof_without_changing_legacy_digest():
    call = {"id": "call", "function": {"name": "act", "arguments": "{}"},
            "intent_proof": {"version": 1, "intent_binding": "a" * 64, "envelope": {"signed": "proof"}}}
    original = deterministic_intent.intent_batch_digest("run", 1, [call])
    altered = copy.deepcopy(call)
    altered["intent_proof"]["intent_binding"] = "b" * 64
    assert deterministic_intent.intent_batch_digest("run", 1, [altered]) != original
    assert batch_digest("run", 1, [altered]) == batch_digest("run", 1, [call])
    altered["intent_proof"]["version"] = 2
    with pytest.raises(ValueError):
        deterministic_intent.intent_batch_digest("run", 1, [altered])


async def running(db, monkeypatch, kind="native"):
    owner, store, slug, *_ = db
    if kind == "agent":
        from test_held_native_agent import agent_setup
        snapshot, fields, registry = await agent_setup(db, monkeypatch)
    elif kind == "endpoint":
        from test_held_intent_creation import setup
        snapshot, fields, registry = await setup(db, monkeypatch)
    else:
        snapshot, fields, registry = await native_setup(db, monkeypatch)
    monkeypatch.setattr(deterministic_intent, "registry", registry)
    await store.create_held_intent_run(snapshot, **fields)
    # Explicit fixture lease: production acquire_lease still refuses authority.
    await owner.execute(
        "UPDATE runs SET status='running',lease_epoch=1,lease_owner='fixture',"
        "lease_expires_at=now()+interval '60 seconds' WHERE run_id=$1", fields["run_id"],
    )
    return fields, registry, Lease(slug, fields["run_id"], fields["thread_id"], 1, 0, "fixture")


@pg
@pytest.mark.parametrize("kind", ["native", "endpoint", "agent"])
async def test_real_native_intent_has_one_mandatory_bound_approval_and_zero_model_work(db, monkeypatch, kind):
    owner, store, slug, *_ = db
    fields, registry, lease = await running(db, monkeypatch, kind)
    assert not registry.tools["act"].requires_approval
    result = await tasks._step(store, lease, await store.get_run(fields["run_id"]))
    assert result["status"] == "input_required"
    saved = await store.get_run(lease.run_id)
    assert saved["status"] == "input_required" and saved["next_action"] == "none"
    batch = await owner.fetchrow("SELECT * FROM approval_batches WHERE run_id=$1", lease.run_id)
    pending = json.loads(batch["tool_calls"])
    assert len(pending) == 1 and pending[0]["requires_approval"] is True
    body = verify_envelope(pending[0]["intent_proof"]["envelope"])
    intent = saved["authority"].intent
    assert body["intent_binding"] == intent.binding == pending[0]["intent_proof"]["intent_binding"]
    assert body["tool_call"]["arguments"] == intent.arguments_json
    calls = saved["pending_tool_calls"]
    assert deterministic_intent.intent_batch_digest(lease.run_id, 1, calls) == batch["batch_digest"]
    assert batch_digest(lease.run_id, 1, calls) != batch["batch_digest"]
    invocation = await owner.fetchrow("SELECT * FROM tool_invocations WHERE run_id=$1", lease.run_id)
    assert invocation["status"] == "awaiting_approval"
    assert invocation["arguments"] == intent.arguments_json
    for table in ("usage_ledger", "run_steps", "outbox_messages", "approval_decisions"):
        assert await owner.fetchval(f"SELECT count(*) FROM {table} WHERE run_id=$1", lease.run_id) == 0
    with pytest.raises(LeaseLostError):
        await store.prepare_intent_approval(lease)
    assert await owner.fetchval(
        "SELECT count(*) FROM tool_invocations WHERE run_id=$1", lease.run_id,
    ) == 1
    from app.core.store import StoreConflictError
    with pytest.raises(StoreConflictError, match="restriction.resume_unavailable"):
        await store.decide_approval(
            slug, run_id=lease.run_id, batch_id=str(batch["batch_id"]),
            batch_digest_value=batch["batch_digest"], approved=True, reason=None,
            actor_principal_id=None, request_id=str(uuid4()), idempotency_key=None, require_binding=True,
        )


@pg
@pytest.mark.parametrize("change", ["plugin", "schema", "target", "ceiling", "policy", "deadline", "cancelled"])
async def test_stale_persisted_intent_never_prepares_approval(db, monkeypatch, change):
    owner, store, _, _, tenant, _, _, old_policy = db
    fields, registry, lease = await running(db, monkeypatch)
    if change == "plugin":
        registry.plugins["fake"].digest = "c" * 64
    elif change == "schema":
        registry.tools["act"].args_schema.model_fields["recipient"].description = "changed"
        registry.tools["act"].args_schema.model_rebuild(force=True)
    elif change == "target":
        registry.tools["act"].queue = "q_tools_sandbox"
    elif change == "ceiling":
        await owner.execute("UPDATE runs SET active_skills_json='[]' WHERE run_id=$1", lease.run_id)
    elif change == "policy":
        await owner.execute(
            "UPDATE tenants SET active_policy_revision_id=$2 WHERE tenant_id=$1", tenant, old_policy,
        )
    elif change == "deadline":
        await owner.execute("UPDATE runs SET deadline_at=now()-interval '1 second' WHERE run_id=$1", lease.run_id)
    else:
        await owner.execute("UPDATE runs SET status='cancelled' WHERE run_id=$1", lease.run_id)
    with pytest.raises((ValueError, LeaseLostError)):
        await store.prepare_intent_approval(lease)
    for table in ("tool_invocations", "approval_batches", "usage_ledger", "outbox_messages"):
        assert await owner.fetchval(f"SELECT count(*) FROM {table} WHERE run_id=$1", lease.run_id) == 0


@pg
async def test_approval_transaction_rollback_retains_stable_effect_identity(db, monkeypatch):
    owner, store, *_ = db
    _, _, lease = await running(db, monkeypatch)
    original = store._create_approval
    seen = []

    async def interrupted(conn, fx, lease, row, step, calls, invocations):
        seen.append((invocations[0]["invocation_id"], invocations[0]["effect_key"]))
        await original(conn, fx, lease, row, step, calls, invocations)
        raise ValueError("synthetic transaction interruption")

    monkeypatch.setattr(store, "_create_approval", interrupted)
    with pytest.raises(ValueError, match="synthetic"):
        await store.prepare_intent_approval(lease)
    assert await owner.fetchval(
        "SELECT count(*) FROM approval_batches WHERE run_id=$1", lease.run_id,
    ) == 0
    monkeypatch.setattr(store, "_create_approval", original)
    await store.prepare_intent_approval(lease)
    invocation = await owner.fetchrow(
        "SELECT invocation_id,effect_key FROM tool_invocations WHERE run_id=$1", lease.run_id,
    )
    assert (str(invocation["invocation_id"]), invocation["effect_key"]) == seen[0]
