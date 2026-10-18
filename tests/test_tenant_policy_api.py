from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

import app.main as main_module
from app.core.identity import AuthenticatedIdentity
from app.core.plugins import (
    PluginRecord,
    PluginRegistry,
    RegisteredSkill,
    RegisteredTool,
)
from app.core.store import ThreadOwnershipError
from app.core.tenant_policy import (
    PluginGrant,
    TenantPolicyRules,
    policy_digest,
)

_IDENTITY = AuthenticatedIdentity(
    tenant_id="acme",
    principal_id="principal-1",
    issuer="https://identity.example.test/",
    subject="user-1",
    roles=frozenset({"tenant_admin"}),
    credential_type="oidc",
)


class MemoryPolicyStore:
    def __init__(self, policy: dict[str, Any] | None) -> None:
        self.policy = policy
        self.runs: list[dict[str, Any]] = []

    async def get_active_tenant_policy(self, _tenant_id: str):
        return self.policy

    async def get_or_create_thread_messages(self, _thread_id: str, _tenant_id: str):
        return []

    async def store_ephemeral_secrets(self, _run_id: str, _secrets: dict[str, str]):
        pass

    async def clear_ephemeral_secrets(self, _run_id: str):
        pass

    async def create_run(self, **values: Any):
        self.runs.append(values)

    async def publish_event(self, *_args: Any, **_kwargs: Any):
        pass


class DispatchRecorder:
    """Runs are enqueued in the same transaction that creates them (transactional outbox)."""

    def __init__(self, store: MemoryPolicyStore) -> None:
        self.calls = store.runs


def _policy_record(rules: TenantPolicyRules) -> dict[str, Any]:
    return {
        "policy_revision_id": "11111111-1111-4111-8111-111111111111",
        "version": 3,
        "digest": policy_digest(rules),
        "rules": rules.model_dump(mode="json", exclude_none=True),
    }


async def _client(monkeypatch: pytest.MonkeyPatch, policy):
    store = MemoryPolicyStore(policy)
    dispatched = DispatchRecorder(store)

    async def authenticate(_credential: str, _tenant: str):
        return _IDENTITY

    monkeypatch.setattr(main_module, "store", store)
    monkeypatch.setattr(
        main_module.identity_authenticator, "authenticate", authenticate
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main_module.app), base_url="http://test"
    )
    return client, store, dispatched


async def test_authenticated_run_fails_closed_without_active_tenant_policy(
    monkeypatch,
):
    client, store, dispatched = await _client(monkeypatch, None)
    async with client:
        response = await client.post(
            "/v1/runs",
            headers={"Authorization": "Bearer opaque", "X-Tenant-ID": "acme"},
            json={"prompt": "run"},
        )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "Tenant has no active policy revision."
    assert store.runs == []
    assert dispatched.calls == []


async def test_authenticated_plugin_catalog_fails_closed_without_active_policy(
    monkeypatch,
):
    client, _store, _dispatched = await _client(monkeypatch, None)
    async with client:
        response = await client.get(
            "/v1/plugins",
            headers={
                "Authorization": "Bearer test-credential",
                "X-Tenant-ID": "acme",
            },
        )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "Tenant has no active policy revision."


async def test_authenticated_plugin_catalog_hides_ungranted_metadata(monkeypatch):
    class EmptyArgs(BaseModel):
        pass

    async def handler() -> str:
        return "ok"

    registry = PluginRegistry()
    registry.plugins["plugin-a"] = PluginRecord(
        "plugin-a",
        "loaded",
        skills=["granted-skill", "hidden-skill"],
        tools=["granted-tool", "hidden-tool"],
        digest="a" * 64,
    )
    registry.plugins["plugin-b"] = PluginRecord(
        "plugin-b",
        "loaded",
        skills=["other-skill"],
        tools=["other-tool"],
        digest="b" * 64,
    )
    for name, plugin, tool in (
        ("granted-skill", "plugin-a", "granted-tool"),
        ("hidden-skill", "plugin-a", "hidden-tool"),
        ("other-skill", "plugin-b", "other-tool"),
    ):
        registry.skills[name] = RegisteredSkill(
            name, name, "", Path("."), plugin_id=plugin, required_tools=[tool]
        )
    for name, plugin in (
        ("granted-tool", "plugin-a"),
        ("hidden-tool", "plugin-a"),
        ("other-tool", "plugin-b"),
    ):
        registry.tools[name] = RegisteredTool(
            name, name, EmptyArgs, handler, False, plugin_id=plugin
        )

    rules = TenantPolicyRules(
        plugin_grants=[
            PluginGrant(
                plugin_id="plugin-a",
                digest="a" * 64,
                skills=["granted-skill"],
            )
        ]
    )
    client, _, _ = await _client(monkeypatch, _policy_record(rules))
    monkeypatch.setattr(main_module, "registry", registry)
    async with client:
        response = await client.get(
            "/v1/plugins",
            headers={"Authorization": "Bearer opaque", "X-Tenant-ID": "acme"},
        )

    assert response.status_code == 200
    body = response.json()
    assert [plugin["id"] for plugin in body["plugins"]] == ["plugin-a"]
    assert body["plugins"][0]["skills"] == ["granted-skill"]
    assert body["plugins"][0]["tools"] == ["granted-tool"]
    assert [skill["name"] for skill in body["skills"]] == ["granted-skill"]
    assert [tool["name"] for tool in body["tools"]] == ["granted-tool"]
    assert body["fingerprint"] != registry.fingerprint


async def test_authenticated_run_uses_policy_models_budgets_and_revision(
    monkeypatch,
):
    rules = TenantPolicyRules(
        model_providers=["openrouter"],
        models=["example/provider-model"],
        default_primary_model="example/provider-model",
        default_planner_model="example/provider-model",
        max_steps=4,
        max_cost_usd=0.1,
    )
    client, store, dispatched = await _client(monkeypatch, _policy_record(rules))
    async with client:
        response = await client.post(
            "/v1/runs",
            headers={"Authorization": "Bearer opaque", "X-Tenant-ID": "acme"},
            json={"prompt": "run"},
        )

    assert response.status_code == 202
    assert len(store.runs) == 1
    run = store.runs[0]
    assert run["primary_model"] == "example/provider-model"
    assert run["planner_model"] == "example/provider-model"
    assert run["max_steps"] == 4
    assert run["max_cost_usd"] == pytest.approx(0.1)
    assert run["policy_revision_id"] == "11111111-1111-4111-8111-111111111111"
    assert dispatched.calls


@pytest.mark.parametrize(
    "override",
    [
        {"primary_model": "other/model"},
        {"max_steps": 5},
        {"max_cost_usd": 0.2},
    ],
)
async def test_authenticated_run_rejects_policy_expansions(
    monkeypatch, override: dict[str, Any]
):
    rules = TenantPolicyRules(
        model_providers=["openrouter"],
        models=["example/provider-model"],
        default_primary_model="example/provider-model",
        default_planner_model="example/provider-model",
        max_steps=4,
        max_cost_usd=0.1,
    )
    client, store, dispatched = await _client(monkeypatch, _policy_record(rules))
    async with client:
        response = await client.post(
            "/v1/runs",
            headers={"Authorization": "Bearer opaque", "X-Tenant-ID": "acme"},
            json={"prompt": "run", **override},
        )

    assert response.status_code == 403
    assert store.runs == []
    assert dispatched.calls == []


async def test_authenticated_run_webhook_must_match_exact_policy_host(
    monkeypatch,
):
    monkeypatch.setattr(main_module, "validate_safe_outbound_url", lambda _url: None)
    rules = TenantPolicyRules(
        model_providers=["openrouter"],
        models=["example/provider-model"],
        default_primary_model="example/provider-model",
        default_planner_model="example/provider-model",
        destination_hosts=["hooks.example.test"],
    )
    client, store, dispatched = await _client(monkeypatch, _policy_record(rules))
    async with client:
        allowed = await client.post(
            "/v1/runs",
            headers={"Authorization": "Bearer test-credential", "X-Tenant-ID": "acme"},
            json={
                "prompt": "run",
                "webhook_url": "https://hooks.example.test/refund",
            },
        )
        denied = await client.post(
            "/v1/runs",
            headers={"Authorization": "Bearer test-credential", "X-Tenant-ID": "acme"},
            json={
                "prompt": "run",
                "webhook_url": "https://sub.hooks.example.test/refund",
            },
        )

    assert allowed.status_code == 202
    assert store.runs[0]["webhook_url"] == "https://hooks.example.test/refund"
    assert denied.status_code == 403
    assert len(store.runs) == len(dispatched.calls) == 1


async def test_authenticated_run_cannot_reuse_another_tenants_thread(monkeypatch):
    rules = TenantPolicyRules(
        model_providers=["openrouter"],
        models=["example/provider-model"],
        default_primary_model="example/provider-model",
        default_planner_model="example/provider-model",
    )
    client, store, dispatched = await _client(monkeypatch, _policy_record(rules))

    async def foreign_thread(_thread_id: str, _tenant_id: str):
        raise ThreadOwnershipError("thread belongs to another tenant")

    monkeypatch.setattr(store, "get_or_create_thread_messages", foreign_thread)
    async with client:
        response = await client.post(
            "/v1/runs",
            headers={
                "Authorization": "Bearer test-credential",
                "X-Tenant-ID": "acme",
            },
            json={"prompt": "run", "thread_id": "foreign-thread"},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Thread not found."
    assert store.runs == []
    assert dispatched.calls == []
