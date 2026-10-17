from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from app.core.plugins import (
    PluginRecord,
    PluginRegistry,
    RegisteredSkill,
    RegisteredTool,
)
from app.core.tenant_policy import (
    PluginGrant,
    TenantPolicy,
    TenantPolicyIntegrityError,
    TenantPolicyRules,
    normalize_hostname,
    policy_digest,
)


def test_policy_defaults_deny_models_plugins_and_destinations():
    rules = TenantPolicyRules()

    assert not rules.model_is_allowed("provider/model")
    assert not rules.plugin_grants
    assert not rules.destination_hosts


def test_policy_model_grants_require_provider_and_exact_model():
    rules = TenantPolicyRules(
        model_providers=["OpenRouter"],
        models=["provider/model-a"],
    )

    assert rules.model_is_allowed("provider/model-a")
    assert not rules.model_is_allowed("provider/model-b")

    rules_without_provider = TenantPolicyRules(models=["provider/model-a"])
    assert not rules_without_provider.model_is_allowed("provider/model-a")


def test_policy_destination_hosts_are_exact_and_normalized():
    rules = TenantPolicyRules(destination_hosts=["ExAmPle.com."])
    policy = TenantPolicy("revision", 1, policy_digest(rules), rules)

    assert rules.destination_hosts == ["example.com"]
    assert policy.destination_is_allowed("https://EXAMPLE.COM/path")
    assert not policy.destination_is_allowed("https://sub.example.com/path")
    assert not policy.destination_is_allowed("https://example.net/path")

    with pytest.raises(ValueError, match="exact hostname"):
        normalize_hostname("*.example.com")


def test_policy_revision_rejects_digest_mismatch_and_malformed_rules():
    rules = TenantPolicyRules(
        model_providers=["openrouter"],
        models=["provider/model-a"],
    )
    record = {
        "policy_revision_id": "revision-1",
        "version": 1,
        "digest": policy_digest(rules),
        "rules": rules.model_dump(mode="json", exclude_none=True),
    }

    policy = TenantPolicy.from_record(record)
    assert policy.policy_revision_id == "revision-1"
    assert policy.rules.model_is_allowed("provider/model-a")

    with pytest.raises(TenantPolicyIntegrityError, match="digest"):
        TenantPolicy.from_record({**record, "digest": "0" * 64})
    with pytest.raises(TenantPolicyIntegrityError, match="malformed"):
        TenantPolicy.from_record(
            {
                **record,
                "digest": policy_digest({"unexpected": True}),
                "rules": {"unexpected": True},
            }
        )


def test_plugin_access_requires_exact_digest_and_skill_grant():
    class EmptyArgs(BaseModel):
        pass

    async def handler() -> str:
        return "ok"

    digest = "a" * 64
    registry = PluginRegistry()
    registry.plugins["plugin-a"] = PluginRecord(
        "plugin-a",
        "loaded",
        skills=["skill-a", "skill-b"],
        tools=["tool-a", "tool-b"],
        digest=digest,
    )
    registry.skills["skill-a"] = RegisteredSkill(
        "skill-a",
        "A",
        "",
        Path("."),
        plugin_id="plugin-a",
        required_tools=["tool-a"],
    )
    registry.skills["skill-b"] = RegisteredSkill(
        "skill-b",
        "B",
        "",
        Path("."),
        plugin_id="plugin-a",
        required_tools=["tool-b"],
    )
    registry.tools["tool-a"] = RegisteredTool(
        "tool-a", "A", EmptyArgs, handler, False, plugin_id="plugin-a"
    )
    registry.tools["tool-b"] = RegisteredTool(
        "tool-b", "B", EmptyArgs, handler, False, plugin_id="plugin-a"
    )

    policy = TenantPolicy(
        "revision",
        1,
        "digest",
        TenantPolicyRules(
            plugin_grants=[
                PluginGrant(plugin_id="plugin-a", digest=digest, skills=["skill-a"])
            ]
        ),
    )
    access = policy.resolve_plugin_access(registry)

    assert access.skill_names == {"skill-a"}
    assert access.plugin_ids == {"plugin-a"}
    assert access.tool_names == {"tool-a"}

    stale_policy = TenantPolicy(
        "revision",
        1,
        "digest",
        TenantPolicyRules(
            plugin_grants=[
                PluginGrant(plugin_id="plugin-a", digest="b" * 64, skills=["skill-a"])
            ]
        ),
    )
    assert not stale_policy.resolve_plugin_access(registry).skill_names


@pytest.mark.parametrize(
    "rules",
    [
        {"model_providers": ["openrouter", "OpenRouter"]},
        {"models": ["model", "model"]},
        {"destination_hosts": ["example.com", "EXAMPLE.COM."]},
        {"plugin_grants": [{"plugin_id": "x", "digest": "not-a-digest"}]},
    ],
)
def test_policy_rejects_ambiguous_or_invalid_grants(rules: dict[str, Any]):
    with pytest.raises(ValueError):
        TenantPolicyRules.model_validate(rules)
