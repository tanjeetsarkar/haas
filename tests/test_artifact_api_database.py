"""Artifact API qualification against a disposable migrated PostgreSQL database."""

import json
import os
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request

import app.main  # noqa: F401
from app.api import artifacts
from app.core.identity import AuthenticatedIdentity
from app.core.object_store import LocalFilesystemObjectStore
from test_phase2_database import admit, db  # noqa: F401

pytestmark = pytest.mark.skipif(
    not os.environ.get("HAAS_TEST_POSTGRES_DSN"),
    reason="Requires disposable PostgreSQL qualification DB",
)


@pytest.fixture
async def artifact_api(db, monkeypatch, tmp_path):  # noqa: F811
    owner, storage, slug, other_slug, tenant, other, principal, _ = db
    run, thread = f"run_{uuid4().hex}", f"thread_{uuid4().hex}"
    await admit(storage, slug, thread, run)
    storage.object_store = LocalFilesystemObjectStore(tmp_path)
    monkeypatch.setattr(artifacts, "store", storage)
    app = FastAPI()
    app.state.identity = AuthenticatedIdentity(
        tenant_id=str(tenant),
        principal_id=str(principal),
        issuer="https://qa.invalid",
        subject=str(principal),
        roles=frozenset({"tenant_admin"}),
        credential_type="oidc",
    )

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.identity = app.state.identity
        return await call_next(request)

    app.include_router(artifacts.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, app, owner, storage, slug, tenant, other, run


def upload_parts(run, *, blob=True):
    return {
        "request": (
            "blob" if blob else None,
            json.dumps({
                "run_id": run,
                "name": "snapshot.json",
                "content_type": "application/json",
                "source": "api",
                "metadata": {"nested": ["value"]},
            }),
            "application/json",
        ),
        "file": ("snapshot.json", b'{"ok":true}', "application/json"),
    }


@pytest.mark.parametrize("selector", ["slug", "uuid"])
@pytest.mark.parametrize("blob", [True, False])
async def test_real_artifact_crud_pagination_and_audit(artifact_api, selector, blob):
    client, app, owner, _, slug, tenant, other, run = artifact_api
    app.state.identity = replace(
        app.state.identity, tenant_id=slug if selector == "slug" else str(tenant)
    )
    # A caller-controlled header cannot override the authenticated tenant.
    client.headers["x-haas-tenant"] = str(other)
    ids = []
    for _ in range(3):
        response = await client.post("/v1/artifacts", files=upload_parts(run, blob=blob))
        assert response.status_code == 200, response.text
        ids.append(response.json()["artifact_id"])
    response = await client.get(f"/v1/artifacts/{ids[0]}")
    assert response.status_code == 200, response.text
    assert response.json()["metadata"] == {"nested": ["value"]}
    assert response.json()["tenant_id"] == slug
    cursor, seen = None, []
    while True:
        params = {"run_id": run, "limit": 1}
        if cursor:
            params["cursor"] = cursor
        response = await client.get("/v1/artifacts", params=params)
        assert response.status_code == 200, response.text
        seen.extend(row["artifact_id"] for row in response.json()["items"])
        cursor = response.json()["next_cursor"]
        if not cursor:
            break
        assert len(seen) < 4
    assert set(seen) == set(ids)
    response = await client.get(f"/v1/artifacts/{ids[0]}/download")
    assert response.status_code == 200
    assert response.content == b'{"ok":true}'
    assert await owner.fetchval(
        "SELECT count(*) FROM audit_events WHERE tenant_id=$1 "
        "AND action='artifact.created' AND target_id=ANY($2::text[])",
        tenant, ids,
    ) == 3
    app.state.identity = replace(app.state.identity, tenant_id=str(other))
    for suffix in ("", "/download"):
        assert (await client.get(f"/v1/artifacts/{ids[0]}{suffix}")).status_code == 404
    assert (await client.delete(f"/v1/artifacts/{ids[0]}")).status_code == 404
    assert (await client.get("/v1/artifacts", params={"run_id": run})).json()["items"] == []
    app.state.identity = replace(app.state.identity, tenant_id=str(tenant))
    assert (await client.delete(f"/v1/artifacts/{ids[0]}")).status_code == 200
    assert await owner.fetchval(
        "SELECT deleted_at IS NOT NULL FROM artifacts WHERE artifact_id=$1::uuid", ids[0]
    )


@pytest.mark.parametrize("target", ["missing", "foreign", "unresolved"])
async def test_invalid_run_or_tenant_fails_before_object_write(artifact_api, target):
    client, app, _, storage, _, _, other, run = artifact_api
    if target == "missing":
        run = "does-not-exist"
    elif target == "foreign":
        app.state.identity = replace(app.state.identity, tenant_id=str(other))
    else:
        app.state.identity = replace(app.state.identity, tenant_id="unknown-tenant")
    response = await client.post("/v1/artifacts", files=upload_parts(run))
    assert response.status_code == 404, response.text
    assert not list(storage.object_store.root.rglob("*"))


@pytest.mark.parametrize("guard", ["quota", "tenant", "run", "thread"])
async def test_current_schema_quota_and_legal_holds(artifact_api, guard):
    client, _, owner, storage, _, tenant, _, run = artifact_api
    if guard == "quota":
        await owner.execute(
            "INSERT INTO retention_policies (tenant_uuid,artifact_quota_bytes) VALUES ($1,1)",
            tenant,
        )
    else:
        subject = run if guard == "run" else None
        if guard == "thread":
            subject = await owner.fetchval("SELECT thread_id FROM runs WHERE run_id=$1", run)
        await owner.execute(
            "INSERT INTO legal_holds (hold_id,tenant_uuid,scope,subject_id,reason) "
            "VALUES ($1,$2,$3,$4,'test')", uuid4(), tenant, guard, subject,
        )
    response = await client.post("/v1/artifacts", files=upload_parts(run))
    assert response.status_code == (413 if guard == "quota" else 409), response.text
    assert not list(storage.object_store.root.rglob("*"))


async def test_bad_cursor_returns_422(artifact_api):
    client, _, _, _, _, _, _, run = artifact_api
    response = await client.get(
        "/v1/artifacts", params={"run_id": run, "cursor": "invalid"}
    )
    assert response.status_code == 422, response.text


async def test_quota_counts_usage_across_slug_and_uuid_selectors(artifact_api):
    client, app, owner, _, slug, tenant, _, run = artifact_api
    await owner.execute(
        "INSERT INTO retention_policies (tenant_uuid,artifact_quota_bytes) VALUES ($1,15)",
        tenant,
    )
    app.state.identity = replace(app.state.identity, tenant_id=slug)
    assert (await client.post("/v1/artifacts", files=upload_parts(run))).status_code == 200
    app.state.identity = replace(app.state.identity, tenant_id=str(tenant))
    response = await client.post("/v1/artifacts", files=upload_parts(run))
    assert response.status_code == 413, response.text


async def test_malformed_metadata_and_forbidden_role_do_not_persist(artifact_api):
    client, app, owner, _, _, tenant, _, run = artifact_api
    files = upload_parts(run)
    files["request"] = ("blob", "{", "application/json")
    assert (await client.post("/v1/artifacts", files=files)).status_code == 422
    app.state.identity = replace(app.state.identity, roles=frozenset({"auditor"}))
    response = await client.post("/v1/artifacts", files=upload_parts(run))
    assert response.status_code == 403
    assert await owner.fetchval(
        "SELECT count(*) FROM artifacts WHERE tenant_uuid=$1", tenant
    ) == 0
