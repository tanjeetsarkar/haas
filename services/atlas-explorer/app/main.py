"""Authenticated Atlas workspace API and process liveness endpoint."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.exception_handlers import http_exception_handler
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.identity import VerifiedIdentity, require_identity, verify_public_run
from app.models import BoundPayload, OutcomePayload, WorkspaceCreate, WorkspaceMutation
from app.storage import WorkspaceStore


class WorkspaceBodyLimitMiddleware:
    """Bound bodies before parsing/buffering or touching authentication/storage."""

    def __init__(self, app: ASGIApp, maximum_bytes: int = 300 * 1024) -> None:
        self.app = app
        self.maximum_bytes = maximum_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/v1/workspaces"):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        declared = headers.get(b"content-length")
        if declared:
            try:
                if int(declared) > self.maximum_bytes:
                    await self._too_large(send)
                    return
            except ValueError:
                await send({"type": "http.response.start", "status": 400, "headers": [(b"content-length", b"0")]})
                await send({"type": "http.response.body", "body": b""})
                return
        messages: list[Message] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            messages.append(message)
            size += len(message.get("body", b""))
            if size > self.maximum_bytes:
                await self._too_large(send)
                return
            if not message.get("more_body", False):
                break
        index = 0

        async def replay() -> Message:
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _too_large(send: Send) -> None:
        body = b'{"code":"request_too_large"}'
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    application.state.store = None
    application.state.store_lock = asyncio.Lock()
    yield
    store = getattr(application.state, "store", None)
    if isinstance(store, WorkspaceStore):
        await store.close()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(WorkspaceBodyLimitMiddleware)


@app.exception_handler(HTTPException)
async def workspace_error_handler(request: Request, error: HTTPException):
    detail = error.detail
    if (
        request.url.path.startswith("/api/v1/workspaces")
        and isinstance(detail, dict)
        and isinstance(detail.get("code"), str)
    ):
        return JSONResponse(status_code=error.status_code, content=detail, headers=error.headers)
    return await http_exception_handler(request, error)


async def get_store(request: Request) -> WorkspaceStore:
    store = getattr(request.app.state, "store", None)
    if store is not None:
        return store
    dsn = os.getenv("ATLAS_DATABASE_URL")
    if not dsn:
        raise HTTPException(503, "Atlas storage is not configured.")
    lock = getattr(request.app.state, "store_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.store_lock = lock
    async with lock:
        store = request.app.state.store
        if store is None:
            try:
                store = await WorkspaceStore.connect(dsn)
            except Exception:
                raise HTTPException(503, "Atlas storage is unavailable or not safely provisioned.") from None
            request.app.state.store = store
    return store


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Report process liveness without claiming database readiness."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(
    _: WorkspaceStore = Depends(get_store),
) -> dict[str, str]:
    """Prove storage connectivity, migration presence and runtime-role safety."""
    return {"status": "ready"}


@app.post("/api/v1/workspaces", status_code=201)
async def create_workspace(
    body: WorkspaceCreate,
    identity: Annotated[VerifiedIdentity, Depends(require_identity)],
    store: Annotated[WorkspaceStore, Depends(get_store)],
) -> dict:
    return await store.create(identity, body)


@app.get("/api/v1/workspaces")
async def list_workspaces(
    identity: Annotated[VerifiedIdentity, Depends(require_identity)],
    store: Annotated[WorkspaceStore, Depends(get_store)],
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    return await store.catalog(identity, cursor, limit)


@app.get("/api/v1/workspaces/{workspace_id}")
async def get_workspace(
    workspace_id: uuid.UUID,
    identity: Annotated[VerifiedIdentity, Depends(require_identity)],
    store: Annotated[WorkspaceStore, Depends(get_store)],
) -> dict:
    return await store.get(identity, workspace_id)


@app.post("/api/v1/workspaces/{workspace_id}/mutations")
async def mutate_workspace(
    workspace_id: uuid.UUID,
    body: WorkspaceMutation,
    identity: Annotated[VerifiedIdentity, Depends(require_identity)],
    store: Annotated[WorkspaceStore, Depends(get_store)],
    request: Request,
) -> dict:
    body.strict_payload()
    replay = await store.replay_mutation(identity, workspace_id, body)
    if replay is not None:
        return replay
    if body.kind == "operation_bound":
        bound_payload = BoundPayload.model_validate(body.payload)
        run = await verify_public_run(
            request.headers.get("authorization"),
            request.headers.get("x-tenant-id"),
            bound_payload.run_id,
            transport=getattr(request.app.state, "identity_transport", None),
        )
        if bound_payload.thread_id is not None and run["thread_id"] != bound_payload.thread_id:
            raise HTTPException(409, {"code": "operation_thread_mismatch"})
    if body.kind == "operation_outcome":
        outcome_payload = OutcomePayload.model_validate(body.payload)
        if outcome_payload.state == "unknown" and outcome_payload.run_id:
            await verify_public_run(
                request.headers.get("authorization"),
                request.headers.get("x-tenant-id"),
                outcome_payload.run_id,
                transport=getattr(request.app.state, "identity_transport", None),
            )
        if outcome_payload.state == "terminal":
            run_id = await store.operation_run_id(identity, workspace_id, outcome_payload.operation_id)
            await verify_public_run(
                request.headers.get("authorization"),
                request.headers.get("x-tenant-id"),
                run_id,
                terminal_status=outcome_payload.status,
                transport=getattr(request.app.state, "identity_transport", None),
            )
    return await store.mutate(identity, workspace_id, body)


@app.get("/api/v1/workspaces/{workspace_id}/journal")
async def workspace_journal(
    workspace_id: uuid.UUID,
    identity: Annotated[VerifiedIdentity, Depends(require_identity)],
    store: Annotated[WorkspaceStore, Depends(get_store)],
    after_revision: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    return await store.journal(identity, workspace_id, after_revision, limit)
