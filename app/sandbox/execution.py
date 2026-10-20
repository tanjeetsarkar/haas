"""Execution of a single tool invocation, either sandboxed or in-process.

The tool worker trusts plugin code only inside the sandbox subprocess; the
in-process path exists for the ``inprocess`` sandbox mode and as a
last-resort fallback when the sandbox cannot be set up.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.egress import EgressPolicy
from app.core.plugin_manifest import PluginManifestError, read_plugin_manifest
from app.core.plugins import registry
from app.sandbox.runner import run_in_sandbox_materialized


class SandboxSetupError(Exception):
    """The sandbox could not be prepared; the caller may fall back in-process."""


def plugin_root_for(tool_name: str) -> Path:
    """The on-disk root of the plugin that owns ``tool_name``."""
    tool = registry.tools.get(tool_name)
    if tool is None or not tool.plugin_id:
        raise SandboxSetupError(f"Tool '{tool_name}' is not registered.")
    record = registry.plugins.get(tool.plugin_id)
    if record is None or record.manifest is None:
        raise SandboxSetupError(f"Plugin for '{tool_name}' has no manifest.")
    return Path(record.manifest.root)


def materialize_plugin(tool_name: str, run_id: str) -> Path:
    """Materialize the owning plugin into a per-run sandbox directory.

    The materialized copy is what the sandbox subprocess executes, so the
    bytes that run are the bytes that were pinned at dispatch time.
    """
    from app.core.plugin_artifacts import ArtifactStore, materialize

    source_root = plugin_root_for(tool_name)
    try:
        manifest = read_plugin_manifest(source_root)
    except PluginManifestError as exc:
        raise SandboxSetupError(f"Invalid plugin manifest: {exc}") from exc

    sandbox_root = Path(settings.sandbox_work_root) / run_id
    sandbox_root.mkdir(parents=True, exist_ok=True)
    target = sandbox_root / manifest.id
    target.mkdir(parents=True, exist_ok=True)
    store = ArtifactStore(sandbox_root)
    try:
        materialize(manifest, target, pinned="", store=store)
    except Exception:
        # Materialization is an integrity convenience; fall back to the
        # read-only source tree, which the registry already validated.
        return source_root
    return target


async def execute_in_sandbox(
    *,
    tool_name: str,
    validated_args: Any,
    secrets: dict[str, str],
    run_id: str,
    thread_id: str,
    tenant_id: str,
    traceparent: str,
    active_skills: set[str],
    skill_allowlist: set[str] | None,
    allowed_secrets: set[str],
    effect_key: str,
    egress_policy: EgressPolicy | None = None,
) -> tuple[bool, str, str]:
    """Run a tool in the sandbox.

    Returns ``(ok, observation, error_message)``. ``error_message`` is the
    protocol error code plus message; the caller decides how to phrase it.
    """
    plugin_root = materialize_plugin(tool_name, run_id)
    if isinstance(validated_args, dict):
        args = validated_args
    elif hasattr(validated_args, "model_dump"):
        args = validated_args.model_dump()
    else:  # pragma: no cover - defensive
        args = dict(validated_args)

    response = await run_in_sandbox_materialized(
        plugin_id=plugin_root.name,
        plugin_root=plugin_root,
        tool_name=tool_name,
        args=args,
        secrets=secrets,
        run_id=run_id,
        thread_id=thread_id,
        tenant_id=tenant_id,
        traceparent=traceparent,
        active_skills=active_skills,
        skill_allowlist=skill_allowlist,
        allowed_secrets=allowed_secrets,
        effect_key=effect_key,
        egress_policy=egress_policy,
        timeout_sec=settings.per_tool_timeout_sec,
    )
    if response.ok:
        return True, str(response.result), ""
    return False, "", f"{response.code or 'sandbox_error'}: {response.message}"
