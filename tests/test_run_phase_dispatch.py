"""Isolated finalizer gateway preparation; not workflow or API activation."""

import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.worker import tasks
from app.core.llm import (
    FinalizerToolCallsError,
    OpenRouterGateway,
    call_tool_free_finalizer_once,
)
from test_phase2_database import DSN, db as db
from test_run_phase_ledger import staged


@pytest.mark.parametrize("with_calls", [False, True])
async def test_one_tool_free_request_and_rejected_response_retains_usage(with_calls):
    requests = []
    message = {"role": "assistant", "content": "result"}
    if with_calls:
        message["tool_calls"] = [{
            "id": "call-1", "type": "function",
            "function": {"name": "generic-tool", "arguments": "{}"},
        }]

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json={
                "choices": [{"message": message}],
                "usage": {"cost": 0.02, "prompt_tokens": 2, "completion_tokens": 1},
            },
        )

    gateway = OpenRouterGateway("unused-test-key", "https://provider.invalid")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        if with_calls:
            with pytest.raises(FinalizerToolCallsError) as exc:
                await call_tool_free_finalizer_once(
                    gateway, client, model="m", messages=[], traceparent="test",
                )
            result = exc.value.completion
            assert result.assistant_message["tool_calls"]
        else:
            result = await call_tool_free_finalizer_once(
                gateway, client, model="m", messages=[], traceparent="test",
            )
        assert result.cost_usd == 0.02
        assert result.prompt_tokens == 2
    assert len(requests) == 1
    assert "tools" not in requests[0]
    assert "tool_choice" not in requests[0]
    assert "stream" not in requests[0]


@pytest.mark.parametrize("status", [400, 404, 429, 503])
async def test_provider_error_propagates_without_fallback_or_retry(status):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(status, json={"error": "test"})

    gateway = OpenRouterGateway("unused-test-key", "https://provider.invalid")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await call_tool_free_finalizer_once(
                gateway, client, model="m", messages=[], traceparent="test",
            )
    assert len(requests) == 1
    assert requests[0]["model"] == "m"


async def test_transport_failure_has_no_automatic_retry():
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("unknown outcome", request=request)

    gateway = OpenRouterGateway("unused-test-key", "https://provider.invalid")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.ReadTimeout):
            await call_tool_free_finalizer_once(
                gateway, client, model="m", messages=[], traceparent="test",
            )
    assert count == 1


async def test_cancel_before_helper_runs_sends_no_request():
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json={"choices": []})

    gateway = OpenRouterGateway("unused-test-key", "https://provider.invalid")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        task = asyncio.create_task(call_tool_free_finalizer_once(
            gateway, client, model="m", messages=[], traceparent="test",
        ))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert count == 0


async def test_cancel_during_inflight_call_does_not_retry():
    entered = asyncio.Event()
    release = asyncio.Event()
    count = 0

    async def handle(request):
        nonlocal count
        count += 1
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"choices": []})

    gateway = OpenRouterGateway("unused-test-key", "https://provider.invalid")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        task = asyncio.create_task(call_tool_free_finalizer_once(
            gateway, client, model="m", messages=[], traceparent="test",
        ))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert count == 1


@pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")
async def test_actual_step_cannot_bypass_phase_hold_before_integration(db, monkeypatch):
    owner, store, slug, *_ = db
    from app.core.run_phases import PhaseConfig
    from test_phase2_database import admit, service_store
    from uuid import uuid4

    run_id = "phase-held-" + uuid4().hex
    await admit(store, slug, run_id, run_id, phase_config=PhaseConfig())
    model_call = AsyncMock()
    build = Mock()
    monkeypatch.setattr(tasks, "_call_model", model_call)
    monkeypatch.setattr(tasks, "build_invocation", build)
    async with service_store(owner) as service:
        assert await service.acquire_lease(
            message_id=uuid4().hex, run_id=run_id, action="step",
            expected_state_version=0, owner="worker",
        ) is None
    model_call.assert_not_called()
    build.assert_not_called()
    for table in ("run_steps", "usage_ledger", "tool_invocations", "outbox_messages"):
        assert await owner.fetchval(
            f"SELECT count(*) FROM {table} WHERE run_id=$1", run_id
        ) == 0


@pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")
@pytest.mark.parametrize("response", [
    "success", "tool", "timeout", "http400", "http429", "cancel_during", "cancel_after",
    "recover_during", "null_content", "empty_content", "whitespace_content",
])
async def test_actual_finalizer_step_claim_dispatch_and_settlement(db, monkeypatch, response):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug, success=1, step_count=2)
    run = await store.get_run(run_id)
    assert run is not None
    seen = []
    invocation = Mock()
    monkeypatch.setattr(tasks, "build_invocation", invocation)
    monkeypatch.setattr(tasks, "estimate_step_cost", lambda *args: Decimal("0.02"))
    real_client = httpx.AsyncClient
    settle = store.settle_bounded_finalizer

    async def cancel_after_result(*args, **kwargs):
        assert await store.request_cancel(run_id, slug) == "cancelled"
        return await settle(*args, **kwargs)

    if response == "cancel_after":
        monkeypatch.setattr(store, "settle_bounded_finalizer", cancel_after_result)

    async def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        state = json.loads(await owner.fetchval(
            "SELECT phase_state_json FROM runs WHERE run_id=$1", run_id
        ))
        assert state["phase"] == "finalization" and state["finalizer_attempted"]
        assert await owner.fetchval(
            "SELECT settlement_status FROM usage_ledger WHERE run_id=$1 AND source_attempt_key=$2",
            run_id, state["finalizer_attempt_key"],
        ) == "reserved"
        if response == "timeout":
            raise httpx.ReadTimeout("ambiguous", request=request)
        if response.startswith("http"):
            return httpx.Response(int(response[4:]), json={"error": "test"})
        if response == "cancel_during":
            assert await store.request_cancel(run_id, slug) == "cancelled"
        if response == "recover_during":
            await owner.execute(
                "UPDATE runs SET lease_epoch=2,state_version=state_version+1,lease_owner='new' "
                "WHERE run_id=$1", run_id,
            )
            from app.core.workflow import Lease
            restarted = Lease(slug, run_id, run_id, 2, lease.version + 1, "new")
            latest = await store.get_run(run_id)
            assert (await tasks._step(store, restarted, latest))["status"] == "failed"
        message = {"role": "assistant", "content": "result"}
        if response in ("null_content", "empty_content", "whitespace_content"):
            message["content"] = {
                "null_content": None, "empty_content": "", "whitespace_content": " \n\t",
            }[response]
        if response == "tool":
            message["tool_calls"] = [{
                "id": "test-call", "type": "function",
                "function": {"name": "generic-tool", "arguments": "{}"},
            }]
        return httpx.Response(200, json={
            "choices": [{"message": message}],
            "usage": {"cost": 0.015, "prompt_tokens": 2, "completion_tokens": 1},
        })

    monkeypatch.setattr(
        tasks.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handle))
    )
    result = await tasks._step(store, lease, run)
    expected = {"success": "completed", "tool": "failed", "timeout": "failed",
                "http400": "failed", "http429": "failed", "cancel_during": "cancelled",
                "cancel_after": "cancelled", "recover_during": "failed",
                "null_content": "failed", "empty_content": "failed",
                "whitespace_content": "failed"}[response]
    assert result["status"] == expected
    assert len(seen) == 1 and "tools" not in seen[0]
    invocation.assert_not_called()
    assert await owner.fetchval(
        "SELECT count(*) FROM tool_invocations WHERE run_id=$1", run_id
    ) == 0
    ledger = await owner.fetchrow(
        "SELECT settlement_status,cost_usd FROM usage_ledger "
        "WHERE run_id=$1 AND unit='model_usd'", run_id,
    )
    unknown = response in ("timeout", "http400", "http429")
    assert ledger["settlement_status"] == ("uncertain" if unknown else "settled")
    assert ledger["cost_usd"] == Decimal("0.02" if unknown else "0.015")
    row = await owner.fetchrow(
        "SELECT status,total_cost_usd,phase_state_json,result_text FROM runs WHERE run_id=$1",
        run_id,
    )
    assert row["status"] == expected
    if not unknown:
        assert Decimal(str(row["total_cost_usd"])) == Decimal("0.015")
    if unknown:
        assert json.loads(row["phase_state_json"])["failure_code"] == "finalizer_outcome_unknown"
    if response in ("null_content", "empty_content", "whitespace_content", "tool"):
        assert json.loads(row["phase_state_json"])["failure_code"] == "finalizer_failed"
        assert row["result_text"].startswith("Orchestrator step failed")
        assert await owner.fetchval(
            "SELECT count(*) FROM run_events WHERE run_id=$1 AND type='run_completed'",
            run_id,
        ) == 0


@pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")
async def test_actual_step_cancel_after_claim_does_not_undo_dispatch(db, monkeypatch):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug, success=1, step_count=2)
    run = await store.get_run(run_id)
    original = store.begin_bounded_phase_step

    async def cancel_after_claim(*args, **kwargs):
        ticket = await original(*args, **kwargs)
        await store.request_cancel(run_id, slug)
        return ticket

    monkeypatch.setattr(store, "begin_bounded_phase_step", cancel_after_claim)
    real_client = httpx.AsyncClient
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": {"cost": 0.015},
        })

    monkeypatch.setattr(
        tasks.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handle))
    )
    monkeypatch.setattr(tasks, "estimate_step_cost", lambda *args: Decimal("0.02"))
    assert (await tasks._step(store, lease, run))["status"] == "cancelled"
    assert len(calls) == 1
    assert await owner.fetchval("SELECT status FROM runs WHERE run_id=$1", run_id) == "cancelled"
    assert await owner.fetchval(
        "SELECT settlement_status FROM usage_ledger WHERE run_id=$1 AND unit='model_usd'",
        run_id,
    ) == "settled"


@pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")
async def test_cancel_during_client_setup_prevents_dispatch(db, monkeypatch):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug, success=1, step_count=2)
    run = await store.get_run(run_id)
    real_client = httpx.AsyncClient

    class CancelOnEnter(real_client):
        async def __aenter__(self):
            await store.request_cancel(run_id, slug)
            return await super().__aenter__()

    monkeypatch.setattr(tasks.httpx, "AsyncClient", CancelOnEnter)
    monkeypatch.setattr(tasks, "estimate_step_cost", lambda *args: Decimal("0.02"))
    call = AsyncMock()
    monkeypatch.setattr(tasks, "call_tool_free_finalizer_once", call)
    from app.core.workflow import LeaseLostError
    with pytest.raises(LeaseLostError):
        await tasks._step(store, lease, run)
    call.assert_not_called()
    assert await owner.fetchval("SELECT status FROM runs WHERE run_id=$1", run_id) == "cancelled"


@pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")
@pytest.mark.parametrize("winner", ["cancel", "dispatch"])
async def test_cancel_and_dispatch_serialize_on_real_run_lock(db, monkeypatch, winner):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug, success=1, step_count=2)
    run = await store.get_run(run_id)
    first_locked, second_started, release_lock = (asyncio.Event() for _ in range(3))
    provider_entered, release_provider = asyncio.Event(), asyncio.Event()
    original_lock = store._lock_run
    real_client = httpx.AsyncClient
    calls = []
    dispatch_locks = 0

    async def lock_with_barrier(conn, *args, **kwargs):
        nonlocal dispatch_locks
        task = asyncio.current_task()
        name = task.get_name() if task else ""
        if name == "dispatch":
            dispatch_locks += 1
        # Dispatch now reads an earlier quota preflight; the linearization
        # test must block its second lock, the actual finalizer claim.
        at_claim = name != "dispatch" or dispatch_locks == 2
        if name == winner and at_claim:
            row = await original_lock(conn, *args, **kwargs)
            first_locked.set()
            await release_lock.wait()
            return row
        if name == loser and (at_claim or winner == "cancel"):
            second_started.set()
        return await original_lock(conn, *args, **kwargs)

    async def handle(request):
        calls.append(request)
        provider_entered.set()
        await release_provider.wait()
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": {"cost": 0.015},
        })

    monkeypatch.setattr(store, "_lock_run", lock_with_barrier)
    monkeypatch.setattr(tasks, "estimate_step_cost", lambda *args: Decimal("0.02"))
    monkeypatch.setattr(
        tasks.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handle))
    )
    from app.core.workflow import LeaseLostError
    operations = {
        "cancel": lambda: store.request_cancel(run_id, slug),
        "dispatch": lambda: tasks._step(store, lease, run),
    }
    loser = "dispatch" if winner == "cancel" else "cancel"
    first = asyncio.create_task(operations[winner](), name=winner)
    second = None
    try:
        await asyncio.wait_for(first_locked.wait(), timeout=3)
        second = asyncio.create_task(operations[loser](), name=loser)
        await asyncio.wait_for(second_started.wait(), timeout=3)
        await asyncio.sleep(0.05)
        assert not second.done()
        release_lock.set()
        if winner == "cancel":
            assert await asyncio.wait_for(first, timeout=3) == "cancelled"
            with pytest.raises(LeaseLostError):
                await asyncio.wait_for(second, timeout=3)
            assert not calls
            assert await owner.fetchval(
                "SELECT count(*) FROM usage_ledger WHERE run_id=$1", run_id,
            ) == 0
        else:
            assert await asyncio.wait_for(second, timeout=3) == "cancelled"
            await asyncio.wait_for(provider_entered.wait(), timeout=3)
            assert await owner.fetchval(
                "SELECT settlement_status FROM usage_ledger WHERE run_id=$1 AND unit='model_usd'",
                run_id,
            ) == "uncertain"
            release_provider.set()
            assert (await asyncio.wait_for(first, timeout=3))["status"] == "cancelled"
            assert len(calls) == 1
            assert await owner.fetchval(
                "SELECT settlement_status FROM usage_ledger WHERE run_id=$1 AND unit='model_usd'",
                run_id,
            ) == "settled"
        assert await owner.fetchval(
            "SELECT status FROM runs WHERE run_id=$1", run_id,
        ) == "cancelled"
    finally:
        release_lock.set()
        release_provider.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")
async def test_actual_step_redelivery_with_started_attempt_never_calls_model(db, monkeypatch):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug, success=1, step_count=2)
    ticket = await store.begin_bounded_phase_step(
        lease, step=3, model="model-a", estimate_usd=Decimal("0.02"),
        finalization=True, acquisition_quota_exhausted=True,
    )
    assert ticket is not None
    run = await store.get_run(run_id)
    call = AsyncMock()
    monkeypatch.setattr(tasks, "call_tool_free_finalizer_once", call)
    monkeypatch.setattr(tasks, "estimate_step_cost", lambda *args: Decimal("0.02"))
    assert (await tasks._step(store, lease, run))["status"] == "failed"
    call.assert_not_called()
    assert await owner.fetchval(
        "SELECT settlement_status FROM usage_ledger WHERE run_id=$1", run_id
    ) == "uncertain"
    assert await owner.fetchval(
        "SELECT count(*) FROM run_steps WHERE run_id=$1", run_id
    ) == 1
