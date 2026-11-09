"""Read-only projection with actual tenant RLS and durable finalizer tickets."""

from decimal import Decimal
from uuid import uuid4

import pytest

from app.core.database_context import tenant_context
from app.core.llm import CompletionResult
from app.core.phase_failure_projection import read_phase_failure_code
from app.core.store import StoreNotFoundError
from app.worker import tasks
from test_phase2_database import DSN, admit, db as db, service_store
from test_run_phase_ledger import staged

pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


async def failed_finalizer(owner, store, slug, *, marker):
    run_id, lease = await staged(owner, store, slug, success=1, step_count=2)
    ticket = await store.begin_bounded_phase_step(
        lease, step=3, model="model-a", estimate_usd=Decimal("0.02"),
        finalization=True, acquisition_quota_exhausted=True,
    )
    assert ticket is not None
    completion = CompletionResult(
        assistant_message={"role": "assistant", "content": None}, model_used="model-a",
        cost_usd=0.01, prompt_tokens=1, cached_tokens=0,
    )
    assert await store.settle_bounded_finalizer(lease, ticket, completion=completion) == "failed"
    # Test fixture only: the owner has not yet wired the tool-call marker writer.
    await owner.execute(
        "UPDATE run_steps SET error=$2 WHERE run_id=$1 AND step_number=$3 AND attempt=$4",
        run_id, marker, ticket.step, ticket.attempt,
    )
    return run_id, ticket


@pytest.mark.parametrize(("marker", "expected"), [
    ("phase.finalizer_tool_calls", "phase.finalizer_tool_calls"),
    ("phase.finalizer_failed", "phase.finalizer_failed"),
])
async def test_exact_finalizer_marker_projects_after_reconnect(db, marker, expected):
    owner, store, slug, *_ = db
    run_id, _ = await failed_finalizer(owner, store, slug, marker=marker)
    assert await read_phase_failure_code(store, tenant_id=slug, run_id=run_id) == expected
    async with service_store(owner) as service:
        assert await read_phase_failure_code(service, tenant_id=slug, run_id=run_id) == expected


@pytest.mark.parametrize("marker", [None, "", "provider phase.finalizer_tool_calls"])
async def test_missing_or_unrecognized_finalizer_marker_fails_closed(db, marker):
    owner, store, slug, *_ = db
    run_id, _ = await failed_finalizer(owner, store, slug, marker=marker)
    with pytest.raises(ValueError, match="phase.inconsistent_finalizer_step"):
        await read_phase_failure_code(store, tenant_id=slug, run_id=run_id)


async def test_marker_from_other_step_cannot_override_claimed_step(db):
    owner, store, slug, *_ = db
    run_id, ticket = await failed_finalizer(owner, store, slug, marker=None)
    await owner.execute(
        "INSERT INTO run_steps (tenant_id,run_id,step_number,attempt,lease_epoch,model,status,error) "
        "VALUES ($1,$2,$3,1,1,'model-a','failed','phase.finalizer_tool_calls')",
        slug, run_id, ticket.step - 1,
    )
    with pytest.raises(ValueError, match="phase.inconsistent_finalizer_step"):
        await read_phase_failure_code(store, tenant_id=slug, run_id=run_id)


@pytest.mark.parametrize("marker", [
    "phase.finalizer_tool_calls", "unrecognized", "",
])
async def test_unknown_finalizer_outcome_rejects_contradictory_claimed_marker(db, marker):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug, success=1, step_count=2)
    ticket = await store.begin_bounded_phase_step(
        lease, step=3, model="model-a", estimate_usd=Decimal("0.02"),
        finalization=True, acquisition_quota_exhausted=True,
    )
    assert ticket is not None
    assert await store.settle_bounded_finalizer(lease, ticket, completion=None) == "failed"
    await owner.execute(
        "UPDATE run_steps SET error=$4 WHERE tenant_id=$1 AND run_id=$2 "
        "AND step_number=$3 AND attempt=$5",
        slug, run_id, ticket.step, marker, ticket.attempt,
    )
    with pytest.raises(ValueError, match="phase.inconsistent_finalizer_step"):
        await read_phase_failure_code(store, tenant_id=slug, run_id=run_id)


@pytest.mark.parametrize("reason", ["no_evidence", "budget", "unknown"])
async def test_existing_phase_failure_transitions_project_without_step_marker(db, reason):
    owner, store, slug, *_ = db
    run_id, lease = await staged(owner, store, slug, success=int(reason != "no_evidence"), step_count=2)
    if reason == "no_evidence":
        assert await tasks._phase_exhaustion_route(store, lease, Decimal("0.01")) == "failed"
        expected = "phase.no_successful_acquisition_result"
    elif reason == "budget":
        assert await store.begin_bounded_phase_step(
            lease, step=3, model="model-a", estimate_usd=Decimal("0.0600000001"),
            finalization=True, acquisition_quota_exhausted=True,
        ) is None
        expected = "phase.insufficient_budget"
    else:
        ticket = await store.begin_bounded_phase_step(
            lease, step=3, model="model-a", estimate_usd=Decimal("0.02"),
            finalization=True, acquisition_quota_exhausted=True,
        )
        assert ticket is not None
        assert await store.settle_bounded_finalizer(lease, ticket, completion=None) == "failed"
        expected = "phase.finalizer_outcome_unknown"
    assert await read_phase_failure_code(store, tenant_id=slug, run_id=run_id) == expected


async def test_visibility_legacy_and_active_phase_are_preserved(db):
    owner, store, slug, other_slug, *_ = db
    legacy = f"projection-legacy-{uuid4().hex}"
    await admit(store, slug, legacy, legacy)
    assert await read_phase_failure_code(store, tenant_id=slug, run_id=legacy) is None
    run_id, _ = await staged(owner, store, slug)
    assert await read_phase_failure_code(store, tenant_id=slug, run_id=run_id) is None
    async with service_store(owner) as service:
        with pytest.raises(StoreNotFoundError):
            await read_phase_failure_code(service, tenant_id=other_slug, run_id=run_id)
    token = tenant_context.set(other_slug)
    try:
        with pytest.raises(StoreNotFoundError):
            await read_phase_failure_code(store, tenant_id=slug, run_id=run_id)
    finally:
        tenant_context.reset(token)
