"""Real admission-request replay/conflict: held intents never schedule an effect."""

import asyncio
from uuid import uuid4

import pytest

from app.core.held_intent import held_request_digest
from app.core.workflow import IdempotencyConflictError, IdempotentReplay
from app.core.store import StoreConflictError
from test_held_intent_creation import setup
from test_phase2_database import DSN, db as db

pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


async def counts(owner, tenant):
    return tuple(await owner.fetchrow(
        "SELECT (SELECT count(*) FROM runs WHERE tenant_uuid=$1),"
        "(SELECT count(*) FROM admission_requests WHERE tenant_uuid=$1),"
        "(SELECT count(*) FROM outbox_messages WHERE tenant_uuid=$1)", tenant,
    ))


async def test_key_bound_held_intent_replays_original_persisted_run(db, monkeypatch):
    owner, store, _, _, tenant, *_ = db
    snapshot, fields, _ = await setup(db, monkeypatch)
    key = uuid4().hex
    await store.create_held_intent_run(snapshot, idempotency_key=key, **fields)
    saved = await store.get_run(fields["run_id"])
    assert saved["authority"].intent.context.idempotency_key == key
    before = await counts(owner, tenant)
    replacement = {**fields, "run_id": uuid4().hex}
    with pytest.raises(IdempotentReplay) as replay:
        await store.create_held_intent_run(snapshot, idempotency_key=key, **replacement)
    assert replay.value.response == {"run_id": fields["run_id"], "status": "queued"}
    assert await counts(owner, tenant) == before == (1, 1, 0)
    assert saved["next_action"] == "none"


@pytest.mark.parametrize("change", ["arguments", "ceiling", "budget", "model", "thread", "policy", "destination"])
async def test_server_digest_rejects_changed_held_request(db, monkeypatch, change):
    owner, store, slug, _, tenant, *_ = db
    snapshot, fields, registry = await setup(db, monkeypatch)
    key = uuid4().hex
    await store.create_held_intent_run(snapshot, idempotency_key=key, **fields)
    before = await counts(owner, tenant)
    fields["run_id"] = uuid4().hex
    if change == "arguments":
        snapshot = snapshot.model_copy(update={"arguments_json": '{"recipient":"changed"}'})
    elif change == "ceiling":
        fields["skill_allowlist"] = None
    elif change == "budget":
        fields["max_steps"] = 2
    elif change == "model":
        fields["primary_model"] = "different"
    elif change == "thread":
        fields["thread_id"] = uuid4().hex
        await store.get_or_create_thread_messages(fields["thread_id"], slug)
    elif change == "destination":
        registry.tools["act"].endpoint_url = "https://action.example.test/other"
    else:
        snapshot = snapshot.model_copy(update={"policy_digest": "0" * 64})
    with pytest.raises(IdempotencyConflictError):
        await store.create_held_intent_run(snapshot, idempotency_key=key, **fields)
    assert await counts(owner, tenant) == before


async def test_concurrent_same_key_creates_only_one_held_run(db, monkeypatch):
    owner, store, _, _, tenant, *_ = db
    snapshot, fields, _ = await setup(db, monkeypatch)
    key = uuid4().hex
    results = await asyncio.gather(
        store.create_held_intent_run(snapshot, idempotency_key=key, **fields),
        store.create_held_intent_run(snapshot, idempotency_key=key, **{**fields, "run_id": uuid4().hex}),
        return_exceptions=True,
    )
    assert sum(isinstance(result, IdempotentReplay) for result in results) == 1
    assert sum(isinstance(result, float) for result in results) == 1
    assert await counts(owner, tenant) == (1, 1, 0)


@pytest.mark.parametrize("key", ["", "spaces not allowed", "x" * 256, "\nunsafe"])
async def test_invalid_key_denies_before_any_admission_write(db, monkeypatch, key):
    owner, store, _, _, tenant, *_ = db
    snapshot, fields, _ = await setup(db, monkeypatch)
    with pytest.raises(ValueError, match="restriction.idempotency_key"):
        await store.create_held_intent_run(snapshot, idempotency_key=key, **fields)
    assert await counts(owner, tenant) == (0, 0, 0)


async def test_caller_cannot_supply_replay_digest_or_response(db, monkeypatch):
    owner, store, _, _, tenant, *_ = db
    snapshot, fields, _ = await setup(db, monkeypatch)
    with pytest.raises(ValueError, match="restriction.server_owned_admission"):
        await store.create_held_intent_run(
            snapshot, **fields, admission={"request_digest": "forged", "response": {}},
        )
    assert await counts(owner, tenant) == (0, 0, 0)


async def test_digest_detaches_assigned_run_but_binds_principal_and_null_ceiling(db, monkeypatch):
    snapshot, fields, _ = await setup(db, monkeypatch)
    digest = held_request_digest(snapshot, fields, "trusted-target")
    assert digest == held_request_digest(snapshot, {**fields, "run_id": "replacement"}, "trusted-target")
    assert digest != held_request_digest(snapshot, {**fields, "skill_allowlist": None}, "trusted-target")
    assert digest != held_request_digest(snapshot, fields, "changed-target")
    assert digest != held_request_digest(
        snapshot.model_copy(update={"submitter_id": str(uuid4())}), fields, "trusted-target",
    )


async def test_foreign_tenant_cannot_replay_held_response(db, monkeypatch):
    owner, store, _, other_slug, tenant, *_ = db
    snapshot, fields, _ = await setup(db, monkeypatch)
    key = uuid4().hex
    await store.create_held_intent_run(snapshot, idempotency_key=key, **fields)
    with pytest.raises(ValueError, match="restriction.provenance_mismatch"):
        await store.create_held_intent_run(
            snapshot, idempotency_key=key, **{**fields, "tenant_id": other_slug, "run_id": uuid4().hex},
        )
    assert await counts(owner, tenant) == (1, 1, 0)


@pytest.mark.parametrize("change", ["membership", "principal", "tenant", "policy", "registry", "plugin"])
async def test_matching_replay_rechecks_current_eligibility_transactionally(db, monkeypatch, change):
    owner, store, _, _, tenant, _, principal, old_policy = db
    snapshot, fields, registry = await setup(db, monkeypatch)
    key = uuid4().hex
    await store.create_held_intent_run(snapshot, idempotency_key=key, **fields)
    before = await counts(owner, tenant)
    original = store.get_active_tenant_policy

    async def revoke_after_read(tenant_id):
        record = await original(tenant_id)
        if change == "membership":
            await owner.execute(
                "UPDATE memberships SET status='revoked' WHERE tenant_id=$1 AND principal_id=$2",
                tenant, principal,
            )
        elif change == "principal":
            await owner.execute("UPDATE principals SET status='revoked' WHERE principal_id=$1", principal)
        elif change == "tenant":
            await owner.execute("UPDATE tenants SET status='suspended' WHERE tenant_id=$1", tenant)
        elif change == "policy":
            await owner.execute(
                "UPDATE tenants SET active_policy_revision_id=$2 WHERE tenant_id=$1", tenant, old_policy,
            )
        elif change == "registry":
            registry.fingerprint = "d" * 16
        else:
            registry.plugins["fake"].status = "disabled"
        return record

    monkeypatch.setattr(store, "get_active_tenant_policy", revoke_after_read)
    with pytest.raises((ValueError, StoreConflictError)):
        await store.create_held_intent_run(
            snapshot, idempotency_key=key, **{**fields, "run_id": uuid4().hex},
        )
    assert await counts(owner, tenant) == before == (1, 1, 0)


@pytest.mark.parametrize("revoke", [False, True])
async def test_published_agent_matching_replay_revalidates_current_registry(db, monkeypatch, revoke):
    from test_held_native_agent import agent_setup

    owner, store, _, _, tenant, *_ = db
    snapshot, fields, registry = await agent_setup(db, monkeypatch)
    key = uuid4().hex
    await store.create_held_intent_run(snapshot, idempotency_key=key, **fields)
    original = store.get_active_tenant_policy

    async def current_policy(tenant_id):
        record = await original(tenant_id)
        if revoke:
            registry.plugins["fake"].status = "disabled"
        return record

    monkeypatch.setattr(store, "get_active_tenant_policy", current_policy)
    if revoke:
        with pytest.raises(ValueError, match="Agent skills"):
            await store.create_held_intent_run(
                snapshot, idempotency_key=key, **{**fields, "run_id": uuid4().hex},
            )
    else:
        with pytest.raises(IdempotentReplay) as replay:
            await store.create_held_intent_run(
                snapshot, idempotency_key=key, **{**fields, "run_id": uuid4().hex},
            )
        assert replay.value.response["run_id"] == fields["run_id"]
    assert await counts(owner, tenant) == (1, 1, 0)
