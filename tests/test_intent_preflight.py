"""Real registry/policy intersection and strict canonical snapshot; no scheduled work."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.core.intent_preflight import IntentPreflightError, preflight_exact_intent
from app.core.plugins import PluginRecord, PluginRegistry, RegisteredSkill, RegisteredTool
from app.core.run_restrictions import RunAuthority
from app.core.tenant_policy import PluginGrant, TenantPolicy, TenantPolicyRules, policy_digest


class Args(BaseModel):
    recipient: str
    count: int = 1
    payload: dict
    nullable: str | None


@pytest.fixture
def inputs():
    def never_execute(*a):
        pytest.fail("preflight executed a handler")

    registry = PluginRegistry()
    registry.fingerprint = "b" * 16  # Actual registry format, not a fabricated RPC revision.
    registry.plugins["fake"] = PluginRecord("fake", "loaded", digest="a" * 64)
    registry.tools["act"] = RegisteredTool("act", "", Args, never_execute, False, plugin_id="fake")
    registry.skills["unlock"] = RegisteredSkill(
        "unlock", "", "", Path("."), plugin_id="fake", required_tools=["act"],
    )
    rules = TenantPolicyRules(plugin_grants=[
        PluginGrant(plugin_id="fake", digest="a" * 64, skills=["unlock"]),
    ])
    return {
        "tool_name": "act", "arguments": {
            "recipient": " exact ", "count": 1, "payload": {"items": [2, 1]}, "nullable": None,
        }, "tools": ("act",), "preload_skills": ("unlock",), "skill_ceiling": {"unlock"},
        "tenant_id": str(uuid4()), "authenticated_submitter_id": str(uuid4()),
        "policy": TenantPolicy(str(uuid4()), 1, policy_digest(rules), rules),
        "registry": registry,
    }


def test_preflight_pins_full_effective_bytes_and_real_revisions_immutably(inputs):
    snapshot = preflight_exact_intent(**inputs)
    assert json.loads(snapshot.arguments_json) == inputs["arguments"]
    assert snapshot.registry_fingerprint == inputs["registry"].fingerprint
    assert snapshot.plugin_digest == "a" * 64
    assert snapshot.skill_ceiling == ("unlock",)
    assert len(snapshot.schema_sha256) == 64
    inputs["arguments"]["payload"]["items"].append(3)
    assert json.loads(snapshot.arguments_json)["payload"]["items"] == [2, 1]
    with pytest.raises(ValidationError):
        snapshot.arguments_json = "{}"
    reordered = {**inputs, "arguments": dict(reversed(json.loads(snapshot.arguments_json).items()))}
    assert preflight_exact_intent(**reordered) == snapshot


@pytest.mark.parametrize("arguments", [
    {"recipient": "a", "payload": {}, "nullable": None},  # Default insertion.
    {"recipient": "a", "count": "1", "payload": {}, "nullable": None},
    {"recipient": "a", "count": 1, "payload": {}, "nullable": None, "extra": 1},
    {"recipient": "a", "count": 1, "payload": {"bad": float("nan")}, "nullable": None},
    {"recipient": "a" * 65536, "count": 1, "payload": {}, "nullable": None},
    [], '{"recipient":"a"}',
])
def test_preflight_rejects_nonexact_arguments(inputs, arguments):
    with pytest.raises(IntentPreflightError) as error:
        preflight_exact_intent(**{**inputs, "arguments": arguments})
    assert error.value.status_code == 422
    assert str(error.value) == "restriction.arguments"


@pytest.mark.parametrize("change,status", [
    ({"authenticated_submitter_id": None}, 401),
    ({"tenant_id": "slug"}, 422),
    ({"tools": None}, 422), ({"tools": ()}, 422),
    ({"tools": ("act", "extra")}, 422), ({"tools": ["act"]}, 422),
    ({"preload_skills": ()}, 403), ({"preload_skills": ("missing",)}, 403),
    ({"preload_skills": ("unlock", "unlock")}, 422),
    ({"skill_ceiling": set()}, 403),
    ({"parent_authority": RunAuthority()}, 403),
])
def test_preflight_requires_authenticated_singleton_and_no_authority_expansion(inputs, change, status):
    with pytest.raises(IntentPreflightError) as error:
        preflight_exact_intent(**{**inputs, **change})
    assert error.value.status_code == status


@pytest.mark.parametrize("change", ["revoked", "plugin_digest", "unready", "skill_owner", "tool_owner"])
def test_preflight_policy_plugin_skill_intersection_is_conjunctive(inputs, change):
    registry = inputs["registry"]
    if change == "revoked":
        registry.plugins["fake"].status = "disabled"
    elif change == "plugin_digest":
        registry.plugins["fake"].digest = "c" * 64
    elif change == "unready":
        registry.skills["unlock"].ready = False
    elif change == "skill_owner":
        registry.skills["unlock"].plugin_id = "foreign"
    else:
        registry.tools["act"].plugin_id = "foreign"
    with pytest.raises(IntentPreflightError) as error:
        preflight_exact_intent(**inputs)
    assert error.value.status_code == 403


@pytest.mark.parametrize("change", ["static", "schema", "policy_digest", "registry"])
def test_preflight_unavailable_or_mutated_revision_denies(inputs, change):
    registry = inputs["registry"]
    if change == "static":
        registry.tools["act"].execution = "static"
    elif change == "schema":
        registry.tools["act"].parameters_schema = {"type": "object"}
    elif change == "policy_digest":
        inputs["policy"].rules.plugin_grants.clear()
    else:
        registry.fingerprint = ""
    with pytest.raises(IntentPreflightError) as error:
        preflight_exact_intent(**inputs)
    assert error.value.status_code == 409


def test_alias_schema_is_bound_and_changes_are_visible(inputs):
    class AliasArgs(BaseModel):
        recipient: str = Field(alias="target")

    inputs["registry"].tools["act"].args_schema = AliasArgs
    inputs["arguments"] = {"target": "unchanged"}
    snapshot = preflight_exact_intent(**inputs)
    assert snapshot.arguments_json == '{"target":"unchanged"}'
    class RevisedArgs(BaseModel):
        target: str

    inputs["registry"].tools["act"].args_schema = RevisedArgs
    assert snapshot.schema_sha256 != preflight_exact_intent(**inputs).schema_sha256
    inputs["registry"].tools["act"].args_schema = AliasArgs
    inputs["arguments"] = {"recipient": "unchanged"}
    with pytest.raises(IntentPreflightError):
        preflight_exact_intent(**inputs)


def test_validator_transform_cannot_rewrite_reviewed_arguments(inputs):
    class TransformedArgs(BaseModel):
        recipient: str

        @field_validator("recipient")
        @classmethod
        def trim(cls, value):
            return value.strip()

    inputs["registry"].tools["act"].args_schema = TransformedArgs
    inputs["arguments"] = {"recipient": " exact "}
    with pytest.raises(IntentPreflightError, match="restriction.arguments"):
        preflight_exact_intent(**inputs)


def test_pins_are_detached_and_fresh_policy_replacement_cannot_reuse_them(inputs):
    previous = preflight_exact_intent(**inputs)
    rules = TenantPolicyRules()
    inputs["policy"] = TenantPolicy(str(uuid4()), 2, policy_digest(rules), rules)
    with pytest.raises(IntentPreflightError, match="restriction.policy_denied"):
        preflight_exact_intent(**inputs)
    assert previous.plugin_digest == "a" * 64
    assert previous.preload_skills == ("unlock",)


def test_hidden_preload_is_permitted_but_does_not_add_companion_tools(inputs):
    inputs["registry"].skills["unlock"].hidden = True
    inputs["registry"].skills["unlock"].required_tools.append("extra")
    snapshot = preflight_exact_intent(**inputs)
    assert snapshot.tool_name == "act"
    assert snapshot.preload_skills == ("unlock",)


def test_ceiling_is_immutable_detached_and_null_remains_distinct(inputs):
    ceiling = ["unlock", "other"]
    snapshot = preflight_exact_intent(**{**inputs, "skill_ceiling": ceiling})
    ceiling.append("later")
    assert snapshot.skill_ceiling == ("other", "unlock")
    assert preflight_exact_intent(**{
        **inputs, "skill_ceiling": ["other", "unlock"],
    }) == snapshot
    assert preflight_exact_intent(**{**inputs, "skill_ceiling": None}).skill_ceiling is None
    with pytest.raises(IntentPreflightError):
        preflight_exact_intent(**{**inputs, "skill_ceiling": []})
