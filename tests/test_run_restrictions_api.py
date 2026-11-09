"""Admission remains unavailable until complete runtime enforcement is reviewed."""

from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import app.main as main
from app.core.identity import AuthenticatedIdentity

ROUTES = [
    ("/v1/runs", {"prompt": "run"}),
    ("/v1/chat/completions", {
        "model": "haas", "messages": [{"role": "user", "content": "run"}],
    }),
    ("/v1/responses", {"model": "haas", "input": "run", "previous_response_id": "old"}),
]
FIELDS = [
    {"tools": None}, {"tools": []}, {"tools": ["synthetic_tool"]},
    {"tools": ["a", "a"]}, {"tools": "*"}, {"invocation_intent": None},
    {"invocation_intent": {}},
    {"invocation_intent": {"tool_name": "synthetic_tool", "arguments": {"x": "private"}}},
    {"tools": [], "invocation_intent": None},
]


@pytest.fixture
def admission(monkeypatch):
    identity = AuthenticatedIdentity(
        tenant_id="test", principal_id="submitter", issuer="https://test.invalid",
        subject="submitter", roles=frozenset({"submitter"}), credential_type="oidc",
    )
    authenticate = AsyncMock(return_value=identity)
    monkeypatch.setattr(main.identity_authenticator, "authenticate", authenticate)
    store = Mock()
    for name in (
        "find_admission", "get_active_tenant_policy", "get_run",
        "resolve_published_agent", "get_or_create_thread_messages", "create_run",
        "store_ephemeral_secrets", "publish_event",
    ):
        setattr(store, name, AsyncMock(side_effect=AssertionError(name + " reached")))
    monkeypatch.setattr(main, "store", store)
    publisher = Mock(side_effect=AssertionError("publisher reached"))
    monkeypatch.setattr(main, "make_outbox_publisher", publisher)
    return store, publisher


def assert_no_admission_effects(admission):
    store, publisher = admission
    assert store.mock_calls == []
    publisher.assert_not_called()


def client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test",
        headers={"Authorization": "Bearer synthetic-test", "X-Tenant-ID": "test"},
    )


@pytest.mark.parametrize("route,base", ROUTES)
@pytest.mark.parametrize("fields", FIELDS)
@pytest.mark.parametrize("idempotency", [None, "prior-request"])
async def test_present_authority_rejected_before_replay_or_admission(
    admission, route, base, fields, idempotency,
):
    headers = {"Idempotency-Key": idempotency} if idempotency else {}
    async with client() as http:
        response = await http.post(route, json={**base, **fields}, headers=headers)
    assert response.status_code == 422, response.text
    if route == "/v1/runs":
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["status"] == 422
        assert response.json()["instance"] == route
    else:
        assert response.json()["error"]["code"] == "restriction.unavailable"
    assert "private" not in response.text
    assert_no_admission_effects(admission)


@pytest.mark.parametrize("route,base", ROUTES[1:])
@pytest.mark.parametrize("fields", FIELDS)
async def test_facade_haas_authority_rejected_before_mapping_or_chain_lookup(
    admission, route, base, fields,
):
    async with client() as http:
        response = await http.post(
            route, json={**base, "haas": fields},
            headers={"Idempotency-Key": "prior-request"},
        )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["param"].startswith("haas.")
    assert_no_admission_effects(admission)


@pytest.mark.parametrize("route,base", ROUTES)
async def test_omitted_authority_retains_existing_idempotent_admission(
    admission, route, base,
):
    store, _ = admission
    store.find_admission.side_effect = None
    # Native admission digest is built after facade mapping; omit chain lookup here.
    base = {key: value for key, value in base.items() if key != "previous_response_id"}
    request = main.CreateRunRequest(prompt="run")
    store.find_admission.return_value = {
        "request_digest": main._admission_digest(request),
        "response": {
            "run_id": "run-test", "thread_id": "thread-test", "status": "queued",
            "stream_url": "/stream", "events_url": "/events", "created_at": 1.0,
        },
    }
    # Facade normally continues to wait/render after admission. Isolate that
    # downstream step so this test proves real mapping + admission, not model work.
    if route != "/v1/runs":
        base = {**base, "stream": True}
        monkey_run = {
            "run_id": "run-test", "thread_id": "thread-test",
            "status": "completed", "result_text": "ok",
        }
        store.get_run.side_effect = None
        store.get_run.return_value = monkey_run
        store.list_ledger = AsyncMock(return_value=[])
        store.list_run_events = AsyncMock(return_value=[])
        store.run_usage = AsyncMock(return_value={"entries": []})
    async with client() as http:
        response = await http.post(
            route, json=base, headers={"Idempotency-Key": "prior-request"},
        )
    assert response.status_code in (200, 202), response.text
    if route != "/v1/runs":
        assert "ok" in response.text and "invalid_request_error" not in response.text
    store.find_admission.assert_awaited_once()
    store.create_run.assert_not_awaited()
    store.store_ephemeral_secrets.assert_not_awaited()


@pytest.mark.parametrize("route,base", ROUTES[1:])
async def test_facade_openai_definitions_keep_existing_fail_closed_error(admission, route, base):
    async with client() as http:
        response = await http.post(route, json={
            **base, "tools": [{"type": "function", "function": {"name": "synthetic"}}],
        })
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_parameter"
    assert_no_admission_effects(admission)
