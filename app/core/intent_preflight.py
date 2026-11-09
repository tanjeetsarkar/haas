"""Pure exact-argument eligibility snapshot, not dispatch authority or a release gate."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from uuid import UUID

from app.core.plugins import PluginRegistry
from app.core.run_restrictions import FrozenModel, RunAuthority, validate_exact_arguments
from app.core.tenant_policy import TenantPolicy, policy_digest


class IntentPreflightError(ValueError):
    """Non-sensitive admission error with an eventual API status mapping."""

    def __init__(self, code: str, status_code: int):
        super().__init__(code)
        self.status_code = status_code


class IntentPreflight(FrozenModel):
    """Immutable bytes/revisions to recheck when constructing run-owned intent."""

    tenant_id: str
    submitter_id: str
    tool_name: str
    arguments_json: str
    schema_sha256: str
    policy_revision_id: str
    policy_digest: str
    registry_fingerprint: str
    plugin_id: str
    plugin_digest: str
    preload_skills: tuple[str, ...]
    skill_ceiling: tuple[str, ...] | None


def preflight_exact_intent(
    *, tool_name: str, arguments: object, tools: tuple[str, ...] | None,
    preload_skills: tuple[str, ...], skill_ceiling: Collection[str] | None,
    tenant_id: str, authenticated_submitter_id: str | None,
    policy: TenantPolicy, registry: PluginRegistry,
    parent_authority: RunAuthority | None = None,
) -> IntentPreflight:
    """Validate trusted pinned policy/agent inputs without running model or tool code.

    Does not resolve destinations, create ExactIntent, schedule work or approve it.
    Tenant/submitter come from authenticated admission, never the request body.
    """
    if not authenticated_submitter_id:
        raise IntentPreflightError("restriction.authentication_required", 401)
    if parent_authority is not None:
        raise IntentPreflightError("restriction.derivation_unavailable", 403)
    try:
        if (
            type(tool_name) is not str or not tool_name
            or type(tools) is not tuple or tools != (tool_name,)
            or type(preload_skills) is not tuple
            or any(type(name) is not str or not name for name in preload_skills)
            or len(set(preload_skills)) != len(preload_skills)
        ):
            raise ValueError()
        RunAuthority(tools=tools)
        if skill_ceiling is not None and (
            isinstance(skill_ceiling, (str, bytes))
            or any(type(name) is not str or not name for name in skill_ceiling)
            or len(set(skill_ceiling)) != len(skill_ceiling)
        ):
            raise ValueError()
        for value in (tenant_id, authenticated_submitter_id, policy.policy_revision_id):
            if str(UUID(value)) != value:
                raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise IntentPreflightError("restriction.invalid_intent", 422) from None
    if policy.digest != policy_digest(policy.rules) or not registry.fingerprint:
        raise IntentPreflightError("restriction.revision_changed", 409)
    access = policy.resolve_plugin_access(registry)
    pinned_ceiling = tuple(sorted(skill_ceiling)) if skill_ceiling is not None else None
    permitted = (
        access.skill_names if pinned_ceiling is None
        else access.skill_names.intersection(pinned_ceiling)
    )
    if not set(preload_skills).issubset(permitted):
        raise IntentPreflightError("restriction.policy_denied", 403)
    if any(not registry.skills[name].ready for name in preload_skills):
        raise IntentPreflightError("restriction.policy_denied", 403)
    unlocked = registry.resolve_unlocked_tool_names(set(preload_skills), permitted)
    tool = registry.tools.get(tool_name)
    if tool is None or tool_name not in access.tool_names or tool_name not in unlocked:
        raise IntentPreflightError("restriction.policy_denied", 403)
    plugin = registry.plugins.get(tool.plugin_id)
    if plugin is None or plugin.status != "loaded" or tool.plugin_id not in access.plugin_ids:
        raise IntentPreflightError("restriction.policy_denied", 403)
    if tool.execution == "static" or tool.parameters_schema is not None:
        raise IntentPreflightError("restriction.schema_unavailable", 409)
    try:
        canonical = validate_exact_arguments(arguments, tool.args_schema)
        schema = json.dumps(
            tool.args_schema.model_json_schema(by_alias=True),
            sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise IntentPreflightError("restriction.arguments", 422) from None
    return IntentPreflight(
        tenant_id=tenant_id, submitter_id=authenticated_submitter_id,
        tool_name=tool_name, arguments_json=canonical,
        schema_sha256=hashlib.sha256(schema).hexdigest(),
        policy_revision_id=policy.policy_revision_id, policy_digest=policy.digest,
        registry_fingerprint=registry.fingerprint, plugin_id=tool.plugin_id,
        plugin_digest=plugin.digest, preload_skills=preload_skills,
        skill_ceiling=pinned_ceiling,
    )
