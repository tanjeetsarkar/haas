"""Regressions for the reviewed bounded-worker quota findings."""

import asyncio
import json
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from app.core.workflow import LeaseLostError
from app.worker import tasks
from test_phase2_database import DSN, db as db
from test_run_phase_ledger import staged

pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


@pytest.mark.parametrize("success", [0, 1])
async def test_exact_acquisition_ceiling_exhausted_even_for_zero_estimate(db, monkeypatch, success):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug, success=success, total=Decimal("0.24"))
    monkeypatch.setattr(tasks, "estimate_step_cost", lambda *args: Decimal(0))
    monkeypatch.setattr(
        tasks.registry, "resolve_sorted_tool_schemas",
        lambda *_: [{"type": "function", "function": {"name": "generic_fake"}}],
    )
    calls = []
    real_client = httpx.AsyncClient

    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "final"}}],
            "usage": {"cost": 0},
        })

    monkeypatch.setattr(
        tasks.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handle))
    )
    result = await tasks._step(store, lease, await store.get_run(run_id))
    assert result["status"] == ("completed" if success else "failed")
    assert len(calls) == success
    assert all("tools" not in request for request in calls)
    state = (await store.get_run(run_id))["phase_state"]
    assert state.phase == ("completed" if success else "failed")
    if not success:
        assert state.failure_code == "no_successful_acquisition_result"


@pytest.mark.parametrize("component", ["reserved", "uncertain"])
async def test_atomic_claim_counts_precise_existing_cost_at_zero_estimate(db, component):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug, total=Decimal("0.20"))
    if component == "reserved":
        await owner.execute(
            "UPDATE runs SET budget_reserved_usd=$2 WHERE run_id=$1", run_id, Decimal("0.04"),
        )
    else:
        await owner.execute(
            "INSERT INTO usage_ledger (entry_id,tenant_id,run_id,source_attempt_key,unit,"
            "quantity,cost_usd,settlement_status,pricing_revision) "
            "VALUES ($1,$2,$3,$4,'model_usd',$5,$5,'uncertain','test')",
            uuid4(), slug, run_id, f"{run_id}:old", Decimal("0.04"),
        )
    assert await store.begin_bounded_phase_step(
        lease, step=1, model="model-a", estimate_usd=Decimal(0), finalization=False,
    ) is None
    assert await owner.fetchval(
        "SELECT count(*) FROM run_steps WHERE run_id=$1", run_id,
    ) == 0


async def test_zero_estimate_allowed_if_exact_acquisition_capacity_remains(db):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug, total=Decimal("0.20"))
    await owner.execute(
        "UPDATE runs SET budget_reserved_usd=$2 WHERE run_id=$1",
        run_id, Decimal("0.03999999999999999999999999999"),
    )
    ticket = await store.begin_bounded_phase_step(
        lease, step=1, model="model-a", estimate_usd=Decimal(0), finalization=False,
    )
    assert ticket is not None
    assert ticket.reserved_usd == 0


async def test_finalizer_estimate_over_reserve_is_durable_budget_failure(db, monkeypatch):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug, success=1, step_count=2)
    monkeypatch.setattr(tasks, "estimate_step_cost", lambda *args: Decimal("0.0600000001"))
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(tasks.gateway, "chat_completion", forbidden)
    result = await tasks._step(store, lease, await store.get_run(run_id))
    assert result["status"] == "failed"
    saved = await store.get_run(run_id)
    assert saved["status"] == "failed"
    assert saved["phase_state"].failure_code == "insufficient_budget"
    assert not calls
    assert await owner.fetchval(
        "SELECT count(*) FROM usage_ledger WHERE run_id=$1", run_id,
    ) == 0


async def test_acquisition_claim_commit_is_point_of_no_return_before_provider(db, monkeypatch):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug)
    original = store.begin_bounded_phase_step
    claimed, release = asyncio.Event(), asyncio.Event()
    calls = []
    real_client = httpx.AsyncClient

    async def hold_after_commit(*args, **kwargs):
        ticket = await original(*args, **kwargs)
        assert ticket is not None
        claimed.set()
        await release.wait()
        return ticket

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "response"}}],
            "usage": {"cost": 0.005},
        })

    monkeypatch.setattr(store, "begin_bounded_phase_step", hold_after_commit)
    monkeypatch.setattr(tasks, "estimate_step_cost", lambda *args: Decimal("0.01"))
    monkeypatch.setattr(
        tasks.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handle))
    )
    worker = asyncio.create_task(tasks._step(store, lease, await store.get_run(run_id)))
    try:
        await asyncio.wait_for(claimed.wait(), timeout=3)
        assert not calls
        assert await owner.fetchval(
            "SELECT settlement_status FROM usage_ledger WHERE run_id=$1", run_id,
        ) == "reserved"
        assert await store.request_cancel(run_id, slug) == "cancelled"
        assert not calls
        assert await owner.fetchval(
            "SELECT settlement_status FROM usage_ledger WHERE run_id=$1", run_id,
        ) == "uncertain"
        release.set()
        with pytest.raises(LeaseLostError):
            await asyncio.wait_for(worker, timeout=3)
        assert len(calls) == 1
        assert await owner.fetchval(
            "SELECT settlement_status FROM usage_ledger WHERE run_id=$1", run_id,
        ) == "uncertain"
        assert (await store.get_run(run_id))["status"] == "cancelled"
    finally:
        release.set()
        if not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.parametrize("winner", ["cancel", "dispatch"])
async def test_acquisition_cancel_and_claim_serialize_under_real_run_lock(db, monkeypatch, winner):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug)
    run = await store.get_run(run_id)
    first_locked, second_started, release_lock = (asyncio.Event() for _ in range(3))
    provider_entered, release_provider = asyncio.Event(), asyncio.Event()
    original_lock = store._lock_run
    real_client = httpx.AsyncClient
    calls = []
    dispatch_locks = 0
    loser = "dispatch" if winner == "cancel" else "cancel"

    async def lock_with_barrier(conn, *args, **kwargs):
        nonlocal dispatch_locks
        task = asyncio.current_task()
        name = task.get_name() if task else ""
        if name == "dispatch":
            dispatch_locks += 1
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
            "choices": [{"message": {"role": "assistant", "content": "response"}}],
            "usage": {"cost": 0.005},
        })

    monkeypatch.setattr(store, "_lock_run", lock_with_barrier)
    monkeypatch.setattr(tasks, "estimate_step_cost", lambda *args: Decimal("0.01"))
    monkeypatch.setattr(
        tasks.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handle))
    )
    operations = {
        "cancel": lambda: store.request_cancel(run_id, slug),
        "dispatch": lambda: tasks._step(store, lease, run),
    }
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
            assert len(calls) == 1
            release_provider.set()
            try:
                await asyncio.wait_for(first, timeout=3)
            except LeaseLostError:
                pass
            assert await owner.fetchval(
                "SELECT settlement_status FROM usage_ledger WHERE run_id=$1", run_id,
            ) in {"uncertain", "settled"}
        assert (await store.get_run(run_id))["status"] == "cancelled"
    finally:
        release_lock.set()
        release_provider.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
