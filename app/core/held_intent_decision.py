"""Persist a verified held decision only; no secrets, scheduling or execution permit."""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import UUID, uuid4

from app.core.deterministic_intent import intent_batch_digest
from app.core.envelope import arguments_digest, verify_envelope
from app.core.held_intent import validate_held_creation, validate_held_key
from app.core.identity import AuthenticatedIdentity
from app.core.intent_preflight import IntentPreflight
from app.core.run_restrictions import decode_authority, validate_intent_provenance
from app.core.workflow import IdempotencyConflictError, decode_run


async def record_held_intent_decision(
    store: Any, *, identity: AuthenticatedIdentity, run_id: str, batch_id: str,
    batch_digest: str, approved: bool, request_id: str, idempotency_key: str | None = None,
    secrets: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Internal authenticated path; reuse existing tables without lifting any hold."""
    if secrets or type(approved) is not bool:
        raise ValueError("restriction.decision_unavailable")
    actor, batch_uuid, request_uuid = UUID(identity.principal_id), UUID(batch_id), UUID(request_id)
    if idempotency_key is not None:
        validate_held_key(idempotency_key)
    decision = "approved" if approved else "rejected"
    async with store.connection() as conn:
        tenant = await conn.fetchrow(
            "SELECT tenant_id,active_policy_revision_id FROM tenants "
            "WHERE tenant_id=resolve_haas_tenant_uuid($1) AND status='active' FOR SHARE",
            identity.tenant_id,
        )
        if tenant is None or "approver" not in identity.roles:
            raise ValueError("restriction.approver_required")
        allowed = await conn.fetchrow(
            "SELECT m.principal_id FROM memberships m JOIN principals p USING(principal_id) "
            "WHERE m.tenant_id=$1 AND m.principal_id=$2 AND m.role='approver' "
            "AND m.status='active' AND p.status='active' FOR SHARE OF m,p",
            tenant["tenant_id"], actor,
        )
        if allowed is None:
            raise ValueError("restriction.approver_required")
        row = await store._lock_run(conn, run_id, str(tenant["tenant_id"]))
        if row is None:
            raise ValueError("restriction.run_unavailable")
        authority = decode_authority(row["authority_json"])
        if authority is None or authority.intent is None:
            raise ValueError("restriction.intent_required")
        intent, run = authority.intent, decode_run(row)
        context = intent.context
        if (
            str(actor) == context.submitter_id
            or str(tenant["active_policy_revision_id"]) != context.policy_revision
            or row["status"] != "input_required" or row["next_action"] != "none"
            or row["phase_config_json"] is not None or row["phase_state_json"] is not None
            or row["deadline_at"] is not None and row["deadline_at"].timestamp() <= time.time()
        ):
            raise ValueError("restriction.decision_state")
        validate_intent_provenance(
            authority, tenant_id=str(tenant["tenant_id"]), run_id=run_id,
            submitter_id=str(row["submitted_by_principal_id"]),
            policy_revision=str(row["policy_revision_id"]), registry_fingerprint=row["registry_fingerprint"],
            idempotency_key=context.idempotency_key,
        )
        policy_digest = await conn.fetchval(
            "SELECT digest FROM tenant_policy_revisions WHERE tenant_id=$1 AND policy_revision_id=$2",
            tenant["tenant_id"], UUID(context.policy_revision),
        )
        ceiling = run["skill_allowlist"]
        await validate_held_creation(
            conn, IntentPreflight(
                tenant_id=context.tenant_id, submitter_id=context.submitter_id,
                tool_name=intent.tool_name, arguments_json=intent.arguments_json,
                schema_sha256=json.loads(context.destination)["schema_sha256"],
                policy_revision_id=context.policy_revision, policy_digest=policy_digest,
                registry_fingerprint=context.registry_fingerprint, plugin_id=context.plugin_id,
                plugin_digest=context.plugin_digest, preload_skills=tuple(run["active_skills"]),
                skill_ceiling=tuple(sorted(ceiling)) if ceiling is not None else None,
            ), authority, skill_ceiling=ceiling,
            agent_revision_id=str(row["agent_revision_id"]) if row["agent_revision_id"] else None,
            agent_fields={key: float(row[key]) if key == "max_cost_usd" else row[key] for key in (
                "primary_model", "planner_model", "max_steps", "max_cost_usd",
            )},
        )
        batch = await conn.fetchrow(
            "SELECT * FROM approval_batches WHERE tenant_id=$1 AND run_id=$2 AND batch_id=$3 FOR UPDATE",
            row["tenant_id"], run_id, batch_uuid,
        )
        calls = run["pending_tool_calls"]
        if (
            batch is None or batch["expires_at"].timestamp() <= time.time()
            or batch["batch_digest"] != batch_digest
            or intent_batch_digest(run_id, batch["step_number"], calls) != batch_digest
        ):
            raise ValueError("restriction.approval_binding")
        pending = json.loads(batch["tool_calls"])
        call = calls[0]
        proof = call["intent_proof"]
        body = verify_envelope(proof["envelope"])
        invocations = await conn.fetch(
            "SELECT * FROM tool_invocations WHERE tenant_id=$1 AND run_id=$2",
            row["tenant_id"], run_id,
        )
        if len(pending) != 1 or len(invocations) != 1:
            raise ValueError("restriction.approval_binding")
        invocation = invocations[0]
        if (
            proof["intent_binding"] != intent.binding or body["intent_binding"] != intent.binding
            or decode_authority(body["authority_json"]) != authority
            or body["tenant_id"] != context.tenant_id or body["run_id"] != run_id
            or body["thread_id"] != row["thread_id"] or body["plugin_digest"] != context.plugin_digest
            or body["step"] != 1 or batch["step_number"] != 1 or row["step_count"] != 1
            or body["ordinal"] != 0 or invocation["ordinal"] != 0 or invocation["step_number"] != 1
            or body["active_skills"] != run["active_skills"] or body["skill_allowlist"] != ceiling
            or body["tool_call"] != {"id": call["id"], "name": intent.tool_name, "arguments": intent.arguments_json}
            or call["function"] != {"name": intent.tool_name, "arguments": intent.arguments_json}
            or pending[0]["intent_proof"] != proof or not pending[0]["requires_approval"]
            or pending[0]["arguments"] != intent.arguments_json or pending[0]["name"] != intent.tool_name
            or pending[0]["id"] != call["id"] or pending[0]["queue"] != body["queue"]
            or pending[0]["allowed_secrets"] != body["allowed_secrets"]
            or str(invocation["invocation_id"]) != body["invocation_id"]
            or invocation["approval_batch_id"] != batch_uuid
            or invocation["tool_name"] != intent.tool_name or invocation["arguments"] != intent.arguments_json
            or invocation["effect_key"] != body["effect_key"] or invocation["queue"] != body["queue"]
            or invocation["arguments_digest"] != arguments_digest(intent.tool_name, intent.arguments_json)
            or invocation["tool_call_id"] != call["id"]
            or body["arguments_digest"] != invocation["arguments_digest"]
        ):
            raise ValueError("restriction.approval_binding")
        prior = await conn.fetchrow(
            "SELECT decision,decided_by,idempotency_key FROM approval_decisions "
            "WHERE tenant_id=$1 AND batch_id=$2", row["tenant_id"], batch_uuid,
        )
        if prior is not None:
            if (
                idempotency_key is None or prior["idempotency_key"] != idempotency_key
                or prior["decision"] != decision or prior["decided_by"] != actor
            ):
                raise IdempotencyConflictError("restriction.decision_conflict")
            return {"run_id": run_id, "batch_id": batch_id, "decision": decision, "replayed": True}
        if batch["status"] != "pending" or invocation["status"] != "awaiting_approval":
            raise ValueError("restriction.decision_state")
        await conn.execute(
            "INSERT INTO approval_decisions(decision_id,batch_id,tenant_id,run_id,decided_by,"
            "decision,batch_digest,idempotency_key,request_id) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            uuid4(), batch_uuid, row["tenant_id"], run_id, actor, decision, batch_digest,
            idempotency_key, request_uuid,
        )
        await conn.execute(
            "UPDATE approval_batches SET status=$2,resolved_at=now() WHERE batch_id=$1", batch_uuid, decision,
        )
        await conn.execute(
            "UPDATE tool_invocations SET status=$2 WHERE invocation_id=$1",
            invocation["invocation_id"], "approved" if approved else "denied",
        )
        await store._insert_audit_event(
            conn, tenant_id=tenant["tenant_id"], actor_principal_id=actor,
            action="run.approved" if approved else "run.rejected", target_type="run",
            target_id=run_id, request_id=request_uuid,
            details={"batch_id": batch_id, "batch_digest": batch_digest, "pending_tool_count": 1},
        )
    return {"run_id": run_id, "batch_id": batch_id, "decision": decision, "replayed": False}
