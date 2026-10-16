from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.metadata
import importlib.util
import inspect
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml
from pydantic import BaseModel, Field, create_model

from app.core.context import (
    RunContext,
    ToolRemediationError,
    validate_safe_outbound_url,
)

log = logging.getLogger("haas.plugins")

VALID_QUEUES = ("q_tools_fast", "q_tools_sandbox")
MCP_PROTOCOL_VERSION = "2026-07-28"
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)\Z", re.DOTALL
)


@dataclass
class RegisteredTool:
    name: str
    description: str
    args_schema: type[BaseModel]
    handler: Callable[..., Awaitable[Any] | Any]
    injects_context: bool
    requires_approval: bool = False
    is_core: bool = False
    queue: str = "q_tools_fast"  # "q_tools_fast" or "q_tools_sandbox"

    def to_openai_schema(self) -> dict[str, Any]:
        schema = self.args_schema.model_json_schema()
        schema.pop("title", None)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }


@dataclass
class RegisteredSkill:
    name: str
    description: str
    instructions_markdown: str
    root_dir: Path
    version: str = "1.0.0"
    required_tools: list[str] = field(default_factory=list)
    allowed_secrets: list[str] = field(default_factory=list)
    available_references: list[str] = field(default_factory=list)


def _as_list(value: Any) -> list[str]:
    """Agent Skills `metadata` values are strings, so lists are space/comma separated."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p for p in re.split(r"[\s,]+", str(value)) if p]


def _expand_env(value: str) -> str:
    """Expands `${VAR}` / `${VAR:-default}` so manifests can be environment-specific."""

    def repl(m: re.Match[str]) -> str:
        return os.environ.get(m.group(1)) or (
            m.group(2) if m.group(2) is not None else ""
        )

    return re.sub(r"\$\{(\w+)(?::-([^}]*))?\}", repl, value)


def _parse_mcp_response(resp_text: str, content_type: str) -> dict[str, Any]:
    """Streamable HTTP servers answer with either a JSON object or an SSE stream."""
    if "text/event-stream" in content_type:
        last: dict[str, Any] | None = None
        for line in resp_text.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk:
                    msg = json.loads(chunk)
                    if "result" in msg or "error" in msg:
                        last = msg
        if last is None:
            raise ToolRemediationError(
                "MCP server returned an SSE stream without a JSON-RPC response."
            )
        return last
    return json.loads(resp_text)


class PluginRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, RegisteredTool] = {}
        self.skills: dict[str, RegisteredSkill] = {}

    def tool(
        self,
        *,
        name: str,
        description: str,
        args_schema: type[BaseModel],
        requires_approval: bool = False,
        is_core: bool = False,
        sandbox: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            sig = inspect.signature(fn)
            self.tools[name] = RegisteredTool(
                name=name,
                description=description,
                args_schema=args_schema,
                handler=fn,
                injects_context="ctx" in sig.parameters,
                requires_approval=requires_approval,
                is_core=is_core,
                queue="q_tools_sandbox" if sandbox else "q_tools_fast",
            )
            return fn

        return decorator

    def load_skill_directory(self, skill_dir: Path) -> None:
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            return
        raw = skill_md.read_text(encoding="utf-8")
        match = FRONTMATTER_RE.match(raw)
        if not match:
            raise ValueError(f"{skill_md} missing YAML frontmatter")
        meta = yaml.safe_load(match.group(1)) or {}
        body = match.group(2)

        name = str(meta.get("name", "")).strip()
        if not name or len(name) > 64 or not SKILL_NAME_RE.match(name):
            raise ValueError(
                f"{skill_md}: invalid skill name '{name}' (lowercase a-z, 0-9, single hyphens)"
            )
        if name != skill_dir.name:
            raise ValueError(
                f"{skill_md}: skill name '{name}' must match directory '{skill_dir.name}'"
            )
        description = str(meta.get("description", "")).strip()
        if not description:
            raise ValueError(f"{skill_md}: `description` is required")

        # Agent Skills keeps product-specific keys under `metadata` (string -> string).
        # Top-level keys are accepted as a lenient fallback.
        extra = meta.get("metadata") or {}
        refs_dir = skill_dir / "references"
        refs = (
            sorted(p.name for p in refs_dir.glob("*") if p.is_file())
            if refs_dir.exists()
            else []
        )
        self.skills[name] = RegisteredSkill(
            name=name,
            description=description[:1024],
            instructions_markdown=body.strip(),
            root_dir=skill_dir.resolve(),
            version=str(extra.get("version", meta.get("version", "1.0.0"))),
            required_tools=_as_list(
                extra.get("required_tools", meta.get("required_tools"))
            ),
            allowed_secrets=_as_list(
                extra.get("allowed_secrets", meta.get("allowed_secrets"))
            ),
            available_references=refs,
        )

    def register_external_manifest(self, manifest: dict[str, Any]) -> None:
        name = manifest["name"]
        description = manifest["description"]
        protocol = manifest.get("protocol", "http")
        if protocol not in ("http", "mcp_http"):
            raise ValueError(f"Manifest '{name}': unsupported protocol '{protocol}'")
        queue = manifest.get("queue", "q_tools_fast")
        if queue not in VALID_QUEUES:
            raise ValueError(f"Manifest '{name}': queue must be one of {VALID_QUEUES}")
        endpoint_url = validate_safe_outbound_url(_expand_env(manifest["endpoint_url"]))
        secret_key = manifest.get("secret_key")
        requires_approval = bool(manifest.get("requires_approval", False))

        params = manifest.get("parameters", {})
        props = params.get("properties", {})
        req_fields = set(params.get("required", []))
        type_map = {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
            "object": dict,
            "array": list,
        }
        field_defs: dict[str, Any] = {}
        for p_name, p_spec in props.items():
            py_t = type_map.get(p_spec.get("type", "string"), Any)
            if p_name in req_fields:
                field_defs[p_name] = (
                    py_t,
                    Field(..., description=p_spec.get("description", "")),
                )
            else:
                field_defs[p_name] = (
                    py_t | None,
                    Field(
                        p_spec.get("default"), description=p_spec.get("description", "")
                    ),
                )
        DynamicSchema = create_model(f"{name}_Args", **field_defs)

        async def _external_handler(ctx: RunContext, args: BaseModel) -> str:
            assert ctx.http_client is not None
            # Re-validate at call time with DNS resolution to catch rebinding / internal aliases.
            await asyncio.to_thread(
                validate_safe_outbound_url, endpoint_url, resolve_dns=True
            )

            payload_dict = args.model_dump(exclude_none=True)
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "traceparent": ctx.traceparent,
            }
            if secret_key:
                secret_val = ctx.get_secret(secret_key)
                sig = hmac.new(
                    secret_val.encode(),
                    json.dumps(payload_dict, sort_keys=True).encode(),
                    hashlib.sha256,
                ).hexdigest()
                headers["Authorization"] = f"Bearer {secret_val}"
                headers["X-HaaS-Signature-256"] = f"sha256={sig}"

            if protocol == "mcp_http":
                headers["MCP-Protocol-Version"] = MCP_PROTOCOL_VERSION
                headers["Mcp-Method"] = "tools/call"
                headers["Mcp-Name"] = name
                body: dict[str, Any] = {
                    "jsonrpc": "2.0",
                    "id": ctx.run_id,
                    "method": "tools/call",
                    "params": {
                        "name": name,
                        "arguments": payload_dict,
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
                            "io.modelcontextprotocol/clientInfo": {
                                "name": "haas",
                                "version": "3.0.0",
                            },
                            "io.modelcontextprotocol/clientCapabilities": {},
                            "traceparent": ctx.traceparent,
                        },
                    },
                }
            else:
                body = payload_dict

            resp = await ctx.http_client.post(
                endpoint_url,
                headers=headers,
                json=body,
                timeout=20.0,
                follow_redirects=False,
            )
            resp.raise_for_status()
            if protocol != "mcp_http":
                return resp.text

            msg = _parse_mcp_response(resp.text, resp.headers.get("content-type", ""))
            if "error" in msg:
                raise ToolRemediationError(f"MCP error from '{name}': {msg['error']}")
            result = msg.get("result", {})
            text = "\n".join(
                c.get("text", "")
                for c in result.get("content", [])
                if c.get("type") == "text"
            ) or json.dumps(result.get("structuredContent", result))
            if result.get("isError"):
                raise ToolRemediationError(
                    f"MCP tool '{name}' reported an error: {text}"
                )
            return text

        self.tools[name] = RegisteredTool(
            name=name,
            description=description,
            args_schema=DynamicSchema,
            handler=_external_handler,
            injects_context=True,
            requires_approval=requires_approval,
            queue=queue,
        )

    def discover_all(self, plugins_dir: Path) -> None:
        skills_path = plugins_dir / "skills"
        if skills_path.exists():
            for child in sorted(skills_path.iterdir()):
                if child.is_dir():
                    self.load_skill_directory(child)

        tools_path = plugins_dir / "tools"
        if tools_path.exists():
            for py_file in sorted(tools_path.glob("*.py")):
                if py_file.name.startswith("_"):
                    continue
                spec = importlib.util.spec_from_file_location(
                    f"haas_plugin_{py_file.stem}", py_file
                )
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    # Pydantic resolves postponed annotations (`from __future__ import annotations`)
                    # through sys.modules, so the plugin module must be registered before it runs.
                    sys.modules[spec.name] = mod
                    spec.loader.exec_module(mod)
                    if hasattr(mod, "register"):
                        mod.register(self)

        manifests_path = plugins_dir / "manifests"
        if manifests_path.exists():
            for jf in sorted(manifests_path.glob("*.json")):
                items = json.loads(jf.read_text(encoding="utf-8"))
                for item in items if isinstance(items, list) else [items]:
                    self.register_external_manifest(item)

        for ep in importlib.metadata.entry_points(group="haas.plugins"):
            ep.load()(self)

        for skill in self.skills.values():
            for tool_name in skill.required_tools:
                if tool_name not in self.tools:
                    log.warning(
                        "Skill '%s' requires unknown tool '%s'", skill.name, tool_name
                    )

    def resolve_allowed_secrets(self, active_skills: set[str]) -> set[str]:
        allowed: set[str] = set()
        for s_name in active_skills:
            skill = self.skills.get(s_name)
            if skill:
                allowed.update(skill.allowed_secrets)
        return allowed

    def resolve_unlocked_tool_names(self, active_skills: set[str]) -> set[str]:
        unlocked = {name for name, t in self.tools.items() if t.is_core}
        for s_name in active_skills:
            skill = self.skills.get(s_name)
            if skill:
                unlocked.update(skill.required_tools)
        return {n for n in unlocked if n in self.tools}

    def resolve_sorted_tool_schemas(
        self, active_skills: set[str]
    ) -> list[dict[str, Any]]:
        return [
            self.tools[name].to_openai_schema()
            for name in sorted(self.resolve_unlocked_tool_names(active_skills))
        ]


registry = PluginRegistry()


# --- Core Built-in Meta-Tools ---


class ActivateSkillArgs(BaseModel):
    skill_names: list[str] = Field(
        ..., description="Skill names from the catalog to activate."
    )


@registry.tool(
    name="activate_skill",
    description="Activates one or more domain skills, returning their Markdown SOP and unlocking their tools.",
    args_schema=ActivateSkillArgs,
    is_core=True,
)
async def activate_skill(ctx: RunContext, args: ActivateSkillArgs) -> str:
    blocks: list[str] = []
    missing: list[str] = []
    for name in args.skill_names:
        skill = registry.skills.get(name)
        if not skill:
            missing.append(name)
            continue
        ctx.active_skills.add(name)
        refs_info = (
            f"\nBundled Tier-3 References (call `read_skill_reference`): {skill.available_references}"
            if skill.available_references
            else ""
        )
        blocks.append(
            f"=== ACTIVATED SKILL: {skill.name} (v{skill.version}) ===\n"
            f"Unlocked Tools: {', '.join(skill.required_tools) or 'None'}{refs_info}\n\n"
            f"{skill.instructions_markdown}"
        )
    if missing:
        avail = ", ".join(sorted(registry.skills.keys()))
        note = f"Unknown skill(s): {missing}. Available: [{avail}]"
        if not blocks:
            raise ToolRemediationError(note)
        blocks.append(f"NOTE: {note}")
    return "\n\n".join(blocks)


class ReadSkillReferenceArgs(BaseModel):
    skill_name: str = Field(..., description="Already activated skill name.")
    filename: str = Field(
        ..., description="File inside the skill's references/ folder."
    )


@registry.tool(
    name="read_skill_reference",
    description="Reads a Tier-3 reference document from an activated skill's `references/` directory.",
    args_schema=ReadSkillReferenceArgs,
    is_core=True,
)
async def read_skill_reference(ctx: RunContext, args: ReadSkillReferenceArgs) -> str:
    skill = registry.skills.get(args.skill_name)
    if args.skill_name not in ctx.active_skills or skill is None:
        raise ToolRemediationError(
            f"Skill '{args.skill_name}' is not active. Call `activate_skill` first."
        )
    ref_dir = (skill.root_dir / "references").resolve()
    target = (ref_dir / args.filename).resolve()
    if not target.is_relative_to(ref_dir) or not target.is_file():
        raise ToolRemediationError(
            f"Reference '{args.filename}' not found. Available: {skill.available_references}"
        )
    return target.read_text(encoding="utf-8")
