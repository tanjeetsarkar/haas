"""Internal persistence proof only; public admission and worker execution stay held."""

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest

from app.core.run_restrictions import (
    ExactIntent, IntentContext, RunAuthority, authority_request_digest,
)
from app.core.store import DistributedStore
from app.core.workflow import IdempotencyConflictError, IdempotentReplay, decode_run
from test_phase2_database import DSN, admit, db as db, service_store

pg = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


def bound(tenant, principal, policy, run, key=None):
    return RunAuthority(tools=("synthetic_tool",), intent=ExactIntent(
        tool_name="synthetic_tool", arguments_json='{"recipient":"a"}',
        context=IntentContext(
            tenant_id=str(tenant), submitter_id=str(principal), run_id=run,
            policy_revision=str(policy), registry_fingerprint="registry",
            plugin_id="synthetic", plugin_digest="a" * 64,
            destination="https://synthetic.invalid", idempotency_key=key,
        ),
    ))


def test_digest_includes_authority_without_server_run_identity():
    base = "a" * 64
    authorities = [None, RunAuthority(), RunAuthority(tools=()),
                   RunAuthority(tools=("a",)), bound("t", "s", "p", "r")]
    assert authority_request_digest(base, None) == base
    assert len({authority_request_digest(base, a) for a in authorities}) == 5
    assert authority_request_digest(base, authorities[-1]) == authority_request_digest(
        base, bound("t", "s", "p", "different-run"),
    )
    changed = bound("t", "s", "p", "r").model_dump()
    changed["intent"]["arguments_json"] = '{"recipient":"b"}'
    assert authority_request_digest(base, authorities[-1]) != authority_request_digest(
        base, RunAuthority.model_validate(changed),
    )


@pytest.mark.parametrize("stored", [None, '{"version":1,"tools":null}', '{"version":1,"tools":[]}'])
def test_decode_only_uses_run_authority_not_messages(stored):
    row = {
        "authority_json": stored, "active_skills_json": "[]", "fingerprints_json": "[]",
        "messages_checkpoint_json": '[{"authority":{"tools":["forged"]}}]',
        "pending_tool_calls_json": None,
    }
    actual = decode_run(row)["authority"]
    assert (actual is None) == (stored is None)
    if actual is not None:
        assert actual.tools == (() if stored.endswith("[]}") else None)


@pytest.mark.parametrize("stored", ['null', '{}', '{"version":2}', '{"version":1,"unknown":true}'])
def test_decode_corrupt_authority_never_defaults_to_legacy(stored):
    with pytest.raises(ValueError):
        decode_run({"authority_json": stored})


@pg
@pytest.mark.parametrize("kind", ["legacy", "null", "empty", "intent"])
async def test_real_insert_restart_checkpoint_cancel_and_storage_hold(db, kind):
    owner, store, slug, other_slug, tenant, _, principal, policy = db
    run = "storage_" + uuid4().hex
    authority = {
        "legacy": None, "null": RunAuthority(), "empty": RunAuthority(tools=()),
        "intent": bound(tenant, principal, policy, run),
    }[kind]
    await store.get_or_create_thread_messages(run, slug)
    await admit(store, slug, run, run, authority=authority,
                submitted_by_principal_id=str(principal), policy_revision_id=str(policy),
                registry_fingerprint="registry")
    saved = await store.get_run(run)
    assert saved["authority"] == authority
    if authority is not None:
        assert saved["next_action"] == "none"
        assert await owner.fetchval(
            "SELECT count(*) FROM outbox_messages WHERE run_id=$1", run,
        ) == 0
        await owner.execute("UPDATE runs SET updated_at=0 WHERE run_id=$1", run)
        async with service_store(owner) as service:
            assert await service.acquire_lease(
                message_id=uuid4().hex, run_id=run, action="none",
                expected_state_version=0, owner="forged-wakeup",
            ) is None
            assert (await service.reconcile())["requeued"] == 0
        for field in ("authority_json", "registry_fingerprint", "submitted_by_principal_id",
                      "policy_revision_id", "agent_revision_id", "tenant_id"):
            with pytest.raises(asyncpg.CheckViolationError):
                # Trigger runs before NOT NULL / FK checks.
                value = "gen_random_uuid()" if field == "agent_revision_id" else "NULL"
                await owner.execute(f"UPDATE runs SET {field}={value} WHERE run_id=$1", run)
    await owner.execute(
        "UPDATE runs SET messages_checkpoint_json=$2::jsonb, active_skills_json='[]' "
        "WHERE run_id=$1", run, json.dumps([{"authority": {"tools": ["forged"]}}]),
    )
    async with store.connection() as conn:
        role = await conn.fetchval("SELECT current_user")
    restarted = DistributedStore()
    restarted.pg_pool = await asyncpg.create_pool(
        DSN, user=role, password="phase2-test-only", min_size=1, max_size=1,
    )
    restarted.redis = AsyncMock()
    try:
        assert (await restarted.get_run(run))["authority"] == authority
        await restarted.request_cancel(run, slug)
        assert (await restarted.get_run(run))["authority"] == authority
        async with restarted.connection() as conn:
            await conn.execute("SELECT set_config('haas.tenant',$1,true)", other_slug)
            assert await conn.fetchval("SELECT count(*) FROM runs WHERE run_id=$1", run) == 0
    finally:
        await restarted.close()


@pg
@pytest.mark.parametrize("field", [
    "tenant_id", "submitter_id", "run_id", "policy_revision", "registry_fingerprint",
    "idempotency_key",
])
async def test_intent_provenance_mismatch_rolls_back_before_outbox(db, field):
    owner, store, slug, _, tenant, _, principal, policy = db
    run = "mismatch_" + uuid4().hex
    data = bound(tenant, principal, policy, run).model_dump()
    data["intent"]["context"][field] = slug if field == "tenant_id" else "different"
    await store.get_or_create_thread_messages(run, slug)
    with pytest.raises(ValueError, match="restriction.provenance_mismatch"):
        await admit(store, slug, run, run, authority=RunAuthority.model_validate(data),
                    submitted_by_principal_id=str(principal), policy_revision_id=str(policy),
                    registry_fingerprint="registry")
    assert await owner.fetchval("SELECT count(*) FROM runs WHERE run_id=$1", run) == 0
    assert await owner.fetchval("SELECT count(*) FROM outbox_messages WHERE run_id=$1", run) == 0


@pg
@pytest.mark.parametrize("kind", ["empty", "intent"])
async def test_authority_replay_conflict_and_required_pins(db, kind):
    _, store, slug, _, tenant, _, principal, policy = db
    key = uuid4().hex
    admission = {
        "principal_key": str(principal), "idempotency_key": key,
        "request_digest": "a" * 64, "response": {"status": "queued"},
    }
    async def create(authority, pins=True):
        run = uuid4().hex
        await store.get_or_create_thread_messages(run, slug)
        if authority == "intent":
            authority = bound(tenant, principal, policy, run, key)
        return await admit(
            store, slug, run, run, authority=authority, admission=admission,
            submitted_by_principal_id=str(principal) if pins else None,
            policy_revision_id=str(policy), registry_fingerprint="registry",
        )
    with pytest.raises(ValueError, match="restriction.authenticated_provenance_required"):
        await create(RunAuthority(tools=()), pins=False)
    chosen = "intent" if kind == "intent" else RunAuthority(tools=())
    await create(chosen)
    with pytest.raises(IdempotentReplay):
        await create(chosen)
    with pytest.raises(IdempotencyConflictError):
        await create(RunAuthority())
    with pytest.raises(IdempotencyConflictError):
        await create(None)
    prior = await store.find_admission(slug, str(principal), key)
    assert prior["request_digest"] == authority_request_digest(
        admission["request_digest"],
        bound(tenant, principal, policy, "ignored-run", key) if kind == "intent"
        else RunAuthority(tools=()),
    )
    admission["principal_key"] = "foreign"
    with pytest.raises(ValueError, match="restriction.admission_principal_mismatch"):
        await create(chosen)
