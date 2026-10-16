"""Plugin packs: manifests, isolation, policy, readiness, visibility and the Tier-1 catalog."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.plugin_manifest import (
    PluginManifestError,
    find_skill_dirs,
    read_plugin_manifest,
)
from app.core.plugins import (
    PluginLoadError,
    PluginRegistry,
    resolve_run_allowlist,
)
from plugin_helpers import TOOL, make_plugin, req


def load(root: Path, **kw) -> PluginRegistry:
    reg = PluginRegistry()
    reg.discover_all(root, **kw)
    return reg


# --- manifest ------------------------------------------------------------------------------


def test_pack_registers_skills_and_tools_with_provenance(tmp_path):
    make_plugin(
        tmp_path,
        "alpha",
        skills={"alpha": req("a_tool")},
        tools=["a_tool"],
        manifest={
            "category": "ops",
            "provides": {"skills": ["alpha"], "tools": ["a_tool"]},
        },
    )
    reg = load(tmp_path)
    rec = reg.plugins["alpha"]
    assert (
        rec.status == "loaded" and rec.skills == ["alpha"] and rec.tools == ["a_tool"]
    )
    assert reg.tools["a_tool"].plugin_id == "alpha"
    assert reg.tools["a_tool"].source == "a_tool.py"
    assert (
        reg.skills["alpha"].plugin_id == "alpha"
        and reg.skills["alpha"].category == "ops"
    )
    assert reg.skills["alpha"].ready


@pytest.mark.parametrize(
    "data,msg",
    [
        ({"id": "Bad_ID"}, "invalid plugin id"),
        ({"id": "other"}, "must match directory"),
        ({"description": ""}, "description"),
        ({"surprise": 1}, "unknown manifest key"),
        ({"tools": ["../evil.py"]}, "inside the plugin"),
        ({"enabled": "yes"}, "boolean"),
        ({"provides": {"widgets": []}}, "provides/requires"),
    ],
)
def test_manifest_validation_errors(tmp_path, data, msg):
    plugin = make_plugin(tmp_path, "alpha")
    base = json.loads((plugin / "plugin.json").read_text())
    base.update(data)
    (plugin / "plugin.json").write_text(json.dumps(base))
    with pytest.raises(PluginManifestError, match=msg):
        read_plugin_manifest(plugin)


def test_manifest_rejects_symlink_escape(tmp_path):
    plugin = make_plugin(tmp_path, "alpha")
    outside = tmp_path / "outside.py"
    outside.write_text(TOOL.format(name="sneaky"))
    (plugin / "tools").mkdir()
    (plugin / "tools" / "sneaky.py").symlink_to(outside)
    reg = load(tmp_path)
    assert reg.plugins["alpha"].status == "failed"
    assert "outside the plugin" in reg.plugins["alpha"].error
    assert "sneaky" not in reg.tools


def test_manifest_is_read_without_importing_code(tmp_path):
    plugin = make_plugin(tmp_path, "alpha", tools=["a_tool"])
    (plugin / "tools" / "a_tool.py").write_text("raise RuntimeError('must not run')")
    m = read_plugin_manifest(plugin)
    assert [f.name for f in m.tool_files] == ["a_tool.py"]


def test_grouped_skill_layout(tmp_path):
    make_plugin(tmp_path, "alpha", skills={"one": "", "two": ""})
    root = tmp_path / "alpha" / "skills"
    (root / "group").mkdir()
    (root / "two").rename(root / "group" / "two")
    names = [d.name for d in find_skill_dirs(root)]
    assert sorted(names) == ["one", "two"]
    assert set(load(tmp_path).skills) == {"one", "two"}


# --- isolation, collisions, provides -------------------------------------------------------


def test_failed_plugin_is_isolated_and_rolled_back(tmp_path):
    make_plugin(
        tmp_path, "good", skills={"good": req("good_tool")}, tools=["good_tool"]
    )
    bad = make_plugin(tmp_path, "bad", skills={"bad": ""}, tools=["bad_ok"])
    (bad / "tools" / "zz_boom.py").write_text("raise RuntimeError('boom')")
    reg = load(tmp_path)
    assert reg.plugins["good"].status == "loaded"
    assert reg.plugins["bad"].status == "failed" and "boom" in reg.plugins["bad"].error
    assert "bad_ok" not in reg.tools and "bad" not in reg.skills  # rolled back
    assert any(d.level == "error" and d.plugin_id == "bad" for d in reg.diagnostics)


def test_strict_mode_aborts(tmp_path):
    bad = make_plugin(tmp_path, "bad")
    (bad / "tools").mkdir()
    (bad / "tools" / "x.py").write_text("raise RuntimeError('boom')")
    with pytest.raises(PluginLoadError, match="bad"):
        load(tmp_path, strict=True)


def test_cross_plugin_tool_and_skill_collisions_fail_the_later_plugin(tmp_path):
    make_plugin(tmp_path, "a-first", skills={"shared": ""}, tools=["dup_tool"])
    make_plugin(tmp_path, "b-second", skills={"shared": ""}, tools=["dup_tool"])
    reg = load(tmp_path)
    assert reg.plugins["a-first"].status == "loaded"
    assert reg.plugins["b-second"].status == "failed"
    assert "already provided by plugin 'a-first'" in reg.plugins["b-second"].error
    assert reg.tools["dup_tool"].plugin_id == "a-first"


def test_provides_must_match_registered(tmp_path):
    make_plugin(
        tmp_path,
        "alpha",
        tools=["a_tool"],
        manifest={"provides": {"tools": ["a_tool", "ghost"]}},
    )
    reg = load(tmp_path)
    assert reg.plugins["alpha"].status == "failed"
    assert "does not match" in reg.plugins["alpha"].error


def test_module_names_are_namespaced_per_plugin(tmp_path):
    make_plugin(tmp_path, "one", tools=["tool_one"])
    make_plugin(tmp_path, "two", tools=["tool_two"])
    (tmp_path / "two" / "tools" / "tool_one.py").write_text(
        TOOL.format(name="tool_two_b")
    )
    reg = load(tmp_path)  # same file stem in two plugins must not clash
    assert {"tool_one", "tool_two", "tool_two_b"} <= set(reg.tools)


def test_legacy_flat_layout_is_reported_not_loaded(tmp_path):
    (tmp_path / "skills" / "old").mkdir(parents=True)
    (tmp_path / "skills" / "old" / "SKILL.md").write_text(
        "---\nname: old\ndescription: d\n---\n"
    )
    reg = load(tmp_path)
    assert reg.skills == {}
    assert any("haas-plugin migrate" in d.message for d in reg.diagnostics)


def test_requires_plugins_orders_and_blocks(tmp_path):
    make_plugin(tmp_path, "a-app", manifest={"requires": {"plugins": ["z-base"]}})
    make_plugin(tmp_path, "z-base", tools=["base_tool"])
    make_plugin(tmp_path, "orphan", manifest={"requires": {"plugins": ["missing"]}})
    reg = load(tmp_path)
    assert reg.plugins["a-app"].status == "loaded"
    assert reg.plugins["orphan"].status == "blocked"


# --- policy and roots ----------------------------------------------------------------------


def test_allow_deny_and_enabled_flag(tmp_path):
    make_plugin(tmp_path, "keep", tools=["keep_tool"])
    make_plugin(tmp_path, "off", tools=["off_tool"], manifest={"enabled": False})
    make_plugin(tmp_path, "denied", tools=["denied_tool"])
    reg = load(tmp_path, deny=["denied"])
    assert reg.plugins["keep"].status == "loaded"
    assert reg.plugins["off"].status == "disabled" and "off_tool" not in reg.tools
    assert reg.plugins["denied"].status == "blocked"

    only = load(tmp_path, allow=["keep"])
    assert (
        only.plugins["off"].status == "blocked"
        and only.plugins["denied"].status == "blocked"
    )
    # an explicit allow switches on a plugin that ships disabled; deny still wins
    forced = load(tmp_path, allow=["off", "denied"], deny=["denied"])
    assert forced.plugins["off"].status == "loaded"
    assert forced.plugins["denied"].status == "blocked"
    assert load(tmp_path, include_disabled=True).plugins["off"].status == "loaded"


def test_extra_roots_have_lower_precedence(tmp_path):
    primary, extra = tmp_path / "primary", tmp_path / "extra"
    make_plugin(primary, "shared", tools=["from_primary"])
    make_plugin(extra, "shared", tools=["from_extra"])
    make_plugin(extra, "only-extra", tools=["extra_only"])
    reg = load(primary, extra_dirs=[extra])
    assert "from_primary" in reg.tools and "from_extra" not in reg.tools
    assert reg.plugins["only-extra"].status == "loaded"
    assert any("shadowed" in d.message for d in reg.diagnostics)


def test_rediscovery_resets_state(tmp_path):
    make_plugin(tmp_path, "alpha", tools=["a_tool"])
    reg = load(tmp_path)
    reg.discover_all(tmp_path)  # a second pass must not raise duplicate-tool errors
    assert reg.plugins["alpha"].status == "loaded" and "a_tool" in reg.tools


# --- readiness, hidden skills, allowlists ---------------------------------------------------


def test_unready_skill_is_not_offered_or_unlocked(tmp_path):
    make_plugin(
        tmp_path,
        "alpha",
        skills={"broken": req("ghost_tool"), "fine": req("real")},
        tools=["real"],
    )
    reg = load(tmp_path)
    broken = reg.skills["broken"]
    assert not broken.ready and "ghost_tool" in broken.unready_reason
    assert [s.name for s in reg.model_visible_skills()] == ["fine"]
    assert reg.resolve_unlocked_tool_names({"broken"}) == set()
    assert reg.resolve_unlocked_tool_names({"fine"}) == {"real"}


def test_hidden_skill_only_via_preload(tmp_path):
    make_plugin(
        tmp_path,
        "alpha",
        skills={
            "secret": 'metadata:\n  disable_model_invocation: "true"\n  required_tools: "real"',
            "open": "",
        },
        tools=["real"],
    )
    reg = load(tmp_path)
    assert reg.skills["secret"].hidden
    assert [s.name for s in reg.model_visible_skills()] == ["open"]
    assert "secret" not in reg.render_catalog()
    assert reg.resolve_unlocked_tool_names({"secret"}) == {
        "real"
    }  # preloaded still works


def test_allowlist_limits_tools_secrets_and_catalog(tmp_path):
    make_plugin(
        tmp_path,
        "alpha",
        skills={
            "one": 'metadata:\n  required_tools: "t1"\n  allowed_secrets: "S1"',
            "two": 'metadata:\n  required_tools: "t2"\n  allowed_secrets: "S2"',
        },
        tools=["t1", "t2"],
    )
    reg = load(tmp_path)
    both = {"one", "two"}
    assert reg.resolve_unlocked_tool_names(both) == {"t1", "t2"}
    assert reg.resolve_unlocked_tool_names(both, {"one"}) == {"t1"}
    assert reg.resolve_allowed_secrets(both, {"one"}) == {"S1"}
    assert reg.resolve_allowed_secrets(both, set()) == set()
    assert "two" not in reg.render_catalog({"one"}) and "one" in reg.render_catalog(
        {"one"}
    )


def test_resolve_run_allowlist_only_narrows():
    assert resolve_run_allowlist(None, None) is None
    assert resolve_run_allowlist(["a", "b"], None) == ["a", "b"]
    assert resolve_run_allowlist(None, ["b"]) == ["b"]
    assert resolve_run_allowlist(["a", "b"], ["b"]) == ["b"]
    assert resolve_run_allowlist(["a"], []) == []
    with pytest.raises(ValueError, match="not permitted"):
        resolve_run_allowlist(["a"], ["a", "c"])


# --- catalog -------------------------------------------------------------------------------


def test_catalog_is_flat_when_small_and_grouped_when_large(tmp_path):
    make_plugin(
        tmp_path, "small", skills={"s1": "", "s2": ""}, manifest={"category": "x"}
    )
    assert "###" not in load(tmp_path).render_catalog()

    root = tmp_path / "big"
    for i in range(5):
        make_plugin(
            root, f"ops-{i}", skills={f"ops-{i}": ""}, manifest={"category": "ops"}
        )
        make_plugin(
            root, f"dev-{i}", skills={f"dev-{i}": ""}, manifest={"category": "dev"}
        )
    text = load(root).render_catalog()
    assert text.index("### dev") < text.index("### ops")
    assert text == load(root).render_catalog()  # deterministic


def test_catalog_budget_compacts_descriptions_then_names(tmp_path):
    for i in range(6):
        make_plugin(tmp_path, f"p{i}", skills={f"p{i}": ""})
    reg = load(tmp_path)
    for s in reg.skills.values():
        s.description = "word " * 100
    full = reg.render_catalog()
    compact = reg.render_catalog(max_chars=600)
    assert len(compact) <= 600 and len(compact) < len(full)
    assert "compacted" in compact and all(f"`p{i}`" in compact for i in range(6))
    names_only = reg.render_catalog(max_chars=150)
    assert "word" not in names_only and "`p0`" in names_only
    assert reg.render_catalog(max_chars=10**6) == full


def test_empty_catalog(tmp_path):
    assert "No domain skills" in load(tmp_path).render_catalog()


# --- fingerprint ---------------------------------------------------------------------------


def test_fingerprint_changes_with_content_not_with_pycache(tmp_path):
    plugin = make_plugin(tmp_path, "alpha", skills={"alpha": ""}, tools=["a_tool"])
    first = load(tmp_path).fingerprint
    assert first and load(tmp_path).fingerprint == first
    (plugin / "skills" / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Use when testing alpha, changed.\n---\nnew body\n"
    )
    second = load(tmp_path).fingerprint
    assert second != first
    (plugin / "tools" / "__pycache__").mkdir(exist_ok=True)
    (plugin / "tools" / "__pycache__" / "x.pyc").write_bytes(b"junk")
    assert load(tmp_path).fingerprint == second
    assert load(tmp_path, deny=["alpha"]).fingerprint != second
