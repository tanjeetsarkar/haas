"""Actual atomic internal release and one-result terminalization; public holds on."""

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from app.core import intent_eligibility as live
from app.core.envelope import EnvelopeError
from app.core.store import DistributedStore
from test_held_intent_decision import pending
from test_phase2_database import DSN, db as db

pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


@pytest_asyncio.fixture
async def execution_store(db):
    owner = db[0]
    role = "qa_execution_" + uuid4().hex
    await owner.execute(
        f"CREATE ROLE {role} LOGIN PASSWORD 'phase2-test-only' IN ROLE haas_execution_service",
    )
    store = DistributedStore()
    store.pg_pool = await asyncpg.create_pool(
        DSN, user=role, password="phase2-test-only", min_size=1, max_size=2,
    )
    store.redis = AsyncMock()
    try:
        yield store
    finally:
        await store.close()
        await owner.execute(f"DROP OWNED BY {role}")
        await owner.execute(f"DROP ROLE {role}")


async def approved(db, monkeypatch):
    _, store, slug, *_ = db
    registry, decision = await pending(db, monkeypatch)
    monkeypatch.setattr(live, "registry", registry)
    # Broker relay is deliberately not run in DB tests.
    monkeypatch.setattr(store, "relay_outbox", AsyncMock())
    await store.record_held_intent_decision(**decision)
    return registry, {"tenant_id": slug, "run_id": decision["run_id"]}


async def test_concurrent_release_preserves_signed_proof_and_enqueues_exactly_once(db, monkeypatch):
    owner, store, *_ = db
    _, request = await approved(db, monkeypatch)
    before = await store.get_run(request["run_id"])
    envelope = before["pending_tool_calls"][0]["intent_proof"]["envelope"]
    results = await asyncio.gather(*(store.release_approved_intent(**request) for _ in range(2)))
    assert sorted(result["replayed"] for result in results) == [False, True]
    saved = await store.get_run(request["run_id"])
    assert saved["status"] == "running" and saved["next_action"] == "none"
    assert saved["tool_invocations_used"] == 1
    messages = await owner.fetch("SELECT * FROM outbox_messages WHERE run_id=$1", request["run_id"])
    assert len(messages) == 1 and messages[0]["destination"] == "tool"
    payload = json.loads(messages[0]["payload"])
    assert payload["envelope"] == envelope
    assert payload["secret_keys"] == envelope["body"]["allowed_secrets"]
    assert messages[0]["dedup_key"] == "intent:" + envelope["body"]["effect_key"]
    assert await owner.fetchval("SELECT count(*) FROM run_steps WHERE run_id=$1", request["run_id"]) == 0


@pytest.mark.parametrize("change", ["plugin", "credential", "revoked", "expired", "target", "budget", "rejected"])
async def test_release_current_eligibility_failure_rolls_back_all_provisional_writes(db, monkeypatch, change):
    owner, store, *_ = db
    registry, request = await approved(db, monkeypatch)
    if change == "plugin":
        registry.plugins["fake"].digest = "c" * 64
    elif change == "credential":
        monkeypatch.setattr(registry, "resolve_allowed_secrets", lambda *args: {"NEW_SECRET"})
    elif change == "revoked":
        await owner.execute(
            "UPDATE principals SET status='revoked' WHERE principal_id="
            "(SELECT decided_by FROM approval_decisions WHERE run_id=$1)", request["run_id"],
        )
    elif change == "expired":
        await owner.execute(
            "UPDATE approval_batches SET created_at=now()-interval '2 seconds',"
            "expires_at=now()-interval '1 second' WHERE run_id=$1", request["run_id"],
        )
    elif change == "target":
        registry.tools["act"].handler = lambda args: {}
    elif change == "budget":
        await owner.execute("UPDATE runs SET tool_invocations_used=1 WHERE run_id=$1", request["run_id"])
    elif change == "rejected":
        await owner.execute("UPDATE approval_batches SET status='rejected' WHERE run_id=$1", request["run_id"])
    with pytest.raises((ValueError, EnvelopeError)):
        await store.release_approved_intent(**request)
    saved = await store.get_run(request["run_id"])
    assert saved["status"] == "input_required"
    assert saved["tool_invocations_used"] == (1 if change == "budget" else 0)
    assert await owner.fetchval(
        "SELECT status FROM tool_invocations WHERE run_id=$1", request["run_id"],
    ) == "approved"
    assert await owner.fetchval(
        "SELECT count(*) FROM outbox_messages WHERE run_id=$1", request["run_id"],
    ) == 0


@pytest.mark.parametrize("status,terminal", [
    ("succeeded", "completed"), ("failed", "failed"), ("unknown", "failed"), ("cancelled", "cancelled"),
])
async def test_single_outcome_is_terminal_no_llm_continuation_or_automatic_resend(
    db, monkeypatch, execution_store, status, terminal,
):
    owner, store, *_ = db
    _, request = await approved(db, monkeypatch)
    await store.release_approved_intent(**request)
    invocation = await owner.fetchval(
        "SELECT invocation_id FROM tool_invocations WHERE run_id=$1", request["run_id"],
    )
    result = {"content": "exact tool result", "is_error": status != "succeeded"}
    assert await execution_store.record_tool_outcome(
        run_id=request["run_id"], invocation_id=str(invocation), status=status, outcome=result,
    )
    assert not await execution_store.record_tool_outcome(
        run_id=request["run_id"], invocation_id=str(invocation), status=status, outcome=result,
    )
    saved = await store.get_run(request["run_id"])
    assert saved["status"] == terminal and saved["next_action"] == "none"
    assert saved["pending_tool_calls"] is None
    assert ("automatic resend is forbidden" in saved["result_text"]) if status == "unknown" else (
        saved["result_text"] == result["content"]
    )
    with pytest.raises(ValueError):
        await store.release_approved_intent(**request)
    assert await owner.fetchval(
        "SELECT count(*) FROM outbox_messages WHERE run_id=$1", request["run_id"],
    ) == 1
    for table in ("run_steps", "usage_ledger"):
        assert await owner.fetchval(f"SELECT count(*) FROM {table} WHERE run_id=$1", request["run_id"]) == 0


async def test_enqueue_failure_rolls_back_release_and_retry_uses_same_effect(db, monkeypatch):
    owner, store, *_ = db
    _, request = await approved(db, monkeypatch)
    enqueue = store._enqueue

    async def broken(*args, **kwargs):
        raise RuntimeError("synthetic outbox failure")

    monkeypatch.setattr(store, "_enqueue", broken)
    with pytest.raises(RuntimeError, match="synthetic"):
        await store.release_approved_intent(**request)
    assert (await store.get_run(request["run_id"]))["status"] == "input_required"
    monkeypatch.setattr(store, "_enqueue", enqueue)
    assert not (await store.release_approved_intent(**request))["replayed"]
    assert await owner.fetchval(
        "SELECT count(*) FROM outbox_messages WHERE run_id=$1", request["run_id"],
    ) == 1
