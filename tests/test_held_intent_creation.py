"""Internal fixed-endpoint intent admission persists inert authority on real PostgreSQL."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.core import held_intent
from app.core.intent_preflight import preflight_exact_intent
from app.core.plugins import PluginRecord, PluginRegistry, RegisteredSkill
from app.core.tenant_policy import PluginGrant, TenantPolicy, TenantPolicyRules, policy_digest
from app.core.store import StoreConflictError, ThreadOwnershipError
from test_phase2_database import DSN, db as db

pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


async def setup(db, monkeypatch, protocol="http"):
    owner, store, slug, _, tenant, _, principal, _ = db
    registry = PluginRegistry()
    registry._current_plugin = "fake"
    registry.register_external_manifest({
        "name": "act", "description": "synthetic", "endpoint_url": "https://action.example.test/send",
        "protocol": protocol,
        "parameters": {"properties": {"recipient": {"type": "string"}}, "required": ["recipient"]},
    })
    registry.plugins["fake"] = PluginRecord("fake", "loaded", digest="a" * 64)
    registry.skills["unlock"] = RegisteredSkill(
        "unlock", "", "", Path("."), plugin_id="fake", required_tools=["act"],
    )
    registry.fingerprint = "b" * 16
    monkeypatch.setattr(held_intent, "registry", registry)
    rules = TenantPolicyRules(
        plugin_grants=[PluginGrant(plugin_id="fake", digest="a" * 64, skills=["unlock"])],
        destination_hosts=["action.example.test"],
    )
    revision = uuid4()
    await owner.execute(
        "INSERT INTO tenant_policy_revisions(policy_revision_id,tenant_id,version,digest,rules) "
        "VALUES($1,$2,2,$3,$4::jsonb)",
        revision, tenant, policy_digest(rules), json.dumps(rules.model_dump(mode="json", exclude_none=True)),
    )
    await owner.execute("UPDATE tenants SET active_policy_revision_id=$2 WHERE tenant_id=$1", tenant, revision)
    snapshot = preflight_exact_intent(
        tool_name="act", arguments={"recipient": "reviewed"}, tools=("act",),
        preload_skills=("unlock",), skill_ceiling={"unlock"}, tenant_id=str(tenant),
        authenticated_submitter_id=str(principal),
        policy=TenantPolicy(str(revision), 2, policy_digest(rules), rules), registry=registry,
    )
    run = uuid4().hex
    await store.get_or_create_thread_messages(run, slug)
    fields = {
        "run_id": run, "thread_id": run, "tenant_id": slug, "traceparent": "",
        "primary_model": "model-a", "planner_model": "model-b",
        "initial_skills": ["unlock"], "initial_messages": [{"role": "user", "content": "review"}],
        "max_steps": 1, "max_cost_usd": .1, "webhook_url": None,
        "skill_allowlist": ["unlock"], "registry_fingerprint": registry.fingerprint,
        "submitted_by_principal_id": str(principal), "policy_revision_id": str(revision),
    }
    return snapshot, fields, registry


@pytest.mark.parametrize("protocol", ["http", "mcp_http"])
async def test_real_endpoint_preflight_creates_complete_immutable_held_intent(db, monkeypatch, protocol):
    owner, store, *_ = db
    snapshot, fields, registry = await setup(db, monkeypatch, protocol)
    await store.create_held_intent_run(snapshot, **fields)
    saved = await store.get_run(fields["run_id"])
    intent = saved["authority"].intent
    assert intent.arguments_json == snapshot.arguments_json
    assert intent.context.run_id == fields["run_id"]
    target = json.loads(intent.context.destination)
    assert target == {
        "version": 1, "url": registry.tools["act"].endpoint_url,
        "protocol": protocol, "queue": registry.tools["act"].queue,
        "schema_sha256": snapshot.schema_sha256,
    }
    assert saved["next_action"] == "none"
    assert await owner.fetchval(
        "SELECT count(*) FROM outbox_messages WHERE run_id=$1", fields["run_id"],
    ) == 0
    assert await store.acquire_lease(
        message_id=uuid4().hex, run_id=fields["run_id"], action="step",
        expected_state_version=0, owner="must-not-run",
    ) is None


@pytest.mark.parametrize("change", [
    "policy", "schema", "plugin", "registry", "destination", "native", "submitter",
    "preloads", "ceiling", "foreign", "phase", "agent",
])
async def test_held_creation_rechecks_before_insert_and_outbox(db, monkeypatch, change):
    owner, store, slug, _, tenant, _, principal, old_policy = db
    snapshot, fields, registry = await setup(db, monkeypatch)
    if change == "policy":
        await owner.execute(
            "UPDATE tenants SET active_policy_revision_id=$2 WHERE tenant_id=$1", tenant, old_policy,
        )
    elif change == "schema":
        registry.tools["act"].args_schema.model_fields["recipient"].description = "changed"
        registry.tools["act"].args_schema.model_rebuild(force=True)
    elif change == "plugin":
        registry.plugins["fake"].digest = "c" * 64
    elif change == "registry":
        registry.fingerprint = "c" * 16
    elif change == "destination":
        registry.tools["act"].endpoint_url = "https://denied.example.test/send"
    elif change == "native":
        registry.tools["act"].endpoint_url = ""
    elif change == "submitter":
        await owner.execute(
            "UPDATE memberships SET status='revoked' WHERE tenant_id=$1 AND principal_id=$2",
            tenant, principal,
        )
    elif change == "preloads":
        fields["initial_skills"] = []
    elif change == "ceiling":
        fields["skill_allowlist"] = []
    elif change == "foreign":
        fields["tenant_id"] = db[3]
    elif change == "agent":
        fields["agent_revision_id"] = str(uuid4())
    else:
        from app.core.run_phases import PhaseConfig
        fields["phase_config"] = PhaseConfig.model_validate({})
    with pytest.raises((ValueError, StoreConflictError, ThreadOwnershipError)):
        await store.create_held_intent_run(snapshot, **fields)
    assert await owner.fetchval("SELECT count(*) FROM runs WHERE run_id=$1", fields["run_id"]) == 0
    assert await owner.fetchval("SELECT count(*) FROM outbox_messages WHERE run_id=$1", fields["run_id"]) == 0


async def test_adapter_null_omission_cannot_change_reviewed_payload(db, monkeypatch):
    from pydantic import BaseModel

    _, store, *_ = db
    snapshot, fields, registry = await setup(db, monkeypatch)

    class Nullable(BaseModel):
        recipient: str | None

    registry.tools["act"].args_schema = Nullable
    snapshot = snapshot.model_copy(update={"arguments_json": '{"recipient":null}'})
    with pytest.raises(ValueError, match="restriction.adapter_arguments_changed"):
        await store.create_held_intent_run(snapshot, **fields)


async def test_policy_change_between_preflight_read_and_transaction_is_rejected(db, monkeypatch):
    owner, store, _, _, tenant, _, _, old_policy = db
    snapshot, fields, _ = await setup(db, monkeypatch)
    original = store.get_active_tenant_policy

    async def stale_record(tenant_id):
        record = await original(tenant_id)
        await owner.execute(
            "UPDATE tenants SET active_policy_revision_id=$2 WHERE tenant_id=$1", tenant, old_policy,
        )
        return record

    monkeypatch.setattr(store, "get_active_tenant_policy", stale_record)
    with pytest.raises(StoreConflictError, match="Tenant policy changed"):
        await store.create_held_intent_run(snapshot, **fields)
    assert await owner.fetchval("SELECT count(*) FROM runs WHERE run_id=$1", fields["run_id"]) == 0


async def test_destination_change_after_binding_is_rejected_in_transaction(db, monkeypatch):
    import app.core.store as store_module

    owner, store, *_ = db
    snapshot, fields, registry = await setup(db, monkeypatch)
    original = held_intent.validate_held_creation

    async def changed_destination(*args, **kwargs):
        registry.tools["act"].endpoint_url = "https://action.example.test/other"
        await original(*args, **kwargs)

    monkeypatch.setattr(store_module, "validate_held_creation", changed_destination)
    with pytest.raises(ValueError, match="restriction.binding_changed"):
        await store.create_held_intent_run(snapshot, **fields)
    assert await owner.fetchval("SELECT count(*) FROM runs WHERE run_id=$1", fields["run_id"]) == 0


@pytest.mark.parametrize("stored_ceiling", [None, [], ["unlock", "other"]])
async def test_transaction_rejects_replaced_pinned_skill_ceiling_before_insert(db, monkeypatch, stored_ceiling):
    owner, store, *_ = db
    snapshot, fields, _ = await setup(db, monkeypatch)
    assert snapshot.skill_ceiling == ("unlock",)
    fields["skill_allowlist"] = stored_ceiling
    with pytest.raises(ValueError, match="restriction.skill_ceiling_changed"):
        await store.create_held_intent_run(snapshot, **fields)
    assert await owner.fetchval("SELECT count(*) FROM runs WHERE run_id=$1", fields["run_id"]) == 0
    assert await owner.fetchval(
        "SELECT count(*) FROM outbox_messages WHERE run_id=$1", fields["run_id"],
    ) == 0


async def test_null_ceiling_persists_only_as_null_and_rejects_empty_replacement(db, monkeypatch):
    from app.core.intent_preflight import preflight_exact_intent

    owner, store, _, _, tenant, _, principal, _ = db
    snapshot, fields, registry = await setup(db, monkeypatch)
    policy = TenantPolicy.from_record(await store.get_active_tenant_policy(str(tenant)))
    snapshot = preflight_exact_intent(
        tool_name="act", arguments=json.loads(snapshot.arguments_json), tools=("act",),
        preload_skills=("unlock",), skill_ceiling=None, tenant_id=str(tenant),
        authenticated_submitter_id=str(principal), policy=policy, registry=registry,
    )
    fields["skill_allowlist"] = []
    with pytest.raises(ValueError, match="restriction.skill_ceiling_changed"):
        await store.create_held_intent_run(snapshot, **fields)
    assert await owner.fetchval("SELECT count(*) FROM runs WHERE run_id=$1", fields["run_id"]) == 0
    fields["skill_allowlist"] = None
    await store.create_held_intent_run(snapshot, **fields)
    saved = await store.get_run(fields["run_id"])
    assert saved["skill_allowlist"] is None
    assert saved["next_action"] == "none"
