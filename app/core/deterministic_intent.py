"""Prepare one held exact-intent approval; no model, grants or dispatch."""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from app.core.config import settings
from app.core.envelope import arguments_digest, sha256_hex, sign_envelope
from app.core.held_intent import validate_held_creation
from app.core.intent_preflight import IntentPreflight
from app.core.plugins import registry
from app.core.run_restrictions import decode_authority, validate_intent_provenance
from app.core.workflow import Effects, Lease, decode_run, effect_key


async def prepare_intent_approval(store: Any, lease: Lease) -> dict[str, Any]:
    fx = Effects()
    async with store.connection() as conn:
        # Admission's lock order: active tenant, then thread/run.
        tenant = await conn.fetchrow(
            "SELECT tenant_id,active_policy_revision_id FROM tenants "
            "WHERE tenant_id=resolve_haas_tenant_uuid($1) AND status='active' FOR SHARE",
            lease.tenant_id,
        )
        if tenant is None:
            raise ValueError("restriction.tenant_unavailable")
        row = await store._fenced(conn, lease)
        authority = decode_authority(row["authority_json"])
        if (
            authority is None or authority.intent is None
            or row["phase_config_json"] is not None or row["phase_state_json"] is not None
        ):
            raise ValueError("restriction.intent_required")
        intent, run = authority.intent, decode_run(row)
        context = intent.context
        if (
            str(tenant["active_policy_revision_id"]) != context.policy_revision
            or row["step_count"] != 0 or row["tool_invocations_used"] != 0
            or row["max_tool_invocations"] < 1 or row["max_steps"] < 1
            or (row["deadline_at"] is not None and row["deadline_at"].timestamp() <= time.time())
        ):
            raise ValueError("restriction.intent_state")
        validate_intent_provenance(
            authority, tenant_id=str(tenant["tenant_id"]), run_id=lease.run_id,
            submitter_id=str(row["submitted_by_principal_id"]),
            policy_revision=str(row["policy_revision_id"]),
            registry_fingerprint=row["registry_fingerprint"],
            idempotency_key=context.idempotency_key,
        )
        destination = json.loads(context.destination)
        ceiling = run["skill_allowlist"]
        record = await conn.fetchrow(
            "SELECT digest FROM tenant_policy_revisions WHERE tenant_id=$1 AND policy_revision_id=$2",
            tenant["tenant_id"], UUID(context.policy_revision),
        )
        if record is None:
            raise ValueError("restriction.policy_unavailable")
        snapshot = IntentPreflight(
            tenant_id=context.tenant_id, submitter_id=context.submitter_id,
            tool_name=intent.tool_name, arguments_json=intent.arguments_json,
            schema_sha256=destination["schema_sha256"], policy_revision_id=context.policy_revision,
            policy_digest=record["digest"], registry_fingerprint=context.registry_fingerprint,
            plugin_id=context.plugin_id, plugin_digest=context.plugin_digest,
            preload_skills=tuple(run["active_skills"]),
            skill_ceiling=tuple(sorted(ceiling)) if ceiling is not None else None,
        )
        await validate_held_creation(
            conn, snapshot, authority, skill_ceiling=ceiling,
            agent_revision_id=str(row["agent_revision_id"]) if row["agent_revision_id"] else None,
            agent_fields={key: float(row[key]) if key == "max_cost_usd" else row[key] for key in (
                "primary_model", "planner_model", "max_steps", "max_cost_usd",
            )},
        )
        if await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM tool_invocations WHERE tenant_id=$1 AND run_id=$2) "
            "OR EXISTS(SELECT 1 FROM approval_batches WHERE tenant_id=$1 AND run_id=$2)",
            row["tenant_id"], lease.run_id,
        ):
            raise ValueError("restriction.intent_already_prepared")
        tool = registry.tools[intent.tool_name]
        call_id = "intent_" + intent.binding
        invocation_id = str(uuid5(NAMESPACE_URL, "haas.intent.v1:" + intent.binding))
        digest = arguments_digest(intent.tool_name, intent.arguments_json)
        secrets = sorted(registry.resolve_allowed_secrets(set(run["active_skills"]), ceiling))
        now = time.time()
        body = {
            "invocation_id": invocation_id, "effect_key": effect_key(
                lease.tenant_id, lease.run_id, 1, 0, call_id,
            ), "tenant_id": context.tenant_id, "run_id": lease.run_id,
            "thread_id": lease.thread_id, "step": 1, "ordinal": 0,
            "tool_call": {"id": call_id, "name": intent.tool_name, "arguments": intent.arguments_json},
            "arguments_digest": digest, "queue": tool.queue,
            "active_skills": run["active_skills"], "skill_allowlist": ceiling,
            "allowed_secrets": secrets, "plugin_digest": context.plugin_digest,
            "traceparent": row["traceparent"], "lease_epoch": lease.epoch,
            "issued_at": now, "expires_at": min(
                now + settings.approval_ttl_sec,
                row["deadline_at"].timestamp() if row["deadline_at"] else float("inf"),
            ), "authority_json": authority.model_dump(mode="json"), "intent_binding": intent.binding,
        }
        proof = {"version": 1, "intent_binding": intent.binding, "envelope": sign_envelope(body)}
        call = {"id": call_id, "function": {
            "name": intent.tool_name, "arguments": intent.arguments_json,
        }, "intent_proof": proof}
        invocation = {
            "invocation_id": invocation_id, "ordinal": 0, "tool_call_id": call_id,
            "tool_name": intent.tool_name, "queue": tool.queue,
            "arguments": intent.arguments_json, "arguments_digest": digest,
            "effect_key": body["effect_key"], "secret_keys": secrets, "requires_approval": True,
        }
        await store._create_approval(conn, fx, lease, row, 1, [call], [invocation])
        await store._bump(
            conn, lease, "status='input_required',next_action='none',step_count=1,"
            "pending_tool_calls_json=$2::jsonb", json.dumps([call]), release=True,
        )
    await store._after_commit(fx)
    return {"status": "input_required", "run_id": lease.run_id}


def intent_batch_digest(run_id: str, step: int, calls: list[dict[str, Any]]) -> str:
    if len(calls) != 1 or calls[0]["intent_proof"]["version"] != 1:
        raise ValueError("restriction.approval_binding")
    return sha256_hex({"domain": "haas.intent-approval.v1", "run_id": run_id, "step": step, "calls": calls})
