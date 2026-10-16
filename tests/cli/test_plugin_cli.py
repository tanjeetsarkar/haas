from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from haas_cli.plugin import ScaffoldError, main, new_skill, validate_plugins

REPO_PLUGINS = Path(__file__).resolve().parents[2] / "plugins"


@pytest.fixture
def pdir(tmp_path: Path) -> Path:
    shutil.copytree(REPO_PLUGINS / "_templates", tmp_path / "_templates")
    return tmp_path


def fill_todos(root: Path) -> None:
    for f in root.rglob("*"):
        if f.suffix in (".md", ".py", ".json") and "_templates" not in f.parts:
            f.write_text(
                f.read_text().replace("TODO:", "Done:").replace("TODO", "Done"),
                encoding="utf-8",
            )


def test_scaffold_all_three_then_validate(pdir: Path, capsys):
    assert (
        main(
            [
                "--plugins-dir",
                str(pdir),
                "new",
                "skill",
                "crm-ops",
                "--secrets",
                "CRM_TOKEN",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--plugins-dir",
                str(pdir),
                "new",
                "tool",
                "crm_lookup",
                "--skill",
                "crm-ops",
                "--approval",
                "--sandbox",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--plugins-dir",
                str(pdir),
                "new",
                "manifest",
                "crm_sync",
                "--protocol",
                "mcp_http",
                "--secret",
                "CRM_TOKEN",
            ]
        )
        == 0
    )

    skill_md = (pdir / "skills/crm-ops/SKILL.md").read_text()
    assert 'required_tools: "crm_lookup"' in skill_md and "{{" not in skill_md
    tool_py = (pdir / "tools/crm_lookup.py").read_text()
    assert (
        "requires_approval=True" in tool_py
        and "sandbox=True" in tool_py
        and "CrmLookupArgs" in tool_py
    )
    assert '"protocol": "mcp_http"' in (pdir / "manifests/crm_sync.json").read_text()

    # Fresh scaffolds must fail validation until TODOs are replaced
    rep = validate_plugins(pdir)
    assert any("TODO" in e for e in rep.errors)
    # crm_sync is not referenced by a skill, which is only a warning
    assert any("crm_sync" in w for w in rep.warnings)

    fill_todos(pdir)
    rep = validate_plugins(pdir)
    assert rep.errors == [], rep.errors
    assert (rep.skills, rep.tools) == (1, 2)
    assert main(["validate", str(pdir)]) == 0


def test_refuses_overwrite_and_bad_names(pdir: Path, capsys):
    new_skill(pdir, "alpha", [], [])
    with pytest.raises(ScaffoldError, match="already exists"):
        new_skill(pdir, "alpha", [], [])
    assert main(["--plugins-dir", str(pdir), "new", "skill", "Bad_Name"]) == 2
    assert main(["--plugins-dir", str(pdir), "new", "tool", "Bad-Name"]) == 2
    assert (
        main(["--plugins-dir", str(pdir), "new", "tool", "x_y", "--skill", "ghost"])
        == 2
    )
    assert (
        main(["--plugins-dir", str(pdir / "nowhere"), "new", "skill", "ok-name"]) == 2
    )
    assert "Template" in capsys.readouterr().err


def test_validate_catches_cross_reference_problems(tmp_path: Path):
    skill = tmp_path / "skills" / "demo"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Use when the user asks for a demo of validation errors in plugins.\n"
        'metadata:\n  required_tools: "ghost_tool secret_tool"\n  allowed_secrets: ""\n---\n'
        "# Demo\nRead `missing.md` first.\n"
    )
    (skill / "references" / "unused.md").write_text("x")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "secret_tool.py").write_text(
        "from pydantic import BaseModel\n"
        "class A(BaseModel):\n    x: str = ''\n"
        "def register(registry):\n"
        "    @registry.tool(name='secret_tool', description='Reads a secret it should not.', args_schema=A)\n"
        "    def secret_tool(ctx, args):\n        return ctx.get_secret('NOT_ALLOWED')\n"
    )
    (tmp_path / "tools" / "broken.py").write_text("raise RuntimeError('boom')\n")
    rep = validate_plugins(tmp_path)
    joined = "\n".join(rep.errors)
    assert "required tool 'ghost_tool'" in joined
    assert "NOT_ALLOWED" in joined
    assert "`missing.md`" in joined
    assert "boom" in joined
    assert any("unused.md" in w for w in rep.warnings)


def test_repo_plugins_validate_cleanly():
    rep = validate_plugins(REPO_PLUGINS)
    assert rep.errors == [], rep.errors
