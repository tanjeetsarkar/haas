from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

import app.core.plugins as plugins_mod
from app.core.config import settings
from app.core.context import (
    RunContext,
    SecurityViolationError,
    ToolRemediationError,
    validate_safe_outbound_url,
)
from app.core.llm import OpenRouterGateway, repair_tool_json, sanitize_assistant_message
from app.core.plugins import (
    PluginRegistry,
    _parse_mcp_response,
    read_skill_reference,
    ReadSkillReferenceArgs,
)
from app.worker import tasks

PLUGINS = Path(__file__).resolve().parent.parent / "plugins"


def make_ctx(**kw) -> RunContext:
    base = dict(
        run_id="run_t",
        thread_id="thd_t",
        tenant_id="t",
        traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01",
    )
    base.update(kw)
    return RunContext(**base)


# --- llm helpers ---------------------------------------------------------------------------


def test_repair_tool_json_handles_fences_and_trailing_commas():
    assert repair_tool_json('```json\n{"a": 1,}\n```') == {"a": 1}
    assert repair_tool_json("") == {}
    assert repair_tool_json({"x": 1}) == {"x": 1}
    with pytest.raises(json.JSONDecodeError):
        repair_tool_json("[1, 2]")


def test_model_routing():
    sel = OpenRouterGateway.select_model_for_step
    assert sel(1, False, "primary", "planner") == "planner"
    assert sel(2, False, "primary", "planner") == "primary"
    assert sel(3, True, "primary", "planner") == "planner"


def test_sanitize_assistant_message_drops_extra_fields():
    msg = {
        "role": "assistant",
        "content": None,
        "reasoning": "secret thoughts",
        "refusal": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "index": 0,
                "function": {"name": "f", "arguments": ""},
            }
        ],
    }
    out = sanitize_assistant_message(msg)
    assert set(out) == {"role", "content", "tool_calls"}
    assert out["tool_calls"][0]["function"]["arguments"] == "{}"


# --- SSRF guard ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/x",
        "http://localhost/x",
        "http://redis:6379/",
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://metadata.google.internal/",
        "http:///nohost",
    ],
)
def test_ssrf_blocks_internal(url):
    with pytest.raises(SecurityViolationError):
        validate_safe_outbound_url(url)


def test_ssrf_allows_public_literal_and_dns_check_blocks_loopback_alias(monkeypatch):
    assert validate_safe_outbound_url("https://93.184.216.34/x")
    monkeypatch.setattr(
        "app.core.context.socket.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 80))],
    )
    with pytest.raises(SecurityViolationError):
        validate_safe_outbound_url("http://innocent.example.org/x", resolve_dns=True)


def test_ssrf_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "ssrf_allowed_hosts", "mock")
    assert validate_safe_outbound_url("http://mock:9000/x", resolve_dns=True)


# --- secrets -------------------------------------------------------------------------------


def test_secret_scoping():
    ctx = make_ctx(allowed_secrets={"A"}, _scoped_secrets={"A": "1", "B": "2"})
    assert ctx.get_secret("A") == "1"
    with pytest.raises(SecurityViolationError):
        ctx.get_secret("B")
    with pytest.raises(ToolRemediationError):
        make_ctx(allowed_secrets={"A"}).get_secret("A")


# --- skills & registry ---------------------------------------------------------------------


def test_discover_sample_plugins():
    reg = PluginRegistry()
    reg.discover_all(PLUGINS)
    skill = reg.skills["revenue-ops"]
    assert skill.version == "1.0.0"
    assert skill.allowed_secrets == ["ANALYTICS_DB_PATH", "BILLING_WEBHOOK_SECRET"]
    assert skill.available_references == ["billing_metrics.md"]
    assert set(skill.required_tools) <= set(reg.tools)
    assert reg.tools["prepare_refund_batch"].requires_approval
    assert reg.tools["prepare_refund_batch"].queue == "q_tools_sandbox"
    assert reg.tools["lookup_customer_ledger"].queue == "q_tools_fast"


def test_tool_schema_list_is_deterministic_and_progressively_disclosed():
    reg = PluginRegistry()
    reg.discover_all(PLUGINS)

    def names(skills):
        return [s["function"]["name"] for s in reg.resolve_sorted_tool_schemas(skills)]

    # `reg` has no core tools of its own (they live on the global registry), so only skill tools unlock.
    assert names(set()) == []
    unlocked = names({"revenue-ops"})
    assert unlocked == sorted(unlocked) and "query_revenue_metrics" in unlocked


def test_skill_validation(tmp_path):
    def write(dirname: str, fm: str):
        d = tmp_path / dirname
        d.mkdir()
        (d / "SKILL.md").write_text(f"---\n{fm}\n---\nbody\n")
        return d

    reg = PluginRegistry()
    with pytest.raises(ValueError, match="must match directory"):
        reg.load_skill_directory(write("one", "name: other\ndescription: d"))
    with pytest.raises(ValueError, match="invalid skill name"):
        reg.load_skill_directory(write("Bad_Name", "name: Bad_Name\ndescription: d"))
    with pytest.raises(ValueError, match="description"):
        reg.load_skill_directory(write("two", "name: two"))
    reg.load_skill_directory(
        write(
            "three",
            "name: three\ndescription: ok\nversion: '2.0'\nrequired_tools: [a, b]",
        )
    )
    assert reg.skills["three"].version == "2.0" and reg.skills[
        "three"
    ].required_tools == ["a", "b"]


@pytest.fixture
def evil_skill(monkeypatch, tmp_path):
    skill_dir = tmp_path / "s"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "references" / "ok.md").write_text("fine")
    (skill_dir / "references_evil").mkdir()
    (skill_dir / "references_evil" / "x.md").write_text("leak")
    (skill_dir / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\nbody")
    reg = PluginRegistry()
    reg.load_skill_directory(skill_dir)
    monkeypatch.setattr(plugins_mod, "registry", reg)
    return reg


async def test_read_skill_reference_blocks_traversal(evil_skill):
    ctx = make_ctx(active_skills={"s"})
    assert (
        await read_skill_reference(
            ctx, ReadSkillReferenceArgs(skill_name="s", filename="ok.md")
        )
        == "fine"
    )
    for bad in ("../SKILL.md", "../references_evil/x.md", "/etc/passwd"):
        with pytest.raises(ToolRemediationError):
            await read_skill_reference(
                ctx, ReadSkillReferenceArgs(skill_name="s", filename=bad)
            )
    with pytest.raises(ToolRemediationError):
        await read_skill_reference(
            make_ctx(), ReadSkillReferenceArgs(skill_name="s", filename="ok.md")
        )


# --- external manifests / MCP --------------------------------------------------------------


def _mcp_registry(handler) -> tuple[PluginRegistry, RunContext]:
    reg = PluginRegistry()
    reg.register_external_manifest(
        {
            "name": "lookup",
            "description": "d",
            "protocol": "mcp_http",
            "endpoint_url": "https://93.184.216.34/mcp",
            "secret_key": "K",
            "parameters": {
                "properties": {
                    "customer": {"type": "string"},
                    "n": {"type": "integer"},
                },
                "required": ["customer"],
            },
        }
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return reg, make_ctx(
        allowed_secrets={"K"}, _scoped_secrets={"K": "sekret"}, http_client=client
    )


async def test_mcp_request_shape_and_json_response():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"], seen["body"] = request.headers, json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "run_t",
                "result": {"content": [{"type": "text", "text": "hello"}]},
            },
        )

    reg, ctx = _mcp_registry(handler)
    tool = reg.tools["lookup"]
    out = await tool.handler(ctx, tool.args_schema.model_validate({"customer": "acme"}))
    assert out == "hello"
    h, b = seen["headers"], seen["body"]
    assert (
        h["mcp-protocol-version"] == "2026-07-28"
        and h["mcp-method"] == "tools/call"
        and h["mcp-name"] == "lookup"
    )
    assert "text/event-stream" in h["accept"] and h["authorization"] == "Bearer sekret"
    assert h["x-haas-signature-256"].startswith("sha256=")
    assert (
        b["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
    )
    assert b["params"]["arguments"] == {"customer": "acme"}


async def test_mcp_sse_response_and_errors():
    sse = 'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\ndata: {"jsonrpc":"2.0","id":"x","result":{"content":[{"type":"text","text":"via sse"}]}}\n\n'
    reg, ctx = _mcp_registry(
        lambda r: httpx.Response(
            200, text=sse, headers={"content-type": "text/event-stream"}
        )
    )
    tool = reg.tools["lookup"]
    assert (
        await tool.handler(ctx, tool.args_schema.model_validate({"customer": "a"}))
        == "via sse"
    )

    reg, ctx = _mcp_registry(
        lambda r: httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "x",
                "error": {"code": -32602, "message": "bad"},
            },
        )
    )
    tool = reg.tools["lookup"]
    with pytest.raises(ToolRemediationError, match="MCP error"):
        await tool.handler(ctx, tool.args_schema.model_validate({"customer": "a"}))

    with pytest.raises(ToolRemediationError):
        _parse_mcp_response("data: {}\n", "text/event-stream")


async def test_manifest_blocks_internal_endpoint_at_registration():
    with pytest.raises(SecurityViolationError):
        PluginRegistry().register_external_manifest(
            {
                "name": "x",
                "description": "d",
                "endpoint_url": "http://127.0.0.1:9/x",
                "parameters": {},
            }
        )


# --- analytics tools -----------------------------------------------------------------------


@pytest.fixture
def analytics(tmp_path):
    reg = PluginRegistry()
    reg.discover_all(PLUGINS)
    db = tmp_path / "a.db"
    ctx = make_ctx(
        allowed_secrets={"ANALYTICS_DB_PATH"},
        _scoped_secrets={"ANALYTICS_DB_PATH": str(db)},
    )
    return reg, ctx


def test_query_revenue_finds_duplicate(analytics):
    reg, ctx = analytics
    t = reg.tools["query_revenue_metrics"]
    out = json.loads(t.handler(ctx, t.args_schema(quarter="2026-Q3")))
    assert out["duplicate_charges"][0]["invoice_ids"] == ["inv_3001", "inv_3002"]
    assert any(r["anomaly"] for r in out["series"] if r["segment"] == "enterprise")
    with pytest.raises(ToolRemediationError):
        t.handler(ctx, t.args_schema(quarter="Q3-2026"))


def test_refund_batch_and_path_guard(analytics, tmp_path):
    reg, ctx = analytics
    t = reg.tools["prepare_refund_batch"]
    out = json.loads(
        t.handler(ctx, t.args_schema(invoice_ids=["inv_3002"], reason="duplicate"))
    )
    assert out["total_usd"] == 12000.0
    with pytest.raises(
        ToolRemediationError
    ):  # already refunded -> no longer refundable
        t.handler(ctx, t.args_schema(invoice_ids=["inv_3002"], reason="duplicate"))
    bad = make_ctx(
        allowed_secrets={"ANALYTICS_DB_PATH"},
        _scoped_secrets={"ANALYTICS_DB_PATH": "/etc/evil.db"},
    )
    with pytest.raises(ToolRemediationError):
        t.handler(bad, t.args_schema(invoice_ids=["inv_1"], reason="whatever"))


# --- task-level tool execution -------------------------------------------------------------


class _Args(BaseModel):
    seconds: float = 0


@pytest.fixture
def task_registry(monkeypatch):
    reg = PluginRegistry()

    @reg.tool(name="slow_sync", description="d", args_schema=_Args, is_core=True)
    def slow_sync(args: _Args) -> str:
        time.sleep(args.seconds)
        return "done"

    @reg.tool(name="locked", description="d", args_schema=_Args)
    def locked(args: _Args) -> str:
        return "should not run"

    @reg.tool(name="boom", description="d", args_schema=_Args, is_core=True)
    async def boom(ctx: RunContext, args: _Args) -> str:
        raise RuntimeError("kaput")

    monkeypatch.setattr(tasks, "registry", reg)
    return reg


async def _run_tool(name: str, args: dict | str, skills=frozenset()):
    return await tasks._async_execute_tool(
        run_id="r",
        thread_id="t",
        tenant_id="x",
        traceparent="tp",
        active_skills=set(skills),
        allowed_secrets=set(),
        scoped_secrets={},
        tool_call={
            "id": "c1",
            "function": {
                "name": name,
                "arguments": args if isinstance(args, str) else json.dumps(args),
            },
        },
    )


async def test_execute_tool_paths(task_registry, monkeypatch):
    ok = await _run_tool("slow_sync", {"seconds": 0})
    assert ok["content"] == "done" and not ok["is_error"]

    assert (await _run_tool("locked", {}))[
        "is_error"
    ]  # not unlocked by any active skill
    assert "does not exist" in (await _run_tool("nope", {}))["content"]
    assert (
        "Schema Validation Failed"
        in (await _run_tool("slow_sync", '{"seconds": "abc"}'))["content"]
    )
    assert "Runtime Error in 'boom'" in (await _run_tool("boom", {}))["content"]

    monkeypatch.setattr(settings, "per_tool_timeout_sec", 0.2)
    start = time.monotonic()
    timed_out = await _run_tool("slow_sync", {"seconds": 1.5})
    assert "Timeout Error" in timed_out["content"]
    # The timeout fires on the event loop instead of blocking behind the sync handler.
    assert time.monotonic() - start < 1.4


def test_bound_observation_spills_large_output(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "scratchpad_dir", tmp_path)
    monkeypatch.setattr(settings, "max_tool_output_chars", 1000)
    big = "A" * 500 + "B" * 5000 + "C" * 500
    out = tasks._bound_observation("tool/../x", "id 1", big)
    assert (
        len(out) < 1200
        and out.startswith("A")
        and out.endswith("C")
        and "HARNESS TRUNCATED" in out
    )
    spilled = list(tmp_path.iterdir())
    assert len(spilled) == 1 and spilled[0].read_text() == big
    assert tasks._bound_observation("t", "i", "short") == "short"
