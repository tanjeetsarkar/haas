from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from typing import Any, cast

import httpx
import redis.asyncio as aioredis
from celery import Task, chord, group
from pydantic import ValidationError

from app.core.config import settings
from app.core.context import (
    RunContext,
    SecurityViolationError,
    ToolRemediationError,
    validate_safe_outbound_url,
)
from app.core.llm import OpenRouterGateway, repair_tool_json
from app.core.plugins import registry
from app.core.store import DistributedStore
from app.worker.celery_app import celery_app
from app.worker.dispatch import signature_for

log = logging.getLogger("haas.tasks")

gateway = OpenRouterGateway(settings.openrouter_api_key, settings.openrouter_base_url)

# Workers open one short-lived pool per task, so keep it small.
TASK_POOL_MAX = 3


def _run_async(coro: Any) -> Any:
    """Executes an async coroutine cleanly inside a Celery worker process."""
    return asyncio.run(coro)


async def _is_cancelled(run_id: str) -> bool:
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        return bool(await redis.exists(f"haas:cancelled:{run_id}"))
    finally:
        await redis.aclose()


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


async def _fail_run(run_id: str, message: str) -> None:
    """Terminal failure path so a crashed step never leaves a run stuck in `running`."""
    store = DistributedStore()
    await store.init(pool_max=TASK_POOL_MAX)
    try:
        run = await store.get_run(run_id)
        if not run or run["status"] in ("completed", "failed", "cancelled"):
            return
        await store.finalize_run(
            run_id, run["thread_id"], "failed", message, run["messages"]
        )
        await store.publish_event(run_id, "run_error", {"error": message})
        if run["webhook_url"]:
            cast(Task, deliver_webhook_task).apply_async(
                args=[
                    run["webhook_url"],
                    {"run_id": run_id, "status": "failed", "error": message},
                ],
                queue="q_orchestrator",
            )
    finally:
        await store.close()


@celery_app.task(
    name="haas.llm_step_task", bind=True, queue="q_orchestrator", max_retries=2
)
def llm_step_task(self, run_id: str) -> dict[str, Any]:
    try:
        return _run_async(_async_llm_step(self, run_id))
    except Exception as exc:
        if _is_retryable(exc) and self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=2 ** (self.request.retries + 1))
        log.exception("llm_step_task failed for %s", run_id)
        _run_async(
            _fail_run(run_id, f"Orchestrator step failed: {type(exc).__name__}: {exc}")
        )
        return {"status": "failed", "reason": str(exc)}


async def _async_llm_step(task_self: Any, run_id: str) -> dict[str, Any]:
    store = DistributedStore()
    await store.init(pool_max=TASK_POOL_MAX)
    try:
        run = await store.get_run(run_id)
        if not run or run["status"] != "running":
            return {"status": "aborted", "reason": "Run not in running state"}

        step = int(run["step_count"]) + 1
        max_steps = int(run["max_steps"])
        if step > max_steps:
            msg = f"Exceeded max_steps budget ({max_steps})."
            await store.finalize_run(
                run_id, run["thread_id"], "failed", msg, run["messages"]
            )
            await store.publish_event(run_id, "run_error", {"error": msg})
            await _maybe_webhook(
                run, {"run_id": run_id, "status": "failed", "error": msg}
            )
            return {"status": "failed", "reason": msg}

        active_skills = set(run["active_skills"])
        messages: list[dict[str, Any]] = run["messages"]
        fingerprints: list[str] = run["fingerprints"]

        chosen_model = gateway.select_model_for_step(
            step=step,
            last_step_had_error=bool(run["last_step_had_error"]),
            primary_model=run["primary_model"],
            planner_model=run["planner_model"],
        )
        skill_allowlist: list[str] | None = run.get("skill_allowlist")
        tools_payload = registry.resolve_sorted_tool_schemas(
            active_skills, skill_allowlist
        )

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

        await store.publish_event(
            run_id,
            "step_started",
            {
                "step": step,
                "model": chosen_model,
                "active_skills": sorted(active_skills),
                "unlocked_tools_count": len(tools_payload),
                **({"registry_drift": True} if drift else {}),
            },
        )

        async with httpx.AsyncClient() as http_client:
            try:
                completion = await gateway.chat_completion(
                    http_client,
                    model=chosen_model,
                    messages=messages,
                    tools=tools_payload,
                    traceparent=run["traceparent"],
                )
            except httpx.HTTPStatusError as exc:
                # Some reasoning models reject tool schemas; degrade to the primary model.
                if (
                    exc.response.status_code in (400, 404)
                    and chosen_model != run["primary_model"]
                ):
                    log.warning(
                        "Model %s rejected request (%s); falling back",
                        chosen_model,
                        exc.response.status_code,
                    )
                    chosen_model = run["primary_model"]
                    completion = await gateway.chat_completion(
                        http_client,
                        model=chosen_model,
                        messages=messages,
                        tools=tools_payload,
                        traceparent=run["traceparent"],
                    )
                else:
                    raise

        current_run = await store.get_run(run_id)
        if not current_run or current_run["status"] != "running":
            return {"status": "aborted", "reason": "Run is no longer active"}

        assistant_msg = completion.assistant_message
        messages.append(assistant_msg)
        new_total_cost = float(run["total_cost_usd"]) + completion.cost_usd

        # Branch A: model finished without tool calls
        tool_calls = assistant_msg.get("tool_calls") or []
        if not tool_calls:
            final_text = assistant_msg.get("content") or ""
            await store.finalize_run(
                run_id,
                run["thread_id"],
                "completed",
                final_text,
                messages,
                cost_delta_usd=completion.cost_usd,
            )
            await store.publish_event(
                run_id,
                "run_completed",
                {
                    "output": final_text,
                    "steps_used": step,
                    "total_cost_usd": round(new_total_cost, 6),
                    "cached_tokens": completion.cached_tokens,
                },
            )
            await _maybe_webhook(
                run, {"run_id": run_id, "status": "completed", "output": final_text}
            )
            return {"status": "completed", "run_id": run_id}

        # USD budget check before executing tools
        if new_total_cost > float(run["max_cost_usd"]):
            err = f"Exceeded max_cost_usd budget (${new_total_cost:.4f} > ${float(run['max_cost_usd']):.4f})."
            await store.finalize_run(
                run_id,
                run["thread_id"],
                "failed",
                err,
                messages,
                cost_delta_usd=completion.cost_usd,
            )
            await store.publish_event(run_id, "run_error", {"error": err})
            await _maybe_webhook(
                run, {"run_id": run_id, "status": "failed", "error": err}
            )
            return {"status": "failed", "reason": err}

        # Branch B: Human-in-the-Loop gate — suspend the workflow (zero worker hold)
        approval_needed = [
            tc
            for tc in tool_calls
            if (t := registry.tools.get(tc["function"]["name"])) and t.requires_approval
        ]
        if approval_needed:
            await store.save_checkpoint(
                run_id,
                step_count=step,
                messages=messages,
                active_skills=active_skills,
                fingerprints=fingerprints,
                cost_delta_usd=completion.cost_usd,
                status="input_required",
                pending_tool_calls=tool_calls,
            )
            await store.publish_event(
                run_id,
                "input_required",
                {
                    "step": step,
                    "pending_tool_calls": [
                        {
                            "id": tc["id"],
                            "name": tc["function"]["name"],
                            "arguments": tc["function"].get("arguments"),
                        }
                        for tc in tool_calls
                    ],
                    "resume_endpoint": f"/v1/runs/{run_id}/resume",
                },
            )
            return {"status": "input_required", "run_id": run_id}

        # Branch C: parallel tool fan-out via Celery Canvas `chord`
        await store.save_checkpoint(
            run_id,
            step_count=step,
            messages=messages,
            active_skills=active_skills,
            fingerprints=fingerprints,
            cost_delta_usd=completion.cost_usd,
            status="running",
        )

        allowed_secret_keys = registry.resolve_allowed_secrets(
            active_skills, skill_allowlist
        )
        scoped_secrets = await store.get_scoped_secrets(run_id, allowed_secret_keys)

        tool_signatures = []
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            reg_tool = registry.tools.get(fn_name)
            target_queue = reg_tool.queue if reg_tool else "q_tools_fast"

            await store.publish_event(
                run_id,
                "tool_dispatched",
                {"tool_call_id": tc["id"], "tool": fn_name, "queue": target_queue},
            )
            tool_signatures.append(
                signature_for(
                    cast(Task, execute_tool_task),
                    run_id=run_id,
                    thread_id=run["thread_id"],
                    tenant_id=run["tenant_id"],
                    traceparent=run["traceparent"],
                    active_skills=sorted(active_skills),
                    skill_allowlist=skill_allowlist,
                    allowed_secrets=sorted(allowed_secret_keys),
                    scoped_secrets=scoped_secrets,
                    tool_call=tc,
                ).set(queue=target_queue)
            )

        callback_sig = signature_for(
            cast(Task, aggregate_step_task), run_id=run_id
        ).set(queue="q_orchestrator")
        chord(group(tool_signatures))(callback_sig)
        return {"status": "dispatched_chord", "tool_count": len(tool_signatures)}
    finally:
        await store.close()


async def _maybe_webhook(run: dict[str, Any], payload: dict[str, Any]) -> None:
    if run.get("webhook_url"):
        cast(Task, deliver_webhook_task).apply_async(
            args=[run["webhook_url"], payload], queue="q_orchestrator"
        )


@celery_app.task(name="haas.execute_tool_task", bind=True)
def execute_tool_task(
    self,
    *,
    run_id: str,
    thread_id: str,
    tenant_id: str,
    traceparent: str,
    active_skills: list[str],
    allowed_secrets: list[str],
    scoped_secrets: dict[str, str],
    tool_call: dict[str, Any],
    skill_allowlist: list[str] | None = None,
) -> dict[str, Any]:
    """
    Runs on `worker-tools` (`q_tools_fast` or `q_tools_sandbox`).
    Has no OpenRouter credentials and no PostgreSQL access.
    """
    cancelled = _run_async(_is_cancelled(run_id))
    if cancelled:
        tc = tool_call
        name = tc["function"]["name"]
        return _outcome(
            tc["id"],
            name,
            _fingerprint(name, tc["function"].get("arguments", "{}")),
            set(active_skills),
            "Run was cancelled before tool execution.",
            True,
        )
    return _run_async(
        _async_execute_tool(
            run_id=run_id,
            thread_id=thread_id,
            tenant_id=tenant_id,
            traceparent=traceparent,
            active_skills=set(active_skills),
            allowed_secrets=set(allowed_secrets),
            scoped_secrets=scoped_secrets,
            tool_call=tool_call,
            skill_allowlist=set(skill_allowlist)
            if skill_allowlist is not None
            else None,
        )
    )


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
        )
        args = (ctx, validated_args) if tool.injects_context else (validated_args,)
        try:
            if inspect.iscoroutinefunction(tool.handler):
                awaitable = tool.handler(*args)
            else:
                # Sync handlers run in a worker thread so the timeout can actually fire.
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


@celery_app.task(name="haas.aggregate_step_task", queue="q_orchestrator")
def aggregate_step_task(
    tool_outcomes: list[dict[str, Any]], run_id: str
) -> dict[str, Any]:
    """
    Celery chord callback: merges parallel tool outputs, enforces the identical-call
    circuit breaker, checkpoints the updated state, and triggers the next `llm_step_task`.
    """
    try:
        return _run_async(_async_aggregate_step(tool_outcomes, run_id))
    except Exception as exc:
        log.exception("aggregate_step_task failed for %s", run_id)
        _run_async(
            _fail_run(run_id, f"Aggregation failed: {type(exc).__name__}: {exc}")
        )
        return {"status": "failed", "reason": str(exc)}


async def _async_aggregate_step(
    tool_outcomes: list[dict[str, Any]], run_id: str
) -> dict[str, Any]:
    store = DistributedStore()
    await store.init(pool_max=TASK_POOL_MAX)
    try:
        run = await store.get_run(run_id)
        if not run or run["status"] != "running":
            return {"status": "aborted"}

        messages: list[dict[str, Any]] = run["messages"]
        active_skills: set[str] = set(run["active_skills"])
        fingerprints: list[str] = run["fingerprints"]
        any_error = False

        for outcome in tool_outcomes:
            active_skills.update(outcome.get("activated_skills", []))
            fp = outcome["fingerprint"]
            fingerprints.append(fp)

            window = settings.max_identical_tool_calls + 1
            if len(fingerprints) >= window and all(
                x == fp for x in fingerprints[-window:]
            ):
                content = (
                    f"Circuit Breaker Triggered: Identical call to '{outcome['name']}' "
                    f"repeated {window} times consecutively. Change parameters or strategy."
                )
                is_err = True
            else:
                content = outcome["content"]
                is_err = bool(outcome["is_error"])

            any_error = any_error or is_err
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": outcome["tool_call_id"],
                    "name": outcome["name"],
                    "content": content,
                }
            )
            await store.publish_event(
                run_id,
                "tool_result",
                {
                    "tool_call_id": outcome["tool_call_id"],
                    "tool": outcome["name"],
                    "is_error": is_err,
                    "preview": content[:300],
                },
            )

        await store.save_checkpoint(
            run_id,
            step_count=int(run["step_count"]),
            messages=messages,
            active_skills=active_skills,
            fingerprints=fingerprints,
            last_step_had_error=any_error,
            status="running",
        )

        cast(Task, llm_step_task).apply_async(args=[run_id], queue="q_orchestrator")
        return {"status": "continued", "run_id": run_id}
    finally:
        await store.close()


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
