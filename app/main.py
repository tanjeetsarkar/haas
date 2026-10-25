from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import re
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable, Collection
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncGenerator, Literal
from uuid import UUID

import asyncpg
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from redis.exceptions import RedisError
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings, validate_profile_settings
from app.core.agents import AgentConfiguration
from app.core.database_context import DatabaseTenantMiddleware
from app.core.authorization import (
    Action,
    enforce_approval_separation_of_duties,
    get_authenticated_identity,
    is_authorized,
    require_action,
    require_any_action,
)
from app.core.context import SecurityViolationError, validate_safe_outbound_url
from app.core.http import (
    RequestSizeLimitMiddleware,
    http_exception_problem,
    unexpected_problem,
    validation_problem,
)
from app.core.identity import IdentityAuthenticator, IdentityMiddleware
from app.core.maintenance import maintenance_loop
from app.core.observability import configure_logging, configure_observability
from app.core import openai_compat as oai
from app.core.output_stream import (
    OUTPUT_DELTA_EVENT,
    OUTPUT_DELTA_FEATURE,
    decode_delta,
    delta_key,
)
from app.core.plugins import registry, resolve_run_allowlist
from app.core.store import (
    DistributedStore,
    StoreConflictError,
    StoreNotFoundError,
    ThreadOwnershipError,
)
from app.core.envelope import sha256_hex
from app.core.identity import IdentityError, IdentityUnavailable
from app.core.workflow import (
    TERMINAL_EVENTS,
    IdempotencyConflictError,
    IdempotentReplay,
    run_error_code,
)
from app.core.tenant_policy import (
    TenantPolicyRules,
    TenantPolicy,
    TenantPolicyIntegrityError,
    policy_digest,
)
from app.worker.tasks import make_outbox_publisher

log = logging.getLogger(__name__)
store = DistributedStore()
identity_authenticator = IdentityAuthenticator(store, settings)

TERMINAL_STATUSES = ("completed", "failed", "cancelled")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


def _require_action(action: Action):
    return Depends(
        require_action(action, allow_legacy_dev=settings.haas_profile == "dev")
    )


def _require_any_action(actions: tuple[Action, ...]):
    return Depends(
        require_any_action(actions, allow_legacy_dev=settings.haas_profile == "dev")
    )


def build_tier1_system_prompt(allowed_skills: list[str] | None = None) -> str:
    catalog_block = registry.render_catalog(
        allowed_skills, settings.skills_prompt_max_chars
    )
    return (
        "You are an autonomous execution agent running inside a deterministic distributed harness.\n"
        "1. Review the Domain Skills Catalog below. When a task matches a skill, call "
        "`activate_skill` FIRST to load its Standard Operating Procedure and unlock its tools.\n"
        "2. Never guess parameters or schema names. Follow remediation errors carefully.\n\n"
        "## Domain Skills Catalog (Tier 1)\n"
        f"{catalog_block}"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    maintenance: asyncio.Task[None] | None = None
    try:
        configure_logging()
        validate_profile_settings(settings, role="api")
        settings.plugins_dir.mkdir(parents=True, exist_ok=True)
        registry.discover_from_settings(settings)
        await store.init()
        store.outbox_publisher = make_outbox_publisher(store)
        if settings.haas_profile == "dev":
            maintenance = asyncio.create_task(
                maintenance_loop(store), name="haas-maintenance"
            )
        else:
            log.info(
                "Production cross-tenant maintenance requires the separate maintenance service."
            )
        yield
    finally:
        if maintenance is not None:
            maintenance.cancel()
            try:
                await maintenance
            except asyncio.CancelledError:
                pass
        await identity_authenticator.aclose()
        await store.close()


class RequestIdMiddleware:
    """Propagates or assigns X-Request-ID; handlers that audit may set their own."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = next(
            (
                value.decode("latin-1")
                for key, value in scope.get("headers", [])
                if key.lower() == b"x-request-id"
            ),
            "",
        )
        request_id = (
            incoming if REQUEST_ID_RE.fullmatch(incoming) else str(uuid.uuid4())
        )
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(key.lower() == b"x-request-id" for key, _ in headers):
                    headers.append((b"x-request-id", request_id.encode("latin-1")))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_id)


app = FastAPI(title=settings.app_name, version="3.0.0", lifespan=lifespan)
configure_observability(app)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins_list,
    allow_methods=settings.cors_allowed_methods_list,
    allow_headers=settings.cors_allowed_headers_list,
    allow_credentials=settings.cors_allow_credentials,
    max_age=settings.cors_max_age,
)
app.add_middleware(
    RequestSizeLimitMiddleware, max_bytes=settings.max_request_body_bytes
)
app.add_middleware(
    IdentityMiddleware,
    authenticator=identity_authenticator,
    allow_legacy_header_tenancy=settings.haas_profile == "dev",
)
app.add_middleware(
    DatabaseTenantMiddleware,
    allow_legacy_dev=settings.haas_profile == "dev",
)
app.add_middleware(RequestIdMiddleware)
app.add_exception_handler(HTTPException, http_exception_problem)
app.add_exception_handler(RequestValidationError, validation_problem)
app.add_exception_handler(Exception, unexpected_problem)


class CreateRunRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=settings.max_prompt_chars)
    thread_id: str | None = None
    agent_revision_id: UUID | None = None
    expected_history_version: int | None = Field(default=None, ge=0)
    preload_skills: list[str] = Field(default_factory=list)
    # Optional allowlist for this run; can only narrow the tenant's allowed skills.
    skills: list[str] | None = None
    primary_model: str | None = None
    planner_model: str | None = None
    max_steps: int | None = Field(default=None, ge=1, le=50)
    max_cost_usd: float | None = Field(default=None, gt=0.0, le=10.0)
    webhook_url: str | None = None
    secrets: dict[str, str] = Field(default_factory=dict)
    deadline_sec: int | None = Field(default=None, ge=10, le=86_400)
    max_tool_invocations: int | None = Field(default=None, ge=1, le=1_000)


class ResumeRunRequest(BaseModel):
    approved: bool
    rejection_reason: str | None = Field(default=None, max_length=2_000)
    secrets: dict[str, str] = Field(default_factory=dict)
    batch_id: UUID | None = None
    batch_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    batch_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str | None = Field(default=None, max_length=2_000)
    secrets: dict[str, str] = Field(default_factory=dict)


TenantRole = Literal["tenant_admin", "submitter", "approver", "auditor"]


class CreateMembershipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=255)
    kind: Literal["human", "service"] = "human"
    role: TenantRole


class CreateTenantAPIKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_id: UUID
    name: str = Field(min_length=1, max_length=128)
    expires_in_days: int = Field(default=90, ge=1, le=365)


class CreateTenantPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: TenantPolicyRules


class CreateAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_name: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    configuration: AgentConfiguration


class BranchThreadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_history_version: int = Field(ge=0)

class CreateThreadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metadata: dict[str, Any] | None = None

@app.get("/v1/threads", dependencies=[_require_action(Action.RUN_READ)])
async def list_threads(
    x_tenant_id: str = Header(default="default-tenant"),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    search: str = Query(default="", max_length=128),
) -> dict[str, Any]:
    rows = await store.list_threads(
        x_tenant_id,
        limit=limit + 1,
        before=_decode_cursor(cursor) if cursor else None,
        search=search,
    )
    items = rows[:limit]
    return {
        "items": items,
        "next_cursor": (
            _encode_cursor(items[-1]["updated_at"], items[-1]["thread_id"])
            if len(rows) > limit
            else None
        ),
    }


@app.post(
    "/v1/threads",
    status_code=201,
    dependencies=[_require_action(Action.RUN_SUBMIT)],
)
async def create_thread(
    req: CreateThreadRequest,
    request: Request,
    response: Response,
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    request_id = str(uuid.uuid4())
    response.headers["X-Request-ID"] = request_id
    identity = get_authenticated_identity(request)
    thread_id = f"thd_{uuid.uuid4().hex[:12]}"
    metadata = req.metadata or {}
    try:
        await store.create_thread(
            thread_id=thread_id,
            tenant_id=x_tenant_id,
            metadata=metadata,
            actor_principal_id=identity.principal_id if identity else None,
            request_id=request_id,
        )
    except StoreConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"thread_id": thread_id, "metadata": metadata}


@app.get("/v1/threads/{thread_id}", dependencies=[_require_action(Action.RUN_READ)])
async def thread_history(
    thread_id: str,
    x_tenant_id: str = Header(default="default-tenant"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    history_version: int | None = Query(default=None, ge=0),
) -> dict[str, Any]:
    try:
        thread = await store.get_thread(thread_id, x_tenant_id)
    except StoreNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    if history_version is not None and history_version != thread["history_version"]:
        raise HTTPException(409, "Thread history changed; restart paging.")
    messages = thread.pop("messages")
    thread["items"] = messages[offset : offset + limit]
    thread["next_offset"] = offset + limit if offset + limit < len(messages) else None
    return thread


@app.post(
    "/v1/threads/{thread_id}/branches",
    status_code=201,
    dependencies=[_require_action(Action.RUN_SUBMIT)],
)
async def branch_thread(
    thread_id: str,
    req: BranchThreadRequest,
    request: Request,
    response: Response,
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    request_id = str(uuid.uuid4())
    response.headers["X-Request-ID"] = request_id
    identity = get_authenticated_identity(request)
    try:
        return await store.branch_thread(
            thread_id,
            x_tenant_id,
            expected_history_version=req.expected_history_version,
            actor_principal_id=identity.principal_id if identity else None,
            request_id=request_id,
        )
    except StoreNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except StoreConflictError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/v1/agents/revisions", dependencies=[_require_action(Action.RUN_READ)])
async def list_agent_revisions(
    x_tenant_id: str = Header(default="default-tenant"),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    agent_name: str | None = Query(default=None, max_length=128),
    state: list[str] | None = Query(
        default=None, description="Revision state filter; repeat or comma-separate."
    ),
) -> dict[str, Any]:
    before = _decode_cursor(cursor) if cursor else None
    if before:
        try:
            UUID(before[1])
        except ValueError as exc:
            raise HTTPException(422, "Invalid revision cursor.") from exc
    states = _csv_filter(state)

    async def fetch(size: int, key: ListKey | None) -> list[dict[str, Any]]:
        return await store.list_agent_revisions(x_tenant_id, limit=size, before=key)

    items, next_key = await _keyset_page(
        fetch,
        before=before,
        limit=limit,
        key=lambda row: (float(row["created_at"]), row["revision_id"]),
        keep=(
            (
                lambda row: (
                    (agent_name is None or row["agent_name"] == agent_name)
                    and (states is None or row["state"] in states)
                )
            )
            if agent_name is not None or states is not None
            else None
        ),
    )
    return {
        "items": items,
        "next_cursor": _encode_cursor(*next_key) if next_key else None,
    }


@app.get(
    "/v1/agents/revisions/{revision_id}",
    dependencies=[_require_action(Action.RUN_READ)],
)
async def get_agent_revision(
    revision_id: UUID,
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    try:
        return await store.get_agent_revision(x_tenant_id, str(revision_id))
    except StoreNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post(
    "/v1/agents/revisions",
    status_code=201,
    dependencies=[_require_action(Action.TENANT_ADMIN)],
)
async def draft_agent_revision(
    req: CreateAgentRequest,
    request: Request,
    response: Response,
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    identity = get_authenticated_identity(request)
    if identity is None:
        raise HTTPException(401, "Agent authoring requires a verified identity.")
    policy = await _get_tenant_policy(x_tenant_id)
    try:
        req.configuration.validate_policy(policy, registry)
        request_id = str(uuid.uuid4())
        result = await store.create_agent_revision(
            x_tenant_id,
            agent_name=req.agent_name,
            configuration=req.configuration.model_dump(mode="json"),
            digest=req.configuration.digest(),
            policy_revision_id=policy.policy_revision_id,
            registry_fingerprint=registry.fingerprint,
            actor_principal_id=identity.principal_id,
            request_id=request_id,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except StoreConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    response.headers["X-Request-ID"] = request_id
    return result


@app.post(
    "/v1/agents/revisions/{revision_id}/{operation}",
    dependencies=[_require_action(Action.TENANT_ADMIN)],
)
async def transition_agent_revision(
    revision_id: UUID,
    operation: Literal["validate", "publish"],
    request: Request,
    response: Response,
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    identity = get_authenticated_identity(request)
    if identity is None:
        raise HTTPException(401, "Agent authoring requires a verified identity.")
    policy = await _get_tenant_policy(x_tenant_id)
    try:
        revision = await store.get_agent_revision(x_tenant_id, str(revision_id))
        config = AgentConfiguration.model_validate(revision["configuration"])
        if config.digest() != revision["digest"]:
            raise HTTPException(503, "Agent configuration integrity check failed.")
        config.validate_policy(policy, registry)
        request_id = str(uuid.uuid4())
        result = await store.transition_agent_revision(
            x_tenant_id,
            str(revision_id),
            target="validated" if operation == "validate" else "published",
            actor_principal_id=identity.principal_id,
            request_id=request_id,
            policy_revision_id=policy.policy_revision_id,
            registry_fingerprint=registry.fingerprint,
        )
    except StoreNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except StoreConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    response.headers["X-Request-ID"] = request_id
    return result


async def _get_owned_run(run_id: str, tenant_id: str) -> dict[str, Any]:
    run = await store.get_run(run_id)
    # 404 (not 403) for other tenants' runs so run ids are not discoverable.
    if not run or tenant_id not in (
        run["tenant_id"],
        str(run["tenant_uuid"]) if run.get("tenant_uuid") is not None else None,
        run.get("tenant_slug"),
    ):
        raise HTTPException(status_code=404, detail="Run not found.")
    return run


async def _get_tenant_policy(tenant_id: str) -> TenantPolicy:
    record = await store.get_active_tenant_policy(tenant_id)
    if record is None:
        raise HTTPException(
            status_code=403, detail="Tenant has no active policy revision."
        )
    try:
        return TenantPolicy.from_record(record)
    except TenantPolicyIntegrityError as exc:
        log.error("Invalid active policy for tenant %s: %s", tenant_id, exc)
        raise HTTPException(
            status_code=503,
            detail="Active tenant policy is invalid; contact an administrator.",
        ) from exc


@app.get("/livez")
async def livez() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> Any:
    checks = {"postgres": "ok", "redis": "ok"}
    try:
        async with store.pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
        checks["postgres"] = "error"
    try:
        await store.rds.ping()
    except (RedisError, OSError, TimeoutError):
        checks["redis"] = "error"
    if "error" in checks.values():
        return JSONResponse({"status": "not_ready", "checks": checks}, status_code=503)
    return {"status": "ok", "checks": checks}


@app.get("/healthz", include_in_schema=False)
async def healthz() -> Any:
    return await readyz()


def _encode_cursor(created_at: float, run_id: str) -> str:
    raw = json.dumps(
        {"created_at": created_at, "run_id": run_id}, separators=(",", ":")
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[float, str]:
    try:
        raw = base64.b64decode(
            cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
        )
        value = json.loads(raw)
        created_at = float(value["created_at"])
        run_id = value["run_id"]
        if not math.isfinite(created_at) or not isinstance(run_id, str) or not run_id:
            raise ValueError
        return created_at, run_id
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid run-list cursor.") from exc


# Rows examined per filtered list request before returning a partial page with a cursor.
_LIST_SCAN_BUDGET = 2_000
ListKey = tuple[float, str]


async def _keyset_page(
    fetch: Callable[[int, ListKey | None], Awaitable[list[dict[str, Any]]]],
    *,
    before: ListKey | None,
    limit: int,
    key: Callable[[dict[str, Any]], ListKey],
    keep: Callable[[dict[str, Any]], bool] | None = None,
    stop: Callable[[dict[str, Any]], bool] | None = None,
) -> tuple[list[dict[str, Any]], ListKey | None]:
    """
    Keyset page over newest-first rows with optional in-API filters. Without filters this
    is a single `limit + 1` query; filtered scans stop after `_LIST_SCAN_BUDGET` rows and
    return a cursor at the last examined row (a short page is not the end of the list).
    """
    batch = limit + 1 if keep is None and stop is None else max(limit + 1, 100)
    items: list[dict[str, Any]] = []
    scanned = 0
    while True:
        rows = await fetch(batch, before)
        for row in rows:
            scanned += 1
            if stop is not None and stop(row):
                return items, None
            if keep is None or keep(row):
                if len(items) == limit:
                    return items, key(items[-1])
                items.append(row)
            before = key(row)
        if len(rows) < batch:
            return items, None
        if scanned >= _LIST_SCAN_BUDGET:
            return items, before


def _epoch(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        return datetime.fromisoformat(value).timestamp()
    return float(value)


def _csv_filter(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    selected = {part.strip() for value in values for part in value.split(",")}
    selected.discard("")
    return selected or None


def _encode_audit_cursor(created_at: Any, event_id: str) -> str:
    raw = json.dumps(
        {"created_at": created_at.timestamp(), "event_id": event_id},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_audit_cursor(cursor: str) -> tuple[float, str]:
    try:
        raw = base64.b64decode(
            cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
        )
        value = json.loads(raw)
        created_at = float(value["created_at"])
        event_id = value["event_id"]
        UUID(event_id)
        if not math.isfinite(created_at) or not isinstance(event_id, str):
            raise ValueError
        return created_at, event_id
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid audit cursor.") from exc


def _require_actor(request: Request):
    identity = get_authenticated_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="Authenticated identity required.")
    return identity


@app.get("/v1/runs", dependencies=[_require_action(Action.RUN_READ)])
async def list_runs(
    x_tenant_id: str = Header(default="default-tenant"),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    status: list[str] | None = Query(
        default=None, description="Run status filter; repeat or comma-separate."
    ),
    thread_id: str | None = Query(default=None, max_length=255),
    created_after: datetime | None = Query(
        default=None, description="Exclusive lower bound (ISO 8601 or epoch seconds)."
    ),
    created_before: datetime | None = Query(
        default=None, description="Exclusive upper bound (ISO 8601 or epoch seconds)."
    ),
) -> dict[str, Any]:
    before = _decode_cursor(cursor) if cursor else None
    if created_before is not None:
        bound = (created_before.timestamp(), "")
        before = min(before, bound) if before else bound
    statuses = _csv_filter(status)
    after = created_after.timestamp() if created_after is not None else None

    async def fetch(size: int, key: ListKey | None) -> list[dict[str, Any]]:
        return await store.list_runs(x_tenant_id, limit=size, before=key)

    items, next_key = await _keyset_page(
        fetch,
        before=before,
        limit=limit,
        key=lambda row: (float(row["created_at"]), row["run_id"]),
        keep=(
            (
                lambda row: (
                    (statuses is None or row["status"] in statuses)
                    and (thread_id is None or row["thread_id"] == thread_id)
                )
            )
            if statuses is not None or thread_id is not None
            else None
        ),
        stop=(
            (lambda row: float(row["created_at"]) <= after)
            if after is not None
            else None
        ),
    )
    next_cursor = _encode_cursor(*next_key) if next_key else None
    return {"items": items, "next_cursor": next_cursor}


@app.post(
    "/v1/runs",
    status_code=202,
    dependencies=[_require_action(Action.RUN_SUBMIT)],
)
async def create_run(
    req: CreateRunRequest,
    request: Request,
    response: Response,
    x_tenant_id: str = Header(default="default-tenant"),
    traceparent: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    identity = get_authenticated_identity(request)
    admission: dict[str, Any] | None = None
    if idempotency_key is not None:
        if not IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
            raise HTTPException(
                422, "Idempotency-Key must be 1-255 URL-safe characters."
            )
        admission = {
            "principal_key": identity.principal_id if identity else "legacy-dev",
            "idempotency_key": idempotency_key,
            "request_digest": _admission_digest(req),
        }
        previous = await store.find_admission(
            x_tenant_id, admission["principal_key"], idempotency_key
        )
        if previous is not None:
            if previous["request_digest"] != admission["request_digest"]:
                raise HTTPException(
                    409, "Idempotency-Key was already used with a different request."
                )
            response.headers["Idempotent-Replayed"] = "true"
            return previous["response"]
    policy = await _get_tenant_policy(x_tenant_id) if identity else None
    if req.agent_revision_id is not None:
        if policy is None:
            raise HTTPException(
                401, "Revision-pinned runs require a verified identity."
            )
        try:
            revision = await store.get_agent_revision(
                x_tenant_id, str(req.agent_revision_id)
            )
        except StoreNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        if (
            revision["state"] != "published"
            or str(revision["policy_revision_id"]) != policy.policy_revision_id
            or revision["registry_fingerprint"] != registry.fingerprint
        ):
            raise HTTPException(
                409, "Revision is unpublished or its policy/inventory changed."
            )
        config = AgentConfiguration.model_validate(revision["configuration"])
        if config.digest() != revision["digest"]:
            raise HTTPException(503, "Agent configuration integrity check failed.")
        try:
            config.validate_policy(policy, registry)
        except ValueError as exc:
            raise HTTPException(403, str(exc)) from exc
        if req.model_fields_set.intersection(AgentConfiguration.model_fields):
            raise HTTPException(422, "Pinned agent configuration cannot be overridden.")
        req = req.model_copy(update=config.model_dump())
    try:
        if policy is not None:
            plugin_access = policy.resolve_plugin_access(registry)
            allowlist = resolve_run_allowlist(
                sorted(plugin_access.skill_names), req.skills
            )
        else:
            plugin_access = None
            allowlist = resolve_run_allowlist(
                settings.tenant_skill_allowlist(x_tenant_id), req.skills
            )
    except ValueError as exc:
        if policy is not None:
            raise HTTPException(
                status_code=403, detail="Requested skills exceed tenant policy."
            ) from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    usable = registry.effective_skills(registry.skills, allowlist)
    if req.skills is not None:
        unknown = sorted(set(req.skills) - set(registry.skills))
        if unknown:
            if policy is not None:
                raise HTTPException(
                    status_code=403,
                    detail="Requested skills exceed tenant policy.",
                )
            raise HTTPException(status_code=422, detail=f"Unknown skills: {unknown}")
    unknown = [s for s in req.preload_skills if s not in usable]
    if unknown:
        if policy is not None:
            raise HTTPException(
                status_code=403,
                detail="Requested preload skills exceed tenant policy.",
            )
        raise HTTPException(
            status_code=422,
            detail=f"Unknown, unavailable or not permitted preload_skills: {unknown}",
        )
    if policy is not None:
        primary_model = (
            req.primary_model
            or policy.rules.default_primary_model
            or settings.default_primary_model
        )
        planner_model = (
            req.planner_model
            or policy.rules.default_planner_model
            or settings.default_planner_model
        )
        if not policy.rules.model_is_allowed(
            primary_model
        ) or not policy.rules.model_is_allowed(planner_model):
            raise HTTPException(
                status_code=403,
                detail="Requested models are not permitted by tenant policy.",
            )
        if req.max_steps is not None and req.max_steps > policy.rules.max_steps:
            raise HTTPException(
                status_code=403,
                detail="Requested max_steps exceeds tenant policy.",
            )
        if (
            req.max_cost_usd is not None
            and req.max_cost_usd > policy.rules.max_cost_usd
        ):
            raise HTTPException(
                status_code=403,
                detail="Requested max_cost_usd exceeds tenant policy.",
            )
        max_steps = min(
            req.max_steps or settings.default_max_steps, policy.rules.max_steps
        )
        max_cost_usd = min(
            req.max_cost_usd or settings.default_max_cost_usd,
            policy.rules.max_cost_usd,
        )
    else:
        primary_model = req.primary_model or settings.default_primary_model
        planner_model = req.planner_model or settings.default_planner_model
        max_steps = req.max_steps or settings.default_max_steps
        max_cost_usd = req.max_cost_usd or settings.default_max_cost_usd
    if req.webhook_url:
        try:
            validate_safe_outbound_url(req.webhook_url)
        except SecurityViolationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if policy is not None and not policy.destination_is_allowed(req.webhook_url):
            raise HTTPException(
                status_code=403,
                detail="Webhook destination is not permitted by tenant policy.",
            )

    run_id = f"run_{uuid.uuid4().hex[:14]}"
    thread_id = req.thread_id or f"thd_{uuid.uuid4().hex[:12]}"
    if traceparent and TRACEPARENT_RE.match(traceparent):
        trace_id = traceparent
    else:
        trace_id = f"00-{uuid.uuid4().hex}-{uuid.uuid4().hex[:16]}-01"

    try:
        history = await store.get_or_create_thread_messages(thread_id, x_tenant_id)
    except ThreadOwnershipError:
        raise HTTPException(status_code=404, detail="Thread not found.")
    if not history:
        history.append(
            {"role": "system", "content": build_tier1_system_prompt(allowlist)}
        )
    history.append({"role": "user", "content": req.prompt})

    result = {
        "run_id": run_id,
        "thread_id": thread_id,
        "status": "queued",
        "stream_url": f"/v1/runs/{run_id}/stream",
        "events_url": f"/v1/runs/{run_id}/events",
    }
    if admission is not None:
        admission["response"] = result
    # Secrets must exist before admission commits: the outbox relay may dispatch immediately.
    await store.store_ephemeral_secrets(run_id, req.secrets)
    try:
        await store.create_run(
            run_id=run_id,
            thread_id=thread_id,
            tenant_id=x_tenant_id,
            traceparent=trace_id,
            primary_model=primary_model,
            planner_model=planner_model,
            initial_skills=req.preload_skills,
            initial_messages=history,
            max_steps=max_steps,
            max_cost_usd=max_cost_usd,
            webhook_url=req.webhook_url,
            skill_allowlist=allowlist,
            registry_fingerprint=registry.fingerprint,
            submitted_by_principal_id=identity.principal_id if identity else None,
            policy_revision_id=policy.policy_revision_id if policy else None,
            agent_revision_id=str(req.agent_revision_id)
            if req.agent_revision_id
            else None,
            expected_history_version=req.expected_history_version,
            admission=admission,
            deadline_sec=req.deadline_sec,
            max_tool_invocations=req.max_tool_invocations,
        )
    except IdempotentReplay as replay:
        await store.clear_ephemeral_secrets(run_id)
        response.headers["Idempotent-Replayed"] = "true"
        return replay.response
    except IdempotencyConflictError as exc:
        await store.clear_ephemeral_secrets(run_id)
        raise HTTPException(
            409, "Idempotency-Key was already used with a different request."
        ) from exc
    except StoreConflictError as exc:
        await store.clear_ephemeral_secrets(run_id)
        raise HTTPException(409, str(exc)) from exc
    except ThreadOwnershipError as exc:
        await store.clear_ephemeral_secrets(run_id)
        raise HTTPException(404, "Thread not found.") from exc
    except BaseException:
        await store.clear_ephemeral_secrets(run_id)
        raise
    return result


def _admission_digest(req: CreateRunRequest) -> str:
    """Canonical request digest; secret values are hashed so they never persist."""
    body = req.model_dump(mode="json", exclude_unset=True)
    if "secrets" in body:
        body["secrets"] = {k: sha256_hex(v) for k, v in sorted(body["secrets"].items())}
    return sha256_hex(body)


@app.post(
    "/v1/runs/{run_id}/cancel",
    status_code=202,
    dependencies=[_require_action(Action.RUN_CANCEL)],
)
async def cancel_run(
    run_id: str,
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, str]:
    await _get_owned_run(run_id, x_tenant_id)
    outcome = await store.request_cancel(run_id, x_tenant_id)
    if outcome is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    if outcome == "terminal":
        raise HTTPException(status_code=409, detail="Run is already terminal.")
    return {"run_id": run_id, "status": outcome}


@app.post(
    "/v1/runs/{run_id}/resume",
    status_code=202,
    dependencies=[_require_action(Action.RUN_APPROVE)],
)
async def resume_run(
    run_id: str,
    req: ResumeRunRequest,
    request: Request,
    response: Response,
    x_tenant_id: str = Header(default="default-tenant"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """
    Records the decision for the run's pending approval batch. Exactly one decision wins;
    production clients must bind `batch_id` and `batch_digest` from the approval record.
    """
    run = await _get_owned_run(run_id, x_tenant_id)
    identity = get_authenticated_identity(request)
    enforce_approval_separation_of_duties(
        identity,
        run.get("submitted_by_principal_id"),
    )
    if run["status"] != "input_required":
        raise HTTPException(
            status_code=409,
            detail=f"Run status is '{run['status']}', not 'input_required'.",
        )
    decision = await _decide(
        request,
        response,
        x_tenant_id,
        run_id=run_id,
        batch_id=str(req.batch_id) if req.batch_id else None,
        batch_digest=req.batch_digest,
        approved=req.approved,
        reason=req.rejection_reason,
        secrets=req.secrets,
        idempotency_key=idempotency_key,
        require_binding=settings.haas_profile == "prod",
    )
    return {
        "run_id": run_id,
        "status": "queued",
        "batch_id": decision["batch_id"],
        "replayed": decision["replayed"],
        "action": "approved_and_dispatched" if req.approved else "rejected_and_resumed",
    }


async def _decide(
    request: Request,
    response: Response,
    tenant_id: str,
    *,
    run_id: str | None,
    batch_id: str | None,
    batch_digest: str | None,
    approved: bool,
    reason: str | None,
    secrets: dict[str, str],
    idempotency_key: str | None,
    require_binding: bool,
) -> dict[str, Any]:
    if idempotency_key is not None and not IDEMPOTENCY_KEY_RE.fullmatch(
        idempotency_key
    ):
        raise HTTPException(422, "Idempotency-Key must be 1-255 URL-safe characters.")
    identity = get_authenticated_identity(request)
    request_id = str(uuid.uuid4())
    response.headers["X-Request-ID"] = request_id
    if secrets and run_id is not None:
        await store.store_ephemeral_secrets(run_id, secrets)
    try:
        decision = await store.decide_approval(
            tenant_id,
            run_id=run_id,
            batch_id=batch_id,
            batch_digest_value=batch_digest,
            approved=approved,
            reason=reason,
            actor_principal_id=identity.principal_id if identity else None,
            request_id=request_id,
            idempotency_key=idempotency_key,
            require_binding=require_binding,
        )
    except StoreNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (StoreConflictError, IdempotencyConflictError) as exc:
        raise HTTPException(409, str(exc)) from exc
    if decision["replayed"]:
        response.headers["Idempotent-Replayed"] = "true"
    return decision


@app.get(
    "/v1/runs/{run_id}/stream",
    dependencies=[_require_action(Action.RUN_STREAM)],
)
async def stream_run_events(
    run_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default-tenant"),
    last_event_id: str = Header(default="0", alias="Last-Event-ID"),
    output_deltas: bool = Query(
        default=False,
        description=(
            "Opt in to live `run.output.delta` previews when the server advertises "
            "`output-deltas.v1`. Deltas carry no SSE id, are never replayed and are "
            "superseded by `run_completed.output`."
        ),
    ),
) -> StreamingResponse:
    """
    SSE replay of the durable PostgreSQL event log. `id` is the per-run event sequence;
    Redis only carries advisory wakeups, so replicas and reconnects never lose events.
    """
    await _get_owned_run(run_id, x_tenant_id)
    credential = _bearer_credential(request)
    deltas_on = output_deltas and settings.output_delta_enabled

    async def sse_generator() -> AsyncGenerator[str, None]:
        cursor = int(last_event_id) if last_event_id.isdigit() else 0
        wake_key = f"haas:wake:{run_id}"
        wake_cursor = "$"
        out_key = delta_key(run_id)
        out_cursor = "0"
        # (step, attempt) of the in-flight model call; deltas for anything else are stale.
        live: tuple[int, int] | None = None
        need_poll = True
        last_write = last_auth = time.monotonic()
        while True:
            if need_poll:
                events = await store.list_run_events(run_id, after=cursor, limit=100)
                for event in events:
                    cursor = event["sequence"]
                    last_write = time.monotonic()
                    data = event["data"]
                    live = (
                        (int(data.get("step", 0)), int(data.get("attempt", 1)))
                        if event["type"] == "step_started"
                        else None
                    )
                    yield (
                        f"id: {cursor}\nevent: {event['type']}\n"
                        f"data: {json.dumps(data, default=str)}\n\n"
                    )
                    if event["type"] in TERMINAL_EVENTS:
                        return
                    if event["type"] == "input_required":
                        run = await store.get_run(run_id)
                        # Replays of an already-resumed run keep streaming past old pauses.
                        if run is None or (
                            run["status"] == "input_required"
                            and int(run["event_sequence"]) == cursor
                        ):
                            return
                if len(events) == 100:
                    continue
                run = await store.get_run(run_id)
                if run is None:
                    return
                latest = int(run["event_sequence"])
                if cursor > latest:
                    yield (
                        "event: stream_gap\n"
                        f"data: {json.dumps({'requested': cursor, 'latest': latest})}\n\n"
                    )
                    cursor = latest
                    continue
                if cursor >= latest and run["status"] in (
                    *TERMINAL_STATUSES,
                    "input_required",
                ):
                    return
            if await request.is_disconnected():
                return
            now = time.monotonic()
            if credential is not None and now - last_auth >= settings.sse_reauth_sec:
                last_auth = now
                try:
                    await identity_authenticator.authenticate(credential, x_tenant_id)
                except IdentityError as exc:
                    yield (
                        "event: stream_closed\n"
                        f"data: {json.dumps({'reason': str(exc)})}\n\n"
                    )
                    return
                except IdentityUnavailable:
                    log.warning("SSE re-authentication unavailable for %s", run_id)
            if now - last_write >= settings.sse_heartbeat_sec:
                last_write = now
                yield ": keep-alive\n\n"
            streams: dict[Any, Any] = {wake_key: wake_cursor}
            if deltas_on and live is not None:
                streams[out_key] = out_cursor
            try:
                entries = await store.rds.xread(streams, count=50, block=2000)
            except RedisError:
                need_poll = True
                await asyncio.sleep(1)
                continue
            # A timeout re-polls PostgreSQL, so lost wakeups only add latency.
            need_poll = not entries
            for key, items in entries or []:
                if not items:
                    continue
                if key == wake_key:
                    wake_cursor = items[-1][0]
                    need_poll = True
                    continue
                out_cursor = items[-1][0]
                for _entry_id, fields in items:
                    delta = decode_delta(fields)
                    if delta is None or live != (delta["step"], delta["attempt"]):
                        continue
                    last_write = time.monotonic()
                    yield (
                        f"event: {OUTPUT_DELTA_EVENT}\ndata: {json.dumps(delta)}\n\n"
                    )

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _bearer_credential(request: Request) -> str | None:
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    return (
        credential.strip()
        if scheme.lower() == "bearer" and credential.strip()
        else None
    )


@app.get(
    "/v1/runs/{run_id}/events",
    dependencies=[_require_action(Action.RUN_READ)],
)
async def list_run_events(
    run_id: str,
    x_tenant_id: str = Header(default="default-tenant"),
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """Versioned, replayable event envelopes (`schema_version` 1) ordered by sequence."""
    run = await _get_owned_run(run_id, x_tenant_id)
    items = await store.list_run_events(run_id, after=after, limit=limit)
    return {
        "items": items,
        "next_after": items[-1]["sequence"] if items else after,
        "latest_sequence": int(run["event_sequence"]),
    }


@app.get(
    "/v1/runs/{run_id}/steps",
    dependencies=[_require_action(Action.RUN_READ)],
)
async def list_run_steps(
    run_id: str,
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    await _get_owned_run(run_id, x_tenant_id)
    return {"run_id": run_id, **await store.list_run_steps(run_id)}


@app.get(
    "/v1/runs/{run_id}/usage",
    dependencies=[_require_action(Action.RUN_READ)],
)
async def get_run_usage(
    run_id: str,
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    run = await _get_owned_run(run_id, x_tenant_id)
    return {
        "run_id": run_id,
        "budget": await store.run_budget(run),
        **await store.run_usage(run_id),
    }


@app.get(
    "/v1/runs/{run_id}",
    dependencies=[_require_any_action((Action.RUN_READ, Action.RUN_APPROVAL_READ))],
)
async def get_run(
    run_id: str,
    request: Request,
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    run = await _get_owned_run(run_id, x_tenant_id)
    identity = get_authenticated_identity(request)
    pending = (
        await store.pending_approval_batch(run_id)
        if run["status"] == "input_required"
        else None
    )
    approval = (
        {
            "batch_id": pending["batch_id"],
            "batch_digest": pending["batch_digest"],
            "expires_at": pending["expires_at"],
            "decision_endpoint": f"/v1/approvals/{pending['batch_id']}/decision",
        }
        if pending
        else None
    )
    if (
        identity is not None
        and is_authorized(identity.roles, Action.RUN_APPROVAL_READ)
        and not is_authorized(identity.roles, Action.RUN_READ)
    ):
        if run["status"] != "input_required" or not run["pending_tool_calls"]:
            raise HTTPException(status_code=404, detail="Pending approval not found.")
        return {
            "run_id": run["run_id"],
            "status": run["status"],
            "pending_tool_calls": run["pending_tool_calls"],
            "approval": approval,
        }
    deadline = run.get("deadline_at")
    return {
        "run_id": run["run_id"],
        "thread_id": run["thread_id"],
        "status": run["status"],
        "step_count": run["step_count"],
        "total_cost_usd": round(float(run["total_cost_usd"]), 6),
        "active_skills": run["active_skills"],
        "pending_tool_calls": run["pending_tool_calls"],
        "result": run["result_text"],
        "state_version": int(run["state_version"]),
        "event_sequence": int(run["event_sequence"]),
        "deadline_at": deadline.isoformat() if deadline is not None else None,
        "parent_run_id": run.get("parent_run_id"),
        "traceparent": run.get("traceparent"),
        "approval": approval,
        "budget": await store.run_budget(run),
    }


@app.get(
    "/v1/approvals",
    dependencies=[_require_any_action((Action.RUN_APPROVAL_READ, Action.RUN_READ))],
)
async def list_approvals(
    x_tenant_id: str = Header(default="default-tenant"),
    status: Literal[
        "pending", "approved", "rejected", "expired", "cancelled", "all"
    ] = Query(default="pending"),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    run_id: str | None = Query(default=None, max_length=255),
) -> dict[str, Any]:
    """Approval inbox: batches with digest, expiry, tool calls and any recorded decision."""
    before = _decode_cursor(cursor) if cursor else None
    if before:
        try:
            UUID(before[1])
        except ValueError as exc:
            raise HTTPException(422, "Invalid approval cursor.") from exc

    def key(row: dict[str, Any]) -> ListKey:
        return _epoch(row["created_at"]), str(row["batch_id"])

    # The inbox query has no keyset parameter: read a newest-first window, trust only rows
    # strictly newer than the window's oldest timestamp (ties may continue past it), and
    # widen the window until the page is full, the table is exhausted or the cap is hit.
    window_cap = 10_000
    window = limit + 2 if before is None and run_id is None else max(4 * limit, 200)
    while True:
        rows = await store.list_approval_batches(
            x_tenant_id, status=None if status == "all" else status, limit=window
        )
        full = len(rows) >= window
        ordered = sorted(rows, key=key, reverse=True)
        if full and ordered:
            floor = key(ordered[-1])[0]
            ordered = [row for row in ordered if key(row)[0] > floor]
        matches = [
            row
            for row in ordered
            if (before is None or key(row) < before)
            and (run_id is None or row["run_id"] == run_id)
        ]
        if len(matches) > limit or not full or window >= window_cap:
            break
        window = min(window * 4, window_cap)
    items = matches[:limit]
    if len(matches) > limit:
        next_key: ListKey | None = key(items[-1])
    elif full and ordered and (before is None or key(ordered[-1]) < before):
        next_key = key(ordered[-1])
    else:
        next_key = None
    return {
        "items": items,
        "next_cursor": _encode_cursor(*next_key) if next_key else None,
    }


@app.get(
    "/v1/approvals/{batch_id}",
    dependencies=[_require_any_action((Action.RUN_APPROVAL_READ, Action.RUN_READ))],
)
async def get_approval(
    batch_id: UUID,
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    batch = await store.get_approval_batch(x_tenant_id, str(batch_id))
    if batch is None:
        raise HTTPException(404, "Approval batch not found.")
    return batch


@app.post(
    "/v1/approvals/{batch_id}/decision",
    status_code=202,
    dependencies=[_require_action(Action.RUN_APPROVE)],
)
async def decide_approval(
    batch_id: UUID,
    req: ApprovalDecisionRequest,
    request: Request,
    response: Response,
    x_tenant_id: str = Header(default="default-tenant"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """Digest-bound decision; stale, expired or already-decided batches return 409."""
    batch = await store.get_approval_batch(x_tenant_id, str(batch_id))
    if batch is None:
        raise HTTPException(404, "Approval batch not found.")
    enforce_approval_separation_of_duties(
        get_authenticated_identity(request),
        batch.get("submitted_by_principal_id"),
    )
    decision = await _decide(
        request,
        response,
        x_tenant_id,
        run_id=batch["run_id"],
        batch_id=str(batch_id),
        batch_digest=req.batch_digest,
        approved=req.decision == "approve",
        reason=req.reason,
        secrets=req.secrets if batch["status"] == "pending" else {},
        idempotency_key=idempotency_key,
        require_binding=True,
    )
    return {**decision, "status": "queued"}


@app.get(
    "/v1/plugins",
    dependencies=[_require_action(Action.PLUGIN_CATALOG_READ)],
)
async def get_plugins(
    request: Request,
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    identity = get_authenticated_identity(request)
    log.info(f"check identity {identity}")
    if identity is not None:
        policy = await _get_tenant_policy(x_tenant_id)
        log.info(f"check policy {policy}")
        plugin_access = policy.resolve_plugin_access(registry)
        tenant_list: Collection[str] | None = plugin_access.skill_names
        plugin_ids: set[str] | frozenset[str] = plugin_access.plugin_ids
        tool_names: set[str] | frozenset[str] = plugin_access.tool_names
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "plugins": sorted(
                        (plugin_id, registry.plugins[plugin_id].digest)
                        for plugin_id in plugin_access.plugin_ids
                    ),
                    "skills": sorted(plugin_access.skill_names),
                    "tools": sorted(plugin_access.tool_names),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    else:
        tenant_list = settings.tenant_skill_allowlist(x_tenant_id)
        plugin_ids = set(registry.plugins)
        tool_names = set(registry.tools)
        fingerprint = registry.fingerprint
    return {
        "fingerprint": fingerprint,
        "plugins": [
            {
                "id": record.id,
                "status": record.status,
                "version": record.manifest.version if record.manifest else None,
                "category": record.manifest.category if record.manifest else None,
                "skills": [name for name in record.skills if name in tenant_list]
                if tenant_list is not None
                else record.skills,
                "tools": [name for name in record.tools if name in tool_names],
                "error": record.error or None,
            }
            for record in sorted(registry.plugins.values(), key=lambda item: item.id)
            if record.id in plugin_ids
        ],
        "skills": [
            {
                "name": skill.name,
                "version": skill.version,
                "description": skill.description,
                "required_tools": skill.required_tools,
                "allowed_secrets": skill.allowed_secrets,
                "references": skill.available_references,
                "plugin": skill.plugin_id,
                "category": skill.category,
                "hidden": skill.hidden,
                "ready": skill.ready,
            }
            for skill in sorted(registry.skills.values(), key=lambda item: item.name)
            if tenant_list is None or skill.name in tenant_list
        ],
        "tools": [
            {
                "name": tool.name,
                "queue": tool.queue,
                "requires_approval": tool.requires_approval,
            }
            for tool in sorted(registry.tools.values(), key=lambda item: item.name)
            if tool.name in tool_names
        ],
    }


@app.get("/v1/me")
async def get_current_identity(request: Request) -> dict[str, Any]:
    identity = get_authenticated_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="Authenticated identity required.")
    return {
        "tenant_id": identity.tenant_id,
        "principal_id": identity.principal_id,
        "roles": sorted(identity.roles),
        "credential_type": identity.credential_type,
        "environment": settings.haas_profile,
        "capabilities": identity_capabilities(identity.roles),
    }


def identity_capabilities(roles: Collection[str]) -> dict[str, Any]:
    return {
        "version": "1",
        "actions": sorted(
            action.value for action in Action if is_authorized(roles, action)
        ),
        "event_schemas": [
            "run-events.v1",
            *(["run-output-delta.v1"] if settings.output_delta_enabled else []),
        ],
        "features": [
            "agent-revisions.v1",
            "thread-history.v1",
            "thread-branches.v1",
            "approval-batches.v1",
            "run-inspector.v1",
            "openai-compat.v1",
            *([OUTPUT_DELTA_FEATURE] if settings.output_delta_enabled else []),
        ],
        "durable_replay": True,
    }


@app.get("/v1/capabilities")
async def get_capabilities(request: Request) -> dict[str, Any]:
    identity = get_authenticated_identity(request)
    if identity is None:
        raise HTTPException(401, "Authenticated identity required.")
    return identity_capabilities(identity.roles)


@app.get(
    "/v1/admin/memberships",
    dependencies=[Depends(require_action(Action.TENANT_ADMIN))],
)
async def list_admin_memberships(
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    return {"items": await store.list_tenant_memberships(x_tenant_id)}


@app.post(
    "/v1/admin/memberships",
    status_code=201,
    dependencies=[Depends(require_action(Action.TENANT_ADMIN))],
)
async def create_admin_membership(
    req: CreateMembershipRequest,
    request: Request,
    response: Response,
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    identity = _require_actor(request)
    if req.kind == "human":
        if not settings.oidc_issuer:
            raise HTTPException(
                status_code=503,
                detail="Human memberships require a configured OIDC issuer.",
            )
        issuer = settings.oidc_issuer
    else:
        issuer = f"urn:haas:service:{x_tenant_id}"
    request_id = str(uuid.uuid4())
    response.headers["X-Request-ID"] = request_id
    try:
        return await store.grant_tenant_membership(
            tenant_selector=x_tenant_id,
            issuer=issuer,
            subject=req.subject,
            kind=req.kind,
            role=req.role,
            actor_principal_id=identity.principal_id,
            request_id=request_id,
        )
    except StoreNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StoreConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete(
    "/v1/admin/memberships/{principal_id}/{role}",
    dependencies=[Depends(require_action(Action.TENANT_ADMIN))],
)
async def revoke_admin_membership(
    principal_id: UUID,
    role: TenantRole,
    request: Request,
    response: Response,
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, str]:
    identity = _require_actor(request)
    request_id = str(uuid.uuid4())
    response.headers["X-Request-ID"] = request_id
    try:
        await store.revoke_tenant_membership(
            tenant_selector=x_tenant_id,
            principal_id=str(principal_id),
            role=role,
            actor_principal_id=identity.principal_id,
            request_id=request_id,
        )
    except StoreNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StoreConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"principal_id": str(principal_id), "role": role, "status": "revoked"}


@app.get(
    "/v1/admin/api-keys",
    dependencies=[Depends(require_action(Action.TENANT_ADMIN))],
)
async def list_admin_api_keys(
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    return {"items": await store.list_tenant_api_keys(x_tenant_id)}


@app.post(
    "/v1/admin/api-keys",
    status_code=201,
    dependencies=[Depends(require_action(Action.TENANT_ADMIN))],
)
async def create_admin_api_key(
    req: CreateTenantAPIKeyRequest,
    request: Request,
    response: Response,
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    identity = _require_actor(request)
    prefix = "hk_live_" if settings.haas_profile == "prod" else "hk_test_"
    credential = prefix + secrets.token_urlsafe(32)
    request_id = str(uuid.uuid4())
    response.headers["X-Request-ID"] = request_id
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        key = await store.create_tenant_api_key(
            tenant_selector=x_tenant_id,
            principal_id=str(req.principal_id),
            credential=credential,
            name=req.name,
            expires_in_days=req.expires_in_days,
            actor_principal_id=identity.principal_id,
            request_id=request_id,
        )
    except StoreNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StoreConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"credential": credential, **key}


@app.delete(
    "/v1/admin/api-keys/{key_id}",
    dependencies=[Depends(require_action(Action.TENANT_ADMIN))],
)
async def revoke_admin_api_key(
    key_id: UUID,
    request: Request,
    response: Response,
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, str]:
    identity = _require_actor(request)
    request_id = str(uuid.uuid4())
    response.headers["X-Request-ID"] = request_id
    try:
        await store.revoke_tenant_api_key(
            tenant_selector=x_tenant_id,
            key_id=str(key_id),
            actor_principal_id=identity.principal_id,
            request_id=request_id,
        )
    except StoreNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"key_id": str(key_id), "status": "revoked"}


@app.get(
    "/v1/admin/policy",
    dependencies=[Depends(require_action(Action.TENANT_ADMIN))],
)
async def get_admin_policy(
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    record = await store.get_active_tenant_policy(x_tenant_id)
    if record is None:
        return {"active": None}
    try:
        policy = TenantPolicy.from_record(record)
    except TenantPolicyIntegrityError as exc:
        log.error("Invalid active policy for tenant %s: %s", x_tenant_id, exc)
        raise HTTPException(
            status_code=503,
            detail="Active tenant policy is invalid; contact an administrator.",
        ) from exc
    return {
        "active": {
            "policy_revision_id": policy.policy_revision_id,
            "version": policy.version,
            "digest": policy.digest,
            "rules": policy.rules.model_dump(mode="json", exclude_none=True),
        }
    }


@app.put(
    "/v1/admin/policy",
    status_code=201,
    dependencies=[Depends(require_action(Action.TENANT_ADMIN))],
)
async def activate_admin_policy(
    req: CreateTenantPolicyRequest,
    request: Request,
    response: Response,
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    identity = _require_actor(request)
    unsupported_providers = set(req.rules.model_providers) - {"openrouter"}
    if unsupported_providers:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported model providers: {sorted(unsupported_providers)}",
        )
    for grant in req.rules.plugin_grants:
        plugin = registry.plugins.get(grant.plugin_id)
        if plugin is None or plugin.status != "loaded" or plugin.digest != grant.digest:
            raise HTTPException(
                status_code=422,
                detail=f"Plugin grant does not match a loaded revision: {grant.plugin_id}",
            )
        known_skills = {
            name
            for name, skill in registry.skills.items()
            if skill.plugin_id == grant.plugin_id
        }
        unknown_skills = sorted(set(grant.skills) - known_skills)
        if unknown_skills:
            raise HTTPException(
                status_code=422,
                detail=f"Plugin grant contains unknown skills: {unknown_skills}",
            )

    rules = req.rules.model_dump(mode="json", exclude_none=True)
    request_id = str(uuid.uuid4())
    response.headers["X-Request-ID"] = request_id
    try:
        return await store.activate_tenant_policy_revision(
            tenant_selector=x_tenant_id,
            rules=rules,
            digest=policy_digest(req.rules),
            actor_principal_id=identity.principal_id,
            request_id=request_id,
        )
    except StoreNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(
    "/v1/audit-events",
    dependencies=[Depends(require_action(Action.AUDIT_READ))],
)
async def list_audit_events(
    x_tenant_id: str = Header(default="default-tenant"),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
) -> dict[str, Any]:
    before = _decode_audit_cursor(cursor) if cursor else None
    rows = await store.list_audit_events(x_tenant_id, limit=limit + 1, before=before)
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = (
        _encode_audit_cursor(items[-1]["created_at"], items[-1]["event_id"])
        if has_more and items
        else None
    )
    return {"items": items, "next_cursor": next_cursor}


# ---- OpenAI-compatible facade (openai-compat.v1) ----------------------------------------

SETTLED_STATUSES = (*TERMINAL_STATUSES, "input_required")


async def _await_settled(run_id: str, timeout: float) -> dict[str, Any] | None:
    """Waits for a terminal or approval-paused state; Redis wakeups only cut latency."""
    deadline = time.monotonic() + max(timeout, 0)
    wake_key, wake_cursor = f"haas:wake:{run_id}", "$"
    while True:
        run = await store.get_run(run_id)
        remaining = deadline - time.monotonic()
        if run is None or run["status"] in SETTLED_STATUSES or remaining <= 0:
            return run
        try:
            entries = await store.rds.xread(
                {wake_key: wake_cursor}, count=10, block=int(min(remaining, 2) * 1000)
            )
            if entries and entries[0][1]:
                wake_cursor = entries[0][1][-1][0]
        except RedisError:
            await asyncio.sleep(min(remaining, 0.5))


async def _facade_submit(
    req: oai.ChatCompletionRequest | oai.ResponsesRequest,
    prompt: str,
    request: Request,
    response: Response,
    *,
    tenant: str,
    traceparent: str | None,
    idempotency_key: str | None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    target = oai.parse_model(req.model)
    fields = oai.run_overrides(req, target)
    if thread_id is not None:
        if fields.get("thread_id") not in (None, thread_id):
            raise oai.facade_error(
                400,
                "previous_response_id and haas.thread_id select different threads.",
                param="previous_response_id",
            )
        fields["thread_id"] = thread_id
    if target.kind == "agent":
        assert target.value is not None
        revision_id = await store.resolve_published_agent(
            tenant, target.value, target.version
        )
        if revision_id is None:
            raise oai.facade_error(
                404,
                f"Model {req.model!r} is not a published agent in this tenant.",
                code="model_not_found",
                param="model",
            )
        fields["agent_revision_id"] = revision_id
    elif target.kind == "revision":
        fields["agent_revision_id"] = target.value
    try:
        run_request = CreateRunRequest(prompt=prompt, **fields)
    except ValidationError as exc:
        error = exc.errors()[0]
        loc = ".".join(str(part) for part in error.get("loc", ()))
        param = "messages" if loc == "prompt" else f"haas.{loc}" if loc else None
        raise oai.facade_error(
            400, f"{param or 'request'}: {error.get('msg')}", param=param
        ) from exc
    # Same admission path as POST /v1/runs: policy, budgets, pins and idempotency (SEC-24).
    return await create_run(
        run_request,
        request,
        response,
        x_tenant_id=tenant,
        traceparent=traceparent,
        idempotency_key=idempotency_key,
    )


def _facade_headers(created: dict[str, Any], response: Response) -> dict[str, str]:
    headers = {
        "X-HaaS-Run-Id": created["run_id"],
        "X-HaaS-Thread-Id": created["thread_id"],
    }
    if response.headers.get("Idempotent-Replayed"):
        headers["Idempotent-Replayed"] = "true"
    return headers


async def _usage(run_id: str) -> tuple[int, int]:
    return oai.usage_totals((await store.run_usage(run_id))["entries"])


async def _approval_info(run: dict[str, Any]) -> dict[str, Any] | None:
    pending = await store.pending_approval_batch(run["run_id"])
    if pending is None:
        return None
    return {
        **pending,
        "decision_endpoint": f"/v1/approvals/{pending['batch_id']}/decision",
    }


async def _unsettled_error(run: dict[str, Any] | None, run_id: str) -> HTTPException:
    """Maps a non-completed run to an OpenAI-style error that SDKs must not retry."""
    if run is None:
        return oai.facade_error(404, "Run not found.", code="run_not_found")
    meta = oai.haas_metadata(run)
    status = run["status"]
    if status == "input_required":
        meta["approval"] = await _approval_info(run)
        return oai.facade_error(
            409,
            "The run is waiting for human approval. Decide it through the approvals "
            f"API, then read the result from GET /v1/responses/{run_id}.",
            code="approval_required",
            haas=meta,
            retry=False,
        )
    if status == "cancelled":
        return oai.facade_error(
            409, "The run was cancelled.", code="run_cancelled", haas=meta, retry=False
        )
    if status == "failed":
        code = run_error_code(run.get("result_text") or "")
        return oai.facade_error(
            422 if code.startswith(("budget.", "approval.")) else 502,
            run.get("result_text") or "The run failed.",
            code=code,
            haas=meta,
            retry=False,
        )
    return oai.facade_error(
        504,
        "The run is still executing. Poll GET /v1/responses/"
        f"{run_id} or stream /v1/runs/{run_id}/stream for the result.",
        code="run_timeout",
        haas=meta,
        retry=False,
    )


def _sse_error(exc: HTTPException) -> str:
    return f"data: {json.dumps(oai.openai_error_body(exc.status_code, exc.detail))}\n\n"


async def _settle_with_keepalive(
    run_id: str,
) -> AsyncGenerator[dict[str, Any] | None, None]:
    """Yields None on each heartbeat while waiting, then the settled (or timed-out) run."""
    deadline = time.monotonic() + settings.openai_facade_wait_sec
    while True:
        remaining = deadline - time.monotonic()
        run = await _await_settled(
            run_id, max(min(remaining, settings.sse_heartbeat_sec), 0)
        )
        if (
            run is None
            or run["status"] in SETTLED_STATUSES
            or time.monotonic() >= deadline
        ):
            yield run
            return
        yield None


@app.post(
    "/v1/chat/completions",
    dependencies=[_require_action(Action.RUN_SUBMIT)],
    tags=["openai-compat"],
)
async def openai_chat_completions(
    req: oai.ChatCompletionRequest,
    request: Request,
    response: Response,
    x_tenant_id: str = Header(default="default-tenant"),
    traceparent: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """OpenAI Chat Completions over a HaaS run (text only, n=1, server-side tools)."""
    oai.reject_unsupported(req)
    created = await _facade_submit(
        req,
        oai.chat_prompt(req),
        request,
        response,
        tenant=x_tenant_id,
        traceparent=traceparent,
        idempotency_key=idempotency_key,
    )
    run_id, ts = created["run_id"], oai.now()
    headers = _facade_headers(created, response)
    if req.stream:
        include_usage = bool((req.stream_options or {}).get("include_usage"))

        async def chunks() -> AsyncGenerator[str, None]:
            yield oai.chat_chunk(
                run_id, req.model, ts, {"role": "assistant", "content": ""}
            )
            run: dict[str, Any] | None = None
            async for run in _settle_with_keepalive(run_id):
                if run is None:
                    yield ": keep-alive\n\n"
            if run is None or run["status"] != "completed":
                yield _sse_error(await _unsettled_error(run, run_id))
                yield "data: [DONE]\n\n"
                return
            text = run.get("result_text") or ""
            for start in range(0, len(text), 4096):
                yield oai.chat_chunk(
                    run_id, req.model, ts, {"content": text[start : start + 4096]}
                )
            yield oai.chat_chunk(run_id, req.model, ts, {}, finish_reason="stop")
            if include_usage:
                yield oai.chat_chunk(
                    run_id, req.model, ts, None, usage=await _usage(run_id)
                )
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            chunks(),
            media_type="text/event-stream",
            headers={**headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    run = await _await_settled(run_id, settings.openai_facade_wait_sec)
    if run is None or run["status"] != "completed":
        exc = await _unsettled_error(run, run_id)
        exc.headers = {**(exc.headers or {}), **headers}
        raise exc
    return JSONResponse(
        oai.chat_completion_body(run, req.model, await _usage(run_id), ts),
        headers=headers,
    )


async def _response_view(
    run: dict[str, Any],
    model: str,
    *,
    created: int,
    previous_response_id: str | None = None,
) -> dict[str, Any]:
    return oai.response_body(
        run,
        model,
        await _usage(run["run_id"]),
        created=created,
        previous_response_id=previous_response_id,
        error_code=run_error_code(run.get("result_text") or "")
        if run["status"] == "failed"
        else None,
        approval=await _approval_info(run)
        if run["status"] == "input_required"
        else None,
    )


@app.post(
    "/v1/responses",
    dependencies=[_require_action(Action.RUN_SUBMIT)],
    tags=["openai-compat"],
)
async def openai_create_response(
    req: oai.ResponsesRequest,
    request: Request,
    response: Response,
    x_tenant_id: str = Header(default="default-tenant"),
    traceparent: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """OpenAI Responses over a HaaS run. The response id is the run id; chaining with
    `previous_response_id` continues that run's thread. Approval pauses return
    `status: incomplete` with `incomplete_details.reason = approval_required`."""
    oai.reject_unsupported(req)
    thread_id = None
    if req.previous_response_id is not None:
        try:
            previous = await _get_owned_run(req.previous_response_id, x_tenant_id)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            raise oai.facade_error(
                404,
                f"Previous response {req.previous_response_id!r} not found.",
                code="previous_response_not_found",
                param="previous_response_id",
            ) from exc
        thread_id = previous["thread_id"]
    created = await _facade_submit(
        req,
        oai.responses_prompt(req),
        request,
        response,
        tenant=x_tenant_id,
        traceparent=traceparent,
        idempotency_key=idempotency_key,
        thread_id=thread_id,
    )
    run_id, ts = created["run_id"], oai.now()
    headers = _facade_headers(created, response)
    view_args = {"created": ts, "previous_response_id": req.previous_response_id}
    if req.stream and not req.background:

        async def events() -> AsyncGenerator[str, None]:
            seq = 0
            first = await store.get_run(run_id)
            if first is not None:
                body = await _response_view(first, req.model, **view_args)
                yield oai.response_event("response.created", seq, response=body)
            run: dict[str, Any] | None = None
            async for run in _settle_with_keepalive(run_id):
                if run is None:
                    yield ": keep-alive\n\n"
            if run is None:
                yield _sse_error(await _unsettled_error(None, run_id))
                return
            body = await _response_view(run, req.model, **view_args)
            if run["status"] == "completed":
                item = f"msg_{run_id}"
                for start in range(0, len(body["output_text"]), 4096):
                    seq += 1
                    yield oai.response_event(
                        "response.output_text.delta",
                        seq,
                        item_id=item,
                        output_index=0,
                        content_index=0,
                        delta=body["output_text"][start : start + 4096],
                    )
                seq += 1
                yield oai.response_event(
                    "response.output_text.done",
                    seq,
                    item_id=item,
                    output_index=0,
                    content_index=0,
                    text=body["output_text"],
                )
            kind = {
                "completed": "response.completed",
                "failed": "response.failed",
                "incomplete": "response.incomplete",
            }.get(body["status"], "response.in_progress")
            yield oai.response_event(kind, seq + 1, response=body)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={**headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    run = (
        await store.get_run(run_id)
        if req.background
        else await _await_settled(run_id, settings.openai_facade_wait_sec)
    )
    if run is None:
        raise await _unsettled_error(None, run_id)
    return JSONResponse(
        await _response_view(run, req.model, **view_args), headers=headers
    )


@app.get(
    "/v1/responses/{response_id}",
    dependencies=[_require_action(Action.RUN_READ)],
    tags=["openai-compat"],
)
async def openai_get_response(
    response_id: str,
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    run = await _get_owned_run(response_id, x_tenant_id)
    model = (
        f"{run['agent_revision_id']}"
        if run.get("agent_revision_id")
        else run.get("primary_model") or oai.DEFAULT_MODEL_ID
    )
    created = run.get("created_at")  # epoch seconds (DOUBLE PRECISION)
    return await _response_view(
        run, str(model), created=int(created) if created is not None else oai.now()
    )


@app.get(
    "/v1/models",
    dependencies=[_require_action(Action.RUN_READ)],
    tags=["openai-compat"],
)
async def openai_list_models(
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    """`haas` (tenant defaults) plus every published agent as `agent/<name>[@<version>]`."""
    data: list[dict[str, Any]] = [
        {
            "id": oai.DEFAULT_MODEL_ID,
            "object": "model",
            "created": 0,
            "owned_by": "haas",
        }
    ]
    seen: set[str] = set()
    for row in await store.list_published_agents(x_tenant_id):
        created = int(row.get("published_at") or 0)
        if row["agent_name"] not in seen:
            seen.add(row["agent_name"])
            data.append(
                {
                    "id": f"agent/{row['agent_name']}",
                    "object": "model",
                    "created": created,
                    "owned_by": "tenant",
                    "haas": {
                        "revision_id": row["revision_id"],
                        "version": row["version"],
                    },
                }
            )
        data.append(
            {
                "id": f"agent/{row['agent_name']}@{row['version']}",
                "object": "model",
                "created": created,
                "owned_by": "tenant",
                "haas": {"revision_id": row["revision_id"], "version": row["version"]},
            }
        )
    return {"object": "list", "data": data}


# Feature routers (one module per roadmap area); imported last so they can use app.main.store.
from app.api import (  # noqa: E402
    access as _access_api,
    agent_mesh as _agent_mesh_api,
    artifacts as _artifacts_api,
    isolation as _isolation_api,
    marketplace as _marketplace_api,
    operations as _operations_api,
)

for _router_module in (
    _isolation_api,
    _access_api,
    _artifacts_api,
    _operations_api,
    _marketplace_api,
    _agent_mesh_api,
):
    app.include_router(_router_module.router)
