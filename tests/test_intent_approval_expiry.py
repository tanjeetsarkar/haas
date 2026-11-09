"""Approval expiry cannot outlive exact-intent proof, without changing legacy."""

import json
from datetime import datetime, timedelta, timezone

import pytest

import test_run_phase_approval_recovery as phase_approval
from app.core import deterministic_intent
from app.core.envelope import verify_envelope
from test_phase2_database import DSN, db as db
from test_run_intent_workflow import running

pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


@pytest.mark.parametrize("short_deadline", [False, True])
async def test_exact_intent_approval_expires_no_later_than_proof_or_deadline(
    db, monkeypatch, short_deadline,
):
    owner, store, *_ = db
    _, _, lease = await running(db, monkeypatch)
    if short_deadline:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=10)
        await owner.execute(
            "UPDATE runs SET deadline_at=$2 WHERE run_id=$1", lease.run_id, deadline,
        )
    await store.prepare_intent_approval(lease)
    batch = await owner.fetchrow(
        "SELECT expires_at,tool_calls FROM approval_batches WHERE run_id=$1", lease.run_id,
    )
    deadline = await owner.fetchval(
        "SELECT deadline_at FROM runs WHERE run_id=$1", lease.run_id,
    )
    proof = json.loads(batch["tool_calls"])[0]["intent_proof"]
    body = verify_envelope(proof["envelope"])
    assert batch["expires_at"].timestamp() <= body["expires_at"]
    assert deadline is None or batch["expires_at"] <= deadline


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1, "123", True])
async def test_invalid_signed_expiry_rolls_back_approval(db, monkeypatch, invalid):
    owner, store, *_ = db
    _, _, lease = await running(db, monkeypatch)
    original = deterministic_intent.sign_envelope

    def malformed(body):
        return original({**body, "expires_at": invalid})

    monkeypatch.setattr(deterministic_intent, "sign_envelope", malformed)
    with pytest.raises(ValueError, match="restriction.approval_expiry"):
        await store.prepare_intent_approval(lease)
    for table in ("approval_batches", "tool_invocations"):
        assert await owner.fetchval(
            f"SELECT count(*) FROM {table} WHERE run_id=$1", lease.run_id,
        ) == 0


async def test_legacy_approval_ttl_does_not_change_with_short_run_deadline(db, monkeypatch):
    owner, store, slug, *_ = db
    deadline = datetime.now(timezone.utc) + timedelta(seconds=10)
    original = phase_approval.staged

    async def staged_with_deadline(*args, **kwargs):
        run_id, lease = await original(*args, **kwargs)
        await owner.execute("UPDATE runs SET deadline_at=$2 WHERE run_id=$1", run_id, deadline)
        return run_id, lease

    monkeypatch.setattr(phase_approval, "staged", staged_with_deadline)
    _, batch, _ = await phase_approval.pause_at_approval(owner, store, slug, monkeypatch)
    expiry = await owner.fetchval(
        "SELECT expires_at FROM approval_batches WHERE batch_id=$1::uuid", batch["batch_id"],
    )
    assert expiry > deadline
