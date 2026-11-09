import hashlib
import json
import socket
import ssl
import time
from uuid import uuid4

import httpx
import jwt
import pytest

from app.core import worker_authorization as client
from app.core.run_restrictions import canonical_arguments
from app.core.worker_identity import PATH
from test_worker_identity import auth as auth  # noqa: F401


@pytest.fixture
def rpc(auth, monkeypatch):
    config, _, _, _, _, _, _ = auth
    config.internal_authorization_url = "https://core.example" + PATH
    binding = {
        "version": 1, "tenant_id": str(uuid4()), "run_id": "run",
        "invocation_id": str(uuid4()), "tool_name": "synthetic",
        "policy_revision_id": str(uuid4()), "arguments_sha256": "a" * 64,
        "intent_binding": "b" * 64, "registry_fingerprint": "c" * 64,
        "plugin_digest": "d" * 64, "envelope_expires_at": time.time() + 60,
    }
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ])
    requests, options = [], []
    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        now = time.time()
        return httpx.Response(200, json={
            "version": 1, "request_id": body["request_id"], "operation": body["operation"],
            "decision": "allow", "binding_sha256": hashlib.sha256(canonical_arguments(body).encode()).hexdigest(),
            "checked_at": now, "valid_until": now + 1,
        })
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    def factory(**kwargs):
        options.append(kwargs)
        return real(transport=transport, **kwargs)
    monkeypatch.setattr(client.httpx, "AsyncClient", factory)
    return config, binding, requests, options, transport


async def test_two_fresh_stage_requests_have_bound_jwts_and_verified_transport(rpc):
    config, binding, requests, options, _ = rpc
    await client.check_worker_authorization(config, binding, "pre_grant")
    await client.check_worker_authorization(config, binding, "final_entry")
    assert len(requests) == 2 and "request_id" not in binding
    bodies = [json.loads(r.content) for r in requests]
    assert bodies[0]["request_id"] != bodies[1]["request_id"]
    for request, body, option in zip(requests, bodies, options, strict=True):
        claims = jwt.decode(request.headers["authorization"][7:], options={"verify_signature": False})
        assert claims["jti"] == body["request_id"]
        assert claims["body_sha256"] == hashlib.sha256(request.content).hexdigest()
        assert option["trust_env"] is option["follow_redirects"] is False
        assert option["verify"].verify_mode == ssl.CERT_REQUIRED and option["verify"].check_hostname
        assert option["timeout"].read == 2.0
        assert request.url.scheme == "https"


@pytest.mark.parametrize("case", [
    "deny", "nonce", "stage", "digest", "expired", "future", "long", "past",
    "version", "boolean_time", "missing", "extra", "redirect", "error", "malformed",
    "oversized", "timeout", "tls", "duplicate", "envelope_expiry",
])
async def test_no_retry_or_fallback_on_bad_response(rpc, case):
    config, binding, requests, _, transport = rpc
    original = transport.handler
    def handler(request):
        response = original(request)
        if case in ("redirect", "error"):
            return httpx.Response(307 if case == "redirect" else 503, headers={"Location": "https://foreign.example"})
        if case == "malformed":
            return httpx.Response(200, text="not-json")
        if case == "oversized":
            return httpx.Response(200, text="x" * 8193)
        if case == "duplicate":
            return httpx.Response(200, text='{"decision":"deny","decision":"allow"}')
        if case in ("timeout", "tls"):
            raise httpx.ConnectError("sensitive TLS failure") if case == "tls" else httpx.ReadTimeout("sensitive timeout")
        body = response.json()
        changes = {
            "deny": ("decision", "deny"), "nonce": ("request_id", "old"),
            "stage": ("operation", "wrong"), "digest": ("binding_sha256", "0" * 64),
            "expired": ("valid_until", time.time() - 1), "future": ("checked_at", time.time() + 6),
            "long": ("valid_until", time.time() + 10), "past": ("checked_at", time.time() - 10),
            "version": ("version", True), "boolean_time": ("checked_at", True),
            "extra": ("unknown", "sensitive"),
            "envelope_expiry": ("valid_until", binding["envelope_expires_at"] + 1),
        }
        if case == "missing":
            body.pop("checked_at")
        else:
            field, value = changes[case]
            body[field] = value
        return httpx.Response(200, json=body)
    transport.handler = handler
    with pytest.raises(client.WorkerAuthorizationError, match="^restriction.authorization_denied$"):
        await client.check_worker_authorization(config, binding, "pre_grant")
    assert len(requests) == 1


@pytest.mark.parametrize("url", [
    None, "http://core.example" + PATH, "https://user:pass@core.example" + PATH,
    "https://core.example/wrong", "https://core.example" + PATH + "?x=y",
    "https://core.example" + PATH + "#fragment", "https://127.0.0.1" + PATH,
])
async def test_invalid_destination_never_sends(rpc, url):
    config, binding, requests, _, _ = rpc
    config.internal_authorization_url = url
    with pytest.raises(client.WorkerAuthorizationError):
        await client.check_worker_authorization(config, binding, "final_entry")
    assert requests == []


async def test_dns_private_rebinding_is_denied_before_transport(rpc, monkeypatch):
    config, binding, requests, _, _ = rpc
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ])
    with pytest.raises(client.WorkerAuthorizationError):
        await client.check_worker_authorization(config, binding, "pre_grant")
    assert requests == []


async def test_missing_ca_and_changed_request_fail_before_network(rpc, tmp_path):
    config, binding, requests, _, _ = rpc
    config.internal_authorization_ca_file = tmp_path / "missing-ca"
    with pytest.raises(client.WorkerAuthorizationError):
        await client.check_worker_authorization(config, binding, "pre_grant")
    config.internal_authorization_ca_file = None
    for field, value in (("version", True), ("tenant_id", "alias"),
                         ("arguments_sha256", "invalid"), ("envelope_expires_at", False)):
        with pytest.raises(client.WorkerAuthorizationError):
            await client.check_worker_authorization(config, {**binding, field: value}, "pre_grant")
    assert requests == []
