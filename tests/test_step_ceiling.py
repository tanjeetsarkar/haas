"""Generic step ceilings do not grant larger tenant budgets."""

import pytest
from pydantic import ValidationError

from app.core.agents import AgentConfiguration
from app.core.config import Settings
from app.core.openai_compat import ChatCompletionRequest, HaasOptions, ResponsesRequest
from app.core.plugins import PluginRegistry
from app.core.tenant_policy import TenantPolicy, TenantPolicyRules, policy_digest
from app.main import CreateRunRequest

_MODEL = "example/model"
_AGENT = {"primary_model": _MODEL, "planner_model": _MODEL}


@pytest.mark.parametrize(
    ("model", "required", "default"),
    [
        (CreateRunRequest, {"prompt": "run"}, None),
        (HaasOptions, {}, None),
        (TenantPolicyRules, {}, 15),
        (AgentConfiguration, _AGENT, 15),
    ],
)
def test_step_ceiling_contract(model, required, default):
    assert model.model_validate(required).max_steps == default
    for steps in (1, 50, 1_000):
        assert model.model_validate({**required, "max_steps": steps}).max_steps == steps
    for steps in (0, 1_001):
        with pytest.raises(ValidationError):
            model.model_validate({**required, "max_steps": steps})


def test_configured_default_is_still_fifteen():
    assert Settings.model_fields["default_max_steps"].default == 15


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (ChatCompletionRequest, {"messages": [{"role": "user", "content": "run"}]}),
        (ResponsesRequest, {"input": "run"}),
    ],
)
def test_facade_step_ceiling(model, payload):
    request = model.model_validate(
        {"model": "haas", **payload, "haas": {"max_steps": 1_000}}
    )
    assert request.haas.max_steps == 1_000
    with pytest.raises(ValidationError):
        model.model_validate(
            {"model": "haas", **payload, "haas": {"max_steps": 1_001}}
        )


def test_agent_step_ceiling_does_not_expand_tenant_policy():
    rules = TenantPolicyRules(model_providers=["openrouter"], models=[_MODEL])
    agent = AgentConfiguration(**_AGENT, max_steps=1_000)
    with pytest.raises(ValueError, match="budgets exceed"):
        agent.validate_policy(
            TenantPolicy("revision", 1, policy_digest(rules), rules), PluginRegistry()
        )
    expanded = rules.model_copy(update={"max_steps": 1_000})
    agent.validate_policy(
        TenantPolicy("revision", 2, policy_digest(expanded), expanded), PluginRegistry()
    )
