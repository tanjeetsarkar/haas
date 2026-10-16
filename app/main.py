from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from celery import chord, group
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.context import SecurityViolationError, validate_safe_outbound_url
from app.core.plugins import registry, resolve_run_allowlist
from app.core.store import DistributedStore, ThreadOwnershipError
from app.worker.tasks import aggregate_step_task, execute_tool_task, llm_step_task

store = DistributedStore()

TERMINAL_STATUSES = ("completed", "failed", "cancelled", "input_required")
TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


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
    settings.plugins_dir.mkdir(parents=True, exist_ok=True)
    registry.discover_from_settings(settings)
    await store.init(ensure_schema=True)
    yield
    await store.close()


app = FastAPI(title=settings.app_name, version="3.0.0", lifespan=lifespan)


class CreateRunRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    thread_id: str | None = None
    preload_skills: list[str] = Field(default_factory=list)
    # Optional allowlist for this run; can only narrow the tenant's allowed skills.
    skills: list[str] | None = None
    primary_model: str | None = None
    planner_model: str | None = None
    max_steps: int = Field(default=settings.default_max_steps, ge=1, le=50)
    max_cost_usd: float = Field(default=settings.default_max_cost_usd, gt=0.0, le=10.0)
    webhook_url: str | None = None
    secrets: dict[str, str] = Field(default_factory=dict)


class ResumeRunRequest(BaseModel):
    approved: bool
    rejection_reason: str | None = None
    secrets: dict[str, str] = Field(default_factory=dict)


async def _get_owned_run(run_id: str, tenant_id: str) -> dict[str, Any]:
    run = await store.get_run(run_id)
    # 404 (not 403) for other tenants' runs so run ids are not discoverable.
    if not run or run["tenant_id"] != tenant_id:
        raise HTTPException(status_code=404, detail="Run not found.")
    return run


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    async with store.pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    await store.rds.ping()
    return {"status": "ok"}


@app.post("/v1/runs", status_code=202)
async def create_run(
    req: CreateRunRequest,
    x_tenant_id: str = Header(default="default-tenant"),
    traceparent: str | None = Header(default=None),
) -> dict[str, Any]:
    try:
        allowlist = resolve_run_allowlist(
            settings.tenant_skill_allowlist(x_tenant_id), req.skills
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    usable = registry.effective_skills(registry.skills, allowlist)
    if req.skills is not None:
        unknown = sorted(set(req.skills) - set(registry.skills))
        if unknown:
            raise HTTPException(status_code=422, detail=f"Unknown skills: {unknown}")
    unknown = [s for s in req.preload_skills if s not in usable]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown, unavailable or not permitted preload_skills: {unknown}",
        )
    if req.webhook_url:
        try:
            validate_safe_outbound_url(req.webhook_url)
        except SecurityViolationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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

    await store.store_ephemeral_secrets(run_id, req.secrets)
    await store.create_run(
        run_id=run_id,
        thread_id=thread_id,
        tenant_id=x_tenant_id,
        traceparent=trace_id,
        primary_model=req.primary_model or settings.default_primary_model,
        planner_model=req.planner_model or settings.default_planner_model,
        initial_skills=req.preload_skills,
        initial_messages=history,
        max_steps=req.max_steps,
        max_cost_usd=req.max_cost_usd,
        webhook_url=req.webhook_url,
        skill_allowlist=allowlist,
        registry_fingerprint=registry.fingerprint,
    )
    await store.publish_event(
        run_id, "run_queued", {"run_id": run_id, "thread_id": thread_id}
    )

    # Dispatch step 1 to the orchestrator queue
    llm_step_task.apply_async(args=[run_id], queue="q_orchestrator")

    return {
        "run_id": run_id,
        "thread_id": thread_id,
        "status": "running",
        "stream_url": f"/v1/runs/{run_id}/stream",
    }


@app.post("/v1/runs/{run_id}/resume", status_code=202)
async def resume_run(
    run_id: str,
    req: ResumeRunRequest,
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    """
    Resumes a paused `input_required` run. The `input_required -> running` transition is a
    single conditional UPDATE, so two operators can never double-approve the same tool batch.
    """
    run = await _get_owned_run(run_id, x_tenant_id)
    if run["status"] != "input_required":
        raise HTTPException(
            status_code=409,
            detail=f"Run status is '{run['status']}', not 'input_required'.",
        )
    if not await store.claim_resume(run_id, x_tenant_id):
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
        llm_step_task.apply_async(args=[run_id], queue="q_orchestrator")
        return {"run_id": run_id, "status": "running", "action": "rejected_and_resumed"}

    # Operator approved: fan out the pending tools via Celery Canvas `chord`
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
            execute_tool_task.s(
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
    callback = aggregate_step_task.s(run_id=run_id).set(queue="q_orchestrator")
    chord(group(sigs))(callback)

    await store.publish_event(run_id, "hitl_approved", {"dispatched_tools": len(sigs)})
    return {"run_id": run_id, "status": "running", "action": "approved_and_dispatched"}


@app.get("/v1/runs/{run_id}/stream")
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


@app.get("/v1/runs/{run_id}")
async def get_run(
    run_id: str, x_tenant_id: str = Header(default="default-tenant")
) -> dict[str, Any]:
    run = await _get_owned_run(run_id, x_tenant_id)
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


@app.get("/v1/plugins")
async def get_plugins(
    x_tenant_id: str = Header(default="default-tenant"),
) -> dict[str, Any]:
    tenant_list = settings.tenant_skill_allowlist(x_tenant_id)
    return {
        "fingerprint": registry.fingerprint,
        "plugins": [
            {
                "id": r.id,
                "status": r.status,
                "version": r.manifest.version if r.manifest else None,
                "category": r.manifest.category if r.manifest else None,
                "skills": r.skills,
                "tools": r.tools,
                "error": r.error or None,
            }
            for r in sorted(registry.plugins.values(), key=lambda x: x.id)
        ],
        "skills": [
            {
                "name": s.name,
                "version": s.version,
                "description": s.description,
                "required_tools": s.required_tools,
                "allowed_secrets": s.allowed_secrets,
                "references": s.available_references,
                "plugin": s.plugin_id,
                "category": s.category,
                "hidden": s.hidden,
                "ready": s.ready,
            }
            for s in sorted(registry.skills.values(), key=lambda x: x.name)
            if tenant_list is None or s.name in tenant_list
        ],
        "tools": [
            {"name": t.name, "queue": t.queue, "requires_approval": t.requires_approval}
            for t in sorted(registry.tools.values(), key=lambda x: x.name)
        ],
    }
