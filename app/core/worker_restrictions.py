"""Exact signed intent checks at worker boundaries; never derive authority from prompts."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel

from app.core.config import settings
from app.core.envelope import arguments_digest
from app.core.plugins import PluginRegistry, RegisteredTool
from app.core.run_restrictions import canonical_arguments, decode_authority, validate_exact_arguments
from app.core.worker_authorization import WorkerAuthorizationError, check_worker_authorization
from app.core.worker_target import validate_worker_target


def _unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


async def authorize_worker_intent(
    body: dict[str, Any], tool: RegisteredTool | None, registry: PluginRegistry,
    operation: Literal["pre_grant", "final_entry"], *, name: str, raw: str,
    validated: BaseModel | None = None,
) -> BaseModel:
    """Return exact arguments only after live authorization; all errors are generic."""
    try:
        authority = decode_authority(body["authority_json"])
        if authority is None or authority.intent is None or tool is None:
            raise ValueError()
        intent, call = authority.intent, body["tool_call"]
        context = intent.context
        # A catalog-only placeholder is not an executable argument validator.
        if tool.parameters_schema is not None or tool.execution == "static":
            raise ValueError()
        arguments = json.loads(raw, object_pairs_hook=_unique)
        canonical = validate_exact_arguments(arguments, tool.args_schema)
        plugin = registry.plugins.get(tool.plugin_id)
        if (
            registry.tools.get(name) is not tool or tool.name != name
            or call["name"] != name or call["arguments"] != raw
            or arguments_digest(name, raw) != body["arguments_digest"]
            or not intent.matches(name, arguments)
            or context.tenant_id != body["tenant_id"] or context.run_id != body["run_id"]
            or plugin is None or context.plugin_id != tool.plugin_id
            or context.plugin_digest != plugin.digest or body["plugin_digest"] != plugin.digest
            or context.registry_fingerprint != registry.fingerprint
            or name not in registry.resolve_unlocked_tool_names(
                set(body.get("active_skills") or []), body.get("skill_allowlist"),
            )
        ):
            raise ValueError()
        exact = tool.args_schema.model_validate(arguments, strict=True)
        validate_worker_target(intent, tool, registry)
        expected_secrets = sorted(registry.resolve_allowed_secrets(
            set(body.get("active_skills") or []), body.get("skill_allowlist"),
        ))
        if body.get("allowed_secrets") != expected_secrets or body.get("queue") != tool.queue:
            raise ValueError()
        if validated is not None and canonical_arguments(
            validated.model_dump(mode="json", by_alias=True)
        ) != canonical:
            raise ValueError()
        binding = {
            "version": 1, "tenant_id": context.tenant_id, "run_id": context.run_id,
            "invocation_id": body["invocation_id"], "tool_name": name,
            "arguments_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            "intent_binding": intent.binding, "policy_revision_id": context.policy_revision,
            "registry_fingerprint": context.registry_fingerprint,
            "plugin_digest": plugin.digest, "envelope_expires_at": body["expires_at"],
        }
        await check_worker_authorization(settings, binding, operation)
        # RPC awaits network I/O; no mutable target/credential change may cross
        # that boundary even when the response itself is authenticated.
        validate_worker_target(intent, tool, registry)
        if (
            body.get("allowed_secrets") != sorted(registry.resolve_allowed_secrets(
                set(body.get("active_skills") or []), body.get("skill_allowlist"),
            ))
            or body.get("queue") != tool.queue
            or registry.plugins.get(tool.plugin_id) is not plugin
            or plugin.digest != context.plugin_digest
            or registry.fingerprint != context.registry_fingerprint
        ):
            raise ValueError()
        if validated is not None and canonical_arguments(
            validated.model_dump(mode="json", by_alias=True)
        ) != canonical:
            raise ValueError()
        return exact
    except Exception:
        raise WorkerAuthorizationError("restriction.authorization_denied") from None
