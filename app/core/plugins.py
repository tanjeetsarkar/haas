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
from typing import Any, Awaitable, Callable, Collection, Iterable

import yaml
from pydantic import BaseModel, Field, create_model

from app.core.context import (
    RunContext,
    ToolRemediationError,
    validate_safe_outbound_url,
)
from app.core.plugin_manifest import (
    LEGACY_DIRS,
    MANIFEST_FILE,
    PluginManifest,
    PluginManifestError,
    find_skill_dirs,
    hash_plugin,
    read_plugin_manifest,
)

log = logging.getLogger("haas.plugins")

VALID_QUEUES = ("q_tools_fast", "q_tools_sandbox")
MCP_PROTOCOL_VERSION = "2026-07-28"
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
CORE_TOOL_NAMES = frozenset({"activate_skill", "read_skill_reference"})
CORE_PLUGIN_ID = "core"
CATALOG_GROUP_THRESHOLD = 8
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
    plugin_id: str = ""
    source: str = ""  # file that registered the tool (python module or endpoint json)
    secret_key: str = ""  # endpoint tools only: secret used to sign/authenticate calls

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
    plugin_id: str = ""
    category: str = ""
    hidden: bool = False  # not offered to the model; only reachable via preload_skills
    ready: bool = True
    unready_reason: str = ""


@dataclass
class PluginRecord:
    """Outcome of discovering/loading one plugin; kept for `inspect`/`doctor`/`/v1/plugins`."""

    id: str
    status: str  # loaded | disabled | blocked | failed
    origin: str = "dir"  # dir | entrypoint
    manifest: PluginManifest | None = None
    error: str = ""
    skills: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    digest: str = ""


@dataclass
class Diagnostic:
    level: str  # warning | error
    plugin_id: str
    message: str


class PluginLoadError(RuntimeError):
    """Raised in strict mode when any plugin failed to load."""


def _as_list(value: Any) -> list[str]:
    """Agent Skills `metadata` values are strings, so lists are space/comma separated."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p for p in re.split(r"[\s,]+", str(value)) if p]


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


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
        self.plugins: dict[str, PluginRecord] = {}
        self.diagnostics: list[Diagnostic] = []
        self.fingerprint: str = ""
        self._current_plugin: str = ""

    def _claim_tool(self, name: str) -> str:
        owner = self._current_plugin
        existing = self.tools.get(name)
        if existing is not None:
            raise ValueError(
                f"Tool '{name}' is already registered by plugin "
                f"'{existing.plugin_id or '?'}'; tool names must be globally unique."
            )
        return owner

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
            owner = self._claim_tool(name) or (CORE_PLUGIN_ID if is_core else "")
            self.tools[name] = RegisteredTool(
                name=name,
                description=description,
                args_schema=args_schema,
                handler=fn,
                injects_context="ctx" in sig.parameters,
                requires_approval=requires_approval,
                is_core=is_core,
                queue="q_tools_sandbox" if sandbox else "q_tools_fast",
                plugin_id=owner,
            )
            return fn

        return decorator

    def load_skill_directory(
        self,
        skill_dir: Path,
        *,
        plugin_id: str = "",
        default_category: str = "",
    ) -> None:
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            return
        if not skill_md.resolve().is_relative_to(skill_dir.resolve()):
            raise ValueError(
                f"{skill_md}: SKILL.md resolves outside its skill directory"
            )
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
        existing = self.skills.get(name)
        if existing is not None and existing.root_dir != skill_dir.resolve():
            raise ValueError(
                f"{skill_md}: skill '{name}' is already provided by plugin "
                f"'{existing.plugin_id or '?'}'; skill names must be globally unique."
            )

        # Agent Skills keeps product-specific keys under `metadata` (string -> string).
        # Top-level keys are accepted as a lenient fallback.
        extra = meta.get("metadata") or {}

        def pick(key: str, default: Any = None) -> Any:
            if key in extra:
                return extra[key]
            return meta.get(key, meta.get(key.replace("_", "-"), default))

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
            version=str(pick("version", "1.0.0")),
            required_tools=_as_list(pick("required_tools")),
            allowed_secrets=_as_list(pick("allowed_secrets")),
            available_references=refs,
            plugin_id=plugin_id or self._current_plugin,
            category=str(pick("category", "") or default_category).strip(),
            hidden=_truthy(pick("disable_model_invocation", False)),
        )

    def register_external_manifest(self, manifest: dict[str, Any]) -> None:
        name = manifest["name"]
        description = manifest["description"]
        owner = self._claim_tool(name)
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
            plugin_id=owner,
            secret_key=secret_key or "",
        )

    # --- discovery -----------------------------------------------------------------------

    def _reset_discovered(self) -> None:
        """Drops everything a previous discovery added; core tools (registered at import) stay."""
        self.tools = {
            n: t for n, t in self.tools.items() if t.plugin_id == CORE_PLUGIN_ID
        }
        self.skills.clear()
        self.plugins.clear()
        self.diagnostics.clear()
        self.fingerprint = ""
        self._current_plugin = ""

    def _diag(self, level: str, plugin_id: str, message: str) -> None:
        self.diagnostics.append(Diagnostic(level, plugin_id, message))
        (log.error if level == "error" else log.warning)(
            "[%s] %s", plugin_id or "-", message
        )

    def discover_all(
        self,
        plugins_dir: Path,
        *,
        extra_dirs: Iterable[Path] = (),
        allow: Iterable[str] | None = None,
        deny: Iterable[str] = (),
        strict: bool = False,
        include_disabled: bool = False,
    ) -> None:
        """Discovers plugin packs (`<root>/<id>/plugin.json`) and builds the registry.

        Phase A reads manifests only (no plugin code runs) and applies policy; phase B
        imports/registers the plugins that survived. A plugin that fails is rolled back and
        recorded; healthy plugins still load unless `strict` is set.
        """
        self._reset_discovered()
        allow_set = {a for a in allow} if allow else None
        deny_set = set(deny)

        candidates: dict[str, PluginManifest] = {}
        for root in [Path(plugins_dir), *[Path(d) for d in extra_dirs]]:
            if not root.is_dir():
                self._diag("warning", "", f"plugin root '{root}' does not exist")
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir() or child.name.startswith(("_", ".")):
                    continue
                if not (child / MANIFEST_FILE).is_file():
                    hint = (
                        "legacy flat layout is no longer loaded; run `haas-plugin migrate`"
                        if child.name in LEGACY_DIRS
                        else f"no {MANIFEST_FILE}; directory ignored"
                    )
                    self._diag("warning", child.name, hint)
                    continue
                try:
                    manifest = read_plugin_manifest(child)
                except PluginManifestError as exc:
                    if child.name not in self.plugins and child.name not in candidates:
                        self.plugins[child.name] = PluginRecord(
                            child.name, "failed", error=str(exc)
                        )
                        self._diag("error", child.name, str(exc))
                    continue
                if manifest.id in candidates:
                    self._diag(
                        "warning",
                        manifest.id,
                        f"'{child}' is shadowed by the higher-precedence copy at "
                        f"'{candidates[manifest.id].root}'",
                    )
                    continue
                candidates[manifest.id] = manifest

        to_load: dict[str, PluginManifest] = {}
        for pid, m in sorted(candidates.items()):
            if pid in deny_set:
                self.plugins[pid] = PluginRecord(
                    pid, "blocked", manifest=m, error="denied by PLUGINS_DENY"
                )
            elif allow_set is not None and pid not in allow_set:
                self.plugins[pid] = PluginRecord(
                    pid, "blocked", manifest=m, error="not in PLUGINS_ALLOW"
                )
            elif (
                not m.enabled
                and not include_disabled
                and not (allow_set and pid in allow_set)
            ):
                self.plugins[pid] = PluginRecord(pid, "disabled", manifest=m)
            else:
                to_load[pid] = m

        for pid in self._load_order(to_load):
            m = to_load[pid]
            missing = [
                d
                for d in m.requires_plugins
                if d not in self.plugins or self.plugins[d].status != "loaded"
            ]
            if missing:
                self.plugins[pid] = PluginRecord(
                    pid,
                    "blocked",
                    manifest=m,
                    error=f"requires plugin(s) not loaded: {missing}",
                )
                self._diag("error", pid, self.plugins[pid].error)
                continue
            self._load_plugin(m)

        self._load_entry_points(allow_set, deny_set)
        self.refresh_readiness()
        self._compute_fingerprint()

        for skill in self.skills.values():
            if not skill.ready:
                log.warning(
                    "skill '%s' (plugin %s) is not ready: %s",
                    skill.name,
                    skill.plugin_id or "-",
                    skill.unready_reason,
                )

        failed = [r for r in self.plugins.values() if r.status == "failed"]
        if failed and strict:
            detail = "; ".join(f"{r.id}: {r.error}" for r in failed)
            raise PluginLoadError(f"plugin(s) failed to load: {detail}")

    def discover_from_settings(self, cfg: Any) -> None:
        """Discovery with the operator policy from `app.core.config.Settings`."""
        cfg.tenant_allowlists()  # fail fast on malformed TENANT_SKILL_ALLOWLISTS
        self.discover_all(
            cfg.plugins_dir,
            extra_dirs=cfg.plugins_extra_dir_list,
            allow=cfg.plugins_allow_list,
            deny=cfg.plugins_deny_list,
            strict=cfg.plugins_strict,
        )
        log.info(
            "plugins loaded: %s | registry fingerprint %s",
            ", ".join(
                sorted(p for p, r in self.plugins.items() if r.status == "loaded")
            )
            or "(none)",
            self.fingerprint,
        )

    @staticmethod
    def _load_order(manifests: dict[str, PluginManifest]) -> list[str]:
        """Dependencies first; ties and unknown/cyclic deps fall back to id order."""
        order: list[str] = []
        state: dict[str, int] = {}

        def visit(pid: str) -> None:
            if state.get(pid):
                return
            state[pid] = 1
            for dep in sorted(manifests[pid].requires_plugins):
                if dep in manifests:
                    visit(dep)
            order.append(pid)

        for pid in sorted(manifests):
            visit(pid)
        return order

    def _load_plugin(self, m: PluginManifest) -> None:
        tools_before, skills_before = dict(self.tools), dict(self.skills)
        modules: list[str] = []
        self._current_plugin = m.id
        try:
            for sroot in m.skill_roots:
                for skill_dir in find_skill_dirs(sroot):
                    self.load_skill_directory(
                        skill_dir, plugin_id=m.id, default_category=m.category
                    )
            for py_file in m.tool_files:
                before = set(self.tools)
                modules.append(self._import_tool_module(m.id, py_file))
                for name in set(self.tools) - before:
                    self.tools[name].source = py_file.name
            for jf in m.endpoint_files:
                items = json.loads(jf.read_text(encoding="utf-8"))
                for item in items if isinstance(items, list) else [items]:
                    self.register_external_manifest(item)
                    self.tools[item["name"]].source = jf.name

            new_tools = sorted(set(self.tools) - set(tools_before))
            new_skills = sorted(set(self.skills) - set(skills_before))
            for declared, actual, what in (
                (m.provides_tools, new_tools, "tools"),
                (m.provides_skills, new_skills, "skills"),
            ):
                if declared is not None and sorted(declared) != actual:
                    raise ValueError(
                        f"`provides.{what}` {sorted(declared)} does not match what the "
                        f"plugin registered {actual}"
                    )
        except BaseException as exc:
            self.tools, self.skills = tools_before, skills_before
            for mod_name in modules:
                sys.modules.pop(mod_name, None)
            if not isinstance(exc, Exception):
                raise
            self.plugins[m.id] = PluginRecord(
                m.id, "failed", manifest=m, error=f"{type(exc).__name__}: {exc}"
            )
            self._diag("error", m.id, self.plugins[m.id].error)
            return
        finally:
            self._current_plugin = ""

        self.plugins[m.id] = PluginRecord(
            m.id,
            "loaded",
            manifest=m,
            skills=new_skills,
            tools=new_tools,
            digest=hash_plugin(m),
        )

    def _import_tool_module(self, plugin_id: str, py_file: Path) -> str:
        mod_name = f"haas_plugin_{plugin_id.replace('-', '_')}_{py_file.stem}"
        spec = importlib.util.spec_from_file_location(mod_name, py_file)
        if not (spec and spec.loader):
            raise ImportError(f"cannot create import spec for {py_file.name}")
        mod = importlib.util.module_from_spec(spec)
        # Pydantic resolves postponed annotations (`from __future__ import annotations`)
        # through sys.modules, so the plugin module must be registered before it runs.
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
            if not hasattr(mod, "register"):
                raise AttributeError(
                    f"{py_file.name} has no `register(registry)` function"
                )
            mod.register(self)
        except BaseException:
            sys.modules.pop(mod_name, None)
            raise
        return mod_name

    def _load_entry_points(self, allow: set[str] | None, deny: set[str]) -> None:
        for ep in importlib.metadata.entry_points(group="haas.plugins"):
            pid = f"entrypoint:{ep.name}"
            if pid in deny or (allow is not None and pid not in allow):
                self.plugins[pid] = PluginRecord(
                    pid,
                    "blocked",
                    origin="entrypoint",
                    error="excluded by plugin policy",
                )
                continue
            tools_before, skills_before = dict(self.tools), dict(self.skills)
            self._current_plugin = pid
            try:
                ep.load()(self)
            except Exception as exc:
                self.tools, self.skills = tools_before, skills_before
                self.plugins[pid] = PluginRecord(
                    pid,
                    "failed",
                    origin="entrypoint",
                    error=f"{type(exc).__name__}: {exc}",
                )
                self._diag("error", pid, self.plugins[pid].error)
            else:
                self.plugins[pid] = PluginRecord(
                    pid,
                    "loaded",
                    origin="entrypoint",
                    skills=sorted(set(self.skills) - set(skills_before)),
                    tools=sorted(set(self.tools) - set(tools_before)),
                )
            finally:
                self._current_plugin = ""

    def refresh_readiness(self) -> None:
        """A skill is ready only if every tool it unlocks exists (core tools included)."""
        known = set(self.tools) | CORE_TOOL_NAMES
        for skill in self.skills.values():
            missing = [t for t in skill.required_tools if t not in known]
            skill.ready = not missing
            skill.unready_reason = (
                f"required tool(s) not registered: {', '.join(missing)}"
                if missing
                else ""
            )

    def _compute_fingerprint(self) -> None:
        h = hashlib.sha256()
        for pid in sorted(self.plugins):
            rec = self.plugins[pid]
            if rec.status == "loaded":
                h.update(f"{pid}\0{rec.digest}\0".encode())
        self.fingerprint = h.hexdigest()[:16]

    # --- resolution (what a run may see / use) --------------------------------------------

    def effective_skills(
        self,
        active_skills: Iterable[str],
        allowed_skills: Collection[str] | None = None,
    ) -> set[str]:
        """Active skills that exist, are ready and are inside the run's allowlist."""
        return {
            n
            for n in active_skills
            if (s := self.skills.get(n)) is not None
            and s.ready
            and (allowed_skills is None or n in allowed_skills)
        }

    def model_visible_skills(
        self, allowed_skills: Collection[str] | None = None
    ) -> list[RegisteredSkill]:
        """Skills advertised to (and activatable by) the model, in deterministic order."""
        return [
            s
            for s in sorted(self.skills.values(), key=lambda x: x.name)
            if s.ready
            and not s.hidden
            and (allowed_skills is None or s.name in allowed_skills)
        ]

    def resolve_allowed_secrets(
        self, active_skills: set[str], allowed_skills: Collection[str] | None = None
    ) -> set[str]:
        allowed: set[str] = set()
        for s_name in self.effective_skills(active_skills, allowed_skills):
            allowed.update(self.skills[s_name].allowed_secrets)
        return allowed

    def resolve_unlocked_tool_names(
        self, active_skills: set[str], allowed_skills: Collection[str] | None = None
    ) -> set[str]:
        unlocked = {name for name, t in self.tools.items() if t.is_core}
        for s_name in self.effective_skills(active_skills, allowed_skills):
            unlocked.update(self.skills[s_name].required_tools)
        return {n for n in unlocked if n in self.tools}

    def resolve_sorted_tool_schemas(
        self, active_skills: set[str], allowed_skills: Collection[str] | None = None
    ) -> list[dict[str, Any]]:
        return [
            self.tools[name].to_openai_schema()
            for name in sorted(
                self.resolve_unlocked_tool_names(active_skills, allowed_skills)
            )
        ]

    def render_catalog(
        self,
        allowed_skills: Collection[str] | None = None,
        max_chars: int = 0,
    ) -> str:
        """Tier-1 catalog: grouped by category when large, compacted to fit `max_chars`."""
        skills = self.model_visible_skills(allowed_skills)
        if not skills:
            return "(No domain skills installed)"

        def line(s: RegisteredSkill, limit: int | None) -> str:
            desc = " ".join(s.description.split())
            if limit == 0:
                desc = ""
            elif limit is not None:
                desc = (
                    desc
                    if len(desc) <= limit
                    else desc[: max(limit - 1, 0)].rstrip() + "…"
                )
            return f"- `{s.name}`: {desc}" if desc else f"- `{s.name}`"

        categories = sorted({s.category or "general" for s in skills})
        grouped = len(skills) > CATALOG_GROUP_THRESHOLD and len(categories) > 1

        def render(limit: int | None) -> str:
            if not grouped:
                return "\n".join(line(s, limit) for s in skills)
            blocks = []
            for cat in categories:
                members = [s for s in skills if (s.category or "general") == cat]
                blocks.append(
                    f"### {cat}\n" + "\n".join(line(s, limit) for s in members)
                )
            return "\n\n".join(blocks)

        text = render(None)
        if not max_chars or len(text) <= max_chars:
            return text
        note = "\n(Catalog compacted to fit the prompt budget.)"
        budget = max_chars - len(note)
        for limit in (160, 100, 60, 40, 20, 0):
            candidate = render(limit)
            if len(candidate) <= budget:
                return candidate + note
        names = ", ".join(f"`{s.name}`" for s in skills)
        return names + note


def resolve_run_allowlist(
    tenant_list: list[str] | None, requested: list[str] | None
) -> list[str] | None:
    """Effective skill allowlist for a run (None = unrestricted).

    A per-run request can only narrow the tenant's list, never widen it.
    """
    if requested is None:
        return sorted(set(tenant_list)) if tenant_list is not None else None
    if tenant_list is None:
        return sorted(set(requested))
    outside = sorted(set(requested) - set(tenant_list))
    if outside:
        raise ValueError(f"skills not permitted for this tenant: {outside}")
    return sorted(set(requested))


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
    visible = {s.name for s in registry.model_visible_skills(ctx.skill_allowlist)}
    reusable = registry.effective_skills(ctx.active_skills, ctx.skill_allowlist)
    for name in args.skill_names:
        skill = registry.skills.get(name)
        # Unavailable (hidden, not ready, outside the run's allowlist) looks the same as unknown.
        if not skill or (name not in visible and name not in reusable):
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
        avail = ", ".join(sorted(visible))
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
    if skill is None or args.skill_name not in registry.effective_skills(
        ctx.active_skills, ctx.skill_allowlist
    ):
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
