"""Live output deltas: gateway streaming, delta publishing, SSE multiplexing, error codes."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

import app.main as main_module
from app.core.llm import OpenRouterGateway, _StreamAccumulator
from app.core.output_stream import (
    OUTPUT_DELTA_EVENT,
    OUTPUT_DELTA_FEATURE,
    DeltaPublisher,
    decode_delta,
    delta_key,
)
from app.core.workflow import RUN_ERROR_CODES, _with_error_code, run_error_code


def _sse(*chunks: Any) -> bytes:
    lines = [": OPENROUTER PROCESSING", ""]
    for chunk in chunks:
        lines += [f"data: {chunk if isinstance(chunk, str) else json.dumps(chunk)}", ""]
    return "\n".join(lines).encode()


STREAM = _sse(
    {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}}]},
    {"choices": [{"index": 0, "delta": {"content": "lo"}}]},
    {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "echo", "arguments": '{"x":'},
                        }
                    ]
                },
            }
        ]
    },
    {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [{"index": 0, "function": {"arguments": ' "1"}'}}]
                },
            }
        ]
    },
    "not json",
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    {
        "choices": [],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "cost": 0.0012,
            "prompt_tokens_details": {"cached_tokens": 3},
        },
    },
    "[DONE]",
)


async def _complete(handler: Any) -> tuple[Any, list[str]]:
    deltas: list[str] = []

    async def sink(text: str) -> None:
        deltas.append(text)

    gateway = OpenRouterGateway("sk-test", "https://llm.test/api/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await gateway.chat_completion(
            client,
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            traceparent="tp",
            on_delta=sink,
        )
    return result, deltas


@pytest.mark.asyncio
async def test_streaming_completion_rebuilds_message_and_usage():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(
            200, content=STREAM, headers={"content-type": "text/event-stream"}
        )

    result, deltas = await _complete(handler)
    assert seen["body"]["stream"] is True
    assert seen["body"]["stream_options"] == {"include_usage": True}
    assert seen["auth"] == "Bearer sk-test"
    assert deltas == ["Hel", "lo"]  # tool-call arguments are never forwarded
    assert result.assistant_message["content"] == "Hello"
    assert result.assistant_message["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "echo", "arguments": '{"x": "1"}'},
        }
    ]
    assert (result.prompt_tokens, result.completion_tokens) == (11, 7)
    assert (result.cached_tokens, result.cost_usd, result.cost_reported) == (
        3,
        0.0012,
        True,
    )


@pytest.mark.asyncio
async def test_streaming_falls_back_to_json_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "whole"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    result, deltas = await _complete(handler)
    assert deltas == ["whole"]
    assert result.assistant_message["content"] == "whole"
    assert (result.completion_tokens, result.cost_reported) == (2, False)


@pytest.mark.asyncio
async def test_streaming_errors_raise_retryable_http_errors():
    def error_chunk(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse({"error": {"message": "upstream"}}),
            headers={"content-type": "text/event-stream"},
        )

    with pytest.raises(httpx.HTTPError, match="stream error"):
        await _complete(error_chunk)

    def status(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"})

    with pytest.raises(httpx.HTTPStatusError):
        await _complete(status)


def test_stream_accumulator_rejects_empty_and_error_finishes():
    with pytest.raises(httpx.HTTPError, match="no choices"):
        _StreamAccumulator().message()
    acc = _StreamAccumulator()
    with pytest.raises(httpx.HTTPError, match="finished with an error"):
        acc.feed({"choices": [{"delta": {}, "finish_reason": "error"}]})


class RecordingRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self.entries: list[tuple[str, dict[str, str], int]] = []
        self.expires: list[tuple[str, int]] = []
        self.fail = fail

    async def xadd(
        self, key: str, fields: dict[str, str], maxlen: int, approximate: bool
    ) -> str:
        if self.fail:
            raise ConnectionError("redis down")
        self.entries.append((key, fields, maxlen))
        return f"{len(self.entries)}-0"

    async def expire(self, key: str, ttl: int) -> None:
        self.expires.append((key, ttl))


def _publisher(redis: Any, **overrides: int) -> DeltaPublisher:
    options = {
        "flush_chars": 5,
        "flush_ms": 60_000,
        "max_chars": 12,
        "buffer": 100,
        **overrides,
    }
    return DeltaPublisher(redis, "run_1", step=2, attempt=1, call=3, **options)


@pytest.mark.asyncio
async def test_delta_publisher_batches_caps_and_finalizes():
    redis = RecordingRedis()
    pub = _publisher(redis)
    await pub.push("ab")
    assert redis.entries == []  # below flush_chars, inside flush window
    await pub.push("cde")
    await pub.push("fghij")
    await pub.push("klmnop")  # exceeds the 12-char cap
    await pub.push("ignored")
    await pub.flush(final=True)
    frames = [decode_delta(fields) for _, fields, _ in redis.entries]
    assert [f["text"] for f in frames if f] == ["abcde", "fghij", "kl", ""]
    assert [f["index"] for f in frames if f] == [0, 1, 2, 3]
    assert [f["truncated"] for f in frames if f] == [False, False, True, True]
    assert frames[-1] and frames[-1]["final"] is True
    assert {(f["step"], f["attempt"], f["call"]) for f in frames if f} == {(2, 1, 3)}
    assert {key for key, _, _ in redis.entries} == {delta_key("run_1")}
    assert {maxlen for _, _, maxlen in redis.entries} == {100}
    assert redis.expires and all(ttl == 3600 for _, ttl in redis.expires)


@pytest.mark.asyncio
async def test_delta_publisher_exact_cap_is_not_truncated():
    redis = RecordingRedis()
    pub = _publisher(redis, flush_chars=100, max_chars=4)
    await pub.push("abcd")
    await pub.flush(final=True)
    [frame] = [decode_delta(fields) for _, fields, _ in redis.entries]
    assert frame == {
        "schema": "run-output-delta.v1",
        "step": 2,
        "attempt": 1,
        "call": 3,
        "index": 0,
        "text": "abcd",
        "final": True,
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_delta_publisher_swallows_redis_failures():
    redis = RecordingRedis(fail=True)
    pub = _publisher(redis)
    await pub.push("abcdefgh")
    assert pub.failed
    await pub.push("more")
    await pub.flush(final=True)  # still never raises


def test_decode_delta_rejects_malformed_entries():
    assert decode_delta({"step": "1"}) is None
    assert (
        decode_delta({"step": "x", "attempt": "1", "call": "1", "index": "0"}) is None
    )


def test_run_error_codes_match_harness_prefixes_only():
    assert run_error_code("Exceeded max_steps budget (3).") == "budget.max_steps"
    assert run_error_code("Exceeded max_cost_usd budget ($1 > $0.5).") == (
        "budget.max_cost"
    )
    assert run_error_code("Exceeded max_tool_invocations budget (2).") == (
        "budget.max_tool_invocations"
    )
    assert run_error_code("Run deadline exceeded.") == "budget.deadline"
    assert run_error_code("Approval expired before a decision was recorded.") == (
        "approval.expired"
    )
    assert run_error_code(
        "Run exceeded its recovery attempts after worker failures."
    ) == ("recovery.exhausted")
    # Provider text embedded in a step failure must not be reclassified as a budget stop.
    assert run_error_code(
        "Orchestrator step failed: HTTPError: Exceeded max_steps budget"
    ) == ("execution.failed")
    assert run_error_code("something else") == "run.failed"
    assert set(RUN_ERROR_CODES) >= {"run.failed", "execution.failed"}
    assert _with_error_code({"error": "Run deadline exceeded."})["code"] == (
        "budget.deadline"
    )
    assert _with_error_code({"error": "x", "code": "custom"})["code"] == "custom"


def test_capabilities_advertise_deltas_only_when_enabled(monkeypatch):
    monkeypatch.setattr(main_module.settings, "output_delta_enabled", False)
    caps = main_module.identity_capabilities({"submitter"})
    assert OUTPUT_DELTA_FEATURE not in caps["features"]
    assert caps["event_schemas"] == ["run-events.v1"]
    assert "openai-compat.v1" in caps["features"]
    monkeypatch.setattr(main_module.settings, "output_delta_enabled", True)
    caps = main_module.identity_capabilities({"submitter"})
    assert OUTPUT_DELTA_FEATURE in caps["features"]
    assert "run-output-delta.v1" in caps["event_schemas"]


class ScriptedStore:
    """Fake store + Redis replaying a scripted run: queued -> step 1 -> completed."""

    def __init__(self, xreads: list[Any]) -> None:
        self.events = [
            {"sequence": 1, "type": "run_queued", "data": {"run_id": "run_1"}},
            {
                "sequence": 2,
                "type": "step_started",
                "data": {"step": 1, "attempt": 2, "model": "m"},
            },
        ]
        self.xreads = xreads
        self.streams: list[dict[str, str]] = []
        self.rds = SimpleNamespace(xread=self.xread)

    async def list_run_events(
        self, run_id: str, *, after: int, limit: int
    ) -> list[dict[str, Any]]:
        return [e for e in self.events if e["sequence"] > after][:limit]

    async def get_run(self, run_id: str) -> dict[str, Any]:
        done = self.events[-1]["type"] == "run_completed"
        return {
            "status": "completed" if done else "running",
            "event_sequence": self.events[-1]["sequence"],
        }

    async def xread(self, streams: dict[str, str], count: int, block: int) -> Any:
        self.streams.append(dict(streams))
        entry = self.xreads.pop(0)
        if entry == "complete":
            self.events.append(
                {
                    "sequence": 3,
                    "type": "run_completed",
                    "data": {"output": "Hello world"},
                }
            )
            return [["haas:wake:run_1", [("9-0", {"seq": "3"})]]]
        return entry


def _delta(step: int, attempt: int, text: str, **extra: str) -> dict[str, str]:
    return {
        "step": str(step),
        "attempt": str(attempt),
        "call": "1",
        "index": "0",
        "text": text,
        "final": "0",
        "truncated": "0",
        **extra,
    }


async def _frames(
    monkeypatch: pytest.MonkeyPatch, store: ScriptedStore, *, output_deltas: bool
) -> list[str]:
    monkeypatch.setattr(main_module, "store", store)
    monkeypatch.setattr(main_module, "_get_owned_run", AsyncMock())
    monkeypatch.setattr(main_module.settings, "output_delta_enabled", True)
    request = SimpleNamespace(headers={}, is_disconnected=AsyncMock(return_value=False))
    response = await main_module.stream_run_events(
        "run_1",
        request,  # type: ignore[arg-type]
        x_tenant_id="t",
        last_event_id="0",
        output_deltas=output_deltas,
    )
    return [
        chunk if isinstance(chunk, str) else bytes(chunk).decode()
        async for chunk in response.body_iterator
    ]


@pytest.mark.asyncio
async def test_sse_interleaves_only_live_deltas_without_ids(monkeypatch):
    key = delta_key("run_1")
    store = ScriptedStore(
        [
            [
                [
                    key,
                    [
                        ("1-0", _delta(1, 2, "Hello")),
                        ("2-0", _delta(1, 1, "stale attempt")),
                        ("3-0", _delta(0, 2, "stale step")),
                        ("4-0", {"step": "broken"}),
                        ("5-0", _delta(1, 2, " world", final="1")),
                    ],
                ]
            ],
            "complete",
        ]
    )
    frames = await _frames(monkeypatch, store, output_deltas=True)
    assert [f.split("\n")[0] for f in frames] == [
        "id: 1",
        "id: 2",
        f"event: {OUTPUT_DELTA_EVENT}",
        f"event: {OUTPUT_DELTA_EVENT}",
        "id: 3",
    ]
    deltas = [json.loads(f.split("data: ", 1)[1]) for f in frames[2:4]]
    assert [d["text"] for d in deltas] == ["Hello", " world"]
    assert deltas[-1]["final"] is True and deltas[0]["schema"] == "run-output-delta.v1"
    assert store.streams[0] == {"haas:wake:run_1": "$", key: "0"}
    assert store.streams[1] == {"haas:wake:run_1": "$", key: "5-0"}


@pytest.mark.asyncio
async def test_sse_without_opt_in_never_reads_deltas(monkeypatch):
    store = ScriptedStore(["complete"])
    frames = await _frames(monkeypatch, store, output_deltas=False)
    assert [f.split("\n")[0] for f in frames] == ["id: 1", "id: 2", "id: 3"]
    assert store.streams == [{"haas:wake:run_1": "$"}]
