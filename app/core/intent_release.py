"""Internal exact-action release. No public admission or worker ALLOW switch."""

from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from app.core.intent_eligibility import validate_dispatch_eligibility
from app.core.run_restrictions import decode_authority
from app.core.workflow import Effects, decode_run


async def release_approved_intent(store: Any, *, tenant_id: str, run_id: str) -> dict[str, Any]:
    """Release one persisted approved invocation, never synthesize/re-sign a call.

    Internal orchestration entry only. Global admission/lease/worker ALLOW holds
    remain independent. A failed eligibility check rolls back provisional state.
    """
    fx = Effects()
    async with store.connection() as conn:
        tenant = await conn.fetchrow(
            "SELECT tenant_id FROM tenants WHERE tenant_id=resolve_haas_tenant_uuid($1) "
            "AND status='active' FOR SHARE", tenant_id,
        )
        row = await store._lock_run(conn, run_id, str(tenant["tenant_id"])) if tenant else None
        if row is None:
            raise ValueError("restriction.run_unavailable")
        authority = decode_authority(row["authority_json"])
        if authority is None or authority.intent is None:
            raise ValueError("restriction.intent_required")
        run = decode_run(row)
        calls = run["pending_tool_calls"]
        if (
            row["status"] not in ("input_required", "running") or row["next_action"] != "none"
            or row["lease_owner"] is not None or len(calls or []) != 1
        ):
            raise ValueError("restriction.release_state")
        envelope = calls[0]["intent_proof"]["envelope"]
        body = envelope["body"]
        invocation = await conn.fetchrow(
            "SELECT * FROM tool_invocations WHERE tenant_id=$1 AND run_id=$2 "
            "AND invocation_id=$3 FOR UPDATE",
            row["tenant_id"], run_id, UUID(body["invocation_id"]),
        )
        if invocation is None:
            raise ValueError("restriction.release_state")
        replay = row["status"] == "running" and invocation["status"] == "dispatched"
        if row["tool_invocations_used"] != (1 if replay else 0) or row["max_tool_invocations"] < 1:
            raise ValueError("restriction.release_budget")
        if not replay:
            if row["status"] != "input_required" or invocation["status"] != "approved":
                raise ValueError("restriction.release_state")
            await conn.execute(
                "UPDATE runs SET status='running',tool_invocations_used=tool_invocations_used+1,"
                "state_version=state_version+1,updated_at=$2 WHERE run_id=$1",
                run_id, time.time(),
            )
            await conn.execute(
                "UPDATE tool_invocations SET status='dispatched',dispatched_at=now() "
                "WHERE invocation_id=$1", invocation["invocation_id"],
            )
        context = authority.intent.context
        check = SimpleNamespace(
            tenant_id=context.tenant_id, run_id=run_id, invocation_id=body["invocation_id"],
            tool_name=authority.intent.tool_name, intent_binding=authority.intent.binding,
            policy_revision_id=context.policy_revision, registry_fingerprint=context.registry_fingerprint,
            plugin_digest=context.plugin_digest, envelope_expires_at=body["expires_at"],
            arguments_sha256=hashlib.sha256(authority.intent.arguments_json.encode()).hexdigest(),
        )
        await validate_dispatch_eligibility(store, conn, check)
        if not replay:
            await store._enqueue(
                conn, fx, tenant_id=row["tenant_id"], run_id=run_id, destination="tool",
                payload={"queue": body["queue"], "envelope": envelope, "secret_keys": body["allowed_secrets"]},
                dedup_key=f"intent:{body['effect_key']}",
            )
            await store._append_events(conn, fx, row["tenant_id"], run_id, [(
                "tool_dispatched", {"invocation_id": body["invocation_id"],
                                    "tool_call_id": body["tool_call"]["id"], "queue": body["queue"],
                                    "effect_key": body["effect_key"], "tool": authority.intent.tool_name},
            )])
    await store._after_commit(fx)
    return {"run_id": run_id, "status": "running", "replayed": replay}


async def finalize_intent_outcome(store: Any, conn: Any, fx: Effects, row: Any) -> None:
    """One exact effect ends the run; unknown is terminal and never auto-resends."""
    authority = decode_authority(row["authority_json"])
    invocations = await conn.fetch(
        "SELECT status,outcome FROM tool_invocations WHERE tenant_id=$1 AND run_id=$2",
        row["tenant_id"], row["run_id"],
    )
    if authority is None or authority.intent is None or len(invocations) != 1:
        raise ValueError("restriction.outcome_binding")
    invocation = invocations[0]
    status = invocation["status"]
    if status not in ("succeeded", "failed", "unknown", "cancelled"):
        raise ValueError("restriction.outcome_state")
    import json
    outcome = invocation["outcome"]
    outcome = json.loads(outcome) if isinstance(outcome, str) else outcome or {}
    terminal = "completed" if status == "succeeded" else "cancelled" if status == "cancelled" else "failed"
    if row["status"] == "cancelling":
        terminal = "cancelled"
    text = ("Tool outcome unknown; automatic resend is forbidden." if status == "unknown"
            else str(outcome.get("content", "")))
    event = (("run_completed", {"output": text, "steps_used": 1,
                               "total_cost_usd": float(row["total_cost_usd"])})
             if terminal == "completed" else ("run_cancelled", {"run_id": row["run_id"]})
             if terminal == "cancelled" else ("run_error", {"error": text}))
    await store._finalize(conn, fx, row, terminal, text, events=[event])
