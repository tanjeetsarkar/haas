"""`haas-plugin`: scaffold, inspect, validate (`doctor`) and migrate plugin packs under plugins/.

A plugin pack is `plugins/<id>/plugin.json` plus its `skills/`, `tools/` and `endpoints/`.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
from collections.abc import Sequence
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.core.config import settings
from app.core.plugin_manifest import (
    MANIFEST_FILE,
    NAME_RE,
    find_skill_dirs,
)
from app.core.plugins import (
    CORE_PLUGIN_ID,
    CORE_TOOL_NAMES,
    SKILL_NAME_RE,
    PluginRegistry,
)

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
ENV_VAR_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
REF_MENTION_RE = re.compile(r"`([\w.\-]+\.(?:md|txt|json|csv|yaml|yml))`")


class ScaffoldError(Exception):
    pass


def render(text: str, values: dict[str, str]) -> str:
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", val)
    return text


def _split(value: str | None) -> list[str]:
    return [p for p in re.split(r"[\s,]+", value or "") if p]


def _title(name: str) -> str:
    return re.sub(r"[-_]+", " ", name).strip().title()


def _write_new(path: Path, content: str) -> Path:
    if path.exists():
        raise ScaffoldError(f"{path} already exists; refusing to overwrite.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _template(plugins_dir: Path, *parts: str) -> str:
    path = plugins_dir / "_templates" / Path(*parts)
    if not path.is_file():
        raise ScaffoldError(
            f"Template {path} not found. Run from the repo root or pass --plugins-dir."
        )
    return path.read_text(encoding="utf-8")


def _check_names(
    *, tools: Sequence[str] = (), secrets: Sequence[str] = (), skill: str | None = None
) -> None:
    if skill is not None and (len(skill) > 64 or not SKILL_NAME_RE.match(skill)):
        raise ScaffoldError(
            f"Invalid skill name '{skill}': lowercase a-z, 0-9 and single hyphens, max 64 chars."
        )
    for t in tools:
        if not TOOL_NAME_RE.match(t):
            raise ScaffoldError(f"Invalid tool name '{t}': use snake_case.")
    for s in secrets:
        if not ENV_VAR_RE.match(s):
            raise ScaffoldError(f"Invalid secret name '{s}': use UPPER_SNAKE_CASE.")


def _plugin_root(plugins_dir: Path, plugin: str) -> Path:
    root = plugins_dir / plugin
    if not (root / MANIFEST_FILE).is_file():
        raise ScaffoldError(
            f"Plugin '{plugin}' not found ({root / MANIFEST_FILE} missing). "
            f"Create it with `haas-plugin new plugin {plugin}`."
        )
    return root


def _edit_manifest(root: Path, fn: Callable[[dict[str, Any]], None]) -> None:
    path = root / MANIFEST_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _provide(root: Path, kind: str, name: str) -> None:
    """Keeps `provides.<kind>` in step with scaffolding (only if the plugin declares it)."""

    def update(data: dict[str, Any]) -> None:
        names = data.get("provides", {}).get(kind)
        if names is not None and name not in names:
            names.append(name)
            names.sort()

    _edit_manifest(root, update)


def _scaffold_skill(
    plugins_dir: Path, root: Path, name: str, tools: list[str], secrets: list[str]
) -> list[Path]:
    values = {
        "name": name,
        "title": _title(name),
        "required_tools": " ".join(tools),
        "allowed_secrets": " ".join(secrets),
        "first_tool": tools[0] if tools else "your_tool",
    }
    skill_dir = root / "skills" / name
    return [
        _write_new(
            skill_dir / "SKILL.md",
            render(_template(plugins_dir, "skill", "SKILL.md"), values),
        ),
        _write_new(
            skill_dir / "references" / "reference.md",
            _template(plugins_dir, "skill", "references", "reference.md"),
        ),
    ]


def new_plugin(
    plugins_dir: Path,
    plugin: str,
    tools: list[str],
    secrets: list[str],
    category: str = "",
) -> list[Path]:
    if len(plugin) > 64 or not NAME_RE.match(plugin):
        raise ScaffoldError(
            f"Invalid plugin id '{plugin}': lowercase a-z, 0-9 and single hyphens, max 64 chars."
        )
    _check_names(tools=tools, secrets=secrets)
    values = {"id": plugin, "name": _title(plugin), "category": category}
    data = json.loads(render(_template(plugins_dir, "plugin", "plugin.json"), values))
    data["provides"] = {"skills": [plugin], "tools": sorted(tools)}
    root = plugins_dir / plugin
    created = [_write_new(root / MANIFEST_FILE, json.dumps(data, indent=2) + "\n")]
    created += _scaffold_skill(plugins_dir, root, plugin, tools, secrets)
    return created


def new_skill(
    plugins_dir: Path, plugin: str, name: str, tools: list[str], secrets: list[str]
) -> list[Path]:
    _check_names(tools=tools, secrets=secrets, skill=name)
    root = _plugin_root(plugins_dir, plugin)
    created = _scaffold_skill(plugins_dir, root, name, tools, secrets)
    _provide(root, "skills", name)
    return created


def new_tool(
    plugins_dir: Path,
    plugin: str,
    name: str,
    approval: bool = False,
    sandbox: bool = False,
    skill: str | None = None,
) -> list[Path]:
    if not TOOL_NAME_RE.match(name):
        raise ScaffoldError(
            f"Invalid tool name '{name}': snake_case, starts with a letter (e.g. github_list_issues)."
        )
    root = _plugin_root(plugins_dir, plugin)
    skill_md: Path | None = None
    if skill:
        found = [d for d in find_skill_dirs(root / "skills") if d.name == skill]
        if not found:
            raise ScaffoldError(f"Skill '{skill}' not found in plugin '{plugin}'.")
        skill_md = found[0] / "SKILL.md"
    values = {
        "tool_name": name,
        "class_name": "".join(p.capitalize() for p in name.split("_")),
        "title": _title(name),
        "requires_approval": str(approval),
        "sandbox": str(sandbox),
    }
    path = _write_new(
        root / "tools" / f"{name}.py",
        render(_template(plugins_dir, "tool", "tool.py"), values),
    )
    _provide(root, "tools", name)
    if skill_md is not None:
        text = skill_md.read_text(encoding="utf-8")
        m = re.search(r'^(\s*required_tools:\s*)"([^"]*)"', text, re.MULTILINE)
        if not m:
            raise ScaffoldError(f"{skill_md}: no `required_tools` line to update.")
        tools = _split(m.group(2))
        if name not in tools:
            tools.append(name)
            text = (
                text[: m.start()] + f'{m.group(1)}"{" ".join(tools)}"' + text[m.end() :]
            )
            skill_md.write_text(text, encoding="utf-8")
    return [path]


def new_endpoint(
    plugins_dir: Path,
    plugin: str,
    name: str,
    protocol: str = "http",
    secret: str | None = None,
) -> list[Path]:
    if not TOOL_NAME_RE.match(name):
        raise ScaffoldError(f"Invalid tool name '{name}': use snake_case.")
    if protocol not in ("http", "mcp_http"):
        raise ScaffoldError("protocol must be 'http' or 'mcp_http'.")
    if secret and not ENV_VAR_RE.match(secret):
        raise ScaffoldError(f"Invalid secret name '{secret}': use UPPER_SNAKE_CASE.")
    root = _plugin_root(plugins_dir, plugin)
    values = {
        "tool_name": name,
        "protocol": protocol,
        "env_var": f"{name.upper()}_URL",
        "secret_key": secret or "",
    }
    content = render(_template(plugins_dir, "endpoint", "external.json"), values)
    if not secret:
        data = json.loads(content)
        data.pop("secret_key")
        content = json.dumps(data, indent=2) + "\n"
    path = _write_new(root / "endpoints" / f"{name}.json", content)
    _provide(root, "tools", name)
    return [path]


# --- doctor / validate ----------------------------------------------------------------------


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    plugins: int = 0
    skills: int = 0
    tools: int = 0


def secrets_used(source: str) -> set[str]:
    """Secret names passed as literals to `get_secret(...)` (comments/docstrings ignored)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_secret"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.add(node.args[0].value)
    return found


def build_registry(
    plugins_dir: Path, extra_dirs: list[Path] | None = None
) -> PluginRegistry:
    """Loads everything exactly like the server does, including disabled plugins."""
    # Never leave (possibly stale) bytecode behind in the plugins directory.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        reg = PluginRegistry()
        reg.discover_all(
            plugins_dir, extra_dirs=extra_dirs or [], include_disabled=True
        )
        return reg
    finally:
        sys.dont_write_bytecode = previous


def validate_plugins(plugins_dir: Path, extra_dirs: list[Path] | None = None) -> Report:
    return _validate(build_registry(plugins_dir, extra_dirs))


def _validate(reg: PluginRegistry) -> Report:
    rep = Report()
    for d in reg.diagnostics:
        target = rep.errors if d.level == "error" else rep.warnings
        target.append(
            f"plugin {d.plugin_id}: {d.message}" if d.plugin_id else d.message
        )

    loaded = {
        p: r for p, r in reg.plugins.items() if r.status == "loaded" and r.manifest
    }
    rep.plugins = len(loaded)
    rep.skills, rep.tools = len(reg.skills), len(reg.tools)

    secrets_by_file: dict[tuple[str, str], set[str]] = {}
    for pid, rec in loaded.items():
        m = rec.manifest
        assert m is not None
        if "TODO" in m.description:
            rep.errors.append(
                f"plugin {pid}: plugin.json description still contains TODO."
            )
        if m.provides_skills is None or m.provides_tools is None:
            rep.warnings.append(
                f"plugin {pid}: plugin.json has no `provides` (skills/tools); declare it so "
                "the registry can verify ownership."
            )
        if not rec.skills and not rec.tools:
            rep.warnings.append(f"plugin {pid}: provides no skills or tools.")
        for f in m.tool_files:
            try:
                secrets_by_file[(pid, f.name)] = secrets_used(
                    f.read_text(encoding="utf-8")
                )
            except SyntaxError:
                secrets_by_file[(pid, f.name)] = set()

    referenced = {t for sk in reg.skills.values() for t in sk.required_tools}

    for name, tool in sorted(reg.tools.items()):
        if tool.plugin_id == CORE_PLUGIN_ID:
            continue
        try:
            tool.to_openai_schema()
        except Exception as exc:
            rep.errors.append(f"tool {name}: cannot build JSON schema: {exc}")
        if len(tool.description.strip()) < 20:
            rep.warnings.append(
                f"tool {name}: description is very short; the model relies on it."
            )
        if "TODO" in tool.description:
            rep.errors.append(f"tool {name}: description still contains TODO.")
        if name not in referenced:
            rep.warnings.append(
                f"tool {name}: not listed in any skill's required_tools (never offered to the model)."
            )
        users = [sk for sk in reg.skills.values() if name in sk.required_tools]
        pool = users or [
            s for s in reg.skills.values() if s.plugin_id == tool.plugin_id
        ]
        allowed = {s for sk in pool for s in sk.allowed_secrets}
        if tool.secret_key:
            used, what = {tool.secret_key}, "endpoint"
        else:
            used = secrets_by_file.get((tool.plugin_id, tool.source), set())
            what = "tool"
        for secret in sorted(used - allowed):
            rep.errors.append(
                f"{what} {name}: reads secret {secret} which no skill that unlocks it lists "
                "in allowed_secrets."
            )

    for sk in sorted(reg.skills.values(), key=lambda s: s.name):
        for t in sk.required_tools:
            if t not in reg.tools and t not in CORE_TOOL_NAMES:
                rep.errors.append(
                    f"skill {sk.name}: required tool '{t}' is not registered."
                )
            elif t in reg.tools:
                owner = reg.tools[t].plugin_id
                rec = reg.plugins.get(sk.plugin_id)
                deps = rec.manifest.requires_plugins if rec and rec.manifest else []
                if owner not in (sk.plugin_id, CORE_PLUGIN_ID) and owner not in deps:
                    rep.warnings.append(
                        f"skill {sk.name}: uses tool '{t}' from plugin '{owner}'; add it to "
                        "`requires.plugins` in plugin.json."
                    )
        text = (sk.root_dir / "SKILL.md").read_text(encoding="utf-8")
        if "TODO" in text:
            rep.errors.append(
                f"skill {sk.name}: SKILL.md still contains TODO placeholders."
            )
        if len(sk.description) < 40:
            rep.warnings.append(
                f"skill {sk.name}: description is short; include when to use it."
            )
        if not re.search(
            r"\buse when\b|\bwhen the user\b|\bwhen asked\b", sk.description, re.I
        ):
            rep.warnings.append(
                f"skill {sk.name}: description should say WHEN to use the skill."
            )
        if len(sk.instructions_markdown.splitlines()) > 150:
            rep.warnings.append(
                f"skill {sk.name}: body is long; move detail to references/."
            )
        for ref in sorted(set(REF_MENTION_RE.findall(sk.instructions_markdown))):
            if ref not in sk.available_references:
                rep.errors.append(
                    f"skill {sk.name}: mentions `{ref}` but references/{ref} does not exist."
                )
        for ref in sk.available_references:
            if ref not in sk.instructions_markdown:
                rep.warnings.append(
                    f"skill {sk.name}: references/{ref} is never mentioned in SKILL.md."
                )

    budget = settings.skills_prompt_max_chars
    size = len(reg.render_catalog(None, 0))
    if budget and size > budget:
        rep.warnings.append(
            f"skill catalog is {size} chars, over SKILLS_PROMPT_MAX_CHARS={budget}; "
            "descriptions will be shortened in the prompt."
        )
    return rep


def cmd_doctor(plugins_dir: Path, extra_dirs: list[Path] | None = None) -> int:
    rep = validate_plugins(plugins_dir, extra_dirs)
    for w in rep.warnings:
        print(f"warning: {w}")
    for e in rep.errors:
        print(f"error:   {e}")
    print(
        f"{rep.plugins} plugin(s), {rep.skills} skill(s), {rep.tools} tool(s): "
        f"{len(rep.errors)} error(s), {len(rep.warnings)} warning(s)"
    )
    return 1 if rep.errors else 0


def cmd_list(plugins_dir: Path, extra_dirs: list[Path] | None = None) -> int:
    reg = build_registry(plugins_dir, extra_dirs)
    rows = [("ID", "STATUS", "VERSION", "CATEGORY", "SKILLS", "TOOLS")]
    for pid, rec in sorted(reg.plugins.items()):
        m = rec.manifest
        rows.append(
            (
                pid,
                rec.status,
                m.version if m else "-",
                (m.category if m else "") or "-",
                str(len(rec.skills)),
                str(len(rec.tools)),
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    print(f"registry fingerprint: {reg.fingerprint or '-'}")
    return 0


def cmd_inspect(
    plugins_dir: Path, plugin: str, extra_dirs: list[Path] | None = None
) -> int:
    reg = build_registry(plugins_dir, extra_dirs)
    rec = reg.plugins.get(plugin)
    if rec is None:
        print(f"error: plugin '{plugin}' not found", file=sys.stderr)
        return 2
    m = rec.manifest
    print(f"{plugin}  [{rec.status}]" + (f"  v{m.version}" if m else ""))
    if m:
        print(f"  {m.description}")
        print(f"  root:     {m.root}")
        print(f"  category: {m.category or '-'}")
        if m.requires_plugins:
            print(f"  requires: {', '.join(m.requires_plugins)}")
    if rec.error:
        print(f"  problem:  {rec.error}")
    for sname in rec.skills:
        sk = reg.skills[sname]
        flags = [
            f for f, on in (("hidden", sk.hidden), ("NOT READY", not sk.ready)) if on
        ]
        print(f"  skill {sname}" + (f"  ({', '.join(flags)})" if flags else ""))
        if not sk.ready:
            print(f"    reason: {sk.unready_reason}")
        print(f"    unlocks: {', '.join(sk.required_tools) or '-'}")
        print(f"    secrets: {', '.join(sk.allowed_secrets) or '-'}")
    for tname in rec.tools:
        t = reg.tools[tname]
        gate = " approval" if t.requires_approval else ""
        print(f"  tool  {tname}  [{t.queue}{gate}]  <- {t.source}")
    return 0


# --- migrate (flat layout -> plugin packs) --------------------------------------------------


def _load_tool_names(py_file: Path) -> list[str]:
    """Imports a legacy tool file in a scratch registry to learn which tools it registers."""
    reg = PluginRegistry()
    spec = importlib.util.spec_from_file_location(
        f"haas_migrate_{py_file.stem}", py_file
    )
    if not (spec and spec.loader):
        raise ScaffoldError(f"cannot import {py_file}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
        mod.register(reg)
    finally:
        sys.modules.pop(spec.name, None)
    return sorted(reg.tools)


def migrate_flat_layout(plugins_dir: Path, dry_run: bool = False) -> list[str]:
    """Moves `skills/ tools/ manifests/` into `<id>/` packs (skills sharing tool files merge)."""
    skills_dir = plugins_dir / "skills"
    tools_dir = plugins_dir / "tools"
    manifests_dir = plugins_dir / "manifests"
    if not any(d.is_dir() for d in (skills_dir, tools_dir, manifests_dir)):
        raise ScaffoldError(
            f"No legacy skills/, tools/ or manifests/ directory in {plugins_dir}."
        )

    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        skill_reg = PluginRegistry()
        skill_dirs: dict[str, Path] = {}
        for d in sorted(skills_dir.glob("*/")) if skills_dir.is_dir() else []:
            if (d / "SKILL.md").is_file():
                skill_reg.load_skill_directory(d)
                skill_dirs[d.name] = d
        tool_files = {
            f: _load_tool_names(f)
            for f in (sorted(tools_dir.glob("*.py")) if tools_dir.is_dir() else [])
            if not f.name.startswith("_")
        }
    finally:
        sys.dont_write_bytecode = previous
    endpoint_files: dict[Path, list[str]] = {}
    for jf in sorted(manifests_dir.glob("*.json")) if manifests_dir.is_dir() else []:
        items = json.loads(jf.read_text(encoding="utf-8"))
        endpoint_files[jf] = [
            i["name"] for i in (items if isinstance(items, list) else [items])
        ]
    names_of_file = {**tool_files, **endpoint_files}

    # Union-find: a skill and every file providing one of its tools form one plugin.
    parent: dict[Any, Any] = {}

    def find(x: Any) -> Any:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    file_of_tool = {t: f for f, names in names_of_file.items() for t in names}
    for name in skill_dirs:
        find(("skill", name))
        for t in skill_reg.skills[name].required_tools:
            if t in file_of_tool:
                parent[find(("skill", name))] = find(("file", file_of_tool[t]))
    for f in names_of_file:
        find(("file", f))

    components: dict[Any, list[Any]] = {}
    for node in list(parent):
        components.setdefault(find(node), []).append(node)

    actions: list[str] = []
    plans: list[tuple[str, list[str], list[Path]]] = []
    for nodes in components.values():
        skills = sorted(n[1] for n in nodes if n[0] == "skill")
        files = sorted((n[1] for n in nodes if n[0] == "file"), key=lambda p: p.name)
        pid = skills[0] if skills else files[0].stem.replace("_", "-")
        if not NAME_RE.match(pid):
            raise ScaffoldError(f"cannot derive a valid plugin id from '{pid}'")
        if len(skills) > 1:
            actions.append(
                f"note: skills {', '.join(skills)} share tool files; merged into plugin '{pid}'"
            )
        plans.append((pid, skills, files))

    if len({p for p, _, _ in plans}) != len(plans):
        raise ScaffoldError("derived plugin ids collide; migrate manually")
    for pid, _, _ in plans:
        if (plugins_dir / pid).exists():
            raise ScaffoldError(
                f"{plugins_dir / pid} already exists; refusing to overwrite."
            )

    for pid, skills, files in sorted(plans):
        root = plugins_dir / pid
        first = skill_reg.skills[skills[0]] if skills else None
        provides_tools = sorted(t for f in files for t in names_of_file[f])
        manifest = {
            "id": pid,
            "name": _title(pid),
            "description": first.description
            if first
            else f"Tools migrated from {files[0].name}.",
            "version": first.version if first else "1.0.0",
            "category": "",
            "provides": {"skills": skills, "tools": provides_tools},
        }
        actions.append(
            f"plugin {pid}: {len(skills)} skill(s), {len(provides_tools)} tool(s)"
        )
        if dry_run:
            continue
        root.mkdir(parents=True)
        (root / MANIFEST_FILE).write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        for s in skills:
            (root / "skills").mkdir(exist_ok=True)
            shutil.move(str(skill_dirs[s]), str(root / "skills" / s))
        for f in files:
            sub = "tools" if f in tool_files else "endpoints"
            (root / sub).mkdir(exist_ok=True)
            shutil.move(str(f), str(root / sub / f.name))

    if not dry_run:
        for legacy in (skills_dir, tools_dir, manifests_dir):
            if legacy.is_dir():
                shutil.rmtree(legacy / "__pycache__", ignore_errors=True)
                if not any(legacy.iterdir()):
                    legacy.rmdir()
                else:
                    actions.append(f"note: {legacy} still has unmigrated files")
    return actions


# --- CLI ------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="haas-plugin", description=__doc__)
    p.add_argument("--plugins-dir", type=Path, default=Path("plugins"))
    p.add_argument(
        "--extra-dir",
        type=Path,
        action="append",
        default=[],
        help="Additional plugin root (lower precedence); repeatable",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    new = sub.add_parser("new", help="Scaffold a plugin, skill, tool or endpoint")
    nsub = new.add_subparsers(dest="kind", required=True)
    pl = nsub.add_parser(
        "plugin", help="Plugin pack with a first skill of the same name"
    )
    pl.add_argument("name")
    pl.add_argument(
        "--tools", help="Space/comma separated tool names the skill unlocks"
    )
    pl.add_argument(
        "--secrets", help="Space/comma separated secret names the skill may read"
    )
    pl.add_argument("--category", default="", help="Catalog category, e.g. devops")
    s = nsub.add_parser("skill")
    s.add_argument("name")
    s.add_argument("--plugin", required=True)
    s.add_argument("--tools")
    s.add_argument("--secrets")
    t = nsub.add_parser("tool")
    t.add_argument("name")
    t.add_argument("--plugin", required=True)
    t.add_argument("--approval", action="store_true", help="Require human approval")
    t.add_argument("--sandbox", action="store_true", help="Run on the sandbox queue")
    t.add_argument("--skill", help="Also add the tool to this skill's required_tools")
    e = nsub.add_parser("endpoint", help="Remote HTTP / MCP tool")
    e.add_argument("name")
    e.add_argument("--plugin", required=True)
    e.add_argument("--protocol", choices=["http", "mcp_http"], default="http")
    e.add_argument("--secret", help="Secret name used to sign/authenticate calls")

    sub.add_parser("list", help="List discovered plugins and their status")
    i = sub.add_parser("inspect", help="Show one plugin's skills, tools and problems")
    i.add_argument("plugin")
    for name in ("doctor", "validate"):
        d = sub.add_parser(
            name,
            help="Load everything like the server and check cross-references"
            + (" (alias of doctor)" if name == "validate" else ""),
        )
        d.add_argument(
            "path", nargs="?", type=Path, help="Plugins dir (default: --plugins-dir)"
        )
    m = sub.add_parser(
        "migrate", help="Convert the legacy flat layout into plugin packs"
    )
    m.add_argument("--dry-run", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pdir: Path = args.plugins_dir
    extra: list[Path] = args.extra_dir
    try:
        if args.cmd in ("doctor", "validate"):
            return cmd_doctor(args.path or pdir, extra)
        if args.cmd == "list":
            return cmd_list(pdir, extra)
        if args.cmd == "inspect":
            return cmd_inspect(pdir, args.plugin, extra)
        if args.cmd == "migrate":
            for line in migrate_flat_layout(pdir, args.dry_run):
                print(line)
            print(
                "dry run: nothing moved."
                if args.dry_run
                else "Migrated. Run `haas-plugin doctor`."
            )
            return 0
        if args.kind == "plugin":
            created = new_plugin(
                pdir, args.name, _split(args.tools), _split(args.secrets), args.category
            )
        elif args.kind == "skill":
            created = new_skill(
                pdir, args.plugin, args.name, _split(args.tools), _split(args.secrets)
            )
        elif args.kind == "tool":
            created = new_tool(
                pdir, args.plugin, args.name, args.approval, args.sandbox, args.skill
            )
        else:
            created = new_endpoint(
                pdir, args.plugin, args.name, args.protocol, args.secret
            )
    except (ScaffoldError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for path in created:
        print(f"created {path}")
    print("Next: replace every TODO, then run `haas-plugin doctor`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
