"""Quota-triggered finalization uses real fenced rows and a mock provider."""

import json
from decimal import Decimal

import httpx
import pytest

from app.core.plugins import CORE_TOOL_NAMES
from app.core.workflow import Lease
from app.worker import tasks
from test_acquisition_evidence_database import invocation, pending
from test_phase2_database import DSN, db as db, service_store
from test_run_phase_ledger import staged

pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


@pytest.mark.parametrize("quota", ["steps", "invocations", "cost"])
async def test_real_worker_quota_routes_prior_evidence_to_one_finalizer(db, monkeypatch, quota):
    owner, store, slug, *_ = db
    step_count = 2 if quota == "steps" else 1
    run_id, lease = await staged(owner, store, slug, step_count=step_count)
    await pending(owner, run_id)
    await invocation(owner, slug, run_id, 0, "generic_fake", "succeeded", {
        "is_error": False, "content": "result",
    })
    # Actual aggregate persists evidence; no forged phase counter.
    await tasks._aggregate(store, lease, await store.get_run(run_id))
    if quota == "invocations":
        await owner.execute(
            "UPDATE runs SET tool_invocations_used=max_tool_invocations WHERE run_id=$1", run_id
        )
    if quota == "cost":
        await owner.execute("UPDATE runs SET total_cost_usd=0.2 WHERE run_id=$1", run_id)

    schemas = [{"type": "function", "function": {"name": "generic_fake"}}]
    monkeypatch.setattr(tasks.registry, "resolve_sorted_tool_schemas", lambda *_: schemas)
    monkeypatch.setattr(
        tasks, "estimate_step_cost",
        lambda _model, _messages, tools: (
            Decimal("0.04000000000000000000000000001") if tools else Decimal("0.02")
        ),
    )
    requests = []
    real_client = httpx.AsyncClient

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "final"}}],
            "usage": {"cost": 0.015},
        })

    monkeypatch.setattr(
        tasks.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handle))
    )
    result = await tasks._step(store, lease, await store.get_run(run_id))
    assert result["status"] == "completed"
    assert len(requests) == 1 and "tools" not in requests[0]
    state = (await store.get_run(run_id))["phase_state"]
    assert state.phase == "completed" and state.successful_tool_outcomes == 1
    assert await owner.fetchval(
        "SELECT count(*) FROM run_steps WHERE run_id=$1", run_id
    ) == 1


async def test_control_only_at_step_limit_fails_before_model_call(db, monkeypatch):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug, step_count=2)
    await pending(owner, run_id)
    for ordinal, name in enumerate(sorted(CORE_TOOL_NAMES)):
        await invocation(owner, slug, run_id, ordinal, name, "succeeded", {
            "is_error": False, "content": "control",
        })
    await tasks._aggregate(store, lease, await store.get_run(run_id))
    monkeypatch.setattr(tasks, "estimate_step_cost", lambda *args: Decimal("0.02"))
    calls = []

    def fail_if_called(*args, **kwargs):
        calls.append(True)
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(tasks.httpx, "AsyncClient", fail_if_called)
    result = await tasks._step(store, lease, await store.get_run(run_id))
    assert result["status"] == "failed"
    run = await store.get_run(run_id)
    assert run["phase_state"].failure_code == "no_successful_acquisition_result"
    assert run["phase_state"].successful_tool_outcomes == 0
    assert not calls
    assert await owner.fetchval("SELECT count(*) FROM run_steps WHERE run_id=$1", run_id) == 0


@pytest.mark.parametrize("max_steps,max_cost,max_calls", [(6, "0.15", 12), (5, "0.10", 8)])
@pytest.mark.parametrize("response", ["no_tool", "over_batch"])
async def test_real_worker_evidence_to_finalizer(
    db, monkeypatch, max_steps, max_cost, max_calls, response,
):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug, step_count=1)
    await owner.execute(
        "UPDATE runs SET max_steps=$2,max_cost_usd=$3,max_tool_invocations=$4,"
        "tool_invocations_used=$5 WHERE run_id=$1",
        run_id, max_steps, float(max_cost), max_calls, max_calls - 1 if response == "over_batch" else 1,
    )
    await pending(owner, run_id)
    await invocation(owner, slug, run_id, 0, "generic_fake", "succeeded", {
        "is_error": False, "content": "acquired",
    })
    await tasks._aggregate(store, lease, await store.get_run(run_id))
    schemas = [{"type": "function", "function": {"name": "generic_fake"}}]
    monkeypatch.setattr(tasks.registry, "resolve_sorted_tool_schemas", lambda *_: schemas)
    monkeypatch.setattr(tasks, "estimate_step_cost", lambda *args: Decimal("0.01"))
    requests = []
    real_client = httpx.AsyncClient

    def handle(request):
        requests.append(json.loads(request.content))
        message = {"role": "assistant", "content": "partial"}
        if len(requests) == 1 and response == "over_batch":
            message["tool_calls"] = [
                {"id": f"call-{i}", "type": "function",
                 "function": {"name": "generic_fake", "arguments": "{}"}}
                for i in range(2)
            ]
        if len(requests) == 2:
            message["content"] = "final"
        return httpx.Response(200, json={
            "choices": [{"message": message}], "usage": {"cost": 0.005},
        })

    monkeypatch.setattr(
        tasks.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handle))
    )
    assert (await tasks._step(store, lease, await store.get_run(run_id)))["status"] == "completed"
    assert len(requests) == 2
    assert "tools" in requests[0] and "tools" not in requests[1]
    saved = await store.get_run(run_id)
    assert saved["phase_state"].successful_tool_outcomes == 1
    assert saved["step_count"] == 3 <= max_steps
    assert saved["max_steps"] == max_steps
    assert Decimal(str(saved["max_cost_usd"])) == Decimal(max_cost)
    assert saved["max_tool_invocations"] == max_calls
    assert saved["tool_invocations_used"] == (max_calls - 1 if response == "over_batch" else 1)
    assert await owner.fetchval(
        "SELECT count(*) FROM tool_invocations WHERE run_id=$1", run_id
    ) == 1


@pytest.mark.parametrize("max_steps,max_cost,max_calls", [(6, "0.15", 12), (5, "0.10", 8)])
async def test_real_worker_source_outcome_no_tool_and_finalizer(
    db, monkeypatch, max_steps, max_cost, max_calls,
):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug)
    await owner.execute(
        "UPDATE runs SET max_steps=$2,max_cost_usd=$3,max_tool_invocations=$4 WHERE run_id=$1",
        run_id, max_steps, float(max_cost), max_calls,
    )
    schemas = [{"type": "function", "function": {"name": "generic_fake"}}]
    monkeypatch.setattr(tasks.registry, "resolve_sorted_tool_schemas", lambda *_: schemas)
    monkeypatch.setattr(tasks, "estimate_step_cost", lambda *args: Decimal("0.01"))
    requests = []
    real_client = httpx.AsyncClient

    def handle(request):
        requests.append(json.loads(request.content))
        message = {"role": "assistant", "content": "response"}
        if len(requests) == 1:
            message["tool_calls"] = [{
                "id": "source-call", "type": "function",
                "function": {"name": "generic_fake", "arguments": "{}"},
            }]
        return httpx.Response(200, json={
            "choices": [{"message": message}], "usage": {"cost": 0.005},
        })

    monkeypatch.setattr(
        tasks.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handle))
    )
    first = await tasks._step(store, lease, await store.get_run(run_id))
    assert first["status"] == "dispatch"
    invocation_id = await owner.fetchval(
        "SELECT invocation_id FROM tool_invocations WHERE run_id=$1", run_id
    )
    async with service_store(owner) as service:
        assert await service.record_tool_outcome(
            run_id=run_id, invocation_id=str(invocation_id), status="succeeded",
            outcome={"is_error": False, "content": "source result"},
        )
    # Test-only lease acquisition: public and scheduling holds remain on.
    row = await owner.fetchrow(
        "UPDATE runs SET status='running',lease_epoch=lease_epoch+1,"
        "state_version=state_version+1,lease_owner='restarted',"
        "lease_expires_at=now()+interval '1 minute' WHERE run_id=$1 "
        "RETURNING lease_epoch,state_version",
        run_id,
    )
    lease = Lease(slug, run_id, run_id, row["lease_epoch"], row["state_version"], "restarted")
    await tasks._aggregate(store, lease, await store.get_run(run_id))
    assert (await store.get_run(run_id))["phase_state"].successful_tool_outcomes == 1
    assert (await tasks._step(store, lease, await store.get_run(run_id)))["status"] == "completed"
    assert len(requests) == 3
    assert "tools" in requests[0] and "tools" in requests[1] and "tools" not in requests[2]
    saved = await store.get_run(run_id)
    assert saved["step_count"] == 3 and saved["tool_invocations_used"] == 1
    assert saved["max_steps"] == max_steps and saved["max_tool_invocations"] == max_calls
    assert Decimal(str(saved["max_cost_usd"])) == Decimal(max_cost)
    assert saved["phase_state"].finalizer_attempted


@pytest.mark.parametrize("max_cost,spent", [("0.15", "0.10"), ("0.10", "0.06")])
async def test_exact_cost_reservation_routes_before_acquisition_call(db, monkeypatch, max_cost, spent):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug, success=1, step_count=1, total=Decimal(spent))
    await owner.execute(
        "UPDATE runs SET max_cost_usd=$2 WHERE run_id=$1", run_id, float(max_cost),
    )
    schemas = [{"type": "function", "function": {"name": "generic_fake"}}]
    monkeypatch.setattr(tasks.registry, "resolve_sorted_tool_schemas", lambda *_: schemas)
    monkeypatch.setattr(
        tasks, "estimate_step_cost",
        lambda _model, _messages, tools: (
            Decimal("0.02000000000000000000000000001") if tools else Decimal("0.01")
        ),
    )
    requests = []
    real_client = httpx.AsyncClient

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "final"}}],
            "usage": {"cost": 0.005},
        })

    monkeypatch.setattr(
        tasks.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handle))
    )
    assert (await tasks._step(store, lease, await store.get_run(run_id)))["status"] == "completed"
    assert len(requests) == 1 and "tools" not in requests[0]


async def test_restart_after_no_tool_commit_uses_durable_completed_step(db, monkeypatch):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug, success=1, step_count=1)
    schemas = [{"type": "function", "function": {"name": "generic_fake"}}]
    monkeypatch.setattr(tasks.registry, "resolve_sorted_tool_schemas", lambda *_: schemas)
    monkeypatch.setattr(tasks, "estimate_step_cost", lambda *args: Decimal("0.01"))
    requests = []
    real_client = httpx.AsyncClient
    original = tasks.commit_acquisition_response

    async def crash_after_commit(*args, **kwargs):
        assert await original(*args, **kwargs) == "finalize"
        raise RuntimeError("simulated worker crash")

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "response"}}],
            "usage": {"cost": 0.005},
        })

    monkeypatch.setattr(tasks, "commit_acquisition_response", crash_after_commit)
    monkeypatch.setattr(
        tasks.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handle))
    )
    with pytest.raises(RuntimeError, match="simulated worker crash"):
        await tasks._step(store, lease, await store.get_run(run_id))
    saved = await store.get_run(run_id)
    assert saved["step_count"] == 2 and not saved["phase_state"].finalizer_attempted
    row = await owner.fetchrow(
        "UPDATE runs SET lease_epoch=lease_epoch+1,state_version=state_version+1,"
        "lease_owner='restarted',lease_expires_at=now()+interval '1 minute' "
        "WHERE run_id=$1 RETURNING lease_epoch,state_version",
        run_id,
    )
    restarted = Lease(slug, run_id, run_id, row["lease_epoch"], row["state_version"], "restarted")
    assert (await tasks._step(store, restarted, await store.get_run(run_id)))["status"] == "completed"
    assert len(requests) == 2 and "tools" not in requests[1]


@pytest.mark.parametrize("response", ["no_tool", "over_batch"])
async def test_no_evidence_fails_without_finalizer_or_partial_batch(db, monkeypatch, response):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug)
    await owner.execute(
        "UPDATE runs SET max_tool_invocations=1 WHERE run_id=$1", run_id,
    )
    schemas = [{"type": "function", "function": {"name": "generic_fake"}}]
    monkeypatch.setattr(tasks.registry, "resolve_sorted_tool_schemas", lambda *_: schemas)
    monkeypatch.setattr(tasks, "estimate_step_cost", lambda *args: Decimal("0.01"))
    requests = []
    real_client = httpx.AsyncClient

    def handle(request):
        requests.append(json.loads(request.content))
        message = {"role": "assistant", "content": "response"}
        if response == "over_batch":
            message["tool_calls"] = [
                {"id": f"call-{i}", "type": "function",
                 "function": {"name": "generic_fake", "arguments": "{}"}}
                for i in range(2)
            ]
        return httpx.Response(200, json={
            "choices": [{"message": message}], "usage": {"cost": 0.005},
        })

    monkeypatch.setattr(
        tasks.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handle))
    )
    assert (await tasks._step(store, lease, await store.get_run(run_id)))["status"] == "failed"
    saved = await store.get_run(run_id)
    assert saved["phase_state"].failure_code == "no_successful_acquisition_result"
    assert len(requests) == 1
    assert await owner.fetchval(
        "SELECT count(*) FROM tool_invocations WHERE run_id=$1", run_id,
    ) == 0
    assert Decimal(str(saved["total_cost_usd"])) == Decimal("0.005")


async def test_expired_deadline_fails_before_acquisition_model_call(db, monkeypatch):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug, success=1)
    await owner.execute(
        "UPDATE runs SET deadline_at=now()-interval '1 second' WHERE run_id=$1", run_id,
    )
    monkeypatch.setattr(tasks, "estimate_step_cost", lambda *args: Decimal("0.01"))
    calls = []
    real_client = httpx.AsyncClient

    def handle(request):
        calls.append(request)
        raise AssertionError("expired deadline must not reach provider")

    monkeypatch.setattr(
        tasks.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handle))
    )
    assert (await tasks._step(store, lease, await store.get_run(run_id)))["status"] == "failed"
    saved = await store.get_run(run_id)
    assert saved["status"] == "failed"
    assert saved["phase_state"].failure_code == "insufficient_budget"
    assert not calls


async def test_acquisition_network_failure_has_no_automatic_paid_retry(db, monkeypatch):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug)
    monkeypatch.setattr(tasks, "estimate_step_cost", lambda *args: Decimal("0.01"))
    calls = []
    real_client = httpx.AsyncClient

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("unknown", request=request)

    monkeypatch.setattr(
        tasks.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handle))
    )
    assert (await tasks._step(store, lease, await store.get_run(run_id)))["status"] == "failed"
    assert len(calls) == 1
    assert await owner.fetchval(
        "SELECT settlement_status FROM usage_ledger WHERE run_id=$1 AND unit='model_usd'", run_id,
    ) == "uncertain"
