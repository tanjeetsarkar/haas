from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import re
import secrets
import uuid
from collections.abc import Collection
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Literal, cast
from uuid import UUID

import asyncpg
from celery import Task, chord, group
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
from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import RedisError

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
from app.core.plugins import registry, resolve_run_allowlist
from app.core.store import (
    DistributedStore,
    StoreConflictError,
    StoreNotFoundError,
    ThreadOwnershipError,
)
from app.core.tenant_policy import (
    TenantPolicyRules,
    TenantPolicy,
    TenantPolicyIntegrityError,
    policy_digest,
)
from app.worker.dispatch import signature_for
from app.worker.tasks import aggregate_step_task, execute_tool_task, llm_step_task

log = logging.getLogger(__name__)
store = DistributedStore()
identity_authenticator = IdentityAuthenticator(store, settings)

TERMINAL_STATUSES = ("completed", "failed", "cancelled", "input_required")
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


app = FastAPI(title=settings.app_name, version="3.0.0", lifespan=lifespan)
configure_observability(app)
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


class ResumeRunRequest(BaseModel):
    approved: bool
    rejection_reason: str | None = None
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


@app.get("/v1/threads", dependencies=[_require_action(Action.RUN_READ)])
async def list_threads(
    x_tenant_id: str = Header(default="default-tenant"),
    limit: int = Query(default=20, ge=1, le=100),
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
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
) -> dict[str, Any]:
    before = _decode_cursor(cursor) if cursor else None
    if before:
        try:
            UUID(before[1])
        except ValueError as exc:
            raise HTTPException(422, "Invalid revision cursor.") from exc
    rows = await store.list_agent_revisions(x_tenant_id, limit=limit + 1, before=before)
    items = rows[:limit]
    return {
        "items": items,
        "next_cursor": (
            _encode_cursor(items[-1]["created_at"], items[-1]["revision_id"])
            if len(rows) > limit
            else None
        ),
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
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
) -> dict[str, Any]:
    before = _decode_cursor(cursor) if cursor else None
    rows = await store.list_runs(x_tenant_id, limit=limit + 1, before=before)
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = (
        _encode_cursor(float(items[-1]["created_at"]), items[-1]["run_id"])
        if has_more and items
        else None
    )
    return {"items": items, "next_cursor": next_cursor}


@app.post(
    "/v1/runs",
    status_code=202,
    dependencies=[_require_action(Action.RUN_SUBMIT)],
)
async def create_run(
    req: CreateRunRequest,
    request: Request,
    x_tenant_id: str = Header(default="default-tenant"),
    traceparent: str | None = Header(default=None),
) -> dict[str, Any]:
    identity = get_authenticated_identity(request)
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
        )
    except StoreConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ThreadOwnershipError as exc:
        raise HTTPException(404, "Thread not found.") from exc
    await store.store_ephemeral_secrets(run_id, req.secrets)
    await store.publish_event(
        run_id, "run_queued", {"run_id": run_id, "thread_id": thread_id}
    )

    # Dispatch step 1 to the orchestrator queue
    cast(Task, llm_step_task).apply_async(args=[run_id], queue="q_orchestrator")

    return {
        "run_id": run_id,
        "thread_id": thread_id,
        "status": "running",
        "stream_url": f"/v1/runs/{run_id}/stream",
    }


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
    if not await store.cancel_run(run_id, x_tenant_id):
        raise HTTPException(status_code=409, detail="Run is already terminal.")
    await store.publish_event(run_id, "run_cancelled", {"run_id": run_id})
    return {"run_id": run_id, "status": "cancelled"}


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
) -> dict[str, Any]:
    """
    Resumes a paused `input_required` run. The `input_required -> running` transition is a
    single conditional UPDATE, so two approvers can never double-decide the same batch.
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
    request_id = str(uuid.uuid4())
    response.headers["X-Request-ID"] = request_id
    if not await store.claim_resume(
        run_id,
        x_tenant_id,
        actor_principal_id=identity.principal_id if identity else None,
        request_id=request_id,
        approved=req.approved,
        pending_tool_count=len(run["pending_tool_calls"] or []),
    ):
        raise HTTPException(
            status_code=409, detail="Resume already in progress for this run."
        )

    if req.secrets:
        await store.store_ephemeral_secrets(run_id, req.secrets)

    pending_calls: list[dict[str, Any]] = run["pending_tool_calls"] or []
    active_skills = set(run["active_skills"])
    messages = run["messages"]

    if not req.approved:
        reason = req.rejection_reason or "Rejected by human operator."
        for tc in pending_calls:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": tc["function"]["name"],
                    "content": f"Action Denied by Human Operator: {reason}",
                }
            )
        await store.save_checkpoint(
            run_id,
            step_count=int(run["step_count"]),
            messages=messages,
            active_skills=active_skills,
            fingerprints=run["fingerprints"],
            last_step_had_error=True,
            status="running",
            pending_tool_calls=None,
        )
        await store.publish_event(run_id, "hitl_rejected", {"reason": reason})
        cast(Task, llm_step_task).apply_async(args=[run_id], queue="q_orchestrator")
        return {"run_id": run_id, "status": "running", "action": "rejected_and_resumed"}

    # Approver decision accepted: fan out the pending tools via Celery Canvas `chord`
    await store.save_checkpoint(
        run_id,
        step_count=int(run["step_count"]),
        messages=messages,
        active_skills=active_skills,
        fingerprints=run["fingerprints"],
        status="running",
        pending_tool_calls=None,
    )
    allowlist = run.get("skill_allowlist")
    allowed_keys = registry.resolve_allowed_secrets(active_skills, allowlist)
    scoped_secrets = await store.get_scoped_secrets(run_id, allowed_keys)

    sigs = []
    for tc in pending_calls:
        tool = registry.tools.get(tc["function"]["name"])
        sigs.append(
            signature_for(
                cast(Task, execute_tool_task),
                run_id=run_id,
                thread_id=run["thread_id"],
                tenant_id=run["tenant_id"],
                traceparent=run["traceparent"],
                active_skills=sorted(active_skills),
                skill_allowlist=allowlist,
                allowed_secrets=sorted(allowed_keys),
                scoped_secrets=scoped_secrets,
                tool_call=tc,
            ).set(queue=tool.queue if tool else "q_tools_fast")
        )
    callback = signature_for(cast(Task, aggregate_step_task), run_id=run_id).set(
        queue="q_orchestrator"
    )
    chord(group(sigs))(callback)

    await store.publish_event(run_id, "hitl_approved", {"dispatched_tools": len(sigs)})
    return {"run_id": run_id, "status": "running", "action": "approved_and_dispatched"}


@app.get(
    "/v1/runs/{run_id}/stream",
    dependencies=[_require_action(Action.RUN_STREAM)],
)
async def stream_run_events(
    run_id: str,
    x_tenant_id: str = Header(default="default-tenant"),
    last_event_id: str = Header(default="0-0", alias="Last-Event-ID"),
) -> StreamingResponse:
    """
    Zero-polling SSE endpoint powered by Redis Streams (`XREAD BLOCK`).
    Works across multiple FastAPI replicas and resumes from `Last-Event-ID`.
    """
    await _get_owned_run(run_id, x_tenant_id)
    stream_key = f"haas:stream:{run_id}"

    async def sse_generator() -> AsyncGenerator[str, None]:
        cursor = last_event_id if re.fullmatch(r"\d+-\d+", last_event_id) else "0-0"
        draining = False
        while True:
            entries = await store.rds.xread(
                {stream_key: cursor}, count=50, block=None if draining else 5000
            )
            if entries:
                _, stream_messages = entries[0]
                for entry_id, fields in stream_messages:
                    cursor = entry_id
                    ev_type = fields.get("event_type", "message")
                    payload = fields.get("payload_json", "{}")
                    yield f"id: {entry_id}\nevent: {ev_type}\ndata: {payload}\n\n"
                    if ev_type in ("run_completed", "run_error"):
                        return
                    if ev_type == "input_required":
                        # Replays of an already-resumed run must keep streaming past old pauses.
                        run = await store.get_run(run_id)
                        if not run or run["status"] == "input_required":
                            return
                continue

            if draining:
                return
            # Idle: once the run can no longer emit events, do one last non-blocking read
            # (status is committed just before the final event is published).
            run = await store.get_run(run_id)
            if not run or run["status"] in TERMINAL_STATUSES:
                draining = True

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
        }
    return {
        "run_id": run["run_id"],
        "thread_id": run["thread_id"],
        "status": run["status"],
        "step_count": run["step_count"],
        "total_cost_usd": round(float(run["total_cost_usd"]), 6),
        "active_skills": run["active_skills"],
        "pending_tool_calls": run["pending_tool_calls"],
        "result": run["result_text"],
    }


@app.get(
    "/v1/plugins",
    dependencies=[_require_action(Action.PLUGIN_CATALOG_READ)],
)
async def get_plugins(
    request: Request,
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    identity = get_authenticated_identity(request)
    if identity is not None:
        policy = await _get_tenant_policy(x_tenant_id)
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
        "event_schemas": ["redis-stream.v1"],
        "features": ["agent-revisions.v1", "thread-history.v1", "thread-branches.v1"],
        "durable_replay": False,
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
    limit: int = Query(default=50, ge=1, le=100),
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
