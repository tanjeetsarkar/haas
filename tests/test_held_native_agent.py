"""Native call identity and published-agent ceilings on real held creation."""

import json
from uuid import UUID, uuid4

import pytest

from app.core.agents import AgentConfiguration
from app.core.store import StoreConflictError
from app.core.tenant_policy import TenantPolicyRules, policy_digest
from test_held_intent_creation import setup
from test_phase2_database import DSN, db as db

pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


def native_handler(ctx, args):
    pytest.fail("held creation must not execute a native handler")


async def native_setup(db, monkeypatch):
    snapshot, fields, registry = await setup(db, monkeypatch)
    tool = registry.tools["act"]
    tool.endpoint_url = tool.endpoint_protocol = ""
    tool.handler = native_handler
    tool.execution = "plugin"
    return snapshot, fields, registry


async def test_native_target_is_registered_call_identity_not_a_url(db, monkeypatch):
    _, store, *_ = db
    snapshot, fields, _ = await native_setup(db, monkeypatch)
    await store.create_held_intent_run(snapshot, **fields)
    saved = await store.get_run(fields["run_id"])
    target = json.loads(saved["authority"].intent.context.destination)
    assert target == {
        "version": 1, "kind": "native", "plugin_id": "fake", "plugin_digest": "a" * 64,
        "tool_name": "act", "module": __name__, "qualname": "native_handler",
        "execution": "plugin", "queue": "q_tools_fast", "injects_context": True,
        "schema_sha256": snapshot.schema_sha256,
    }
    assert "url" not in target
    assert saved["next_action"] == "none"


@pytest.mark.parametrize("change", ["handler", "queue", "injection"])
async def test_native_target_change_between_binding_and_insert_denies(db, monkeypatch, change):
    import app.core.held_intent as held
    import app.core.store as storage

    owner, store, *_ = db
    snapshot, fields, registry = await native_setup(db, monkeypatch)
    original = held.validate_held_creation

    async def replaced(*args, **kwargs):
        tool = registry.tools["act"]
        if change == "handler":
            def other(ctx, args):
                pytest.fail("never execute")
            tool.handler = other
        elif change == "queue":
            tool.queue = "q_tools_sandbox"
        else:
            tool.injects_context = False
        await original(*args, **kwargs)

    monkeypatch.setattr(storage, "validate_held_creation", replaced)
    with pytest.raises(ValueError, match="restriction.binding_changed"):
        await store.create_held_intent_run(snapshot, **fields)
    assert await owner.fetchval("SELECT count(*) FROM runs WHERE run_id=$1", fields["run_id"]) == 0


async def agent_setup(db, monkeypatch):
    owner, _, _, _, tenant, *_ = db
    snapshot, fields, registry = await native_setup(db, monkeypatch)
    # Published revision uses the real policy/config digest and storage schema.
    rules_record = await owner.fetchval(
        "SELECT rules FROM tenant_policy_revisions WHERE policy_revision_id=$1",
        UUID(snapshot.policy_revision_id),
    )
    rules = TenantPolicyRules.model_validate_json(rules_record)
    rules = rules.model_copy(update={"model_providers": ["openrouter"], "models": ["model-a", "model-b"]})
    # Test fixture only: create a new immutable policy revision, never mutate one.
    revision = uuid4()
    await owner.execute(
        "INSERT INTO tenant_policy_revisions(policy_revision_id,tenant_id,version,digest,rules) "
        "VALUES($1,$2,3,$3,$4::jsonb)", revision, tenant, policy_digest(rules),
        json.dumps(rules.model_dump(mode="json", exclude_none=True)),
    )
    await owner.execute("UPDATE tenants SET active_policy_revision_id=$2 WHERE tenant_id=$1", tenant, revision)
    snapshot = snapshot.model_copy(update={"policy_revision_id": str(revision), "policy_digest": policy_digest(rules)})
    fields["policy_revision_id"] = str(revision)
    config = AgentConfiguration(
        primary_model="model-a", planner_model="model-b", skills=["unlock"],
        preload_skills=["unlock"], max_steps=1, max_cost_usd=.1,
    )
    agent_id = uuid4()
    await owner.execute(
        "INSERT INTO agent_revisions(revision_id,tenant_id,agent_name,version,state,configuration,"
        "digest,policy_revision_id,registry_fingerprint,created_by,published_at) "
        "VALUES($1,$2,'synthetic',1,'published',$3::jsonb,$4,$5,$6,$7,now())",
        agent_id, tenant, config.model_dump_json(), config.digest(), revision,
        registry.fingerprint, db[6],
    )
    fields["agent_revision_id"] = str(agent_id)
    return snapshot, fields, registry


async def test_published_agent_pins_native_intent_without_configuration_overrides(db, monkeypatch):
    _, store, *_ = db
    snapshot, fields, _ = await agent_setup(db, monkeypatch)
    await store.create_held_intent_run(snapshot, **fields)
    saved = await store.get_run(fields["run_id"])
    assert str(saved["agent_revision_id"]) == fields["agent_revision_id"]
    assert saved["authority"].intent.context.policy_revision == fields["policy_revision_id"]
    assert saved["skill_allowlist"] == ["unlock"]
    assert saved["next_action"] == "none"


@pytest.mark.parametrize("field,value", [
    ("skill_allowlist", None), ("skill_allowlist", ["unlock", "extra"]),
    ("initial_skills", []), ("primary_model", "other"),
    ("max_steps", 2), ("max_cost_usd", .2), ("agent_revision_id", str(uuid4())),
])
async def test_published_agent_overrides_fail_before_insert(db, monkeypatch, field, value):
    owner, store, *_ = db
    snapshot, fields, _ = await agent_setup(db, monkeypatch)
    fields[field] = value
    with pytest.raises((ValueError, StoreConflictError)):
        await store.create_held_intent_run(snapshot, **fields)
    assert await owner.fetchval("SELECT count(*) FROM runs WHERE run_id=$1", fields["run_id"]) == 0
    assert await owner.fetchval("SELECT count(*) FROM outbox_messages WHERE run_id=$1", fields["run_id"]) == 0
