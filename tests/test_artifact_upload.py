"""Public multipart uploads and request tenant-context lifecycle regressions."""

import asyncio
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request

import app.main  # noqa: F401  # Initialize feature routers in application order.
from app.api import artifacts
from app.api.deps import PrincipalInfo, tenant_scope
from app.core.database_context import tenant_context
from app.core.identity import AuthenticatedIdentity

TENANT = str(uuid.UUID(int=1))
IDENTITY = AuthenticatedIdentity(
    tenant_id=TENANT,
    principal_id=str(uuid.UUID(int=2)),
    issuer="https://identity.example.test/",
    subject="user",
    roles=frozenset({"tenant_admin"}),
    credential_type="oidc",
)
METADATA = {
    "run_id": "run-test",
    "name": "probe.json",
    "content_type": "application/json",
    "source": "api",
    "metadata": {"nested": ["value"]},
}


@pytest.fixture
def upload_app(monkeypatch):
    class MemoryStore:
        def __init__(self):
            self.object_store = AsyncMock()
            self.object_store.get.return_value = b"{}"
            self._insert_audit_event = AsyncMock()
            self.rows = {}
            self.connection_count = 0

        @asynccontextmanager
        async def connection(self):
            assert tenant_context.get() in {TENANT, str(uuid.UUID(int=3))}
            self.connection_count += 1
            yield self

        async def fetchrow(self, sql, *args):
            if "FROM tenants" in sql:
                assert args == (tenant_context.get(),)
                return {
                    "tenant_id": uuid.UUID(TENANT),
                    "retention_class": "standard",
                }
            if "FROM runs" in sql:
                return {"tenant_id": TENANT, "thread_id": "thread-test"}
            if "FROM retention_policies" in sql:
                return None
            if "FROM legal_holds" in sql:
                return None
            row = self.rows.get(args[0])
            # Simulate the unchanged forced RLS visibility rule.
            return row if row and row["tenant_id"] == tenant_context.get() else None

        async def execute(self, sql, *args):
            assert "INSERT INTO artifacts" in sql
            self.rows[args[0]] = {
                "tenant_id": args[1],
                "object_key": args[8],
                "content_type": args[5],
                "name": args[4],
            }
            assert json.loads(args[11]) == METADATA["metadata"]

        async def fetchval(self, sql, *args):
            return 0

    memory = MemoryStore()
    monkeypatch.setattr(artifacts, "store", memory)
    app = FastAPI()
    app.state.identity = IDENTITY

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.identity = app.state.identity
        return await call_next(request)

    app.include_router(artifacts.router)
    return app, memory


def parts(metadata, *, blob=True):
    return {
        "file": ("probe.json", b"{}", "application/json"),
        "request": ("blob" if blob else None, metadata, "application/json"),
    }


@pytest.mark.parametrize("blob", [True, False], ids=["sdk-blob", "form-string"])
async def test_upload_and_download(upload_app, blob):
    app, memory = upload_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/artifacts", files=parts(json.dumps(METADATA), blob=blob)
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["size_bytes"] == 2
        assert body["sha256"] == hashlib.sha256(b"{}").hexdigest()
        memory.object_store.put.assert_awaited_once_with(body["object_key"], b"{}")
        artifact_id = body["artifact_id"]
        response = await client.get(f"/v1/artifacts/{artifact_id}/download")
        assert response.status_code == 200
        assert response.content == b"{}"
        app.state.identity = replace(IDENTITY, tenant_id=str(uuid.UUID(int=3)))
        response = await client.get(f"/v1/artifacts/{artifact_id}/download")
        assert response.status_code == 404
        memory.object_store.get.assert_awaited_once()


@pytest.mark.parametrize("blob", [True, False])
@pytest.mark.parametrize(
    "metadata",
    [
        "{",
        "[]",
        "{}",
        json.dumps({**METADATA, "tenant_id": "other"}),
        json.dumps({**METADATA, "source": "invalid"}),
        json.dumps({**METADATA, "name": ""}),
        json.dumps({**METADATA, "metadata": []}),
        '{"metadata": {"value": NaN}}',
        '{"metadata": {"value": 1e999}}',
        b"\xff",
        pytest.param(" " * (64 * 1024 + 1), id="oversized"),
        pytest.param("é" * (32 * 1024 + 1), id="oversized-utf8"),
    ],
)
async def test_bad_metadata_is_422_without_storage(upload_app, blob, metadata):
    app, memory = upload_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/v1/artifacts", files=parts(metadata, blob=blob))
    assert response.status_code == 422, response.text
    assert memory.connection_count == 0
    memory.object_store.put.assert_not_awaited()


@pytest.mark.parametrize("blob", [True, False], ids=["strict-blob", "form-fallback"])
async def test_complete_json_with_invalid_utf8_byte(upload_app, blob):
    app, memory = upload_app
    metadata = json.dumps({**METADATA, "name": "invalid-byte"}).encode("utf-8")
    metadata = metadata.replace(b"invalid-byte", b"\xff")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/v1/artifacts", files=parts(metadata, blob=blob))
    if blob:
        assert response.status_code == 422
        assert memory.connection_count == 0
        memory.object_store.put.assert_not_awaited()
    else:
        assert response.status_code == 200, response.text
        row = memory.rows[uuid.UUID(response.json()["artifact_id"])]
        assert row["name"] == "\u00ff"  # Starlette's Latin-1 fallback, not raw UTF-8.


@pytest.mark.parametrize("missing", ["file", "request"])
async def test_missing_part_is_422(upload_app, missing):
    app, memory = upload_app
    files = parts(json.dumps(METADATA))
    del files[missing]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/v1/artifacts", files=files)
    assert response.status_code == 422
    assert memory.connection_count == 0


async def test_file_must_be_upload(upload_app):
    app, memory = upload_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/artifacts",
            files={
                "request": ("blob", json.dumps(METADATA), "application/json"),
                "file": (None, "not a file"),
            },
        )
    assert response.status_code == 422
    assert memory.connection_count == 0


async def test_empty_cursor_returns_422(upload_app):
    app, _ = upload_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/artifacts", params={"run_id": "run-test", "cursor": ""}
        )
    assert response.status_code == 422, response.text


async def test_upload_authorization_is_unchanged(upload_app):
    app, memory = upload_app
    app.state.identity = replace(IDENTITY, roles=frozenset({"auditor"}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/artifacts", files=parts(json.dumps(METADATA))
        )
    assert response.status_code == 403
    assert memory.connection_count == 0


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("error", [False, True])
async def test_scope_visible_and_restored_for_handlers(sync, error):
    app = FastAPI()

    def handler(principal=Depends(tenant_scope)):
        assert tenant_context.get() == principal.tenant_id
        if error:
            raise HTTPException(409, "intentional")
        return {"tenant": principal.tenant_id}

    async def async_handler(principal=Depends(tenant_scope)):
        return handler(principal)

    app.get("/probe")(handler if sync else async_handler)
    # Supply authentication without replacing tenant_scope itself.
    from app.api.deps import get_principal

    app.dependency_overrides[get_principal] = lambda: PrincipalInfo(
        principal_id=IDENTITY.principal_id, roles=["tenant_admin"], tenant_id=TENANT
    )
    token = tenant_context.set("outer")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/probe")
        assert response.status_code == (409 if error else 200)
        assert tenant_context.get() == "outer"
    finally:
        tenant_context.reset(token)


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_scope_restored_on_exception_and_cancellation(error):
    principal = PrincipalInfo(
        principal_id=IDENTITY.principal_id, roles=[], tenant_id=TENANT
    )
    token = tenant_context.set("outer")
    try:
        with pytest.raises(error):
            async with asynccontextmanager(tenant_scope)(principal):
                assert tenant_context.get() == TENANT
                raise error()
        assert tenant_context.get() == "outer"
    finally:
        tenant_context.reset(token)


async def test_scope_restored_when_task_is_cancelled():
    entered = asyncio.Event()
    restored = []
    principal = PrincipalInfo(
        principal_id=IDENTITY.principal_id, roles=[], tenant_id=TENANT
    )

    async def worker():
        token = tenant_context.set("outer")
        try:
            async with asynccontextmanager(tenant_scope)(principal):
                entered.set()
                await asyncio.Event().wait()
        finally:
            restored.append(tenant_context.get())
            tenant_context.reset(token)

    task = asyncio.create_task(worker())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert restored == ["outer"]


async def test_scope_binds_real_store_connection_to_authenticated_tenant():
    from app.core.config import settings
    from app.core.store import DistributedStore

    @asynccontextmanager
    async def transaction():
        yield

    conn = SimpleNamespace(transaction=transaction, execute=AsyncMock())

    @asynccontextmanager
    async def acquire():
        yield conn

    storage = DistributedStore()
    storage.pg_pool = SimpleNamespace(acquire=acquire)
    principal = PrincipalInfo(
        principal_id=IDENTITY.principal_id, roles=[], tenant_id=TENANT
    )
    async with asynccontextmanager(tenant_scope)(principal):
        async with storage.connection():
            conn.execute.assert_awaited_once_with(
                "SELECT set_config('haas.tenant', $1, true), "
                "set_config('haas.legacy_dev', $2, true)",
                TENANT,
                "on" if settings.haas_profile == "dev" else "off",
            )
