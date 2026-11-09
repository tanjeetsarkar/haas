"""Claim-bound phase failure marker, ledger, event and webhook atomicity."""

import json
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.core.llm import CompletionResult
from app.core.phase_failure_projection import read_phase_failure_code
from app.core.workflow import LeaseLostError
from test_phase2_database import DSN, db as db
from test_run_phase_ledger import staged

pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


async def claimed(db, monkeypatch):
    owner, store, slug, *_ = db
    run, lease = await staged(owner, store, slug, success=1, step_count=2)
    ticket = await store.begin_bounded_phase_step(
        lease, step=3, model="model-a", estimate_usd=Decimal("0.02"),
        finalization=True, acquisition_quota_exhausted=True,
    )
    assert ticket is not None
    await owner.execute(
        "UPDATE runs SET webhook_url='https://callback.invalid/result' WHERE run_id=$1", run,
    )
    monkeypatch.setattr(store, "relay_outbox", AsyncMock())
    return run, lease, ticket


def completion(tool_calls):
    return CompletionResult(
        assistant_message={"role": "assistant", "content": None,
                           "tool_calls": [{"id": "forbidden"}] if tool_calls else []},
        model_used="model-a", cost_usd=0.01, prompt_tokens=1, cached_tokens=0,
    )


@pytest.mark.parametrize("tools", [False, True])
async def test_exact_claim_marker_and_all_terminal_projections_agree(db, monkeypatch, tools):
    owner, store, slug, *_ = db
    run, lease, ticket = await claimed(db, monkeypatch)
    assert await store.settle_bounded_finalizer(lease, ticket, completion=completion(tools)) == "failed"
    code = "phase.finalizer_tool_calls" if tools else "phase.finalizer_failed"
    assert await read_phase_failure_code(store, tenant_id=slug, run_id=run) == code
    step = await owner.fetchrow(
        "SELECT status,error FROM run_steps WHERE run_id=$1 AND step_number=$2 AND attempt=$3",
        run, ticket.step, ticket.attempt,
    )
    assert step["status"] == "failed" and step["error"] == code
    assert await owner.fetchval(
        "SELECT settlement_status FROM usage_ledger WHERE run_id=$1 AND unit='model_usd'", run,
    ) == "settled"
    event = await owner.fetchval(
        "SELECT payload FROM run_events WHERE run_id=$1 AND type='run_error'", run,
    )
    assert json.loads(event)["code"] == code
    webhook = await owner.fetchval(
        "SELECT payload FROM outbox_messages WHERE run_id=$1 AND destination='webhook'", run,
    )
    assert json.loads(webhook)["body"]["code"] == code


async def test_terminal_enqueue_failure_rolls_back_marker_state_and_ledger(db, monkeypatch):
    owner, store, _, *_ = db
    run, lease, ticket = await claimed(db, monkeypatch)
    enqueue = store._enqueue

    async def broken(*args, **kwargs):
        raise RuntimeError("synthetic webhook failure")

    monkeypatch.setattr(store, "_enqueue", broken)
    with pytest.raises(RuntimeError, match="synthetic"):
        await store.settle_bounded_finalizer(lease, ticket, completion=completion(True))
    step = await owner.fetchrow("SELECT status,error FROM run_steps WHERE run_id=$1", run)
    assert step["status"] == "model_running" and step["error"] is None
    assert await owner.fetchval(
        "SELECT settlement_status FROM usage_ledger WHERE run_id=$1 AND unit='model_usd'", run,
    ) == "reserved"
    saved = await store.get_run(run)
    assert saved["status"] == "running" and saved["phase_state"].phase == "finalization"
    monkeypatch.setattr(store, "_enqueue", enqueue)
    assert await store.settle_bounded_finalizer(lease, ticket, completion=completion(True)) == "failed"


async def test_claimed_finalizer_reentry_projects_unknown_consistently(db, monkeypatch):
    owner, store, slug, *_ = db
    run, lease, _ = await claimed(db, monkeypatch)
    assert await store.begin_bounded_phase_step(
        lease, step=3, model="model-a", estimate_usd=Decimal("0.02"),
        finalization=True, acquisition_quota_exhausted=True,
    ) is None
    code = "phase.finalizer_outcome_unknown"
    assert await read_phase_failure_code(store, tenant_id=slug, run_id=run) == code
    for destination in ("event", "webhook"):
        if destination == "event":
            payload = json.loads(await owner.fetchval(
                "SELECT payload FROM run_events WHERE run_id=$1 AND type='run_error'", run,
            ))
        else:
            payload = json.loads(await owner.fetchval(
                "SELECT payload FROM outbox_messages WHERE run_id=$1 AND destination='webhook'", run,
            ))["body"]
        assert payload["code"] == code


@pytest.mark.parametrize("mismatch", ["step", "attempt"])
async def test_ticket_identity_mismatch_cannot_settle_other_same_epoch_step(db, monkeypatch, mismatch):
    owner, store, *_ = db
    run, lease, ticket = await claimed(db, monkeypatch)
    other_step = ticket.step + (mismatch == "step")
    other_attempt = ticket.attempt + (mismatch == "attempt")
    await owner.execute(
        "INSERT INTO run_steps(tenant_id,run_id,step_number,attempt,lease_epoch,model,status,reserved_usd) "
        "VALUES($1,$2,$3,$4,$5,'model-a','model_running',$6)",
        lease.tenant_id, run, other_step, other_attempt, lease.epoch, ticket.reserved_usd,
    )
    forged = replace(ticket, step=other_step, attempt=other_attempt)
    with pytest.raises(LeaseLostError):
        await store.settle_bounded_finalizer(lease, forged, completion=completion(True))
    assert await owner.fetchval(
        "SELECT settlement_status FROM usage_ledger WHERE run_id=$1 AND unit='model_usd'", run,
    ) == "reserved"
    assert await owner.fetchval("SELECT count(*) FROM run_steps WHERE run_id=$1 AND error IS NOT NULL", run) == 0
    assert await owner.fetchval("SELECT count(*) FROM outbox_messages WHERE run_id=$1", run) == 0
    assert (await store.get_run(run))["phase_state"].phase == "finalization"
