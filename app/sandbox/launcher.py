"""Sandbox launcher for executing tools via haas.sandbox/1 protocol in subprocess."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from app.core.context import RunContext
from app.core.plugins import registry
from app.core.llm import repair_tool_json
from app.core.egress import EgressPolicy, active_policy, active_meter, InMemoryEgressMeter
from pydantic import ValidationError


def main() -> None:
    """Entry point for sandbox launcher subprocess.
    
    Expects 5 command-line arguments:
    1. plugin_root (str): Path to plugin directory
    2. tool_name (str): Name of tool to execute
    3. args_json (str): JSON-encoded tool arguments
    4. secrets_json (str): JSON-encoded secrets mapping
    5. context_json (str): JSON-encoded context dict
    
    Reads nothing from stdin, writes one JSON line to stdout:
    {"abi": "haas.sandbox/1", "ok": bool, "result": any} or
    {"abi": "haas.sandbox/1", "ok": false, "error": {"code": str, "message": str}}
    """
    if len(sys.argv) != 6:
        print(json.dumps({
            "abi": "haas.sandbox/1",
            "ok": False,
            "error": {
                "code": "BAD_REQUEST",
                "message": "Expected 5 arguments: plugin_root tool_name args_json secrets_json context_json"
            }
        }))
        return

    plugin_root = Path(sys.argv[1])
    tool_name = sys.argv[2]
    args_json = sys.argv[3]
    secrets_json = sys.argv[4]
    context_json = sys.argv[5]

    try:
        # Add plugin root to path
        sys.path.insert(0, str(plugin_root))

        # Import all tool modules in the plugin's tools directory
        tools_dir = plugin_root / "tools"
        if tools_dir.is_dir():
            for py_file in tools_dir.glob("*.py"):
                if py_file.name.startswith("__"):
                    continue
                _import_tool_module(py_file)

        # Look up the tool
        tool = registry.tools.get(tool_name)
        if tool is None:
            print(json.dumps({
                "abi": "haas.sandbox/1",
                "ok": False,
                "error": {
                    "code": "NOT_FOUND",
                    "message": f"Tool '{tool_name}' not found in plugin"
                }
            }))
            return

        # Parse inputs
        args = json.loads(args_json)
        secrets = json.loads(secrets_json)
        context = json.loads(context_json)

        # Bind egress policy if provided in context
        egress_policy_data = context.get("egress_policy")
        if egress_policy_data:
            policy = EgressPolicy(
                allow_hosts=tuple(egress_policy_data.get("allow_hosts", ())),
                allow_cidrs=tuple(egress_policy_data.get("allow_cidrs", ())),
                max_bytes_per_run=egress_policy_data.get("max_bytes_per_run"),
                version=egress_policy_data.get("version", 0),
            )
            active_policy.set(policy)
            # Create a meter for this invocation
            meter = InMemoryEgressMeter()
            active_meter.set(meter)

        # Validate arguments
        try:
            validated_args = tool.args_schema.model_validate(repair_tool_json(args))
        except (json.JSONDecodeError, ValidationError) as exc:
            print(json.dumps({
                "abi": "haas.sandbox/1",
                "ok": False,
                "error": {
                    "code": "INVALID_ARGUMENTS",
                    "message": f"Schema Validation Failed: {exc}"
                }
            }))
            return

        # Create context
        ctx = RunContext(
            run_id=context.get("run_id", ""),
            thread_id=context.get("thread_id", ""),
            tenant_id=context.get("tenant_id", ""),
            traceparent=context.get("traceparent", ""),
            active_skills=set(context.get("active_skills", [])),
            skill_allowlist=set(context.get("skill_allowlist", [])) if context.get("skill_allowlist") else None,
            allowed_secrets=set(context.get("allowed_secrets", [])),
            _scoped_secrets=secrets,
            effect_key=context.get("effect_key", ""),
        )

        # Execute tool
        import inspect
        if inspect.iscoroutinefunction(tool.handler):
            import asyncio
            result = asyncio.run(
                tool.handler(ctx, validated_args) if tool.injects_context else tool.handler(validated_args)
            )
        else:
            result = tool.handler(ctx, validated_args) if tool.injects_context else tool.handler(validated_args)

        # Output result
        print(json.dumps({
            "abi": "haas.sandbox/1",
            "ok": True,
            "result": result
        }, default=str))

    except Exception as exc:
        # Output error
        print(json.dumps({
            "abi": "haas.sandbox/1",
            "ok": False,
            "error": {
                "code": "RUNTIME_ERROR",
                "message": f"{type(exc).__name__}: {exc}"
            }
        }, default=str))


def _import_tool_module(py_file: Path) -> None:
    """Import a tool module and call its register function."""
    mod_name = f"haas_plugin_sandbox_{py_file.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, py_file)
    if not (spec and spec.loader):
        raise ImportError(f"cannot create import spec for {py_file.name}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
        if not hasattr(mod, "register"):
            raise AttributeError(f"{py_file.name} has no 'register(registry)' function")
        mod.register(registry)
    except Exception:
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        raise


if __name__ == "__main__":
    main()