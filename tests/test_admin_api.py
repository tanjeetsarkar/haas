from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest

import app.main as main_module
from app.core.identity import AuthenticatedIdentity
from app.core.store import StoreConflictError, StoreNotFoundError
from app.core.tenant_policy import TenantPolicyRules, policy_digest

_ACTOR_ID = "75850cfb-1874-480b-b35e-75a3cb999139"


class MemoryAdminStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.events: list[dict[str, Any]] = []

    async def list_tenant_memberships(self, tenant_selector: str):
        self.calls.append(("list_memberships", {"tenant": tenant_selector}))
        return []

    async def grant_tenant_membership(self, **values: Any):
        self.calls.append(("grant_membership", values))
        if values["subject"] == "missing":
            raise StoreNotFoundError("Tenant not found.")
        if values["subject"] == "conflict":
            raise StoreConflictError("Principal already exists.")
        return {"principal_id": _ACTOR_ID, "role": values["role"], "status": "active"}

    async def revoke_tenant_membership(self, **values: Any):
        self.calls.append(("revoke_membership", values))

    async def list_tenant_api_keys(self, tenant_selector: str):
        self.calls.append(("list_api_keys", {"tenant": tenant_selector}))
        return []

    async def create_tenant_api_key(self, **values: Any):
        self.calls.append(("create_api_key", values))
        return {
            "key_id": "11111111-1111-4111-8111-111111111111",
            "principal_id": values["principal_id"],
            "key_prefix": values["credential"][:16],
            "name": values["name"],
            "created_at": datetime.now(UTC),
            "expires_at": datetime.now(UTC) + timedelta(days=values["expires_in_days"]),
        }

    async def revoke_tenant_api_key(self, **values: Any):
        self.calls.append(("revoke_api_key", values))

    async def get_active_tenant_policy(self, _tenant_selector: str):
        return None

    async def activate_tenant_policy_revision(self, **values: Any):
        self.calls.append(("activate_policy", values))
        return {
            "policy_revision_id": "22222222-2222-4222-8222-222222222222",
            "version": 1,
            "digest": values["digest"],
            "rules": values["rules"],
        }

    async def list_audit_events(
        self, _tenant_selector: str, *, limit: int, before=None
    ):
        self.calls.append(("list_audit_events", {"limit": limit, "before": before}))
        return self.events[:limit] if before is None else self.events[1:2]


async def _client(monkeypatch: pytest.MonkeyPatch, roles: set[str]):
    store = MemoryAdminStore()
    identity = AuthenticatedIdentity(
        tenant_id="acme",
        principal_id=_ACTOR_ID,
        issuer="https://identity.example.test/",
        subject="admin",
        roles=frozenset(roles),
        credential_type="oidc",
    )

    async def authenticate(_credential: str, _tenant: str):
        return identity

    monkeypatch.setattr(main_module, "store", store)
    monkeypatch.setattr(
        main_module.identity_authenticator, "authenticate", authenticate
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main_module.app), base_url="http://test"
    )
    return client, store


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer opaque", "X-Tenant-ID": "acme"}


async def test_tenant_admin_can_grant_membership_with_verified_actor(
    monkeypatch,
):
    monkeypatch.setattr(main_module.settings, "oidc_issuer", "https://idp.test/")
    client, store = await _client(monkeypatch, {"tenant_admin"})
    async with client:
        response = await client.post(
            "/v1/admin/memberships",
            headers=_headers(),
            json={"subject": "user-2", "role": "approver"},
        )

    assert response.status_code == 201
    method, values = store.calls[-1]
    assert method == "grant_membership"
    assert values["issuer"] == "https://idp.test/"
    assert values["subject"] == "user-2"
    assert values["role"] == "approver"
    assert values["actor_principal_id"] == _ACTOR_ID
    assert values["request_id"]


@pytest.mark.parametrize("role", ["operator", "break_glass"])
async def test_tenant_admin_cannot_assign_platform_or_break_glass_roles(
    monkeypatch, role: str
):
    client, store = await _client(monkeypatch, {"tenant_admin"})
    async with client:
        response = await client.post(
            "/v1/admin/memberships",
            headers=_headers(),
            json={"subject": "user-2", "role": role},
        )

    assert response.status_code == 422
    assert store.calls == []


async def test_submitter_cannot_use_tenant_admin_api(monkeypatch):
    client, store = await _client(monkeypatch, {"submitter"})
    async with client:
        response = await client.get("/v1/admin/memberships", headers=_headers())

    assert response.status_code == 403
    assert store.calls == []


async def test_current_identity_returns_only_verified_authority(monkeypatch):
    monkeypatch.setattr(main_module.settings, "haas_profile", "prod")
    monkeypatch.setattr(main_module.settings, "output_delta_enabled", False)
    client, _store = await _client(monkeypatch, {"tenant_admin", "auditor"})
    async with client:
        response = await client.get("/v1/me", headers=_headers())

    assert response.status_code == 200
    body = response.json()
    capabilities = body.pop("capabilities")
    assert capabilities["version"] == "1"
    assert "tenant.admin" in capabilities["actions"]
    assert "audit.read" in capabilities["actions"]
    assert capabilities["event_schemas"] == ["run-events.v1"]
    assert capabilities["durable_replay"] is True
    assert body == {
        "tenant_id": "acme",
        "principal_id": _ACTOR_ID,
        "roles": ["auditor", "tenant_admin"],
        "credential_type": "oidc",
        "environment": "prod",
    }


async def test_current_identity_rejects_anonymous_request():
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main_module.app), base_url="http://test"
    )
    async with client:
        response = await client.get("/v1/me")

    assert response.status_code == 401


async def test_api_key_is_returned_once_with_cache_protection(monkeypatch):
    monkeypatch.setattr(main_module.settings, "haas_profile", "dev")
    client, store = await _client(monkeypatch, {"tenant_admin"})
    async with client:
        response = await client.post(
            "/v1/admin/api-keys",
            headers=_headers(),
            json={
                "principal_id": _ACTOR_ID,
                "name": "automation",
                "expires_in_days": 30,
            },
        )

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    body = response.json()
    assert body["credential"].startswith("hk_test_")
    assert body["key_prefix"] == body["credential"][:16]
    assert "key_hash" not in body
    method, values = store.calls[-1]
    assert method == "create_api_key"
    assert values["actor_principal_id"] == _ACTOR_ID
    assert values["expires_in_days"] == 30
    assert values["credential"] == body["credential"]


async def test_admin_policy_endpoint_creates_a_digest_pinned_revision(monkeypatch):
    client, store = await _client(monkeypatch, {"tenant_admin"})
    rules = TenantPolicyRules(
        model_providers=["openrouter"],
        models=["example/model"],
        default_primary_model="example/model",
        default_planner_model="example/model",
    )
    async with client:
        response = await client.put(
            "/v1/admin/policy",
            headers=_headers(),
            json={"rules": rules.model_dump(mode="json", exclude_none=True)},
        )

    assert response.status_code == 201
    method, values = store.calls[-1]
    assert method == "activate_policy"
    assert values["actor_principal_id"] == _ACTOR_ID
    assert values["digest"] == policy_digest(rules)
    assert response.json()["digest"] == policy_digest(rules)


async def test_admin_policy_rejects_unimplemented_provider(monkeypatch):
    client, store = await _client(monkeypatch, {"tenant_admin"})
    async with client:
        response = await client.put(
            "/v1/admin/policy",
            headers=_headers(),
            json={"rules": {"model_providers": ["other"], "models": []}},
        )

    assert response.status_code == 422
    assert store.calls == []


async def test_auditor_can_page_redacted_audit_records(monkeypatch):
    client, store = await _client(monkeypatch, {"auditor"})
    created_at = datetime.now(UTC)
    store.events = [
        {
            "event_id": str(uuid4()),
            "actor_principal_id": _ACTOR_ID,
            "action": f"action-{index}",
            "target_type": "policy_revision",
            "target_id": str(index),
            "request_id": str(uuid4()),
            "details": {"version": index},
            "created_at": created_at - timedelta(seconds=index),
        }
        for index in range(2)
    ]
    async with client:
        first = await client.get(
            "/v1/audit-events", headers=_headers(), params={"limit": 1}
        )
        second = await client.get(
            "/v1/audit-events",
            headers=_headers(),
            params={"limit": 1, "cursor": first.json()["next_cursor"]},
        )

    assert first.status_code == second.status_code == 200
    assert first.json()["items"][0]["action"] == "action-0"
    assert first.json()["next_cursor"]
    assert second.json()["items"][0]["action"] == "action-1"


async def test_tenant_admin_can_read_tenant_audit_events(monkeypatch):
    client, store = await _client(monkeypatch, {"tenant_admin"})
    async with client:
        response = await client.get("/v1/audit-events", headers=_headers())

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}
    assert store.calls == [("list_audit_events", {"limit": 51, "before": None})]


async def test_auditor_does_not_get_run_content_access(monkeypatch):
    client, store = await _client(monkeypatch, {"auditor"})
    async with client:
        response = await client.get("/v1/runs", headers=_headers())

    assert response.status_code == 403
    assert store.calls == []
