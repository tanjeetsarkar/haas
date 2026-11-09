"""Phase approval and deadline proof with held admission and a mock provider."""

import json
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from app.worker import tasks
from test_phase2_database import DSN, db as db, service_store
from test_run_phase_ledger import staged

pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


async def pause_at_approval(owner, store, slug, monkeypatch):
    run_id, lease = await staged(owner, store, slug, success=1, step_count=1)
    schemas = [{"type": "function", "function": {"name": "generic_fake"}}]
    monkeypatch.setattr(tasks.registry, "resolve_sorted_tool_schemas", lambda *_: schemas)
    monkeypatch.setattr(tasks, "estimate_step_cost", lambda *args: Decimal("0.01"))
    real_build = tasks.build_invocation

    def approval_invocation(*args, **kwargs):
        return {**real_build(*args, **kwargs), "requires_approval": True}

    monkeypatch.setattr(tasks, "build_invocation", approval_invocation)
    requests = []
    real_client = httpx.AsyncClient

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "approval-call", "type": "function",
                    "function": {"name": "generic_fake", "arguments": "{}"},
                }],
            }}],
            "usage": {"cost": 0.005},
        })

    monkeypatch.setattr(
        tasks.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handle))
    )
    assert (await tasks._step(store, lease, await store.get_run(run_id)))["status"] == "approval"
    batch = await store.pending_approval_batch(run_id)
    assert batch is not None
    return run_id, batch, requests


async def decide(store, slug, principal, batch):
    return await store.decide_approval(
        slug, run_id=None, batch_id=batch["batch_id"], approved=True,
        reason=None, actor_principal_id=str(principal), require_binding=True,
        batch_digest_value=batch["batch_digest"], request_id=str(uuid4()),
        idempotency_key=None,
    )


def durable_budget(run):
    return (
        run["phase_state"], run["step_count"], run["total_cost_usd"],
        run["tool_invocations_used"], run["max_steps"], run["max_cost_usd"],
        run["max_tool_invocations"], run["deadline_at"],
    )


async def test_approval_decision_and_released_lease_preserve_phase_and_budgets(db, monkeypatch):
    owner, store, slug, _, _, _, principal, _ = db
    run_id, batch, requests = await pause_at_approval(owner, store, slug, monkeypatch)
    paused = await store.get_run(run_id)
    assert paused["status"] == "input_required"
    assert paused["phase_state"].phase == "acquisition"
    assert not paused["phase_state"].finalizer_attempted
    before = durable_budget(paused)

    await decide(store, slug, principal, batch)
    queued = await store.get_run(run_id)
    assert queued["status"] == "queued" and durable_budget(queued) == before
    async with service_store(owner) as service:
        acquired = await service.acquire_lease(
            message_id=str(uuid4()), run_id=run_id, action="decision",
            expected_state_version=queued["state_version"], owner="approval-recovery",
        )
        assert acquired is not None
        lease, resumed = acquired
        assert resumed["phase_config_json"] is not None
        assert resumed["phase_state_json"] is not None
        assert durable_budget(await store.get_run(run_id)) == before
        assert await tasks._decision(service, lease, resumed) is None
    dispatched = await store.get_run(run_id)
    assert dispatched["phase_state"] == paused["phase_state"]
    assert dispatched["step_count"] == paused["step_count"]
    assert dispatched["total_cost_usd"] == paused["total_cost_usd"]
    assert dispatched["deadline_at"] == paused["deadline_at"]
    assert dispatched["tool_invocations_used"] == paused["tool_invocations_used"] + 1
    assert len(requests) == 1
    assert await owner.fetchval(
        "SELECT status FROM tool_invocations WHERE run_id=$1", run_id,
    ) == "dispatched"


@pytest.mark.parametrize("decision_first", [False, True])
async def test_deadline_reconciliation_during_pause_or_queued_resume(
    db, monkeypatch, decision_first,
):
    owner, store, slug, _, _, _, principal, _ = db
    run_id, batch, requests = await pause_at_approval(owner, store, slug, monkeypatch)
    paused = await store.get_run(run_id)
    if decision_first:
        await decide(store, slug, principal, batch)
    await owner.execute(
        "UPDATE runs SET deadline_at=now()-interval '1 second' WHERE run_id=$1", run_id,
    )
    async with service_store(owner) as service:
        await service.reconcile()
    failed = await store.get_run(run_id)
    assert failed["status"] == "failed"
    assert failed["phase_state"] == paused["phase_state"]
    assert failed["step_count"] == paused["step_count"]
    assert failed["total_cost_usd"] == paused["total_cost_usd"]
    assert failed["tool_invocations_used"] == paused["tool_invocations_used"]
    assert len(requests) == 1
    assert await owner.fetchval(
        "SELECT count(*) FROM tool_invocations WHERE run_id=$1 AND status='dispatched'", run_id,
    ) == 0


async def test_expired_paused_approval_resume_cannot_acquire_a_dispatch_lease(db, monkeypatch):
    owner, store, slug, _, _, _, principal, _ = db
    run_id, batch, requests = await pause_at_approval(owner, store, slug, monkeypatch)
    paused = await store.get_run(run_id)
    await owner.execute(
        "UPDATE runs SET deadline_at=now()-interval '1 second' WHERE run_id=$1", run_id,
    )
    await decide(store, slug, principal, batch)
    queued = await store.get_run(run_id)
    async with service_store(owner) as service:
        acquired = await service.acquire_lease(
            message_id=str(uuid4()), run_id=run_id, action="decision",
            expected_state_version=queued["state_version"], owner="approval-recovery",
        )
    assert acquired is None
    failed = await store.get_run(run_id)
    assert failed["status"] == "failed"
    assert failed["phase_state"] == paused["phase_state"]
    assert failed["step_count"] == paused["step_count"]
    assert failed["tool_invocations_used"] == paused["tool_invocations_used"]
    assert len(requests) == 1
