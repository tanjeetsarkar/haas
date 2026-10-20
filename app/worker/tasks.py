from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import socket
import time
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import httpx
import redis.asyncio as aioredis
from celery import Task
from pydantic import ValidationError

from app.core.config import settings
from app.core.context import (
    RunContext,
    SecurityViolationError,
    ToolRemediationError,
    validate_safe_outbound_url,
)
from app.core.credentials import (
    GrantError,
    prepare_tool_dispatch,
    redeem_grants,
)
from app.core import egress
from app.core.egress import (
    EgressPolicy,
    InMemoryEgressMeter,
)
from app.core.envelope import (
    EnvelopeError,
    arguments_digest,
    sign_envelope,
    verify_envelope,
)
from app.core.llm import CompletionResult, OpenRouterGateway, repair_tool_json
from app.core.output_stream import DeltaPublisher
from app.core.plugins import registry
from app.core.store import DistributedStore
from app.core.workflow import (
    UNKNOWN_OUTCOME,
    Lease,
    LeaseLostError,
    Publisher,
    effect_key,
)
from app.worker.celery_app import celery_app
from app.sandbox.execution import execute_in_sandbox, SandboxSetupError

log = logging.getLogger("haas.tasks")

gateway = OpenRouterGateway(settings.openrouter_api_key, settings.openrouter_base_url)

# Workers open one short-lived pool per task, so keep it small.
TASK_POOL_MAX = 3
EFFECT_TTL_SEC = 7 * 86_400


def _run_async(coro: Any) -> Any:
    """Executes an async coroutine cleanly inside a Celery worker process."""
    return asyncio.run(coro)


def _bound_observation(tool_name: str, call_id: str, raw_text: str) -> str:
    limit = settings.max_tool_output_chars
    if len(raw_text) <= limit:
        return raw_text
    half = (limit - 400) // 2
    safe_id = "".join(
        c for c in f"{tool_name}_{call_id}" if c.isalnum() or c in ("_", "-")
    )[:48]
    spill_file = settings.scratchpad_dir / f"{safe_id}.txt"
    spill_file.write_text(raw_text, encoding="utf-8")
    omitted = len(raw_text) - (half * 2)
    return (
        f"{raw_text[:half]}\n\n"
        f"... [HARNESS TRUNCATED {omitted:,} CHARS — Full output at {spill_file}] ...\n\n"
        f"{raw_text[-half:]}"
    )


def _fingerprint(name: str, raw_args: Any) -> str:
    canonical = json.dumps({"n": name, "a": raw_args}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code in (408, 409, 429) or code >= 500
    return isinstance(exc, httpx.HTTPError)


# ---------------------------------------------------------------- outbox publishing


def make_outbox_publisher(store: DistributedStore) -> Publisher:
    """Turns committed outbox rows into broker messages; every message is safe to duplicate."""

    async def publish(message: dict[str, Any]) -> None:
        message_id = str(message["message_id"])
        payload = message["payload"]
        destination = message["destination"]
        if destination == "orchestrator":
            cast(Task, orchestrate_task).apply_async(
                kwargs={
                    "message_id": message_id,
                    "run_id": payload["run_id"],
                    "action": payload["action"],
                    "expected_state_version": int(payload["expected_state_version"]),
                },
                queue="q_orchestrator",
                task_id=message_id,
            )
        elif destination == "tool":
            # The credential broker seals each allowed secret into a single-use,
            # audience-bound grant. Only opaque grant references travel over the
            # broker; raw secret values never leave Redis.
            dispatch = await prepare_tool_dispatch(store, message)
            cast(Task, execute_tool_task).apply_async(
                kwargs={
                    "envelope": payload["envelope"],
                    "credential_grants": dispatch["credential_grants"],
                    "egress_policy": dispatch["egress_policy"],
                },
                queue=payload["queue"],
                task_id=message_id,
            )
        elif destination == "webhook":
            cast(Task, deliver_webhook_task).apply_async(
                args=[payload["url"], payload["body"]],
                queue="q_orchestrator",
                task_id=message_id,
            )
        else:
            raise ValueError(f"Unknown outbox destination {destination!r}.")

    return publish


async def _open_store() -> DistributedStore:
    store = DistributedStore()
    await store.init(pool_max=TASK_POOL_MAX, service=True)
    store.outbox_publisher = make_outbox_publisher(store)
    return store


# ---------------------------------------------------------------- orchestration


def estimate_step_cost(
    model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> Decimal:
    """Pre-dispatch reservation; conservative when the model has a configured price."""
    price = settings.model_pricing().get(model)
    if not price:
        return Decimal(str(settings.default_step_reservation_usd))
    prompt_tokens = (len(json.dumps(messages)) + len(json.dumps(tools))) / 3
    usd = (
        prompt_tokens * price.get("input_per_mtok", 0)
        + settings.reservation_output_tokens * price.get("output_per_mtok", 0)
    ) / 1_000_000
    return Decimal(str(round(usd, 8))) or Decimal(
        str(settings.default_step_reservation_usd)
    )


def _delta_publisher(
    redis: Any, run: dict[str, Any], *, step: int, attempt: int, call: int
) -> DeltaPublisher | None:
    if not settings.output_delta_enabled or redis is None:
        return None
    return DeltaPublisher(
        redis,
        run["run_id"],
        step=step,
        attempt=attempt,
        call=call,
        flush_chars=settings.output_delta_flush_chars,
        flush_ms=settings.output_delta_flush_ms,
        max_chars=settings.output_delta_max_chars_per_step,
        buffer=settings.output_delta_buffer,
    )


async def _call_model(
    run: dict[str, Any],
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    redis: Any = None,
    step: int = 0,
    step_attempt: int = 0,
) -> CompletionResult:
    attempts = max(1, settings.model_call_attempts)
    async with httpx.AsyncClient() as http_client:
        for attempt in range(1, attempts + 1):
            # Each provider call restarts the preview; clients reset on a new `call`.
            publisher = _delta_publisher(
                redis, run, step=step, attempt=step_attempt, call=attempt
            )
            try:
                result = await gateway.chat_completion(
                    http_client,
                    model=model,
                    messages=messages,
                    tools=tools,
                    traceparent=run["traceparent"],
                    on_delta=publisher.push if publisher else None,
                )
                if publisher is not None:
                    await publisher.flush(final=True)
                return result
            except httpx.HTTPStatusError as exc:
                # Some reasoning models reject tool schemas; degrade to the primary model.
                if (
                    exc.response.status_code in (400, 404)
                    and model != run["primary_model"]
                ):
                    log.warning(
                        "Model %s rejected request (%s); falling back",
                        model,
                        exc.response.status_code,
                    )
                    model = run["primary_model"]
                    continue
                if not _is_retryable(exc) or attempt == attempts:
                    raise
            except httpx.HTTPError:
                if attempt == attempts:
                    raise
            await asyncio.sleep(min(2**attempt, 10) / 2)
    raise RuntimeError("Model call attempts exhausted.")


def _tool_call_arguments(tc: dict[str, Any]) -> str:
    raw = tc["function"].get("arguments")
    if raw is None:
        return ""
    return raw if isinstance(raw, str) else json.dumps(raw, sort_keys=True)


def build_invocation(
    lease: Lease,
    run: dict[str, Any],
    *,
    step: int,
    ordinal: int,
    tool_call_id: str,
    tool_name: str,
    arguments: str,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    """Builds the signed, idempotent tool envelope for one tool call."""
    active_skills = set(run["active_skills"])
    allowlist = run.get("skill_allowlist")
    tool = registry.tools.get(tool_name)
    queue = tool.queue if tool else "q_tools_fast"
    plugin = registry.plugins.get(tool.plugin_id) if tool and tool.plugin_id else None
    secret_keys = sorted(registry.resolve_allowed_secrets(active_skills, allowlist))
    key = effect_key(lease.tenant_id, lease.run_id, step, ordinal, tool_call_id)
    invocation_id = invocation_id or str(uuid4())
    digest = arguments_digest(tool_name, arguments)
    now = time.time()
    body = {
        "invocation_id": invocation_id,
        "effect_key": key,
        "tenant_id": lease.tenant_id,
        "run_id": lease.run_id,
        "thread_id": lease.thread_id,
        "step": step,
        "ordinal": ordinal,
        "tool_call": {"id": tool_call_id, "name": tool_name, "arguments": arguments},
        "arguments_digest": digest,
        "queue": queue,
        "active_skills": sorted(active_skills),
        "skill_allowlist": sorted(allowlist) if allowlist is not None else None,
        "allowed_secrets": secret_keys,
        "plugin_digest": plugin.digest if plugin is not None else "",
        "traceparent": run["traceparent"],
        "lease_epoch": lease.epoch,
        "issued_at": now,
        "expires_at": now + settings.tool_outcome_timeout_sec,
    }
    return {
        "invocation_id": invocation_id,
        "ordinal": ordinal,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "queue": queue,
        "arguments": arguments,
        "arguments_digest": digest,
        "effect_key": key,
        "envelope": sign_envelope(body),
        "secret_keys": secret_keys,
        "requires_approval": bool(tool and tool.requires_approval),
    }


@celery_app.task(name="haas.orchestrate", bind=True, queue="q_orchestrator")
def orchestrate_task(
    self,
    *,
    message_id: str,
    run_id: str,
    action: str,
    expected_state_version: int,
) -> dict[str, Any]:
    owner = f"{socket.gethostname()}:{os.getpid()}:{self.request.id or message_id}"
    return _run_async(
        _orchestrate(message_id, run_id, action, expected_state_version, owner)
    )


async def _orchestrate(
    message_id: str,
    run_id: str,
    action: str,
    expected_state_version: int,
    owner: str,
) -> dict[str, Any]:
    store = await _open_store()
    try:
        acquired = await store.acquire_lease(
            message_id=message_id,
            run_id=run_id,
            action=action,
            expected_state_version=expected_state_version,
            owner=owner,
        )
        if acquired is None:
            return {"status": "skipped", "run_id": run_id}
        lease, run = acquired
        try:
            if action == "aggregate":
                run = await _aggregate(store, lease, run)
            elif action == "decision":
                decided = await _decision(store, lease, run)
                if decided is None:
                    return {"status": "dispatched", "run_id": run_id}
                run = decided
            return await _step(store, lease, run)
        except LeaseLostError:
            return {"status": "aborted", "reason": "lease lost", "run_id": run_id}
        except Exception as exc:
            log.exception("orchestration %s failed for %s", action, run_id)
            await store.fail_with_lease(
                lease, f"Orchestrator step failed: {type(exc).__name__}: {exc}"
            )
            return {"status": "failed", "reason": str(exc)}
    finally:
        await store.close()


async def _step(
    store: DistributedStore, lease: Lease, run: dict[str, Any]
) -> dict[str, Any]:
    run_id = lease.run_id
    step = int(run["step_count"]) + 1
    active_skills = set(run["active_skills"])
    messages: list[dict[str, Any]] = run["messages"]
    skill_allowlist: list[str] | None = run.get("skill_allowlist")
    model = gateway.select_model_for_step(
        step=step,
        last_step_had_error=bool(run["last_step_had_error"]),
        primary_model=run["primary_model"],
        planner_model=run["planner_model"],
    )
    tools_payload = registry.resolve_sorted_tool_schemas(active_skills, skill_allowlist)
    ticket = await store.begin_step(
        lease,
        step=step,
        model=model,
        estimate_usd=estimate_step_cost(model, messages, tools_payload),
    )
    if ticket is None:
        return {"status": "failed", "reason": "budget", "run_id": run_id}

    # API and workers load plugins independently (rolling restarts, stale mounts); flag it.
    drift = bool(
        run.get("registry_fingerprint")
        and run["registry_fingerprint"] != registry.fingerprint
    )
    if drift:
        log.warning(
            "registry drift on %s: run created with %s, this worker has %s",
            run_id,
            run["registry_fingerprint"],
            registry.fingerprint,
        )
    await store.emit_step_started(
        lease,
        {
            "step": step,
            "attempt": ticket.attempt,
            "model": model,
            "active_skills": sorted(active_skills),
            "unlocked_tools_count": len(tools_payload),
            "reserved_usd": float(ticket.reserved_usd),
            **({"registry_drift": True} if drift else {}),
        },
    )
    try:
        completion = await _call_model(
            run,
            model,
            messages,
            tools_payload,
            redis=store.redis,
            step=step,
            step_attempt=ticket.attempt,
        )
    except Exception as exc:
        log.exception("model call failed for %s", run_id)
        await store.fail_step(
            lease, ticket, f"Orchestrator step failed: {type(exc).__name__}: {exc}"
        )
        return {"status": "failed", "reason": str(exc)}

    assistant_msg = completion.assistant_message
    messages.append(assistant_msg)
    tool_calls: list[dict[str, Any]] = assistant_msg.get("tool_calls") or []
    outcome: dict[str, Any]
    if not tool_calls:
        outcome = {"kind": "complete", "text": assistant_msg.get("content") or ""}
    else:
        invocations = [
            build_invocation(
                lease,
                run,
                step=step,
                ordinal=ordinal,
                tool_call_id=tc["id"],
                tool_name=tc["function"]["name"],
                arguments=_tool_call_arguments(tc),
            )
            for ordinal, tc in enumerate(tool_calls)
        ]
        outcome = {
            "kind": "approval"
            if any(inv["requires_approval"] for inv in invocations)
            else "dispatch",
            "tool_calls": tool_calls,
            "invocations": invocations,
        }
    kind = await store.commit_step(
        lease,
        ticket,
        cost_usd=completion.cost_usd,
        cost_reported=completion.cost_reported,
        prompt_tokens=completion.prompt_tokens,
        completion_tokens=completion.completion_tokens,
        cached_tokens=completion.cached_tokens,
        messages=messages,
        outcome=outcome,
    )
    return {"status": kind, "run_id": run_id, "step": step}


async def _aggregate(
    store: DistributedStore, lease: Lease, run: dict[str, Any]
) -> dict[str, Any]:
    """Merges recorded tool outcomes in ordinal order and applies the circuit breaker."""
    outcomes = await store.load_step_outcomes(lease, int(run["step_count"]))
    messages: list[dict[str, Any]] = run["messages"]
    active_skills: set[str] = set(run["active_skills"])
    fingerprints: list[str] = list(run["fingerprints"])
    events: list[tuple[str, dict[str, Any]]] = []
    any_error = False
    window = settings.max_identical_tool_calls + 1
    for item in outcomes:
        if item["status"] in {"denied", "cancelled"} and not item["outcome"]:
            continue
        result = item["outcome"]
        name = item["tool_name"]
        active_skills.update(result.get("activated_skills") or [])
        fp = result.get("fingerprint") or _fingerprint(name, item["arguments"])
        fingerprints.append(fp)
        if len(fingerprints) >= window and all(x == fp for x in fingerprints[-window:]):
            content = (
                f"Circuit Breaker Triggered: Identical call to '{name}' "
                f"repeated {window} times consecutively. Change parameters or strategy."
            )
            is_err = True
        else:
            content = str(result.get("content", UNKNOWN_OUTCOME))
            is_err = bool(result.get("is_error", item["status"] != "succeeded"))
        any_error = any_error or is_err
        messages.append(
            {
                "role": "tool",
                "tool_call_id": item["tool_call_id"],
                "name": name,
                "content": content,
            }
        )
        events.append(
            (
                "tool_result",
                {
                    "tool_call_id": item["tool_call_id"],
                    "tool": name,
                    "status": item["status"],
                    "invocation_id": item["invocation_id"],
                    "is_error": is_err,
                    "preview": content[:300],
                },
            )
        )
    await store.commit_aggregate(
        lease,
        messages=messages,
        active_skills=sorted(active_skills),
        fingerprints=fingerprints,
        had_error=any_error,
        events=events,
    )
    return {
        **run,
        "messages": messages,
        "active_skills": sorted(active_skills),
        "fingerprints": fingerprints[-20:],
        "last_step_had_error": any_error,
    }


async def _decision(
    store: DistributedStore, lease: Lease, run: dict[str, Any]
) -> dict[str, Any] | None:
    """Applies a recorded approval decision; returns the run to continue, or None."""
    batch = await store.load_decided_batch(lease)
    if batch is None:
        await store.fail_with_lease(lease, "Approval decision record is missing.")
        return None
    if batch["status"] == "approved":
        invocations = [
            build_invocation(
                lease,
                run,
                step=int(batch["step"]),
                ordinal=int(inv["ordinal"]),
                tool_call_id=inv["tool_call_id"],
                tool_name=inv["tool_name"],
                arguments=inv["arguments"],
                invocation_id=inv["invocation_id"],
            )
            for inv in batch["invocations"]
        ]
        await store.dispatch_approved(lease, invocations)
        return None
    reason = batch["reason"] or "Rejected by human operator."
    messages: list[dict[str, Any]] = run["messages"]
    for inv in batch["invocations"]:
        messages.append(
            {
                "role": "tool",
                "tool_call_id": inv["tool_call_id"],
                "name": inv["tool_name"],
                "content": f"Action Denied by Human Operator: {reason}",
            }
        )
    await store.commit_aggregate(
        lease,
        messages=messages,
        active_skills=list(run["active_skills"]),
        fingerprints=list(run["fingerprints"]),
        had_error=True,
        events=[],
    )
    return {**run, "messages": messages, "last_step_had_error": True}


@celery_app.task(
    name="haas.record_tool_outcome",
    bind=True,
    queue="q_orchestrator",
    max_retries=5,
)
def record_tool_outcome_task(
    self,
    *,
    run_id: str,
    invocation_id: str,
    status: str,
    outcome: dict[str, Any],
) -> dict[str, Any]:
    try:
        return _run_async(_record_outcome(run_id, invocation_id, status, outcome))
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            log.exception("tool outcome for %s could not be recorded", invocation_id)
            return {"status": "dropped"}
        raise self.retry(exc=exc, countdown=2 ** (self.request.retries + 1))


async def _record_outcome(
    run_id: str, invocation_id: str, status: str, outcome: dict[str, Any]
) -> dict[str, Any]:
    store = await _open_store()
    try:
        recorded = await store.record_tool_outcome(
            run_id=run_id, invocation_id=invocation_id, status=status, outcome=outcome
        )
        return {"status": "recorded" if recorded else "duplicate"}
    finally:
        await store.close()


# ---------------------------------------------------------------- tool execution


@celery_app.task(name="haas.execute_tool_task", bind=True)
def execute_tool_task(
    self,
    *,
    envelope: dict[str, Any],
    credential_grants: list[str] | None = None,
    egress_policy: EgressPolicy | None = None,
) -> dict[str, Any]:
    """
    Runs on `worker-tools` (`q_tools_fast` or `q_tools_sandbox`).
    Has no OpenRouter credentials and no PostgreSQL access; trusts only the signed envelope.
    """
    try:
        body = verify_envelope(envelope)
    except EnvelopeError as exc:
        log.warning("rejected tool envelope: %s", exc)
        return {"status": "rejected", "reason": str(exc)}
    status, outcome = _run_async(
        _execute_envelope(
            body, 
            credential_grants=credential_grants or [],
            egress_policy=egress_policy,
        )
    )
    cast(Task, record_tool_outcome_task).apply_async(
        kwargs={
            "run_id": body["run_id"],
            "invocation_id": body["invocation_id"],
            "status": status,
            "outcome": outcome,
        },
        queue="q_orchestrator",
    )
    return {"status": status, "invocation_id": body["invocation_id"]}


async def _execute_envelope(
    body: dict[str, Any],
    *,
    credential_grants: list[str],
    egress_policy: EgressPolicy | None = None,
) -> tuple[str, dict[str, Any]]:
    call = body["tool_call"]
    name, call_id, raw_args = call["name"], call["id"], call["arguments"]
    fp = _fingerprint(name, raw_args)
    skills = set(body.get("active_skills") or [])
    key = body["effect_key"]
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        if await redis.exists(f"haas:cancelled:{body['run_id']}"):
            return "cancelled", _outcome(
                call_id,
                name,
                fp,
                skills,
                "Run was cancelled before tool execution.",
                True,
            )
        if not await redis.set(
            f"haas:effect:{key}", body["invocation_id"], nx=True, ex=EFFECT_TTL_SEC
        ):
            cached = await redis.get(f"haas:effect-result:{key}")
            if cached:
                status, outcome = json.loads(cached)
                return status, outcome
            # A previous delivery started this effect and never recorded a result.
            return "unknown", _outcome(call_id, name, fp, skills, UNKNOWN_OUTCOME, True)
        tool = registry.tools.get(name)
        plugin = (
            registry.plugins.get(tool.plugin_id) if tool and tool.plugin_id else None
        )
        expected = body.get("plugin_digest") or ""
        if expected and (plugin is None or plugin.digest != expected):
            outcome = _outcome(
                call_id,
                name,
                fp,
                skills,
                f"Tool Error: plugin revision for '{name}' changed since dispatch.",
                True,
            )
        else:
            allowlist = body.get("skill_allowlist")
            # Redeem credential grants into scoped secrets (single-use GETDEL).
            granted_secrets: dict[str, str] = {}
            try:
                if credential_grants:
                    redeemed = await redeem_grants(
                        credential_grants,
                        tool_name=name,
                        invocation_id=body["invocation_id"],
                        run_id=body["run_id"],
                        redis=redis,
                    )
                    granted_secrets = redeemed
            except GrantError as exc:
                granted_secrets = {}
                log.warning(
                    "credential grant redeemed failed for %s: %s",
                    body["invocation_id"],
                    exc,
                )
            outcome = await _async_execute_tool(
                run_id=body["run_id"],
                thread_id=body["thread_id"],
                tenant_id=body["tenant_id"],
                traceparent=body["traceparent"],
                active_skills=skills,
                allowed_secrets=set(body.get("allowed_secrets") or []),
                scoped_secrets=granted_secrets,
                tool_call={
                    "id": call_id,
                    "function": {"name": name, "arguments": raw_args},
                },
                skill_allowlist=set(allowlist) if allowlist is not None else None,
                effect_key=key,
                egress_policy=egress_policy,
            )
        status = "failed" if outcome["is_error"] else "succeeded"
        await redis.setex(
            f"haas:effect-result:{key}", EFFECT_TTL_SEC, json.dumps([status, outcome])
        )
        return status, outcome
    finally:
        await redis.aclose()


def _outcome(
    tc_id: str, name: str, fp: str, skills: set[str], content: str, is_error: bool
) -> dict[str, Any]:
    return {
        "tool_call_id": tc_id,
        "name": name,
        "fingerprint": fp,
        "is_error": is_error,
        "activated_skills": sorted(skills),
        "content": content,
    }


async def _async_execute_tool(
    *,
    run_id: str,
    thread_id: str,
    tenant_id: str,
    traceparent: str,
    active_skills: set[str],
    allowed_secrets: set[str],
    scoped_secrets: dict[str, str],
    tool_call: dict[str, Any],
    skill_allowlist: set[str] | None = None,
    effect_key: str = "",
    egress_policy: EgressPolicy | None = None,
) -> dict[str, Any]:
    tc_id = tool_call["id"]
    fn_name = tool_call["function"]["name"]
    raw_args = tool_call["function"].get("arguments", "{}")
    fp = _fingerprint(fn_name, raw_args)

    tool = registry.tools.get(fn_name)
    if not tool or fn_name not in registry.resolve_unlocked_tool_names(
        active_skills, skill_allowlist
    ):
        return _outcome(
            tc_id,
            fn_name,
            fp,
            active_skills,
            f"Error: Tool '{fn_name}' does not exist or is not unlocked. "
            f"Call `activate_skill` first to unlock domain tools.",
            True,
        )

    try:
        validated_args = tool.args_schema.model_validate(repair_tool_json(raw_args))
    except (json.JSONDecodeError, ValidationError) as exc:
        return _outcome(
            tc_id,
            fn_name,
            fp,
            active_skills,
            f"Schema Validation Failed for '{fn_name}': {exc}. Fix arguments and retry.",
            True,
        )

    async with httpx.AsyncClient() as http_client:
        # Create an egress meter for this tool invocation
        meter = InMemoryEgressMeter()
        ctx = RunContext(
            run_id=run_id,
            thread_id=thread_id,
            tenant_id=tenant_id,
            traceparent=traceparent,
            active_skills=registry.effective_skills(active_skills, skill_allowlist),
            skill_allowlist=skill_allowlist,
            allowed_secrets=allowed_secrets,
            _scoped_secrets=scoped_secrets,
            http_client=http_client,
            effect_key=effect_key,
        )
        # Bind the egress policy and meter to context vars for egress_request
        if egress_policy is not None:
            egress.active_policy.set(egress_policy)
            egress.active_meter.set(meter)
        args = (ctx, validated_args) if tool.injects_context else (validated_args,)

        # Execute tool either in sandbox or in-process
        if settings.sandbox_mode == "sandbox":
            try:
                ok, observation, error_msg = await execute_in_sandbox(
                    tool_name=fn_name,
                    validated_args=validated_args,
                    secrets=scoped_secrets,
                    run_id=run_id,
                    thread_id=thread_id,
                    tenant_id=tenant_id,
                    traceparent=traceparent,
                    active_skills=active_skills,
                    skill_allowlist=skill_allowlist,
                    allowed_secrets=allowed_secrets,
                    effect_key=effect_key,
                    egress_policy=egress_policy,
                )
                if ok:
                    return _outcome(
                        tc_id,
                        fn_name,
                        fp,
                        active_skills,
                        _bound_observation(fn_name, tc_id, observation),
                        False,
                    )
                return _outcome(
                    tc_id,
                    fn_name,
                    fp,
                    active_skills,
                    f"Sandbox Error [{error_msg}]",
                    True,
                )
            except SandboxSetupError:
                # Fall through to in-process execution
                pass
            except Exception as exc:
                return _outcome(
                    tc_id,
                    fn_name,
                    fp,
                    active_skills,
                    f"Sandbox Runtime Error: {exc}",
                    True,
                )

        # In-process execution (fallback or when sandbox_mode != "sandbox")
        try:
            if inspect.iscoroutinefunction(tool.handler):
                awaitable = tool.handler(*args)
            else:
                awaitable = asyncio.to_thread(tool.handler, *args)
            raw_out = await asyncio.wait_for(
                awaitable, timeout=settings.per_tool_timeout_sec
            )
            return _outcome(
                tc_id,
                fn_name,
                fp,
                ctx.active_skills,
                _bound_observation(fn_name, tc_id, str(raw_out)),
                False,
            )
        except asyncio.TimeoutError:
            return _outcome(
                tc_id,
                fn_name,
                fp,
                ctx.active_skills,
                f"Timeout Error: '{fn_name}' exceeded {settings.per_tool_timeout_sec}s.",
                True,
            )
        except (ToolRemediationError, SecurityViolationError) as exc:
            return _outcome(
                tc_id, fn_name, fp, ctx.active_skills, f"Tool Error: {exc}", True
            )
        except Exception as exc:
            return _outcome(
                tc_id,
                fn_name,
                fp,
                ctx.active_skills,
                f"Runtime Error in '{fn_name}' ({type(exc).__name__}): {exc}",
                True,
            )


@celery_app.task(
    name="haas.deliver_webhook_task",
    bind=True,
    queue="q_orchestrator",
    max_retries=3,
)
def deliver_webhook_task(self, webhook_url: str, payload: dict[str, Any]) -> None:
    try:
        validate_safe_outbound_url(webhook_url, resolve_dns=True)
    except (SecurityViolationError, ToolRemediationError) as exc:
        log.warning("Webhook blocked: %s", exc)
        return
    try:
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            client.post(webhook_url, json=payload).raise_for_status()
    except httpx.HTTPError as exc:
        if self.request.retries >= self.max_retries:
            log.error("Webhook delivery to %s failed permanently: %s", webhook_url, exc)
            return
        raise self.retry(exc=exc, countdown=2 ** (self.request.retries + 1))
