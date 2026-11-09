"""Internal held-intent creation; never enables admission, approval or dispatch."""

from __future__ import annotations

import json
import inspect
import hashlib
import re
from typing import Any
from uuid import UUID
from urllib.parse import urlsplit

from app.core.context import validate_safe_outbound_url
from app.core.intent_preflight import IntentPreflight, preflight_exact_intent
from app.core.plugins import registry
from app.core.run_restrictions import ExactIntent, IntentContext, RunAuthority, canonical_arguments
from app.core.tenant_policy import TenantPolicy
from app.core.agents import AgentConfiguration


def recheck_preflight(
    snapshot: IntentPreflight, policy: TenantPolicy, *, skill_ceiling: list[str] | None,
) -> None:
    # NULL means policy-derived ceiling, not the same authority as an empty list.
    # Compare before policy/registry work; no caller ceiling may replace the pin.
    stored_ceiling = tuple(sorted(skill_ceiling)) if skill_ceiling is not None else None
    if stored_ceiling != snapshot.skill_ceiling:
        raise ValueError("restriction.skill_ceiling_changed")
    current = preflight_exact_intent(
        tool_name=snapshot.tool_name, arguments=json.loads(snapshot.arguments_json),
        tools=(snapshot.tool_name,), preload_skills=snapshot.preload_skills,
        skill_ceiling=snapshot.skill_ceiling,
        tenant_id=snapshot.tenant_id, authenticated_submitter_id=snapshot.submitter_id,
        policy=policy, registry=registry,
    )
    if current != snapshot:
        raise ValueError("restriction.revision_changed")


def destination_binding(snapshot: IntentPreflight, policy: TenantPolicy) -> str:
    tool = registry.tools.get(snapshot.tool_name)
    if tool is None:
        raise ValueError("restriction.destination_unavailable")
    if not tool.endpoint_url:
        if (
            tool.endpoint_protocol or tool.execution not in ("plugin", "inprocess")
            or tool.plugin_id != snapshot.plugin_id
            or not inspect.isfunction(tool.handler)
            or not tool.handler.__module__ or not tool.handler.__qualname__
        ):
            raise ValueError("restriction.destination_unavailable")
        # Identity of the registered call target, not a claim about URLs that
        # plugin code may use. Runtime egress/SSRF/credential policy still applies.
        return canonical_arguments({
            "version": 1, "kind": "native", "plugin_id": snapshot.plugin_id,
            "plugin_digest": snapshot.plugin_digest, "tool_name": tool.name,
            "module": tool.handler.__module__, "qualname": tool.handler.__qualname__,
            "execution": tool.execution, "queue": tool.queue,
            "injects_context": tool.injects_context, "schema_sha256": snapshot.schema_sha256,
        })
    if tool.endpoint_protocol not in ("http", "mcp_http"):
        raise ValueError("restriction.destination_unavailable")
    parsed = urlsplit(tool.endpoint_url)
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("restriction.destination_unavailable")
    validate_safe_outbound_url(tool.endpoint_url)
    if not policy.destination_is_allowed(tool.endpoint_url):
        raise ValueError("restriction.destination_denied")
    # Actual endpoint adapters omit None and use field names, not aliases.
    # Reject any mismatch instead of changing legacy adapter behavior.
    args = tool.args_schema.model_validate(json.loads(snapshot.arguments_json), strict=True)
    if canonical_arguments(args.model_dump(mode="json", exclude_none=True)) != snapshot.arguments_json:
        raise ValueError("restriction.adapter_arguments_changed")
    return canonical_arguments({
        "version": 1, "url": tool.endpoint_url, "protocol": tool.endpoint_protocol,
        "queue": tool.queue, "schema_sha256": snapshot.schema_sha256,
    })


def held_request_digest(snapshot: IntentPreflight, fields: dict[str, Any], destination: str) -> str:
    """Server-owned replay identity; assigned run ID is not a request parameter."""
    return hashlib.sha256(canonical_arguments({
        "domain": "haas.held-intent-admission.v1",
        "snapshot": snapshot.model_dump(mode="json"),
        "destination": destination,
        "request": {key: value for key, value in fields.items() if key != "run_id"},
    }).encode()).hexdigest()


def validate_held_key(key: str) -> None:
    if not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9._~:-]{1,255}", key) is None:
        raise ValueError("restriction.idempotency_key")


def bind_held_intent(
    snapshot: IntentPreflight, *, run_id: str, destination: str, idempotency_key: str | None = None,
) -> RunAuthority:
    return RunAuthority(tools=(snapshot.tool_name,), intent=ExactIntent(
        tool_name=snapshot.tool_name, arguments_json=snapshot.arguments_json,
        context=IntentContext(
            tenant_id=snapshot.tenant_id, submitter_id=snapshot.submitter_id,
            run_id=run_id, policy_revision=snapshot.policy_revision_id,
            registry_fingerprint=snapshot.registry_fingerprint,
            plugin_id=snapshot.plugin_id, plugin_digest=snapshot.plugin_digest,
            destination=destination, idempotency_key=idempotency_key,
        ),
    ))


async def validate_held_creation(
    conn: Any, snapshot: IntentPreflight, authority: RunAuthority,
    *, skill_ceiling: list[str] | None,
    agent_revision_id: str | None = None, agent_fields: dict[str, Any] | None = None,
) -> None:
    """Caller holds active tenant-policy lock until run INSERT commits."""
    record = await conn.fetchrow(
        "SELECT policy_revision_id,version,digest,rules FROM tenant_policy_revisions "
        "WHERE tenant_id=$1 AND policy_revision_id=$2",
        UUID(snapshot.tenant_id), UUID(snapshot.policy_revision_id),
    )
    if record is None:
        raise ValueError("restriction.policy_unavailable")
    eligible = await conn.fetchrow(
        "SELECT m.principal_id FROM memberships m JOIN principals p USING(principal_id) "
        "WHERE m.tenant_id=$1 AND m.principal_id=$2 AND m.status='active' "
        "AND p.status='active' AND m.role IN ('tenant_admin','submitter') FOR SHARE OF m,p",
        UUID(snapshot.tenant_id), UUID(snapshot.submitter_id),
    )
    if not eligible:
        raise ValueError("restriction.submitter_unavailable")
    policy = TenantPolicy.from_record(dict(record))
    if agent_revision_id is not None:
        agent = await conn.fetchrow(
            "SELECT configuration,digest FROM agent_revisions WHERE tenant_id=$1 "
            "AND revision_id=$2 AND state='published' AND policy_revision_id=$3 "
            "AND registry_fingerprint=$4 FOR SHARE",
            UUID(snapshot.tenant_id), UUID(agent_revision_id),
            UUID(snapshot.policy_revision_id), snapshot.registry_fingerprint,
        )
        if agent is None:
            raise ValueError("restriction.agent_unavailable")
        raw = agent["configuration"]
        config = AgentConfiguration.model_validate(json.loads(raw) if isinstance(raw, str) else raw)
        if config.digest() != agent["digest"]:
            raise ValueError("restriction.agent_integrity")
        config.validate_policy(policy, registry)
        if (
            skill_ceiling is None or set(skill_ceiling) != set(config.skills)
            or snapshot.preload_skills != tuple(config.preload_skills)
            or agent_fields != {
                name: getattr(config, name)
                for name in ("primary_model", "planner_model", "max_steps", "max_cost_usd")
            }
        ):
            raise ValueError("restriction.agent_override")
    recheck_preflight(snapshot, policy, skill_ceiling=skill_ceiling)
    expected = bind_held_intent(
        snapshot, run_id=authority.intent.context.run_id if authority.intent else "",
        destination=destination_binding(snapshot, policy),
        idempotency_key=authority.intent.context.idempotency_key if authority.intent else None,
    )
    if expected != authority:
        raise ValueError("restriction.binding_changed")
