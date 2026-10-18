"""Skill allowlists, hidden skills and registry drift across API, activation and tool dispatch."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import app.core.plugins as plugins_mod
import app.main as main_mod
from app.core.config import settings
from app.core.context import RunContext
from app.core.plugins import ActivateSkillArgs, PluginRegistry, activate_skill
from app.worker import tasks

from plugin_helpers import make_plugin, req


@pytest.fixture
def reg(tmp_path: Path) -> PluginRegistry:
    make_plugin(
        tmp_path,
        "alpha",
        skills={
            "one": req("t_one"),
            "two": req("t_two"),
            "ghost": req("nowhere"),
            "hidden": 'metadata:\n  disable_model_invocation: "true"\n  required_tools: "t_one"',
        },
        tools=["t_one", "t_two"],
    )
    r = PluginRegistry()
    r.discover_all(tmp_path)
    # Core tools live on the module-level registry; copy the real ones in so dispatch tests work.
    for name in ("activate_skill", "read_skill_reference"):
        r.tools[name] = plugins_mod.registry.tools[name]
    return r


def ctx(**kw: Any) -> RunContext:
    return RunContext(
        run_id="r",
        thread_id="t",
        tenant_id="x",
        traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01",
        **kw,
    )


# --- activate_skill -------------------------------------------------------------------------


async def test_activate_skill_respects_visibility(reg, monkeypatch):
    monkeypatch.setattr(plugins_mod, "registry", reg)
    c = ctx(skill_allowlist={"one", "hidden", "ghost"})
    out = await activate_skill(
        c, ActivateSkillArgs(skill_names=["one", "two", "ghost", "hidden"])
    )
    assert "ACTIVATED SKILL: one" in out
    # outside the allowlist, not ready and hidden all read as "unknown" and are not activated
    assert c.active_skills == {"one"}
    assert "Unknown skill(s): ['two', 'ghost', 'hidden']" in out
    assert "Available: [one]" in out


async def test_activate_skill_rejects_when_nothing_is_available(reg, monkeypatch):
    from app.core.context import ToolRemediationError

    monkeypatch.setattr(plugins_mod, "registry", reg)
    with pytest.raises(ToolRemediationError, match="Unknown skill"):
        await activate_skill(
            ctx(skill_allowlist=set()), ActivateSkillArgs(skill_names=["one"])
        )


async def test_preloaded_hidden_skill_can_be_reread(reg, monkeypatch):
    monkeypatch.setattr(plugins_mod, "registry", reg)
    c = ctx(active_skills={"hidden"})
    out = await activate_skill(c, ActivateSkillArgs(skill_names=["hidden"]))
    assert "ACTIVATED SKILL: hidden" in out


# --- tool dispatch --------------------------------------------------------------------------


async def run_tool(reg, name: str, skills: set[str], allowlist: set[str] | None):
    return await tasks._async_execute_tool(
        run_id="r",
        thread_id="t",
        tenant_id="x",
        traceparent="tp",
        active_skills=skills,
        allowed_secrets=set(),
        scoped_secrets={},
        tool_call={"id": "c1", "function": {"name": name, "arguments": "{}"}},
        skill_allowlist=allowlist,
    )


async def test_dispatch_blocks_tools_of_skills_outside_allowlist(reg, monkeypatch):
    monkeypatch.setattr(tasks, "registry", reg)
    assert (await run_tool(reg, "t_two", {"two"}, None))["content"] == "ok"
    blocked = await run_tool(reg, "t_two", {"two"}, {"one"})
    assert blocked["is_error"] and "not unlocked" in blocked["content"]
    assert (await run_tool(reg, "t_one", {"one"}, {"one"}))["content"] == "ok"
    # an unready skill never unlocks anything
    assert (await run_tool(reg, "nowhere", {"ghost"}, None))["is_error"]


# --- API ------------------------------------------------------------------------------------


class FakeStore:
    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []

    async def get_or_create_thread_messages(self, thread_id, tenant_id):
        return self.history

    async def store_ephemeral_secrets(self, run_id, secrets):
        pass

    async def clear_ephemeral_secrets(self, run_id):
        pass

    async def create_run(self, **kw):
        self.runs.append(kw)

    async def publish_event(self, *a, **kw):
        pass


class Dispatched:
    """create_run enqueues through the transactional outbox, so a created run is dispatched."""

    def __init__(self, store: FakeStore) -> None:
        self.calls = store.runs


@pytest.fixture
def api(reg, monkeypatch):
    store = FakeStore()
    dispatched = Dispatched(store)
    monkeypatch.setattr(main_mod, "registry", reg)
    monkeypatch.setattr(main_mod, "store", store)
    monkeypatch.setattr(
        settings,
        "tenant_skill_allowlists",
        json.dumps({"locked": ["one"], "*": ["one", "two", "hidden"]}),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main_mod.app), base_url="http://t"
    )
    return client, store, dispatched


async def post(client, tenant: str, **body):
    return await client.post(
        "/v1/runs", json={"prompt": "hi", **body}, headers={"X-Tenant-ID": tenant}
    )


async def test_create_run_persists_effective_allowlist_and_fingerprint(api):
    client, store, dispatched = api
    r = await post(client, "locked")
    assert r.status_code == 202
    run = store.runs[-1]
    assert run["skill_allowlist"] == ["one"]
    assert run["registry_fingerprint"] == main_mod.registry.fingerprint
    system = store.history[0]["content"]
    assert "`one`" in system and "`two`" not in system and "hidden" not in system
    assert dispatched.calls


async def test_run_request_can_only_narrow_tenant_allowlist(api):
    client, store, _ = api
    assert (await post(client, "other", skills=["two"])).status_code == 202
    assert store.runs[-1]["skill_allowlist"] == ["two"]
    wider = await post(client, "locked", skills=["one", "two"])
    assert wider.status_code == 422 and "not permitted" in wider.json()["detail"]
    unknown = await post(client, "other", skills=["nope"])
    assert unknown.status_code == 422


async def test_preload_rules(api):
    client, store, _ = api
    assert (
        await post(client, "other", preload_skills=["hidden"])
    ).status_code == 202  # hidden is preloadable
    assert store.runs[-1]["initial_skills"] == ["hidden"]
    for bad in (["ghost"], ["nope"]):  # unready / unknown
        assert (await post(client, "other", preload_skills=bad)).status_code == 422
    assert (
        await post(client, "locked", preload_skills=["two"])
    ).status_code == 422  # outside tenant list


async def test_plugins_endpoint_is_tenant_filtered_and_reports_state(api):
    client, _, _ = api
    body = (await client.get("/v1/plugins", headers={"X-Tenant-ID": "locked"})).json()
    assert body["fingerprint"] == main_mod.registry.fingerprint
    assert {s["name"] for s in body["skills"]} == {"one"}
    assert (
        body["plugins"][0]["id"] == "alpha" and body["plugins"][0]["status"] == "loaded"
    )
    everyone = (await client.get("/v1/plugins", headers={"X-Tenant-ID": "x"})).json()
    flags = {
        s["name"]: (s["hidden"], s["ready"])
        for s in everyone["skills"]
        if s["name"] in ("one", "two")
    }
    assert flags == {"one": (False, True), "two": (False, True)}


def test_malformed_tenant_allowlist_fails_fast(monkeypatch):
    monkeypatch.setattr(settings, "tenant_skill_allowlists", '{"a": "not-a-list"}')
    with pytest.raises(ValueError, match="TENANT_SKILL_ALLOWLISTS"):
        PluginRegistry().discover_from_settings(settings)
