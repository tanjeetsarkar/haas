"""Ledger substrate tests use a disposable PostgreSQL; no provider dispatch."""

import asyncio
import json
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest

from app.core.run_phases import PhaseConfig
from app.core.workflow import Lease, LeaseLostError
from test_phase2_database import DSN, admit, db as db, service_store

pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


async def staged(owner, store, slug, *, success=0, step_count=0, total=Decimal("0")):
    run = "phase-ledger-" + uuid4().hex
    await store.get_or_create_thread_messages(run, slug)
    await admit(store, slug, run, run, phase_config=PhaseConfig())
    if success:
        await owner.execute(
            "UPDATE runs SET phase_state_json=jsonb_set("
            "jsonb_set(phase_state_json,'{successful_tool_outcomes}','1'::jsonb),"
            "'{state_version}','1'::jsonb) WHERE run_id=$1", run,
        )
    await owner.execute(
        "UPDATE runs SET status='running', lease_owner='ledger-test', lease_epoch=1, "
        "state_version=1, step_count=$2, total_cost_usd=$3, "
        "lease_expires_at=now()+interval '1 minute' WHERE run_id=$1",
        run, step_count, total,
    )
    return run, Lease(slug, run, run, 1, 1, "ledger-test")


@pytest.mark.parametrize(
    "spent,estimate,allowed",
    [("0.20", "0.04", True), ("0.20", "0.040001", False)],
)
async def test_acquisition_cost_boundary_preserves_twenty_percent(db, spent, estimate, allowed):
    owner, store, slug, *_ = db
    run, lease = await staged(owner, store, slug, total=Decimal(spent))
    result = await store.begin_bounded_phase_step(
        lease, step=1, model="model-a", estimate_usd=Decimal(estimate),
        finalization=False,
    )
    assert (result is not None) is allowed
    assert await owner.fetchval(
        "SELECT count(*) FROM usage_ledger WHERE run_id=$1", run
    ) == int(allowed)


@pytest.mark.parametrize(
    "finalization,spent,estimate",
    [
        (False, "0.20", "0.04000000000000000000000000001"),
        (True, "0.25", "0.05000000000000000000000000001"),
    ],
)
@pytest.mark.parametrize("precision", [9, 28])
async def test_subprecision_overrun_never_creates_reservation(
    db, finalization, spent, estimate, precision,
):
    owner, store, slug, *_ = db
    run, lease = await staged(owner, store, slug, success=int(finalization), total=Decimal(spent))
    with localcontext() as context:
        context.prec = precision
        assert await store.begin_bounded_phase_step(
            lease, step=1, model="model-a", estimate_usd=Decimal(estimate),
            finalization=finalization, no_tool_response=finalization,
        ) is None
    assert await owner.fetchval("SELECT count(*) FROM usage_ledger WHERE run_id=$1", run) == 0
    assert await owner.fetchval("SELECT count(*) FROM run_steps WHERE run_id=$1", run) == 0


@pytest.mark.parametrize("component", ["uncertain", "reserved"])
async def test_existing_precise_cost_components_count_toward_ceiling(db, component):
    owner, store, slug, *_ = db
    run, lease = await staged(owner, store, slug, total=Decimal("0.20"))
    if component == "reserved":
        await owner.execute(
            "UPDATE runs SET budget_reserved_usd=$2 WHERE run_id=$1",
            run, Decimal("0.01000000000000000000000000001"),
        )
    else:
        await owner.execute(
            "INSERT INTO usage_ledger (entry_id,tenant_id,run_id,source_attempt_key,unit,"
            "quantity,cost_usd,settlement_status,pricing_revision) "
            "VALUES ($1,$2,$3,$4,'model_usd',$5,$5,'uncertain','test')",
            uuid4(), slug, run, f"{run}:prior", Decimal("0.01000000000000000000000000001"),
        )
    with localcontext() as context:
        context.prec = 9
        assert await store.begin_bounded_phase_step(
            lease, step=1, model="model-a", estimate_usd=Decimal("0.03"), finalization=False,
        ) is None
    assert await owner.fetchval("SELECT count(*) FROM run_steps WHERE run_id=$1", run) == 0


async def test_small_estimate_is_preserved_in_all_reservations(db):
    owner, store, slug, *_ = db
    run, lease = await staged(owner, store, slug)
    prior = Decimal("0.00000000000000000000000000002")
    await owner.execute(
        "UPDATE runs SET budget_reserved_usd=$2 WHERE run_id=$1", run, prior,
    )
    amount = Decimal("0.01000000000000000000000000001")
    with localcontext() as context:
        context.prec = 9
        ticket = await store.begin_bounded_phase_step(
            lease, step=1, model="model-a", estimate_usd=amount, finalization=False,
        )
    assert ticket and ticket.reserved_usd == amount
    assert await owner.fetchval(
        "SELECT budget_reserved_usd FROM runs WHERE run_id=$1", run
    ) == Decimal("0.01000000000000000000000000003")
    assert await owner.fetchval("SELECT cost_usd FROM usage_ledger WHERE run_id=$1", run) == amount
    assert await owner.fetchval("SELECT reserved_usd FROM run_steps WHERE run_id=$1", run) == amount


@pytest.mark.parametrize("estimate", [
    Decimal("-0.01"), Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"),
    Decimal("-Infinity"), 0.01,
])
async def test_invalid_estimate_rejected_without_writes(db, estimate):
    owner, store, slug, *_ = db
    run, lease = await staged(owner, store, slug)
    with pytest.raises(ValueError, match="invalid_estimate"):
        await store.begin_bounded_phase_step(
            lease, step=1, model="model-a", estimate_usd=estimate, finalization=False,
        )
    assert await owner.fetchval("SELECT count(*) FROM usage_ledger WHERE run_id=$1", run) == 0


def test_quota_ignores_low_ambient_decimal_precision():
    from app.core.run_phases import PhaseBudget, calculate_quota

    cost = Decimal("0.123456789012345678901234567890123456789")
    with localcontext() as context:
        context.prec = 5
        quota = calculate_quota(
            PhaseConfig(), PhaseBudget(max_steps=2, max_cost_usd=cost, max_tool_invocations=0),
            estimated_finalizer_cost_usd=Decimal("0"),
        )
    assert quota.finalization_reserved_cost_usd == Decimal("0.0246913578024691357802469135780246913578")
    assert quota.acquisition_max_cost_usd == Decimal("0.0987654312098765431209876543120987654312")


async def test_reserved_step_and_exact_final_estimate(db):
    owner, store, slug, *_ = db
    run, lease = await staged(owner, store, slug, success=1, step_count=2, total=Decimal("0.24"))
    ticket = await store.begin_bounded_phase_step(
        lease, step=3, model="model-a", estimate_usd=Decimal("0.06"),
        finalization=True, acquisition_quota_exhausted=True,
    )
    assert ticket is not None
    row = await owner.fetchrow(
        "SELECT phase_state_json,budget_reserved_usd FROM runs WHERE run_id=$1", run
    )
    state = json.loads(row["phase_state_json"])
    assert state["finalizer_attempted"] is True
    assert state["finalizer_attempt_key"] == ticket.attempt_key
    assert row["budget_reserved_usd"] == Decimal("0.06")
    assert await owner.fetchval(
        "SELECT count(*) FROM usage_ledger WHERE run_id=$1 AND source_attempt_key=$2",
        run, ticket.attempt_key,
    ) == 1
    # A redelivery cannot issue another ticket after the durable claim.
    assert await store.begin_bounded_phase_step(
        lease, step=3, model="model-a", estimate_usd=Decimal("0.06"),
        finalization=True, acquisition_quota_exhausted=True,
    ) is None
    assert await owner.fetchval("SELECT status FROM runs WHERE run_id=$1", run) == "failed"
    assert await owner.fetchval(
        "SELECT settlement_status FROM usage_ledger WHERE run_id=$1", run
    ) == "uncertain"
    assert json.loads(await owner.fetchval(
        "SELECT phase_state_json FROM runs WHERE run_id=$1", run
    ))["failure_code"] == "finalizer_outcome_unknown"


async def test_acquisition_cannot_consume_reserved_step(db):
    owner, store, slug, *_ = db
    run, lease = await staged(owner, store, slug, success=1, step_count=2)
    assert await store.begin_bounded_phase_step(
        lease, step=3, model="model-a", estimate_usd=Decimal("0"), finalization=False,
    ) is None
    assert await owner.fetchval("SELECT count(*) FROM run_steps WHERE run_id=$1", run) == 0
    assert await store.begin_bounded_phase_step(
        lease, step=3, model="model-a", estimate_usd=Decimal("0.06"),
        finalization=True, acquisition_quota_exhausted=True,
    )


async def test_competing_claims_fenced_and_rollback_on_ledger_failure(db, monkeypatch):
    owner, store, slug, *_ = db
    run, lease = await staged(owner, store, slug, success=1)
    stale = Lease(lease.tenant_id, run, run, lease.epoch, lease.version, lease.owner)
    original = store._bump

    async def fail(*args, **kwargs):
        raise RuntimeError("forced rollback")

    monkeypatch.setattr(store, "_bump", fail)
    with pytest.raises(RuntimeError, match="forced rollback"):
        await store.begin_bounded_phase_step(
            lease, step=1, model="model-a", estimate_usd=Decimal("0.06"),
            finalization=True, no_tool_response=True,
        )
    assert await owner.fetchval("SELECT count(*) FROM run_steps WHERE run_id=$1", run) == 0
    assert await owner.fetchval("SELECT count(*) FROM usage_ledger WHERE run_id=$1", run) == 0
    assert not json.loads(await owner.fetchval(
        "SELECT phase_state_json FROM runs WHERE run_id=$1", run
    ))["finalizer_attempted"]
    monkeypatch.setattr(store, "_bump", original)
    async def claim(candidate):
        return await store.begin_bounded_phase_step(
            candidate, step=1, model="model-a", estimate_usd=Decimal("0.06"),
            finalization=True, no_tool_response=True,
        )
    results = await asyncio.gather(claim(lease), claim(stale), return_exceptions=True)
    assert sum(isinstance(result, LeaseLostError) for result in results) == 1
    assert sum(hasattr(result, "attempt_key") for result in results) == 1


async def test_guards_and_existing_ledger_cannot_bypass_phase(db):
    owner, store, slug, other_slug, *_ = db
    run, lease = await staged(owner, store, slug)
    with pytest.raises(ValueError, match="requires_bounded_ledger"):
        await store.begin_step(lease, step=1, model="model-a", estimate_usd=Decimal(0))
    with pytest.raises(ValueError, match="success_required"):
        await store.begin_bounded_phase_step(
            lease, step=1, model="model-a", estimate_usd=Decimal(0),
            finalization=True, no_tool_response=True,
        )
    async with service_store(owner) as service:
        foreign = Lease(other_slug, run, run, lease.epoch, lease.version, lease.owner)
        with pytest.raises(LeaseLostError):
            await service.begin_bounded_phase_step(
                foreign, step=1, model="model-a", estimate_usd=Decimal(0),
                finalization=False,
            )
    assert await owner.fetchval("SELECT count(*) FROM usage_ledger WHERE run_id=$1", run) == 0


async def test_trigger_and_finalizer_reserve_checks(db):
    owner, store, slug, *_ = db
    run, lease = await staged(owner, store, slug, success=1)
    with pytest.raises(ValueError, match="trigger_required"):
        await store.begin_bounded_phase_step(
            lease, step=1, model="model-a", estimate_usd=Decimal("0.06"),
            finalization=True,
        )
    assert await store.begin_bounded_phase_step(
        lease, step=1, model="model-a", estimate_usd=Decimal("0.060001"),
        finalization=True, no_tool_response=True,
    ) is None
    saved = await store.get_run(run)
    assert saved["status"] == "failed"
    assert saved["phase_state"].failure_code == "insufficient_budget"
    assert await owner.fetchval("SELECT count(*) FROM usage_ledger WHERE run_id=$1", run) == 0


async def test_restarted_claim_fails_closed(db):
    owner, store, slug, *_ = db
    run, lease = await staged(owner, store, slug, success=1)
    ticket = await store.begin_bounded_phase_step(
        lease, step=1, model="model-a", estimate_usd=Decimal("0.06"),
        finalization=True, no_tool_response=True,
    )
    assert ticket is not None
    await owner.execute(
        "UPDATE runs SET lease_epoch=2,state_version=state_version+1,lease_owner='restarted' "
        "WHERE run_id=$1", run,
    )
    restarted = Lease(slug, run, run, 2, lease.version + 1, "restarted")
    assert await store.begin_bounded_phase_step(
        restarted, step=1, model="model-a", estimate_usd=Decimal("0.06"),
        finalization=True, no_tool_response=True,
    ) is None
    assert await owner.fetchval(
        "SELECT count(*) FROM usage_ledger WHERE run_id=$1", run
    ) == 1
    assert await owner.fetchval(
        "SELECT settlement_status FROM usage_ledger WHERE run_id=$1", run
    ) == "uncertain"


@pytest.mark.parametrize("reason", ["cost", "deadline"])
async def test_finalizer_without_capacity_fails_durably(db, reason):
    owner, store, slug, *_ = db
    run, lease = await staged(owner, store, slug, success=1)
    if reason == "cost":
        await owner.execute(
            "UPDATE runs SET total_cost_usd=0.25 WHERE run_id=$1", run
        )
    else:
        await owner.execute(
            "UPDATE runs SET deadline_at=now()-interval '1 second' WHERE run_id=$1", run
        )
    assert await store.begin_bounded_phase_step(
        lease, step=1, model="model-a", estimate_usd=Decimal("0.06"),
        finalization=True, no_tool_response=True,
    ) is None
    row = await owner.fetchrow(
        "SELECT status,phase_state_json FROM runs WHERE run_id=$1", run
    )
    assert row["status"] == "failed"
    assert json.loads(row["phase_state_json"])["failure_code"] == "insufficient_budget"
    assert await owner.fetchval("SELECT count(*) FROM usage_ledger WHERE run_id=$1", run) == 0


async def test_acquisition_inflight_never_replaced(db):
    owner, store, slug, *_ = db
    run, lease = await staged(owner, store, slug)
    assert await store.begin_bounded_phase_step(
        lease, step=1, model="model-a", estimate_usd=Decimal("0.01"),
        finalization=False,
    )
    with pytest.raises(ValueError, match="model_attempt_inflight"):
        await store.begin_bounded_phase_step(
            lease, step=1, model="model-a", estimate_usd=Decimal("0.01"),
            finalization=False,
        )
    assert await owner.fetchval("SELECT count(*) FROM usage_ledger WHERE run_id=$1", run) == 1
