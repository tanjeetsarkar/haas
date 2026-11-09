"""Phase failure API projection over held, test-staged runs."""

from contextlib import asynccontextmanager
from decimal import Decimal
import json
from uuid import uuid4

import httpx
import pytest

import app.main as main
from app.core.identity import AuthenticatedIdentity
from app.worker import tasks
from test_phase2_database import DSN, admit, db as db, service_store
from test_phase_failure_projection import failed_finalizer
from test_run_phase_ledger import staged

pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


@asynccontextmanager
async def api(monkeypatch, store, slug, principal):
    async def authenticate(_credential, tenant):
        return AuthenticatedIdentity(
            tenant_id=tenant, principal_id=str(principal), issuer="https://qa.invalid",
            subject=str(principal), roles=frozenset({"tenant_admin"}), credential_type="oidc",
        )

    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main.identity_authenticator, "authenticate", authenticate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://qa",
        headers={"Authorization": "Bearer test-only", "X-Tenant-ID": slug},
    ) as client:
        yield client


@pytest.mark.parametrize(("marker", "code"), [
    ("phase.finalizer_tool_calls", "phase.finalizer_tool_calls"),
    ("phase.finalizer_failed", "phase.finalizer_failed"),
])
async def test_native_and_responses_read_exact_code_after_event_purge_and_restart(
    db, monkeypatch, marker, code,
):
    owner, store, slug, _, _, _, principal, _ = db
    run_id, lease = await staged(owner, store, slug, success=1, step_count=2)
    await owner.execute(
        "UPDATE runs SET webhook_url='https://invalid.example/hook' WHERE run_id=$1", run_id,
    )
    monkeypatch.setattr(tasks, "estimate_step_cost", lambda *args: Decimal("0.02"))
    requests = []
    real_client = httpx.AsyncClient

    def handle(request):
        requests.append(json.loads(request.content))
        message = {"role": "assistant", "content": None}
        if marker == "phase.finalizer_tool_calls":
            message["tool_calls"] = [{
                "id": "test", "type": "function",
                "function": {"name": "fake", "arguments": "{}"},
            }]
        return httpx.Response(200, json={
            "choices": [{"message": message}], "usage": {"cost": 0.01},
        })

    monkeypatch.setattr(
        tasks.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handle))
    )
    assert (await tasks._step(store, lease, await store.get_run(run_id)))["status"] == "failed"
    monkeypatch.setattr(tasks.httpx, "AsyncClient", real_client)
    assert len(requests) == 1 and "tools" not in requests[0]
    saved = await store.get_run(run_id)
    attempt_key = saved["phase_state"].finalizer_attempt_key
    step = await owner.fetchrow(
        "SELECT s.error,l.settlement_status FROM run_steps s JOIN usage_ledger l "
        "ON l.tenant_id=s.tenant_id AND l.run_id=s.run_id "
        "AND l.source_attempt_key=$2 AND l.unit='model_usd' "
        "WHERE s.run_id=$1 AND s.step_number=3 AND s.attempt=1",
        run_id, attempt_key,
    )
    assert step["error"] == marker and step["settlement_status"] == "settled"
    event = json.loads(await owner.fetchval(
        "SELECT payload FROM run_events WHERE run_id=$1 AND type='run_error'", run_id,
    ))
    webhook = json.loads(await owner.fetchval(
        "SELECT payload FROM outbox_messages WHERE run_id=$1 AND destination='webhook'", run_id,
    ))
    assert event["code"] == webhook["body"]["code"] == code
    async with owner.transaction():
        await owner.execute("SELECT set_config('haas.retention_purge','on',true)")
        await owner.execute("DELETE FROM run_events WHERE run_id=$1", run_id)
    assert await owner.fetchval("SELECT count(*) FROM run_events WHERE run_id=$1", run_id) == 0
    async with service_store(owner) as restarted:
        async with api(monkeypatch, restarted, slug, principal) as client:
            native = await client.get(f"/v1/runs/{run_id}")
            response = await client.get(f"/v1/responses/{run_id}")
    assert native.status_code == 200 and native.json()["error_code"] == code
    assert response.status_code == 200 and response.json()["error"]["code"] == code


async def test_missing_marker_is_explicit_unavailable_not_generic_failure(db, monkeypatch):
    owner, store, slug, _, _, _, principal, _ = db
    run_id, _ = await failed_finalizer(owner, store, slug, marker=None)
    async with api(monkeypatch, store, slug, principal) as client:
        for path in (f"/v1/runs/{run_id}", f"/v1/responses/{run_id}"):
            result = await client.get(path)
            assert result.status_code == 503
            assert "Phase failure information is unavailable." in result.text
            assert result.headers["x-should-retry"] == "false"
            assert "execution.failed" not in result.text


async def test_unknown_phase_with_tool_marker_is_unavailable_in_native_and_facade(db, monkeypatch):
    owner, store, slug, _, _, _, principal, _ = db
    run_id, lease = await staged(owner, store, slug, success=1, step_count=2)
    ticket = await store.begin_bounded_phase_step(
        lease, step=3, model="model-a", estimate_usd=Decimal("0.02"),
        finalization=True, acquisition_quota_exhausted=True,
    )
    assert ticket is not None
    assert await store.settle_bounded_finalizer(lease, ticket, completion=None) == "failed"
    await owner.execute(
        "UPDATE run_steps SET error='phase.finalizer_tool_calls' "
        "WHERE tenant_id=$1 AND run_id=$2 AND step_number=$3 AND attempt=$4",
        slug, run_id, ticket.step, ticket.attempt,
    )
    async with api(monkeypatch, store, slug, principal) as client:
        for path in (f"/v1/runs/{run_id}", f"/v1/responses/{run_id}"):
            result = await client.get(path)
            assert result.status_code == 503
            assert result.headers["x-should-retry"] == "false"
            assert "Phase failure information is unavailable." in result.text
            assert "phase.finalizer_outcome_unknown" not in result.text


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("route", ["chat/completions", "responses"])
async def test_facade_submit_failure_never_streams_or_returns_generic_code(
    db, monkeypatch, route, stream,
):
    owner, store, slug, _, _, _, principal, _ = db
    run_id, _ = await failed_finalizer(owner, store, slug, marker=None)

    async def submitted(*args, **kwargs):
        return {"run_id": run_id, "thread_id": run_id}

    monkeypatch.setattr(main, "_facade_submit", submitted)
    body = (
        {"model": "model-a", "messages": [{"role": "user", "content": "test"}]}
        if route == "chat/completions" else {"model": "model-a", "input": "test"}
    )
    async with api(monkeypatch, store, slug, principal) as client:
        result = await client.post(f"/v1/{route}", json={**body, "stream": stream})
    assert result.status_code == (200 if stream else 503)
    assert "Phase failure information is unavailable." in result.text
    assert "execution.failed" not in result.text
    if not stream:
        assert result.headers["x-should-retry"] == "false"


@pytest.mark.parametrize(("reason", "code"), [
    ("no_evidence", "phase.no_successful_acquisition_result"),
    ("budget", "phase.insufficient_budget"),
    ("unknown", "phase.finalizer_outcome_unknown"),
])
async def test_phase_failure_categories_match_on_native_and_responses(db, monkeypatch, reason, code):
    owner, store, slug, _, _, _, principal, _ = db
    run_id, lease = await staged(
        owner, store, slug, success=int(reason != "no_evidence"), step_count=2,
    )
    if reason == "no_evidence":
        assert await tasks._phase_exhaustion_route(store, lease, Decimal("0.01")) == "failed"
    elif reason == "budget":
        assert await store.begin_bounded_phase_step(
            lease, step=3, model="model-a", estimate_usd=Decimal("0.0600000001"),
            finalization=True, acquisition_quota_exhausted=True,
        ) is None
    else:
        ticket = await store.begin_bounded_phase_step(
            lease, step=3, model="model-a", estimate_usd=Decimal("0.02"),
            finalization=True, acquisition_quota_exhausted=True,
        )
        assert ticket is not None
        assert await store.settle_bounded_finalizer(lease, ticket, completion=None) == "failed"
    async with api(monkeypatch, store, slug, principal) as client:
        native = await client.get(f"/v1/runs/{run_id}")
        response = await client.get(f"/v1/responses/{run_id}")
    assert native.json()["error_code"] == response.json()["error"]["code"] == code


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("route", ["chat/completions", "responses"])
async def test_facade_submit_failure_projects_claimed_marker(db, monkeypatch, route, stream):
    owner, store, slug, _, _, _, principal, _ = db
    run_id, _ = await failed_finalizer(owner, store, slug, marker="phase.finalizer_tool_calls")

    async def submitted(*args, **kwargs):
        return {"run_id": run_id, "thread_id": run_id}

    monkeypatch.setattr(main, "_facade_submit", submitted)
    body = (
        {"model": "model-a", "messages": [{"role": "user", "content": "test"}]}
        if route == "chat/completions" else {"model": "model-a", "input": "test"}
    )
    async with api(monkeypatch, store, slug, principal) as client:
        result = await client.post(f"/v1/{route}", json={**body, "stream": stream})
    assert result.status_code == (502 if route == "chat/completions" and not stream else 200)
    assert "phase.finalizer_tool_calls" in result.text
    assert "execution.failed" not in result.text


async def test_real_finalizer_tool_rejection_matches_event_webhook_and_api(db, monkeypatch):
    owner, store, slug, _, _, _, principal, _ = db
    run_id, lease = await staged(owner, store, slug, success=1, step_count=2)
    await owner.execute(
        "UPDATE runs SET webhook_url='https://invalid.example/hook' WHERE run_id=$1", run_id,
    )
    monkeypatch.setattr(tasks, "estimate_step_cost", lambda *args: Decimal("0.02"))
    real_client = httpx.AsyncClient
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {
                "role": "assistant", "content": "response",
                "tool_calls": [{"id": "test", "type": "function",
                                "function": {"name": "fake", "arguments": "{}"}}],
            }}],
            "usage": {"cost": 0.01},
        })

    monkeypatch.setattr(
        tasks.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handle))
    )
    assert (await tasks._step(store, lease, await store.get_run(run_id)))["status"] == "failed"
    assert len(requests) == 1 and "tools" not in requests[0]
    monkeypatch.setattr(tasks.httpx, "AsyncClient", real_client)
    event = json.loads(await owner.fetchval(
        "SELECT payload FROM run_events WHERE run_id=$1 AND type='run_error'", run_id,
    ))
    webhook = json.loads(await owner.fetchval(
        "SELECT payload FROM outbox_messages WHERE run_id=$1 AND destination='webhook'", run_id,
    ))
    assert event["code"] == webhook["body"]["code"] == "phase.finalizer_tool_calls"
    async with api(monkeypatch, store, slug, principal) as client:
        native = await client.get(f"/v1/runs/{run_id}")
        response = await client.get(f"/v1/responses/{run_id}")
    assert native.json()["error_code"] == response.json()["error"]["code"] == event["code"]


async def test_legacy_failed_run_does_not_gain_phase_error_code(db, monkeypatch):
    owner, store, slug, _, _, _, principal, _ = db
    run_id = f"legacy-projection-{uuid4().hex}"
    await admit(store, slug, run_id, run_id)
    await owner.execute(
        "UPDATE runs SET status='failed',result_text='Orchestrator step failed: old' WHERE run_id=$1",
        run_id,
    )
    async with api(monkeypatch, store, slug, principal) as client:
        native = await client.get(f"/v1/runs/{run_id}")
        response = await client.get(f"/v1/responses/{run_id}")
    assert native.status_code == 200 and "error_code" not in native.json()
    assert response.status_code == 200 and response.json()["error"]["code"] == "execution.failed"


async def test_other_tenant_cannot_read_phase_projection(db, monkeypatch):
    owner, store, slug, other_slug, _, _, principal, _ = db
    run_id, _ = await failed_finalizer(owner, store, slug, marker="phase.finalizer_tool_calls")
    async with service_store(owner) as service:
        async with api(monkeypatch, service, other_slug, principal) as client:
            for path in (f"/v1/runs/{run_id}", f"/v1/responses/{run_id}"):
                assert (await client.get(path)).status_code == 404
