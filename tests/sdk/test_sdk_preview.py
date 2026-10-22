"""Preview namespace tests: routes, warning, artifacts paths (§9.8-9.10)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from haas_sdk import Haas
from haas_sdk.preview import PreviewWarning


def _client(handler: Any) -> Haas:
    return Haas(
        base_url="http://haas.test",
        api_key="hk_test_abc",
        tenant="acme",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )


def test_preview_warning_emitted_once() -> None:
    from haas_sdk import preview as preview_module

    preview_module._warned = False
    with pytest.warns(PreviewWarning):

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": []})

        with _client(handler) as haas:
            haas.preview.quotas.get()
    preview_module._warned = False


def test_artifacts_uses_server_routes() -> None:
    """SDK-RUN-05 analogue for §9.8: 0.1 paths (/runs/{id}/artifacts) are gone."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(
                201,
                json={"artifact_id": "a1", "size_bytes": 3, "sha256": "abc"},
            )
        if request.url.path.endswith("/download"):
            return httpx.Response(200, content=b"abc")
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path == "/v1/artifacts/a1":
            return httpx.Response(
                200,
                json={
                    "artifact_id": "a1",
                    "run_id": "r1",
                    "name": "x.txt",
                    "content_type": "text/plain",
                    "size_bytes": 3,
                    "sha256": "abc",
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "artifact_id": "a1",
                        "run_id": "r1",
                        "name": "x.txt",
                        "content_type": "text/plain",
                        "size_bytes": 3,
                        "sha256": "abc",
                    }
                ]
            },
        )

    with _client(handler) as haas:
        page = haas.preview.artifacts.list("r1")
        assert [a.artifact_id for a in page.items] == ["a1"]
        assert haas.preview.artifacts.get("a1").name == "x.txt"
        assert haas.preview.artifacts.download("a1") == b"abc"
        uploaded = haas.preview.artifacts.upload(
            "r1", b"abc", name="x.txt", content_type="text/plain"
        )
        assert uploaded.artifact_id == "a1"
        haas.preview.artifacts.delete("a1")
    paths = [(r.method, r.url.path) for r in seen]
    assert paths == [
        ("GET", "/v1/artifacts"),
        ("GET", "/v1/artifacts/a1"),
        ("GET", "/v1/artifacts/a1/download"),
        ("POST", "/v1/artifacts"),
        ("DELETE", "/v1/artifacts/a1"),
    ]
    # Multipart body carries the JSON request part and the file.
    assert b'name="request"' in seen[3].content
    assert b'name="file"; filename="x.txt"' in seen[3].content


def test_webhooks_admin_routes() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST" and "rotate" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "key_id": "k1",
                    "status": "active",
                    "retire_at": None,
                    "revoked_at": None,
                },
            )
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "endpoint_id": "e1",
                    "url": "https://x",
                    "events": ["run.completed"],
                    "enabled": True,
                    "description": "",
                },
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        if "signing-keys" in request.url.path:
            return httpx.Response(200, json={"items": []})
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "endpoint_id": "e1",
                        "url": "https://x",
                        "events": [],
                        "enabled": True,
                        "description": "",
                    }
                ]
            },
        )

    with _client(handler) as haas:
        page = haas.preview.webhooks.list_endpoints()
        assert [e.endpoint_id for e in page.items] == ["e1"]
        created = haas.preview.webhooks.create_endpoint(
            "https://x", events=["run.completed"]
        )
        assert created.endpoint_id == "e1"
        haas.preview.webhooks.delete_endpoint("e1")
        keys = haas.preview.webhooks.list_signing_keys()
        assert keys == []
        rotated = haas.preview.webhooks.rotate_signing_key("k1", "env")
        assert rotated.key_id == "k1"
    assert [r.url.path for r in seen] == [
        "/v1/webhooks/endpoints",
        "/v1/webhooks/endpoints",
        "/v1/webhooks/endpoints/e1",
        "/v1/webhooks/signing-keys",
        "/v1/webhooks/signing-keys/k1/rotate",
    ]


def test_quotas_get_and_set() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            body = json.loads(request.content)
            return httpx.Response(200, json={"weight": 1, **body})
        return httpx.Response(
            200,
            json={
                "tenant_id": "t",
                "max_concurrent_runs": 5,
                "max_inflight_runs": 10,
                "requests_per_minute": 60,
                "principal_requests_per_minute": 30,
                "sse_connections": 4,
                "weight": 1,
            },
        )

    with _client(handler) as haas:
        quota = haas.preview.quotas.get()
        assert quota.max_concurrent_runs == 5
        updated = haas.preview.quotas.set(max_concurrent_runs=8)
        assert updated.max_concurrent_runs == 8


def test_egress_policy_roundtrip() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(
                200,
                json={
                    "tenant_id": "t",
                    "allow_hosts": ["api.example.com"],
                    "allow_cidrs": [],
                    "max_bytes_per_run": 1024,
                    "version": 2,
                    "updated_by": None,
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            )
        return httpx.Response(
            200,
            json={
                "tenant_id": "t",
                "allow_hosts": [],
                "allow_cidrs": [],
                "max_bytes_per_run": None,
                "version": 1,
                "updated_by": None,
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )

    with _client(handler) as haas:
        assert haas.preview.egress.policy().version == 1
        updated = haas.preview.egress.set_policy(allow_hosts=["api.example.com"])
        assert updated.allow_hosts == ("api.example.com",)
