"""Import adapters: convert foreign tool descriptions into a manifest v2 draft.

Supported inputs:

* ``mcp`` – an MCP ``tools/list`` result (``{"tools": [...]}``) plus the server URL. Each
  tool's input schema is pinned by digest so upstream changes require re-consent.
* ``openai`` – OpenAI function/tool JSON (``{"type": "function", "function": {...}}``,
  a bare function object, or a list of either).
* ``skill`` – an Agent Skill ``SKILL.md`` with YAML-ish front matter (``name``/``description``).

Adapters never guess permissions upward: risk comes from explicit hints only, and the
result is still validated by :func:`app.marketplace.manifest.parse_manifest`.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from app.marketplace.canonical import digest_json
from app.marketplace.manifest import (
    NAME_RE,
    TOOL_NAME_RE,
    ManifestError,
    ParsedManifest,
    normalize_origin,
    parse_manifest,
)

FORMATS = ("mcp", "openai", "skill")


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:64] or "plugin"


def _tool_name(value: Any) -> str:
    name = str(value or "")
    if not TOOL_NAME_RE.fullmatch(name):
        raise ManifestError([f"tool name '{name}' is not a valid HaaS tool name"])
    return name


def _mcp_risk(annotations: dict[str, Any]) -> str:
    if annotations.get("readOnlyHint") is True:
        return "read"
    if annotations.get("destructiveHint") is True:
        return "destructive"
    if annotations.get("openWorldHint") is True:
        return "exfiltration-capable"
    return "write"


def _base(namespace: str, name: str, version: str, description: str) -> dict[str, Any]:
    if not NAME_RE.fullmatch(namespace):
        raise ManifestError([f"namespace '{namespace}' is invalid"])
    return {
        "manifest_version": 2,
        "id": f"@{namespace}/{_slug(name)}",
        "version": version,
        "name": name,
        "description": description,
    }


def from_mcp(
    document: Any,
    *,
    namespace: str,
    name: str,
    server_url: str,
    version: str = "0.1.0",
    description: str | None = None,
    protocol_version: str | None = None,
) -> dict[str, Any]:
    tools = document.get("tools") if isinstance(document, dict) else document
    if not isinstance(tools, list) or not tools:
        raise ManifestError(["MCP document must contain a non-empty 'tools' list"])
    specs: dict[str, Any] = {}
    digests: dict[str, str] = {}
    for tool in tools:
        if not isinstance(tool, dict):
            raise ManifestError(["every MCP tool must be an object"])
        tool_name = _tool_name(tool.get("name"))
        schema = tool.get("inputSchema") or {"type": "object"}
        annotations = tool.get("annotations") or {}
        risk = _mcp_risk(annotations if isinstance(annotations, dict) else {})
        specs[tool_name] = {
            "risk": risk,
            "approval": "policy" if risk == "read" else "required",
            "input_schema": schema,
            **({"description": str(tool["description"])[:2000]} if tool.get("description") else {}),
        }
        digests[tool_name] = digest_json(
            {"name": tool_name, "description": tool.get("description"), "inputSchema": schema}
        )
    try:
        parts = urlsplit(server_url)
        origin = normalize_origin(f"{parts.scheme}://{parts.netloc}")
    except ValueError as exc:
        raise ManifestError([f"server url: {exc}"]) from exc
    manifest = _base(
        namespace, name, version, description or f"MCP server {name} imported from {origin}."
    )
    manifest.update(
        {
            "provides": {"tools": sorted(specs)},
            "tools": specs,
            "permissions": {"egress": [origin], "sandbox_profile": "network-restricted"},
            "remote": {
                "protocol": "mcp",
                "url": server_url,
                "tool_schema_digests": digests,
                **({"protocol_version": protocol_version} if protocol_version else {}),
            },
        }
    )
    return manifest


def from_openai(
    document: Any,
    *,
    namespace: str,
    name: str,
    version: str = "0.1.0",
    description: str | None = None,
) -> dict[str, Any]:
    items = document if isinstance(document, list) else [document]
    if isinstance(document, dict) and isinstance(document.get("tools"), list):
        items = document["tools"]
    specs: dict[str, Any] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ManifestError(["every OpenAI tool must be an object"])
        function = item.get("function") if item.get("type") == "function" else item
        if not isinstance(function, dict):
            raise ManifestError(["OpenAI tool is missing its 'function' object"])
        tool_name = _tool_name(function.get("name"))
        specs[tool_name] = {
            # OpenAI function JSON carries no side-effect hints; assume write + approval.
            "risk": "write",
            "approval": "required",
            "input_schema": function.get("parameters") or {"type": "object"},
            **({"description": str(function["description"])[:2000]} if function.get("description") else {}),
        }
    if not specs:
        raise ManifestError(["no OpenAI functions found"])
    manifest = _base(
        namespace, name, version, description or f"{name}: tools imported from OpenAI function JSON."
    )
    manifest.update({"provides": {"tools": sorted(specs)}, "tools": specs})
    return manifest


_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)


def from_skill(
    text: str, *, namespace: str, version: str = "0.1.0"
) -> dict[str, Any]:
    match = _FRONT_MATTER.match(text)
    if not match:
        raise ManifestError(["SKILL.md must start with '---' front matter"])
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip().strip("'\"")
    skill = fields.get("name", "")
    if not NAME_RE.fullmatch(skill):
        raise ManifestError([f"skill name '{skill}' must be lowercase-hyphenated"])
    manifest = _base(namespace, skill, version, fields.get("description", ""))
    manifest["provides"] = {"skills": [skill]}
    if fields.get("license"):
        manifest["license"] = fields["license"][:64]
    return manifest


def convert(
    fmt: str,
    document: Any,
    *,
    namespace: str,
    name: str | None = None,
    version: str = "0.1.0",
    server_url: str | None = None,
    description: str | None = None,
) -> ParsedManifest:
    if fmt == "mcp":
        if not server_url or not name:
            raise ManifestError(["mcp import requires name and server_url"])
        raw = from_mcp(
            document,
            namespace=namespace,
            name=name,
            server_url=server_url,
            version=version,
            description=description,
        )
    elif fmt == "openai":
        if not name:
            raise ManifestError(["openai import requires name"])
        raw = from_openai(
            document, namespace=namespace, name=name, version=version, description=description
        )
    elif fmt == "skill":
        if not isinstance(document, str):
            raise ManifestError(["skill import expects SKILL.md text"])
        raw = from_skill(document, namespace=namespace, version=version)
    else:
        raise ManifestError([f"unknown import format '{fmt}'; expected one of {list(FORMATS)}"])
    return parse_manifest(raw, namespace=namespace)
