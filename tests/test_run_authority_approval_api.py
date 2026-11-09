"""Approval API hold precedes ephemeral secret writes and decision replay."""

from uuid import uuid4

import pytest

import app.main as main
from app.core.run_restrictions import RunAuthority
from test_authorization import _request_app


@pytest.fixture
def api_store(monkeypatch):
    class Store:
        def __init__(self):
            self.effects = []
            self.run = {
                "run_id": "run", "tenant_id": "acme", "status": "input_required",
                "submitted_by_principal_id": "different-submitter", "authority_json": None,
            }
            self.batch = {
                "run_id": "run", "status": "pending",
                "submitted_by_principal_id": "different-submitter",
            }

        async def get_run(self, run_id):
            return self.run

        async def get_approval_batch(self, tenant, batch):
            return self.batch

        async def store_ephemeral_secrets(self, run_id, secrets):
            self.effects.append(("secrets", run_id, secrets))

        async def decide_approval(self, tenant, **kw):
            self.effects.append(("decision", tenant, kw))
            return {"run_id": "run", "batch_id": str(uuid4()), "replayed": True}

    store = Store()
    monkeypatch.setattr(main, "store", store)
    return store


async def decide(monkeypatch, route, approved=True):
    path = "/v1/runs/run/resume" if route == "resume" else f"/v1/approvals/{uuid4()}/decision"
    body = (
        {"approved": approved} if route == "resume"
        else {"decision": "approve" if approved else "reject", "batch_digest": "a" * 64}
    )
    return await _request_app(
        monkeypatch, "POST", path, roles={"approver"},
        json_body={**body, "secrets": {"synthetic": "not-a-live-secret"}},
    )


@pytest.mark.parametrize("route", ["resume", "batch"])
@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.parametrize("stored", [
    '{"version":1,"tools":null}', '{"version":1,"tools":[]}', '{}', 'null',
    '{"version":1,"tools":["act"],"intent":{"invalid":true}}',
])
async def test_restricted_decisions_never_write_secrets_or_decisions(
    api_store, monkeypatch, route, approved, stored,
):
    api_store.run["authority_json"] = stored
    response = await decide(monkeypatch, route, approved)
    assert response.status_code == 409
    assert response.json()["detail"] == "restriction.resume_unavailable"
    assert api_store.effects == []


@pytest.mark.parametrize("route", ["resume", "batch"])
async def test_decoded_authority_also_denies_without_raw_json(api_store, monkeypatch, route):
    api_store.run.pop("authority_json")
    api_store.run["authority"] = RunAuthority(tools=())
    assert (await decide(monkeypatch, route)).status_code == 409
    assert api_store.effects == []


@pytest.mark.parametrize("route", ["resume", "batch"])
@pytest.mark.parametrize("foreign", [False, True])
async def test_self_and_foreign_decisions_remain_denied(api_store, monkeypatch, route, foreign):
    if foreign:
        api_store.run["tenant_id"] = "foreign"
    else:
        api_store.run["submitted_by_principal_id"] = "principal-1"
        api_store.batch["submitted_by_principal_id"] = "principal-1"
    assert (await decide(monkeypatch, route)).status_code == (404 if foreign else 403)
    assert api_store.effects == []


@pytest.mark.parametrize("status", ["pending", "approved", "rejected"])
async def test_closed_batch_and_idempotent_replay_cannot_bypass_hold(api_store, monkeypatch, status):
    import httpx
    from dataclasses import replace
    from test_authorization import IDENTITY

    async def authenticate(*args):
        return replace(IDENTITY, roles=frozenset({"approver"}))

    monkeypatch.setattr(main.identity_authenticator, "authenticate", authenticate)
    api_store.batch["status"] = status
    api_store.run["authority_json"] = '{"version":1,"tools":[]}'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test",
    ) as client:
        response = await client.post(
            f"/v1/approvals/{uuid4()}/decision",
            headers={"Authorization": "Bearer synthetic", "X-Tenant-ID": "acme",
                     "Idempotency-Key": "prior-decision"},
            json={"decision": "approve", "batch_digest": "a" * 64,
                  "secrets": {"synthetic": "not-a-live-secret"}},
        )
    assert response.status_code == 409
    assert api_store.effects == []


@pytest.mark.parametrize("route", ["resume", "batch"])
async def test_legacy_decisions_preserve_secret_and_verified_actor_flow(api_store, monkeypatch, route):
    response = await decide(monkeypatch, route)
    assert response.status_code == 202
    assert response.headers["Idempotent-Replayed"] == "true"
    assert [effect[0] for effect in api_store.effects] == ["secrets", "decision"]
    assert api_store.effects[1][2]["actor_principal_id"] == "principal-1"
