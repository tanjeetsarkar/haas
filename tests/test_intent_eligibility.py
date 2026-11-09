"""Actual PG eligibility evidence, not an operational release or handler test."""

import hashlib
import json
from uuid import UUID

import asyncpg
import pytest

import app.main as main
from app.api import internal_run_authorization as api
from app.core import intent_eligibility as live
from app.core.deterministic_intent import intent_batch_digest
from app.core.envelope import sign_envelope
from test_held_intent_decision import pending
from test_internal_run_authorization import send
from test_phase2_database import DSN, db as db
from test_worker_identity import auth as auth  # noqa: F401

pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


async def candidate(db, monkeypatch, *, staged=True):
    owner, store, *_ = db
    registry, decision = await pending(db, monkeypatch)
    monkeypatch.setattr(live, "registry", registry)
    await store.record_held_intent_decision(**decision)
    run = await store.get_run(decision["run_id"])
    body = run["pending_tool_calls"][0]["intent_proof"]["envelope"]["body"]
    context = run["authority"].intent.context
    check = api.AuthorizationCheck.model_validate({
        "version": 1, "operation": "pre_grant", "request_id": "request-0123456789",
        "tenant_id": context.tenant_id, "run_id": context.run_id,
        "invocation_id": body["invocation_id"], "tool_name": body["tool_call"]["name"],
        "arguments_sha256": hashlib.sha256(body["tool_call"]["arguments"].encode()).hexdigest(),
        "intent_binding": body["intent_binding"], "policy_revision_id": context.policy_revision,
        "registry_fingerprint": context.registry_fingerprint, "plugin_digest": context.plugin_digest,
        "envelope_expires_at": body["expires_at"],
    })
    if staged:
        # Test-only DB state. No production release path or permit is implied.
        await owner.execute("UPDATE runs SET status='running' WHERE run_id=$1", check.run_id)
        await owner.execute(
            "UPDATE tool_invocations SET status='dispatched' WHERE run_id=$1", check.run_id,
        )
    return registry, check, decision


@pytest.mark.parametrize("operation", ["pre_grant", "final_entry"])
async def test_live_candidate_proof_passes_without_writes_or_releasing_anything(db, monkeypatch, operation):
    owner, store, *_ = db
    _, check, _ = await candidate(db, monkeypatch)
    check = check.model_copy(update={"operation": operation})
    async with store.connection() as conn:
        assert await live.validate_dispatch_eligibility(store, conn, check) is None
    assert await owner.fetchval(
        "SELECT count(*) FROM outbox_messages WHERE run_id=$1", check.run_id,
    ) == 0
    assert await owner.fetchval(
        "SELECT count(*) FROM run_steps WHERE run_id=$1", check.run_id,
    ) == 0


@pytest.mark.parametrize("change", [
    "held", "submitter", "approver", "policy", "plugin", "target", "schema",
    "credentials", "arguments", "digest", "rejected", "finished", "expiry",
])
async def test_current_eligibility_revocation_or_altered_proof_denies(db, monkeypatch, change):
    owner, store, _, _, tenant, *_ = db
    registry, check, decision = await candidate(db, monkeypatch, staged=change != "held")
    if change in ("submitter", "approver"):
        principal = UUID(decision["identity"].principal_id) if change == "approver" else db[6]
        await owner.execute("UPDATE principals SET status='revoked' WHERE principal_id=$1", principal)
    elif change == "policy":
        await owner.execute("UPDATE tenants SET active_policy_revision_id=NULL WHERE tenant_id=$1", tenant)
    elif change == "plugin":
        registry.plugins["fake"].digest = "b" * 64
    elif change == "target":
        def replacement(args):
            return {}
        registry.tools["act"].handler = replacement
    elif change == "schema":
        registry.tools["act"].endpoint_url = "https://changed.invalid/action"
    elif change == "credentials":
        monkeypatch.setattr(registry, "resolve_allowed_secrets", lambda *args: {"NEW_SECRET"})
    elif change == "arguments":
        await owner.execute("UPDATE tool_invocations SET arguments='{}' WHERE run_id=$1", check.run_id)
    elif change == "digest":
        await owner.execute(
            "UPDATE approval_batches SET batch_digest=$2 WHERE run_id=$1", check.run_id, "0" * 64,
        )
    elif change == "rejected":
        await owner.execute("UPDATE approval_batches SET status='rejected' WHERE run_id=$1", check.run_id)
    elif change == "finished":
        await owner.execute("UPDATE tool_invocations SET status='succeeded' WHERE run_id=$1", check.run_id)
    elif change == "expiry":
        check = check.model_copy(update={"envelope_expires_at": check.envelope_expires_at + 1})
    with pytest.raises(ValueError):
        async with store.connection() as conn:
            await live.validate_dispatch_eligibility(store, conn, check)


@pytest.mark.parametrize("field,value", [("allowed_secrets", ["EXTRA"]), ("ordinal", 1)])
async def test_resigned_proof_cannot_rebind_immutable_approval(db, monkeypatch, field, value):
    owner, store, *_ = db
    _, check, _ = await candidate(db, monkeypatch)
    run = await store.get_run(check.run_id)
    calls = run["pending_tool_calls"]
    body = calls[0]["intent_proof"]["envelope"]["body"]
    body[field] = value
    calls[0]["intent_proof"]["envelope"] = sign_envelope(body)
    digest = intent_batch_digest(check.run_id, 1, calls)
    await owner.execute(
        "UPDATE runs SET pending_tool_calls_json=$2::jsonb WHERE run_id=$1", check.run_id, json.dumps(calls),
    )
    batch = await owner.fetchrow("SELECT * FROM approval_batches WHERE run_id=$1", check.run_id)
    previews = json.loads(batch["tool_calls"])
    previews[0]["intent_proof"] = calls[0]["intent_proof"]
    await owner.execute(
        "UPDATE approval_batches SET tool_calls=$2::jsonb,batch_digest=$3 WHERE run_id=$1",
        check.run_id, json.dumps(previews), digest,
    )
    with pytest.raises(asyncpg.RaiseError, match="immutable"):
        await owner.execute(
            "UPDATE approval_decisions SET batch_digest=$2 WHERE run_id=$1", check.run_id, digest,
        )
    with pytest.raises(ValueError):
        async with store.connection() as conn:
            await live.validate_dispatch_eligibility(store, conn, check)


@pytest.mark.parametrize("staged", [False, True])
async def test_real_worker_signed_rpc_still_denies_held_and_eligible_candidate(db, monkeypatch, auth, staged):
    _, store, *_ = db
    _, check, _ = await candidate(db, monkeypatch, staged=staged)
    config, _, _, _, _, replay, _ = auth
    store.redis = replay
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(api, "settings", config)
    response = await send(config, check.model_dump())
    assert response.status_code == 200
    result = response.json()
    assert result["decision"] == "deny"
    assert result["code"] == ("restriction.unavailable" if staged else "restriction.denied")
    assert result["valid_until"] <= result["checked_at"]
