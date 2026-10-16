from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from haas_cli.plugin import (
    ScaffoldError,
    main,
    migrate_flat_layout,
    new_plugin,
    new_skill,
    validate_plugins,
)
from plugin_helpers import TOOL, make_plugin, req

REPO_PLUGINS = Path(__file__).resolve().parents[2] / "plugins"


@pytest.fixture
def pdir(tmp_path: Path) -> Path:
    shutil.copytree(REPO_PLUGINS / "_templates", tmp_path / "_templates")
    return tmp_path


def run(pdir: Path, *args: str) -> int:
    return main(["--plugins-dir", str(pdir), *args])


def fill_todos(root: Path) -> None:
    for f in root.rglob("*"):
        if f.suffix in (".md", ".py", ".json") and "_templates" not in f.parts:
            f.write_text(
                f.read_text().replace("TODO:", "Done:").replace("TODO", "Done"),
                encoding="utf-8",
            )


def test_scaffold_plugin_tool_skill_endpoint_then_doctor(pdir: Path):
    assert (
        run(
            pdir,
            "new",
            "plugin",
            "crm-ops",
            "--secrets",
            "CRM_TOKEN",
            "--category",
            "sales",
        )
        == 0
    )
    assert (
        run(
            pdir,
            "new",
            "tool",
            "crm_lookup",
            "--plugin",
            "crm-ops",
            "--skill",
            "crm-ops",
            "--approval",
            "--sandbox",
        )
        == 0
    )
    assert (
        run(
            pdir,
            "new",
            "endpoint",
            "crm_sync",
            "--plugin",
            "crm-ops",
            "--protocol",
            "mcp_http",
            "--secret",
            "CRM_TOKEN",
        )
        == 0
    )
    assert run(pdir, "new", "skill", "crm-reports", "--plugin", "crm-ops") == 0

    root = pdir / "crm-ops"
    manifest = json.loads((root / "plugin.json").read_text())
    assert manifest["category"] == "sales"
    assert manifest["provides"] == {
        "skills": ["crm-ops", "crm-reports"],
        "tools": ["crm_lookup", "crm_sync"],
    }
    skill_md = (root / "skills/crm-ops/SKILL.md").read_text()
    assert 'required_tools: "crm_lookup"' in skill_md and "{{" not in skill_md
    tool_py = (root / "tools/crm_lookup.py").read_text()
    assert (
        "requires_approval=True" in tool_py
        and "sandbox=True" in tool_py
        and "CrmLookupArgs" in tool_py
    )
    assert '"protocol": "mcp_http"' in (root / "endpoints/crm_sync.json").read_text()

    rep = validate_plugins(pdir)  # fresh scaffolds fail until TODOs are replaced
    assert any("TODO" in e for e in rep.errors)
    assert any(
        "crm_sync" in w for w in rep.warnings
    )  # endpoint not referenced by a skill

    fill_todos(pdir)
    rep = validate_plugins(pdir)
    assert rep.errors == [], rep.errors
    assert (rep.plugins, rep.skills, rep.tools) == (1, 2, 2)
    assert run(pdir, "doctor") == 0 and run(pdir, "validate") == 0


def test_refuses_overwrite_and_bad_names(pdir: Path, capsys):
    new_plugin(pdir, "alpha", [], [])
    with pytest.raises(ScaffoldError, match="already exists"):
        new_plugin(pdir, "alpha", [], [])
    with pytest.raises(ScaffoldError, match="already exists"):
        new_skill(pdir, "alpha", "alpha", [], [])
    assert run(pdir, "new", "plugin", "Bad_Name") == 2
    assert run(pdir, "new", "skill", "Bad_Name", "--plugin", "alpha") == 2
    assert run(pdir, "new", "tool", "Bad-Name", "--plugin", "alpha") == 2
    assert run(pdir, "new", "tool", "x_y", "--plugin", "alpha", "--skill", "ghost") == 2
    assert run(pdir, "new", "tool", "x_y", "--plugin", "missing") == 2
    assert run(pdir / "nowhere", "new", "plugin", "ok-name") == 2
    err = capsys.readouterr().err
    assert "Template" in err and "not found" in err


def test_doctor_catches_cross_reference_problems(tmp_path: Path):
    plugin = make_plugin(
        tmp_path,
        "demo",
        skills={"demo": req("ghost_tool", "secret_tool")},
        manifest={"provides": {"skills": ["demo"], "tools": ["secret_tool"]}},
    )
    (plugin / "skills/demo/SKILL.md").write_text(
        "---\nname: demo\ndescription: Use when the user asks for a demo of validation errors in plugins.\n"
        'metadata:\n  required_tools: "ghost_tool secret_tool"\n  allowed_secrets: ""\n---\n'
        "# Demo\nRead `missing.md` first.\n"
    )
    (plugin / "skills/demo/references").mkdir()
    (plugin / "skills/demo/references/unused.md").write_text("x")
    (plugin / "tools").mkdir()
    (plugin / "tools/secret_tool.py").write_text(
        "from pydantic import BaseModel\n"
        "class A(BaseModel):\n    x: str = ''\n"
        "def register(registry):\n"
        "    @registry.tool(name='secret_tool', description='Reads a secret it should not.', args_schema=A)\n"
        "    def secret_tool(ctx, args):\n        return ctx.get_secret('NOT_ALLOWED')\n"
    )
    make_plugin(tmp_path, "broken")
    (tmp_path / "broken/tools").mkdir()
    (tmp_path / "broken/tools/b.py").write_text("raise RuntimeError('boom')\n")

    rep = validate_plugins(tmp_path)
    joined = "\n".join(rep.errors)
    assert "required tool 'ghost_tool'" in joined
    assert "NOT_ALLOWED" in joined
    assert "`missing.md`" in joined
    assert "boom" in joined and "plugin broken" in joined
    assert any("unused.md" in w for w in rep.warnings)


def test_doctor_warns_on_undeclared_cross_plugin_dependency(tmp_path: Path):
    make_plugin(
        tmp_path,
        "base",
        tools=["base_tool"],
        manifest={"provides": {"skills": [], "tools": ["base_tool"]}},
    )
    make_plugin(
        tmp_path,
        "app",
        skills={"app": req("base_tool")},
        manifest={"provides": {"skills": ["app"], "tools": []}},
    )
    rep = validate_plugins(tmp_path)
    assert any("requires.plugins" in w and "base" in w for w in rep.warnings)


def test_doctor_validates_disabled_plugins_too(tmp_path: Path):
    bad = make_plugin(tmp_path, "off", manifest={"enabled": False})
    (bad / "tools").mkdir()
    (bad / "tools/t.py").write_text("raise RuntimeError('latent bug')")
    assert any("latent bug" in e for e in validate_plugins(tmp_path).errors)


def test_doctor_warns_when_catalog_exceeds_budget(tmp_path: Path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "skills_prompt_max_chars", 30)
    make_plugin(tmp_path, "alpha", skills={"alpha": ""})
    assert any(
        "SKILLS_PROMPT_MAX_CHARS" in w for w in validate_plugins(tmp_path).warnings
    )


def test_repo_plugins_doctor_cleanly():
    rep = validate_plugins(REPO_PLUGINS)
    assert rep.errors == [] and rep.warnings == [], (rep.errors, rep.warnings)
    assert rep.plugins == 7


def test_list_and_inspect(tmp_path: Path, capsys):
    make_plugin(
        tmp_path,
        "alpha",
        skills={"alpha": req("a_tool", "missing_tool")},
        tools=["a_tool"],
        manifest={"category": "ops"},
    )
    assert run(tmp_path, "list") == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "loaded" in out and "ops" in out and "fingerprint" in out
    assert run(tmp_path, "inspect", "alpha") == 0
    out = capsys.readouterr().out
    assert "NOT READY" in out and "missing_tool" in out and "a_tool" in out
    assert run(tmp_path, "inspect", "nope") == 2


# --- migrate ----------------------------------------------------------------------------------


def write_flat_layout(root: Path) -> None:
    def skill(name: str, tools: str) -> None:
        d = root / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Use when testing {name}.\nmetadata:\n"
            f'  version: "2.1.0"\n  required_tools: "{tools}"\n---\nbody\n'
        )

    skill("solo", "solo_tool solo_remote")
    skill("pair-a", "shared_a")
    skill("pair-b", "shared_b")
    skill("plain", "")
    (root / "tools").mkdir()
    for fname, names in {
        "solo_tools.py": ["solo_tool"],
        "shared_tools.py": ["shared_a", "shared_b"],
        "orphan_tools.py": ["orphan_tool"],
    }.items():
        body = TOOL.format(name=names[0])
        for extra in names[1:]:
            body = body.replace(
                "def register(registry):",
                "def register(registry):\n    _more(registry)",
                1,
            )
            body = (
                "def _more(registry):\n    from pydantic import BaseModel\n    class B(BaseModel):\n        pass\n"
                f"    @registry.tool(name='{extra}', description='Another tool for tests.', args_schema=B)\n"
                "    def _t(args):\n        return 'ok'\n\n" + body
            )
        (root / "tools" / fname).write_text(body)
    (root / "manifests").mkdir()
    (root / "manifests" / "remote.json").write_text(
        json.dumps(
            [
                {
                    "name": "solo_remote",
                    "description": "Remote tool for tests.",
                    "endpoint_url": "https://example.com/x",
                    "parameters": {"type": "object", "properties": {}},
                }
            ]
        )
    )


def test_migrate_flat_layout(tmp_path: Path, capsys):
    write_flat_layout(tmp_path)
    assert main(["--plugins-dir", str(tmp_path), "migrate", "--dry-run"]) == 0
    assert (tmp_path / "skills").is_dir() and not (tmp_path / "solo").exists()

    actions = migrate_flat_layout(tmp_path)
    assert any("merged into plugin 'pair-a'" in a for a in actions)
    assert not (tmp_path / "skills").exists() and not (tmp_path / "tools").exists()
    assert not (tmp_path / "manifests").exists()
    for pid in ("solo", "pair-a", "plain", "orphan-tools"):
        assert (tmp_path / pid / "plugin.json").is_file()
    assert not (tmp_path / "pair-b").exists()
    assert (tmp_path / "pair-a/skills/pair-b/SKILL.md").is_file()
    assert (tmp_path / "pair-a/tools/shared_tools.py").is_file()
    assert (tmp_path / "orphan-tools/tools/orphan_tools.py").is_file()
    solo = json.loads((tmp_path / "solo/plugin.json").read_text())
    assert solo["version"] == "2.1.0" and solo["provides"]["skills"] == ["solo"]
    assert solo["provides"]["tools"] == ["solo_remote", "solo_tool"]
    assert (tmp_path / "solo/endpoints/remote.json").is_file()

    rep = validate_plugins(tmp_path)
    assert not any(
        "error" in e.lower() and "not registered" in e for e in rep.errors
    ), rep.errors
    assert rep.plugins == 4 and rep.skills == 4
    with pytest.raises(ScaffoldError, match="No legacy"):
        migrate_flat_layout(tmp_path)
