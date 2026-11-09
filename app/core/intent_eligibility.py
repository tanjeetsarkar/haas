"""Live read-only eligibility evidence; never a release or execution permit."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from uuid import UUID

from app.core.deterministic_intent import intent_batch_digest
from app.core.envelope import arguments_digest, verify_envelope
from app.core.held_intent import validate_held_creation
from app.core.intent_preflight import IntentPreflight
from app.core.plugins import registry
from app.core.run_restrictions import decode_authority, validate_intent_provenance
from app.core.workflow import decode_run, effect_key


async def validate_dispatch_eligibility(store: Any, conn: Any, check: Any) -> None:
    """Caller owns tenant-context transaction. Success is evidence, NOT permission.

    Lock order follows admission: tenant, thread/run, current principals. No
    grants, secrets, writes, release switch or worker-controlled state changes.
    """
    tenant = await conn.fetchrow(
        "SELECT tenant_id,active_policy_revision_id FROM tenants "
        "WHERE tenant_id=$1 AND status='active' FOR SHARE", UUID(check.tenant_id),
    )
    row = await store._lock_run(conn, check.run_id, check.tenant_id) if tenant else None
    if row is None:
        raise ValueError("restriction.denied")
    authority = decode_authority(row["authority_json"])
    if authority is None or authority.intent is None:
        raise ValueError("restriction.denied")
    intent, run = authority.intent, decode_run(row)
    context, now = intent.context, time.time()
    if (
        row["status"] != "running" or row["next_action"] != "none"
        or row["phase_config_json"] is not None or row["phase_state_json"] is not None
        or row["step_count"] != 1
        or row["deadline_at"] is not None and row["deadline_at"].timestamp() <= now
        or str(tenant["active_policy_revision_id"]) != context.policy_revision
        or check.intent_binding != intent.binding or check.tool_name != intent.tool_name
        or check.policy_revision_id != context.policy_revision
        or check.registry_fingerprint != context.registry_fingerprint
        or check.plugin_digest != context.plugin_digest
        or check.arguments_sha256 != hashlib.sha256(intent.arguments_json.encode()).hexdigest()
    ):
        raise ValueError("restriction.denied")
    validate_intent_provenance(
        authority, tenant_id=check.tenant_id, run_id=check.run_id,
        submitter_id=str(row["submitted_by_principal_id"]),
        policy_revision=str(row["policy_revision_id"]), registry_fingerprint=row["registry_fingerprint"],
        idempotency_key=context.idempotency_key,
    )
    record = await conn.fetchrow(
        "SELECT digest FROM tenant_policy_revisions WHERE tenant_id=$1 AND policy_revision_id=$2",
        UUID(check.tenant_id), UUID(context.policy_revision),
    )
    if record is None:
        raise ValueError("restriction.denied")
    ceiling = run["skill_allowlist"]
    await validate_held_creation(
        conn, IntentPreflight(
            tenant_id=context.tenant_id, submitter_id=context.submitter_id,
            tool_name=intent.tool_name, arguments_json=intent.arguments_json,
            schema_sha256=json.loads(context.destination)["schema_sha256"],
            policy_revision_id=context.policy_revision, policy_digest=record["digest"],
            registry_fingerprint=context.registry_fingerprint, plugin_id=context.plugin_id,
            plugin_digest=context.plugin_digest, preload_skills=tuple(run["active_skills"]),
            skill_ceiling=tuple(sorted(ceiling)) if ceiling is not None else None,
        ), authority, skill_ceiling=ceiling,
        agent_revision_id=str(row["agent_revision_id"]) if row["agent_revision_id"] else None,
        agent_fields={key: float(row[key]) if key == "max_cost_usd" else row[key] for key in (
            "primary_model", "planner_model", "max_steps", "max_cost_usd",
        )},
    )
    invocations = await conn.fetch(
        "SELECT * FROM tool_invocations WHERE tenant_id=$1 AND run_id=$2 FOR SHARE",
        row["tenant_id"], check.run_id,
    )
    if len(invocations) != 1:
        raise ValueError("restriction.denied")
    invocation = invocations[0]
    batch = await conn.fetchrow(
        "SELECT * FROM approval_batches WHERE tenant_id=$1 AND run_id=$2 AND batch_id=$3 FOR SHARE",
        row["tenant_id"], check.run_id, invocation["approval_batch_id"],
    )
    decision = await conn.fetchrow(
        "SELECT d.* FROM approval_decisions d "
        "JOIN memberships m ON m.tenant_id=$1 AND m.principal_id=d.decided_by "
        "JOIN principals p ON p.principal_id=d.decided_by "
        "WHERE d.tenant_id=$2 AND d.run_id=$3 AND d.batch_id=$4 "
        "AND m.role='approver' AND m.status='active' AND p.status='active' FOR SHARE OF m,p",
        UUID(check.tenant_id), row["tenant_id"], check.run_id, invocation["approval_batch_id"],
    )
    if (
        batch is None or decision is None or batch["status"] != "approved"
        or decision["decision"] != "approved" or str(decision["decided_by"]) == context.submitter_id
        or batch["expires_at"].timestamp() <= now or batch["step_number"] != 1
        or decision["batch_digest"] != batch["batch_digest"]
    ):
        raise ValueError("restriction.denied")
    calls = run["pending_tool_calls"]
    if intent_batch_digest(check.run_id, 1, calls) != batch["batch_digest"]:
        raise ValueError("restriction.denied")
    call, pending = calls[0], json.loads(batch["tool_calls"])
    proof = call["intent_proof"]
    body = verify_envelope(proof["envelope"])
    secrets = sorted(registry.resolve_allowed_secrets(set(run["active_skills"]), ceiling))
    if (
        len(pending) != 1 or pending[0]["intent_proof"] != proof
        or proof["intent_binding"] != intent.binding or body["intent_binding"] != intent.binding
        or decode_authority(body["authority_json"]) != authority
        or body["tenant_id"] != check.tenant_id or body["run_id"] != check.run_id
        or body["thread_id"] != row["thread_id"] or body["expires_at"] != check.envelope_expires_at
        or row["deadline_at"] is not None and body["expires_at"] > row["deadline_at"].timestamp()
        or body["plugin_digest"] != context.plugin_digest
        or body["active_skills"] != run["active_skills"] or body["skill_allowlist"] != ceiling
        or body["allowed_secrets"] != secrets or pending[0]["allowed_secrets"] != secrets
        or body["step"] != 1 or body["ordinal"] != 0
        or body["queue"] != registry.tools[intent.tool_name].queue
        or body["effect_key"] != effect_key(row["tenant_id"], check.run_id, 1, 0, call["id"])
        or body["invocation_id"] != check.invocation_id
        or str(invocation["invocation_id"]) != check.invocation_id
        or invocation["status"] != "dispatched" or invocation["step_number"] != 1
        or invocation["ordinal"] != 0 or invocation["tool_call_id"] != call["id"]
        or invocation["tool_name"] != intent.tool_name or invocation["arguments"] != intent.arguments_json
        or invocation["effect_key"] != body["effect_key"] or invocation["queue"] != body["queue"]
        or invocation["arguments_digest"] != arguments_digest(intent.tool_name, intent.arguments_json)
        or body["arguments_digest"] != invocation["arguments_digest"]
        or body["tool_call"] != {"id": call["id"], "name": intent.tool_name, "arguments": intent.arguments_json}
        or call["function"] != {"name": intent.tool_name, "arguments": intent.arguments_json}
        or pending[0]["id"] != call["id"] or pending[0]["name"] != intent.tool_name
        or pending[0]["arguments"] != intent.arguments_json or pending[0]["queue"] != body["queue"]
        or not pending[0]["requires_approval"]
    ):
        raise ValueError("restriction.denied")
