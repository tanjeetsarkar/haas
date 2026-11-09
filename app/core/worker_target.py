"""Recheck immutable registered target identity without DB access or authority minting."""

import hashlib
import inspect
import json

from app.core.context import validate_safe_outbound_url
from app.core.plugins import PluginRegistry, RegisteredTool
from app.core.run_restrictions import ExactIntent, canonical_arguments


def validate_worker_target(intent: ExactIntent, tool: RegisteredTool, registry: PluginRegistry) -> None:
    schema = json.dumps(
        tool.args_schema.model_json_schema(by_alias=True), sort_keys=True,
        separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    context = intent.context
    if tool.endpoint_url:
        from urllib.parse import urlsplit
        parsed = urlsplit(tool.endpoint_url)
        if (
            tool.endpoint_protocol not in ("http", "mcp_http")
            or parsed.username is not None or parsed.password is not None or parsed.fragment
        ):
            raise ValueError("restriction.target_changed")
        validate_safe_outbound_url(tool.endpoint_url)
        args = tool.args_schema.model_validate_json(intent.arguments_json, strict=True)
        if canonical_arguments(args.model_dump(mode="json", exclude_none=True)) != intent.arguments_json:
            raise ValueError("restriction.target_changed")
        destination = {
            "version": 1, "url": tool.endpoint_url, "protocol": tool.endpoint_protocol,
            "queue": tool.queue, "schema_sha256": hashlib.sha256(schema).hexdigest(),
        }
    else:
        if (
            tool.endpoint_protocol or tool.execution not in ("plugin", "inprocess")
            or not inspect.isfunction(tool.handler)
            or not tool.handler.__module__ or not tool.handler.__qualname__
        ):
            raise ValueError("restriction.target_changed")
        destination = {
            "version": 1, "kind": "native", "plugin_id": tool.plugin_id,
            "plugin_digest": context.plugin_digest, "tool_name": tool.name,
            "module": tool.handler.__module__, "qualname": tool.handler.__qualname__,
            "execution": tool.execution, "queue": tool.queue,
            "injects_context": tool.injects_context, "schema_sha256": hashlib.sha256(schema).hexdigest(),
        }
    if (
        registry.tools.get(intent.tool_name) is not tool
        or canonical_arguments(destination) != context.destination
    ):
        raise ValueError("restriction.target_changed")
