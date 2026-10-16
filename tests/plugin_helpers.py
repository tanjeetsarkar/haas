"""Builders for throw-away plugin packs used by the loader, policy and CLI tests."""

from __future__ import annotations

import json
from pathlib import Path

TOOL = """
from pydantic import BaseModel


class A(BaseModel):
    x: str = ""


def register(registry):
    @registry.tool(name="{name}", description="A tool for tests that does nothing.", args_schema=A)
    def {name}(args: A) -> str:
        return "ok"
"""


def make_plugin(
    root: Path,
    pid: str,
    *,
    skills: dict[str, str] | None = None,
    tools: list[str] = (),
    manifest: dict | None = None,
    skill_dir: str = "skills",
) -> Path:
    """skills: name -> extra frontmatter lines (YAML)."""
    plugin = root / pid
    plugin.mkdir(parents=True)
    data = {"id": pid, "description": f"Plugin {pid} for tests."}
    data.update(manifest or {})
    (plugin / "plugin.json").write_text(json.dumps(data))
    for name, extra in (skills or {}).items():
        d = plugin / skill_dir / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Use when testing {name}.\n{extra}\n---\nbody of {name}\n"
        )
    for t in tools:
        (plugin / "tools").mkdir(exist_ok=True)
        (plugin / "tools" / f"{t}.py").write_text(TOOL.format(name=t))
    return plugin


def req(*tools: str) -> str:
    return f'metadata:\n  required_tools: "{" ".join(tools)}"'
