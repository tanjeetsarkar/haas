"""`haas-plugin`: scaffold skills/tools/manifests from plugins/_templates and validate a plugins dir."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from app.core.plugins import SKILL_NAME_RE, PluginRegistry
from app.core.plugins import registry as core_registry

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


def new_skill(
    plugins_dir: Path, name: str, tools: list[str], secrets: list[str]
) -> list[Path]:
    if len(name) > 64 or not SKILL_NAME_RE.match(name):
        raise ScaffoldError(
            f"Invalid skill name '{name}': lowercase a-z, 0-9 and single hyphens, max 64 chars."
        )
    for t in tools:
        if not TOOL_NAME_RE.match(t):
            raise ScaffoldError(f"Invalid tool name '{t}': use snake_case.")
    for s in secrets:
        if not ENV_VAR_RE.match(s):
            raise ScaffoldError(f"Invalid secret name '{s}': use UPPER_SNAKE_CASE.")
    skill_dir = plugins_dir / "skills" / name
    values = {
        "name": name,
        "title": _title(name),
        "required_tools": " ".join(tools),
        "allowed_secrets": " ".join(secrets),
        "first_tool": tools[0] if tools else "your_tool",
    }
    created = [
        _write_new(
            skill_dir / "SKILL.md",
            render(_template(plugins_dir, "skill", "SKILL.md"), values),
        ),
        _write_new(
            skill_dir / "references" / "reference.md",
            _template(plugins_dir, "skill", "references", "reference.md"),
        ),
    ]
    return created


def new_tool(
    plugins_dir: Path,
    name: str,
    approval: bool = False,
    sandbox: bool = False,
    skill: str | None = None,
) -> list[Path]:
    if not TOOL_NAME_RE.match(name):
        raise ScaffoldError(
            f"Invalid tool name '{name}': snake_case, starts with a letter (e.g. github_list_issues)."
        )
    values = {
        "tool_name": name,
        "class_name": "".join(p.capitalize() for p in name.split("_")),
        "title": _title(name),
        "requires_approval": str(approval),
        "sandbox": str(sandbox),
    }
    path = _write_new(
        plugins_dir / "tools" / f"{name}.py",
        render(_template(plugins_dir, "tool", "tool.py"), values),
    )
    if skill:
        skill_md = plugins_dir / "skills" / skill / "SKILL.md"
        if not skill_md.is_file():
            raise ScaffoldError(f"Skill '{skill}' not found at {skill_md}.")
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


def new_manifest(
    plugins_dir: Path, name: str, protocol: str = "http", secret: str | None = None
) -> list[Path]:
    if not TOOL_NAME_RE.match(name):
        raise ScaffoldError(f"Invalid tool name '{name}': use snake_case.")
    if protocol not in ("http", "mcp_http"):
        raise ScaffoldError("protocol must be 'http' or 'mcp_http'.")
    if secret and not ENV_VAR_RE.match(secret):
        raise ScaffoldError(f"Invalid secret name '{secret}': use UPPER_SNAKE_CASE.")
    values = {
        "tool_name": name,
        "protocol": protocol,
        "env_var": f"{name.upper()}_URL",
        "secret_key": secret or "",
    }
    content = render(_template(plugins_dir, "manifest", "external.json"), values)
    if not secret:
        data = json.loads(content)
        data.pop("secret_key")
        content = json.dumps(data, indent=2) + "\n"
    return [_write_new(plugins_dir / "manifests" / f"{name}.json", content)]


# --- validation -----------------------------------------------------------------------------


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
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


def _load_tool_module(py_file: Path, reg: PluginRegistry) -> None:
    spec = importlib.util.spec_from_file_location(
        f"haas_plugin_{py_file.stem}", py_file
    )
    if not (spec and spec.loader):
        raise ImportError("cannot create import spec")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = (
        mod  # same as the server: pydantic needs it for postponed annotations
    )
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    if not hasattr(mod, "register"):
        raise AttributeError("module has no `register(registry)` function")
    mod.register(reg)


def validate_plugins(plugins_dir: Path) -> Report:
    # Never leave (possibly stale) bytecode behind in the plugins directory.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        return _validate(plugins_dir)
    finally:
        sys.dont_write_bytecode = previous


def _validate(plugins_dir: Path) -> Report:
    rep = Report()
    reg = PluginRegistry()
    tool_files: dict[str, set[str]] = {}  # tool name -> secrets used in source
    core = set(core_registry.tools)

    for skill_dir in sorted((plugins_dir / "skills").glob("*/")):
        try:
            reg.load_skill_directory(skill_dir)
        except Exception as exc:
            rep.errors.append(f"skill {skill_dir.name}: {exc}")

    for py_file in sorted((plugins_dir / "tools").glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        before = set(reg.tools)
        try:
            _load_tool_module(py_file, reg)
        except Exception as exc:
            rep.errors.append(f"tools/{py_file.name}: {type(exc).__name__}: {exc}")
            continue
        used = secrets_used(py_file.read_text(encoding="utf-8"))
        for t in set(reg.tools) - before:
            tool_files[t] = used
        if not set(reg.tools) - before:
            rep.warnings.append(f"tools/{py_file.name}: register() added no tools.")

    manifest_secrets: dict[str, str] = {}
    for jf in sorted((plugins_dir / "manifests").glob("*.json")):
        try:
            items = json.loads(jf.read_text(encoding="utf-8"))
            for item in items if isinstance(items, list) else [items]:
                reg.register_external_manifest(item)
                if item.get("secret_key"):
                    manifest_secrets[item["name"]] = item["secret_key"]
        except Exception as exc:
            rep.errors.append(f"manifests/{jf.name}: {type(exc).__name__}: {exc}")

    rep.skills, rep.tools = len(reg.skills), len(reg.tools)
    allowed_anywhere = {s for sk in reg.skills.values() for s in sk.allowed_secrets}
    referenced = {t for sk in reg.skills.values() for t in sk.required_tools}

    for name, tool in reg.tools.items():
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
    for name in sorted(tool_files):
        for secret in sorted(tool_files[name] - allowed_anywhere):
            rep.errors.append(
                f"tool {name}: reads secret {secret} which no skill lists in allowed_secrets."
            )
    for name, secret in sorted(manifest_secrets.items()):
        if secret not in allowed_anywhere:
            rep.errors.append(
                f"manifest {name}: secret_key {secret} is not in any skill's allowed_secrets."
            )

    for sk in reg.skills.values():
        for t in sk.required_tools:
            if t not in reg.tools and t not in core:
                rep.errors.append(
                    f"skill {sk.name}: required tool '{t}' is not registered."
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
    return rep


def cmd_validate(plugins_dir: Path) -> int:
    rep = validate_plugins(plugins_dir)
    for w in rep.warnings:
        print(f"warning: {w}")
    for e in rep.errors:
        print(f"error:   {e}")
    print(
        f"{rep.skills} skill(s), {rep.tools} tool(s): "
        f"{len(rep.errors)} error(s), {len(rep.warnings)} warning(s)"
    )
    return 1 if rep.errors else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="haas-plugin", description=__doc__)
    p.add_argument("--plugins-dir", type=Path, default=Path("plugins"))
    sub = p.add_subparsers(dest="cmd", required=True)
    new = sub.add_parser(
        "new", help="Create a skill, tool or manifest from the templates"
    )
    nsub = new.add_subparsers(dest="kind", required=True)
    s = nsub.add_parser("skill")
    s.add_argument("name")
    s.add_argument("--tools", help="Space/comma separated tool names the skill unlocks")
    s.add_argument(
        "--secrets", help="Space/comma separated secret names the skill may read"
    )
    t = nsub.add_parser("tool")
    t.add_argument("name")
    t.add_argument("--approval", action="store_true", help="Require human approval")
    t.add_argument("--sandbox", action="store_true", help="Run on the sandbox queue")
    t.add_argument("--skill", help="Also add the tool to this skill's required_tools")
    m = nsub.add_parser("manifest")
    m.add_argument("name")
    m.add_argument("--protocol", choices=["http", "mcp_http"], default="http")
    m.add_argument("--secret", help="Secret name used to sign/authenticate calls")
    v = sub.add_parser("validate", help="Validate every skill/tool/manifest")
    v.add_argument(
        "path", nargs="?", type=Path, help="Plugins dir (default: --plugins-dir)"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pdir: Path = args.plugins_dir
    try:
        if args.cmd == "validate":
            return cmd_validate(args.path or pdir)
        if args.kind == "skill":
            created = new_skill(
                pdir, args.name, _split(args.tools), _split(args.secrets)
            )
        elif args.kind == "tool":
            created = new_tool(pdir, args.name, args.approval, args.sandbox, args.skill)
        else:
            created = new_manifest(pdir, args.name, args.protocol, args.secret)
    except ScaffoldError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for path in created:
        print(f"created {path}")
    print("Next: replace every TODO, then run `haas-plugin validate`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
