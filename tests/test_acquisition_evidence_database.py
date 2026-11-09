"""Actual aggregate evidence tests, using disposable PostgreSQL only."""

import json
from uuid import uuid4

import pytest

from app.core.plugins import CORE_TOOL_NAMES
from app.core.workflow import Lease, LeaseLostError
from app.worker.tasks import _aggregate
from test_phase2_database import DSN, db as db
from test_run_phase_ledger import staged

pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


async def invocation(owner, slug, run, ordinal, name, status, outcome):
    ident = uuid4()
    await owner.execute(
        "INSERT INTO tool_invocations (invocation_id,tenant_id,run_id,step_number,ordinal,"
        "tool_call_id,tool_name,queue,arguments,arguments_digest,effect_key,status,"
        "lease_epoch,outcome) VALUES ($1,$2,$3,1,$4,$5,$6,'q_tools_fast','{}',$7,$8,$9,1,$10::jsonb)",
        ident, slug, run, ordinal, str(ident), name, "a" * 64, uuid4().hex * 2,
        status, json.dumps(outcome) if outcome is not None else None,
    )
    return ident


async def pending(owner, run_id):
    await owner.execute(
        "UPDATE runs SET next_action='aggregate' WHERE run_id=$1", run_id
    )


async def test_worker_aggregate_counts_only_success_and_control_excluded_across_restart(db):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug, step_count=1)
    await pending(owner, run_id)
    for ordinal, name in enumerate(sorted(CORE_TOOL_NAMES)):
        await invocation(owner, slug, run_id, ordinal, name, "succeeded", {
            "is_error": False, "content": "control",
        })
    await invocation(owner, slug, run_id, len(CORE_TOOL_NAMES), "generic_fake", "succeeded", {
        "is_error": False, "content": "result",
    })
    await invocation(owner, slug, run_id, len(CORE_TOOL_NAMES) + 1, "generic_other", "failed", {
        "is_error": True, "content": "failure",
    })
    run = await store.get_run(run_id)
    stale = Lease(slug, run_id, run_id, lease.epoch, lease.version, lease.owner)
    before_messages = len(run["messages"])
    result = await _aggregate(store, lease, run)
    assert len(result["messages"]) == before_messages + len(CORE_TOOL_NAMES) + 2
    saved = await store.get_run(run_id)
    assert saved["phase_state"].successful_tool_outcomes == 1
    assert saved["phase_state"].state_version == 1
    with pytest.raises(LeaseLostError):
        await _aggregate(store, stale, await store.get_run(run_id))
    # Even a same-lease duplicate cannot append the checkpoint twice.
    with pytest.raises(ValueError, match="aggregate_not_pending"):
        await _aggregate(store, lease, await store.get_run(run_id))
    again = await store.get_run(run_id)
    assert again["phase_state"].successful_tool_outcomes == 1
    assert again["phase_state"].state_version == 1


@pytest.mark.parametrize("outcome", [
    None, {}, {"is_error": "false"}, {"is_error": False},
    {"is_error": False, "content": None}, {"is_error": False, "content": 7},
    {"is_error": False, "content": {"value": "invalid"}},
])
async def test_malformed_success_rolls_back_checkpoint_and_evidence(db, outcome):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug, step_count=1)
    await pending(owner, run_id)
    await invocation(owner, slug, run_id, 0, "generic_fake", "succeeded", outcome)
    before = await store.get_run(run_id)
    persisted_before = json.dumps(before["messages"])
    with pytest.raises(ValueError, match="invalid_outcome"):
        await _aggregate(store, lease, before)
    after = await store.get_run(run_id)
    assert after["phase_state"].successful_tool_outcomes == 0
    assert after["phase_state"].state_version == 0
    assert after["state_version"] == lease.version
    assert json.dumps(after["messages"]) == persisted_before
    assert "Tool outcome unknown:" not in json.dumps(after["messages"])


async def test_control_only_never_adds_evidence(db):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug, step_count=1)
    await pending(owner, run_id)
    for ordinal, name in enumerate(sorted(CORE_TOOL_NAMES)):
        await invocation(owner, slug, run_id, ordinal, name, "succeeded", {
            "is_error": False, "content": "control",
        })
    await _aggregate(store, lease, await store.get_run(run_id))
    assert (await store.get_run(run_id))["phase_state"].successful_tool_outcomes == 0


async def test_evidence_and_checkpoint_rollback_together(db, monkeypatch):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug, step_count=1)
    await pending(owner, run_id)
    await invocation(owner, slug, run_id, 0, "generic_fake", "succeeded", {
        "is_error": False, "content": "result",
    })
    before = await store.get_run(run_id)
    checkpoint = json.dumps(before["messages"])

    async def fail(*args, **kwargs):
        raise RuntimeError("forced aggregate rollback")

    monkeypatch.setattr(store, "_append_events", fail)
    with pytest.raises(RuntimeError, match="forced aggregate rollback"):
        await _aggregate(store, lease, before)
    after = await store.get_run(run_id)
    assert after["phase_state"].successful_tool_outcomes == 0
    assert after["phase_state"].state_version == 0
    assert json.dumps(after["messages"]) == checkpoint
    assert after["next_action"] == "aggregate"
