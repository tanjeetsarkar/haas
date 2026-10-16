from __future__ import annotations

from pathlib import Path

from app.core.plugins import PluginRegistry

TOOL_SRC = """
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Args(BaseModel):
    mode: Literal["a", "b"] = "a"


def register(registry):
    @registry.tool(name="lit_tool", description="Uses a Literal under postponed annotations.", args_schema=Args)
    def lit_tool(args: Args) -> str:
        return args.mode
"""


def test_discover_all_supports_postponed_annotations_with_typing_imports(
    tmp_path: Path,
):
    (tmp_path / "lit" / "tools").mkdir(parents=True)
    (tmp_path / "lit" / "plugin.json").write_text(
        '{"id": "lit", "description": "Literal tool plugin."}'
    )
    (tmp_path / "lit" / "tools" / "lit_tools.py").write_text(TOOL_SRC)
    reg = PluginRegistry()
    reg.discover_all(tmp_path)
    schema = reg.tools["lit_tool"].to_openai_schema()
    assert schema["function"]["parameters"]["properties"]["mode"]["enum"] == ["a", "b"]
    assert reg.tools["lit_tool"].plugin_id == "lit"
