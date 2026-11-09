import hashlib
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest

import app.main as main
from app.api import internal_run_authorization as api
from app.core.database_context import tenant_context
from app.core.run_restrictions import canonical_arguments
from app.core.worker_identity import PATH, sign_worker_request
from test_run_authority_storage import bound
from test_worker_identity import auth as auth  # noqa: F401


@pytest.fixture
def endpoint(auth, monkeypatch):
    config, _, _, _, _, replay, _ = auth
    tenant, invocation, policy, submitter = map(str, (uuid4() for _ in range(4)))
    authority = bound(tenant, submitter, policy, "run")
    # Match the schema's digest-format registry identity.
    data = authority.model_dump()
    data["intent"]["context"]["registry_fingerprint"] = "b" * 64
    authority = type(authority).model_validate(data)
    body = {
        "version": 1, "operation": "pre_grant", "request_id": "request-0123456789",
        "tenant_id": tenant, "run_id": "run", "invocation_id": invocation,
        "tool_name": "synthetic_tool", "arguments_sha256": hashlib.sha256(
            authority.intent.arguments_json.encode()).hexdigest(),
        "intent_binding": authority.intent.binding, "policy_revision_id": policy,
        "registry_fingerprint": "b" * 64, "plugin_digest": "a" * 64,
        "envelope_expires_at": time.time() + 60,
    }
    future = datetime.fromtimestamp(time.time() + 60, timezone.utc)
    snapshot = {
        "tenant_uuid": tenant, "authority_json": authority.model_dump_json(),
        "run_id": "run", "tool_name": "synthetic_tool", "invocation_id": invocation,
        "submitted_by_principal_id": submitter, "policy_revision_id": policy,
        "active_policy_revision_id": policy, "registry_fingerprint": "b" * 64,
        "arguments": authority.intent.arguments_json, "status": "running",
        "tenant_status": "active", "deadline_at": future, "invocation_status": "dispatched",
        "approval_status": "approved", "decision": "approved", "decided_by": str(uuid4()),
        "approval_expires_at": future, "batch_digest": "digest", "decision_digest": "digest",
    }
    conn, store = Mock(), Mock()
    conn.fetchrow = AsyncMock(return_value=snapshot)
    @asynccontextmanager
    async def connection():
        assert tenant_context.get() == tenant
        yield conn
    store.connection, store.rds = connection, replay
    monkeypatch.setattr(api, "settings", config)
    monkeypatch.setattr(api, "validate_dispatch_eligibility", AsyncMock())
    monkeypatch.setattr(main, "store", store)
    return config, body, snapshot, conn, replay


async def send(config, body, headers=None):
    token = sign_worker_request(config, body)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="https://core.test",
    ) as client:
        return await client.post(PATH, json=body, headers=(
            {"Authorization": "Bearer " + token, "X-Tenant-ID": "foreign"} if headers is None else headers
        ))


@pytest.mark.parametrize("operation", ["pre_grant", "final_entry"])
async def test_even_matching_authenticated_state_stays_denied(endpoint, operation):
    config, body, _, conn, _ = endpoint
    body["operation"] = operation
    old = tenant_context.get()
    response = await send(config, body)
    assert response.status_code == 200
    result = response.json()
    assert result["decision"] == "deny" and result["code"] == "restriction.unavailable"
    assert result["binding_sha256"] == hashlib.sha256(canonical_arguments(body).encode()).hexdigest()
    assert result["operation"] == operation and result["request_id"] == body["request_id"]
    assert result["valid_until"] <= result["checked_at"] <= body["envelope_expires_at"]
    assert tenant_context.get() == old
    assert len(conn.mock_calls) == 1  # Read only; no secrets/grants/handler hooks.


@pytest.mark.parametrize("field,value", [
    ("tenant_uuid", str(uuid4())), ("authority_json", None), ("arguments", "{}"),
    ("active_policy_revision_id", str(uuid4())), ("registry_fingerprint", "c" * 64),
    ("status", "cancelled"), ("tenant_status", "suspended"), ("invocation_status", "succeeded"),
    ("approval_status", "pending"), ("decision", "rejected"), ("decided_by", None),
    ("deadline_at", datetime.fromtimestamp(0, timezone.utc)), ("decision_digest", "altered"),
])
async def test_persisted_binding_or_eligibility_denies(endpoint, field, value):
    config, body, snapshot, _, _ = endpoint
    snapshot[field] = value
    response = await send(config, body)
    assert response.json()["code"] == "restriction.denied"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer hk_test_user"},
                                    {"Authorization": "Bearer invalid"}])
async def test_no_user_or_anonymous_fallback(endpoint, headers):
    config, body, _, conn, _ = endpoint
    response = await send(config, body, headers)
    assert response.status_code == 401
    conn.fetchrow.assert_not_awaited()


@pytest.mark.parametrize("field,value", [
    ("version", 2), ("version", True), ("operation", "dispatch"), ("unknown", True), ("tenant_id", "alias"),
    ("arguments_sha256", "invalid"), ("envelope_expires_at", 0), ("envelope_expires_at", True),
])
async def test_authenticated_invalid_contract_has_no_db_read(endpoint, field, value):
    config, body, _, conn, _ = endpoint
    body[field] = value
    response = await send(config, body)
    assert response.status_code == 422
    conn.fetchrow.assert_not_awaited()


async def test_replay_missing_state_and_database_outage(endpoint):
    config, body, _, conn, replay = endpoint
    replay.set.return_value = False
    assert (await send(config, body)).status_code == 401
    conn.fetchrow.assert_not_awaited()
    replay.set.return_value = True
    conn.fetchrow.return_value = None
    assert (await send(config, body)).json()["code"] == "restriction.denied"
    conn.fetchrow.side_effect = RuntimeError("sensitive database failure")
    response = await send(config, body)
    assert response.status_code == 503 and "sensitive" not in response.text
    assert PATH not in main.app.openapi()["paths"]
