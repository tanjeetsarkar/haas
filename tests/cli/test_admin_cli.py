from __future__ import annotations

import json

import httpx

from haas_cli.client import HaasClient


async def test_admin_client_methods_use_authorized_tenant_routes():
    requests: list[tuple[str, str, dict[str, str], object, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append(
            (
                request.method,
                request.url.path,
                dict(request.headers),
                body,
                dict(request.url.params),
            )
        )
        return httpx.Response(200, json={"ok": True})

    client = HaasClient(
        "http://api",
        "tenant-a",
        credential="hk_test_cli",
        transport=httpx.MockTransport(handler),
    )
    await client.list_memberships()
    await client.grant_membership(subject="user-1", role="approver", kind="human")
    await client.revoke_membership("principal-1", "approver")
    await client.list_api_keys()
    await client.create_api_key(
        principal_id="principal-1", name="automation", expires_in_days=14
    )
    await client.revoke_api_key("key-1")
    await client.get_tenant_policy()
    await client.activate_tenant_policy({"models": []})
    await client.list_audit_events(limit=9, cursor="cursor-token")
    await client.aclose()

    assert [(method, path) for method, path, _, _, _ in requests] == [
        ("GET", "/v1/admin/memberships"),
        ("POST", "/v1/admin/memberships"),
        ("DELETE", "/v1/admin/memberships/principal-1/approver"),
        ("GET", "/v1/admin/api-keys"),
        ("POST", "/v1/admin/api-keys"),
        ("DELETE", "/v1/admin/api-keys/key-1"),
        ("GET", "/v1/admin/policy"),
        ("PUT", "/v1/admin/policy"),
        ("GET", "/v1/audit-events"),
    ]
    assert all(
        headers["authorization"] == "Bearer hk_test_cli"
        for _, _, headers, _, _ in requests
    )
    assert all(headers["x-tenant-id"] == "tenant-a" for _, _, headers, _, _ in requests)
    assert requests[1][3] == {
        "subject": "user-1",
        "role": "approver",
        "kind": "human",
    }
    assert requests[4][3]["expires_in_days"] == 14
    assert requests[7][3] == {"rules": {"models": []}}
    assert requests[8][3] is None
    assert requests[8][4] == {"limit": "9", "cursor": "cursor-token"}
