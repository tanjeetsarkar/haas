"""OpenAI facade over the real admission path and durable workflow (PostgreSQL)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

import app.main as main_module
from app.core.agents import AgentConfiguration

from test_phase2_database import DSN, db  # noqa: F401  (fixture re-export)
from test_phase3_workflow import Runtime, _api, _completion, rt  # noqa: F401

pytestmark = pytest.mark.skipif(
    not DSN, reason="Requires disposable PostgreSQL qualification DB"
)
CHAT = {"model": "model-a", "messages": [{"role": "user", "content": "hello"}]}


class WakeRedis:
    """Stand-in for the wake stream: no wakeups, so the facade falls back to polling."""

    def __getattr__(self, name: str) -> Any:
        return AsyncMock()

    async def xadd(self, *_args: Any, **_kwargs: Any) -> str:
        return "0-1"

    async def xread(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        await asyncio.sleep(0.01)
        return []


@pytest.fixture
def facade(rt: Runtime, monkeypatch):  # noqa: F811
    rt.store.redis = WakeRedis()  # type: ignore[assignment]
    monkeypatch.setattr(main_module.settings, "openai_facade_wait_sec", 5)
    monkeypatch.setattr(main_module.settings, "sse_heartbeat_sec", 1)
    return rt


async def _drive(runtime: Runtime, call) -> httpx.Response:
    """Runs the HTTP call while acting as the broker until it returns."""
    task = asyncio.ensure_future(call)
    while not task.done():
        await asyncio.sleep(0.01)
        if runtime.bus:
            await runtime.pump()
    return await task


def _sse(body: str) -> list[Any]:
    return [
        line[6:] if line[6:] == "[DONE]" else json.loads(line[6:])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


async def test_chat_completion_runs_through_durable_workflow(
    facade: Runtime, monkeypatch
):
    facade.script.append(_completion("hi from haas"))
    async with await _api(
        facade, monkeypatch, {"submitter"}, str(facade.principal)
    ) as client:
        resp = await _drive(
            facade,
            client.post(
                "/v1/chat/completions", json=CHAT, headers={"Idempotency-Key": "k-1"}
            ),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        run_id = resp.headers["x-haas-run-id"]
        assert body["id"] == f"chatcmpl-{run_id}" and body["model"] == "model-a"
        [choice] = body["choices"]
        assert choice["message"]["content"] == "hi from haas"
        assert (choice["index"], choice["finish_reason"]) == (0, "stop")
        assert body["usage"] == {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
        }
        assert body["haas"]["run_id"] == run_id
        run = await facade.store.get_run(run_id)
        assert run["primary_model"] == "model-a" and run["status"] == "completed"

        # Retrying with the same key replays the original run instead of re-admitting.
        replay = await client.post(
            "/v1/chat/completions", json=CHAT, headers={"Idempotency-Key": "k-1"}
        )
        assert replay.status_code == 200
        assert replay.headers["x-haas-run-id"] == run_id
        assert replay.headers["idempotent-replayed"] == "true"
        assert replay.json()["choices"][0]["message"]["content"] == "hi from haas"


async def test_chat_streaming_emits_chunks_usage_and_done(facade: Runtime, monkeypatch):
    facade.script.append(_completion("streamed answer"))
    async with await _api(
        facade, monkeypatch, {"submitter"}, str(facade.principal)
    ) as client:
        resp = await _drive(
            facade,
            client.post(
                "/v1/chat/completions",
                json={
                    **CHAT,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                },
            ),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _sse(resp.text)
        assert events[-1] == "[DONE]"
        chunks = events[:-1]
        assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
        text = "".join(
            c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"]
        )
        assert text == "streamed answer"
        assert any(
            c["choices"] and c["choices"][0]["finish_reason"] == "stop" for c in chunks
        )
        assert chunks[-1]["usage"]["total_tokens"] == 14


async def test_approval_pause_is_a_non_retryable_conflict(facade: Runtime, monkeypatch):
    facade.script.append(_completion(tool_calls=[("danger", {"x": "prod"})]))
    async with await _api(
        facade, monkeypatch, {"submitter"}, str(facade.principal)
    ) as client:
        resp = await _drive(facade, client.post("/v1/chat/completions", json=CHAT))
        assert resp.status_code == 409
        assert resp.headers["x-should-retry"] == "false"
        error = resp.json()["error"]
        assert (error["type"], error["code"]) == ("conflict_error", "approval_required")
        approval = error["haas"]["approval"]
        assert (
            approval["decision_endpoint"]
            == f"/v1/approvals/{approval['batch_id']}/decision"
        )
        assert approval["batch_digest"]


async def test_responses_stream_event_sequence(facade: Runtime, monkeypatch):
    facade.script.append(_completion("streamed response"))
    async with await _api(
        facade, monkeypatch, {"submitter"}, str(facade.principal)
    ) as client:
        resp = await _drive(
            facade,
            client.post(
                "/v1/responses",
                json={"model": "model-a", "input": "go", "stream": True},
            ),
        )
        assert resp.status_code == 200
        events = [
            json.loads(line[6:])
            for line in resp.text.splitlines()
            if line.startswith("data: ")
        ]
        kinds = [e["type"] for e in events]
        assert kinds[0] == "response.created" and kinds[-1] == "response.completed"
        assert [e["sequence_number"] for e in events] == list(range(len(events)))
        text = "".join(
            e["delta"] for e in events if e["type"] == "response.output_text.delta"
        )
        assert text == "streamed response"
        final = events[-1]["response"]
        assert (
            final["status"] == "completed"
            and final["output_text"] == "streamed response"
        )


async def test_responses_background_poll_and_previous_response(
    facade: Runtime, monkeypatch
):
    async with await _api(
        facade, monkeypatch, {"submitter"}, str(facade.principal)
    ) as client:
        queued = await client.post(
            "/v1/responses",
            json={"model": "model-a", "input": "first", "background": True},
        )
        assert queued.status_code == 200, queued.text
        first = queued.json()
        assert first["object"] == "response" and first["status"] == "queued"
        facade.script.append(_completion("first answer"))
        await facade.pump()
        polled = await client.get(f"/v1/responses/{first['id']}")
        assert polled.status_code == 200
        done = polled.json()
        assert done["status"] == "completed" and done["output_text"] == "first answer"
        assert done["usage"]["output_tokens"] == 4

        facade.script.append(_completion("second answer"))
        follow = await _drive(
            facade,
            client.post(
                "/v1/responses",
                json={
                    "model": "model-a",
                    "input": "more",
                    "previous_response_id": first["id"],
                },
            ),
        )
        assert follow.status_code == 200, follow.text
        assert follow.json()["haas"]["thread_id"] == first["haas"]["thread_id"]
        run = await facade.store.get_run(follow.json()["id"])
        assert [m["content"] for m in run["messages"] if m["role"] == "user"] == [
            "first",
            "more",
        ]

        missing = await client.get("/v1/responses/run_missing")
        assert missing.status_code == 404
        assert missing.json()["error"]["type"] == "not_found_error"
        bad_prev = await client.post(
            "/v1/responses",
            json={
                "model": "model-a",
                "input": "x",
                "previous_response_id": "run_missing",
            },
        )
        assert bad_prev.status_code == 404
        assert bad_prev.json()["error"]["param"] == "previous_response_id"


async def test_policy_parity_and_agent_models(facade: Runtime, monkeypatch):
    config = AgentConfiguration(primary_model="model-a", planner_model="model-b")
    policy = str(
        await facade.owner.fetchval(
            "SELECT active_policy_revision_id FROM tenants WHERE slug = $1", facade.slug
        )
    )
    revision = await facade.store.create_agent_revision(
        facade.slug,
        agent_name="billing",
        configuration=config.model_dump(mode="json"),
        digest=config.digest(),
        policy_revision_id=policy,
        registry_fingerprint=main_module.registry.fingerprint,
        actor_principal_id=str(facade.principal),
        request_id=str(uuid4()),
    )
    async with await _api(
        facade, monkeypatch, {"submitter"}, str(facade.principal)
    ) as client:
        denied = await client.post(
            "/v1/chat/completions", json={**CHAT, "model": "openai/not-allowed"}
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["type"] == "permission_error"
        over = await client.post(
            "/v1/chat/completions", json={**CHAT, "haas": {"max_steps": 99}}
        )
        assert (
            over.status_code == 400
            and over.json()["error"]["param"] == "haas.max_steps"
        )

        unknown = await client.post(
            "/v1/chat/completions", json={**CHAT, "model": "agent/billing"}
        )
        assert unknown.status_code == 404  # drafted, not yet published
        assert unknown.json()["error"]["code"] == "model_not_found"
        for target in ("validated", "published"):
            await facade.store.transition_agent_revision(
                facade.slug,
                revision["revision_id"],
                target=target,
                actor_principal_id=str(facade.principal),
                request_id=str(uuid4()),
                policy_revision_id=policy,
                registry_fingerprint=main_module.registry.fingerprint,
            )
        models = await client.get("/v1/models")
        assert models.status_code == 200
        ids = [m["id"] for m in models.json()["data"]]
        assert ids[0] == "haas" and {"agent/billing", "agent/billing@1"} <= set(ids)

        facade.script.append(_completion("pinned"))
        pinned = await _drive(
            facade,
            client.post(
                "/v1/chat/completions", json={**CHAT, "model": "agent/billing@1"}
            ),
        )
        assert pinned.status_code == 200, pinned.text
        run = await facade.store.get_run(pinned.headers["x-haas-run-id"])
        assert str(run["agent_revision_id"]) == revision["revision_id"]

    async with await _api(
        facade, monkeypatch, {"auditor"}, str(facade.principal)
    ) as client:
        assert (await client.post("/v1/chat/completions", json=CHAT)).status_code == 403


async def test_official_openai_sdk_round_trip(facade: Runtime, monkeypatch):
    """The unmodified OpenAI SDK works by changing only base_url, key and tenant header."""
    openai = pytest.importorskip("openai")
    async with await _api(
        facade, monkeypatch, {"submitter"}, str(facade.principal)
    ) as raw:
        sdk = openai.AsyncOpenAI(
            api_key="hk_test_qa",
            base_url="http://qa/v1",
            default_headers={"X-Tenant-ID": facade.slug},
            http_client=httpx.AsyncClient(transport=raw._transport),  # noqa: SLF001
            max_retries=0,
        )
        facade.script.append(_completion("sdk says hi"))
        completion = await _drive(
            facade,
            sdk.chat.completions.create(
                model="model-a",
                messages=[{"role": "user", "content": "hi"}],
                extra_body={"haas": {"max_steps": 2}},
            ),
        )
        assert completion.choices[0].message.content == "sdk says hi"
        assert completion.usage is not None and completion.usage.total_tokens == 14

        facade.script.append(_completion("sdk streamed"))

        async def stream_text() -> str:
            stream = await sdk.chat.completions.create(
                model="model-a",
                messages=[{"role": "user", "content": "s"}],
                stream=True,
            )
            return "".join(
                [c.choices[0].delta.content or "" async for c in stream if c.choices]
            )

        assert await _drive(facade, stream_text()) == "sdk streamed"

        facade.script.append(_completion("sdk response"))
        result = await _drive(facade, sdk.responses.create(model="model-a", input="r"))
        assert result.status == "completed" and result.output_text == "sdk response"
        assert (await sdk.responses.retrieve(result.id)).output_text == "sdk response"
        listed = await sdk.models.list()
        assert "haas" in [m.id for m in listed.data]

        with pytest.raises(openai.PermissionDeniedError) as denied:
            await sdk.chat.completions.create(
                model="openai/not-allowed", messages=[{"role": "user", "content": "x"}]
            )
        assert denied.value.status_code == 403
        await sdk.close()
