"""HTTP contract tests with public identity and storage dependency seams."""

import httpx
import pytest
from fastapi import HTTPException

from app.identity import VerifiedIdentity, require_identity
from app.main import app, get_store
from app.models import WorkspaceCreate, WorkspaceMutation

VERIFIED = {
    "tenant_id": "verified-tenant",
    "principal_id": "verified-principal",
    "capabilities": {"actions": ["run.submit"]},
}


class StoreStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def create(self, identity: VerifiedIdentity, body: WorkspaceCreate) -> dict:
        self.calls.append(("create", (identity, body)))
        return {
            "workspace_id": "00000000-0000-0000-0000-000000000001",
            "mutation_id": body.mutation_id,
            "revision": 1,
            "saved_at": "2026-01-01T00:00:00+00:00",
        }

    async def catalog(self, identity: VerifiedIdentity, cursor: str | None, limit: int) -> dict:
        self.calls.append(("catalog", (identity, cursor, limit)))
        return {"items": [], "next_cursor": None}

    async def get(self, identity: VerifiedIdentity, workspace_id: object) -> dict:
        self.calls.append(("get", (identity, workspace_id)))
        return {"workspace_id": str(workspace_id), "state": {"draft": ""}, "revision": 1}

    async def mutate(self, identity: VerifiedIdentity, workspace_id: object, body: WorkspaceMutation) -> dict:
        if body.mutation_id == "force-conflict":
            raise HTTPException(409, {"code": "revision_conflict", "current_revision": 9})
        self.calls.append(("mutate", (identity, workspace_id, body)))
        return {
            "workspace_id": str(workspace_id),
            "mutation_id": body.mutation_id,
            "revision": body.expected_revision + 1,
            "saved_at": "2026-01-01T00:00:00+00:00",
        }

    async def replay_mutation(
        self, identity: VerifiedIdentity, workspace_id: object, body: WorkspaceMutation
    ) -> dict | None:
        self.calls.append(("replay", (identity, workspace_id, body)))
        return None

    async def journal(
        self, identity: VerifiedIdentity, workspace_id: object, after_revision: int, limit: int
    ) -> dict:
        self.calls.append(("journal", (identity, workspace_id, after_revision, limit)))
        return {"items": [], "next_revision": None}


async def setup_client(monkeypatch: pytest.MonkeyPatch, store: StoreStub):
    monkeypatch.setenv("ATLAS_HAAS_API_ORIGIN", "https://trusted.example")

    def identity_service(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://trusted.example/v1/me"
        assert request.headers["authorization"] == "Bearer synthetic"
        return httpx.Response(200, json=VERIFIED)

    app.state.identity_transport = httpx.MockTransport(identity_service)
    app.dependency_overrides[get_store] = lambda: store
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://atlas.test",
    )
    return client


@pytest.mark.asyncio
async def test_public_identity_is_canonical_not_browser_tenant_selector(monkeypatch) -> None:
    store = StoreStub()
    client = await setup_client(monkeypatch, store)
    try:
        response = await client.post(
            "/api/v1/workspaces",
            headers={"Authorization": "Bearer synthetic", "X-Tenant-ID": "tenant-selector"},
            json={"schema_version": 1, "mutation_id": "create-1", "title": "Title", "draft": ""},
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()
        app.state.identity_transport = None
    assert response.status_code == 201
    identity, _ = store.calls[0][1]
    assert identity == VerifiedIdentity("verified-tenant", "verified-principal")


@pytest.mark.asyncio
async def test_workspace_catalog_and_mutation_routes_match_v1_contract(monkeypatch) -> None:
    store = StoreStub()
    client = await setup_client(monkeypatch, store)
    workspace_id = "00000000-0000-0000-0000-000000000001"
    try:
        headers = {"Authorization": "Bearer synthetic"}
        catalog = await client.get("/api/v1/workspaces?limit=10", headers=headers)
        detail = await client.get(f"/api/v1/workspaces/{workspace_id}", headers=headers)
        mutation = await client.post(
            f"/api/v1/workspaces/{workspace_id}/mutations",
            headers=headers,
            json={
                "schema_version": 1,
                "mutation_id": "checkpoint-1",
                "expected_revision": 1,
                "kind": "draft_checkpoint",
                "payload": {"draft": "draft"},
            },
        )
        journal = await client.get(f"/api/v1/workspaces/{workspace_id}/journal", headers=headers)
    finally:
        await client.aclose()
        app.dependency_overrides.clear()
        app.state.identity_transport = None
    assert catalog.status_code == detail.status_code == mutation.status_code == journal.status_code == 200
    assert detail.json()["state"] == {"draft": ""}
    assert mutation.json() == {
        "workspace_id": workspace_id,
        "mutation_id": "checkpoint-1",
        "revision": 2,
        "saved_at": "2026-01-01T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_header_only_or_unauthorized_identity_never_calls_storage(monkeypatch) -> None:
    store = StoreStub()
    monkeypatch.setenv("ATLAS_HAAS_API_ORIGIN", "https://trusted.example")
    app.dependency_overrides[get_store] = lambda: store
    called = False

    def unauthorized(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(401)

    app.state.identity_transport = httpx.MockTransport(unauthorized)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://atlas.test",
    )
    try:
        response = await client.get("/api/v1/workspaces", headers={"X-Tenant-ID": "forged"})
    finally:
        await client.aclose()
        app.dependency_overrides.clear()
        app.state.identity_transport = None
    assert response.status_code == 401
    assert called is False
    assert not store.calls


@pytest.mark.asyncio
async def test_identity_verifier_denial_does_not_touch_storage(monkeypatch) -> None:
    store = StoreStub()
    client = await setup_client(monkeypatch, store)

    async def denied() -> VerifiedIdentity:
        raise HTTPException(403, "denied")

    app.dependency_overrides[require_identity] = denied
    try:
        response = await client.get("/api/v1/workspaces")
    finally:
        await client.aclose()
        app.dependency_overrides.clear()
        app.state.identity_transport = None
    assert response.status_code == 403
    assert not store.calls


@pytest.mark.asyncio
async def test_oversized_workspace_request_is_rejected_before_storage(monkeypatch) -> None:
    store = StoreStub()
    client = await setup_client(monkeypatch, store)
    try:
        response = await client.post("/api/v1/workspaces", content=b"x" * (300 * 1024 + 1))
    finally:
        await client.aclose()
        app.dependency_overrides.clear()
        app.state.identity_transport = None
    assert response.status_code == 413
    assert response.json() == {"code": "request_too_large"}
    assert not store.calls


@pytest.mark.asyncio
async def test_conflict_response_is_the_documented_top_level_code(monkeypatch) -> None:
    store = StoreStub()
    client = await setup_client(monkeypatch, store)
    try:
        response = await client.post(
            "/api/v1/workspaces/00000000-0000-0000-0000-000000000001/mutations",
            headers={"Authorization": "Bearer synthetic"},
            json={
                "schema_version": 1,
                "mutation_id": "force-conflict",
                "expected_revision": 1,
                "kind": "draft_checkpoint",
                "payload": {"draft": "draft"},
            },
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()
        app.state.identity_transport = None
    assert response.status_code == 409
    assert response.json() == {"code": "revision_conflict", "current_revision": 9}
