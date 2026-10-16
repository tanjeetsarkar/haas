from __future__ import annotations

import httpx
import pytest

from haas_cli.client import HaasAPIError, HaasClient, SSEParser
from haas_cli.config import ChatSettings, secrets_from_env
from haas_cli.session import ChatSession, Decision, handle_command

from fake_haas import FakeHaas


def feed_all(text: str):
    p = SSEParser()
    return [e for line in text.split("\n") if (e := p.feed(line))]


def test_sse_parser_handles_comments_multiline_and_bad_json():
    events = feed_all(
        ': keepalive\n\nid: 1-0\nevent: step_started\ndata: {"step": 1}\n\n'
        "id: 2-0\ndata: not-json\n\nid: 3-0\nevent: x\ndata: [1]\n\n"
    )
    assert [(e.id, e.event) for e in events] == [
        ("1-0", "step_started"),
        ("2-0", "message"),
        ("3-0", "x"),
    ]
    assert events[0].data == {"step": 1}
    assert events[1].data == {"raw": "not-json"}
    assert events[2].data == {"value": [1]}


def test_secrets_from_env_and_settings():
    env = {
        "HAAS_SECRET_GITHUB_TOKEN": "t",
        "HAAS_SECRET_": "x",
        "HAAS_URL": "http://h:1/",
        "HAAS_MAX_STEPS": "7",
        "HAAS_PRELOAD_SKILLS": "a,b c",
    }
    assert secrets_from_env(env) == {"GITHUB_TOKEN": "t"}
    s = ChatSettings.from_env(env)
    assert (s.base_url, s.max_steps, s.preload_skills) == (
        "http://h:1",
        7,
        ["a", "b", "c"],
    )


async def test_client_errors_are_mapped():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/plugins":
            return httpx.Response(422, json={"detail": "Unknown preload_skills: ['x']"})
        raise httpx.ConnectError("refused")

    c = HaasClient("http://api", transport=httpx.MockTransport(handler))
    with pytest.raises(HaasAPIError) as e1:
        await c.plugins()
    assert e1.value.status == 422 and "Unknown preload_skills" in str(e1.value)
    with pytest.raises(HaasAPIError) as e2:
        await c.health()
    assert e2.value.status is None and "Cannot reach" in str(e2.value)


async def test_stream_reconnects_from_last_event_id():
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/stream"):
            calls.append(req.headers["last-event-id"])
            if len(calls) == 1:  # first connection yields one event then drops

                class Boom(httpx.SyncByteStream, httpx.AsyncByteStream):
                    async def __aiter__(self):
                        yield b'id: 1-0\nevent: step_started\ndata: {"step": 1}\n\n'
                        raise httpx.ReadError("dropped")

                return httpx.Response(200, stream=Boom())
            return httpx.Response(
                200,
                content='id: 2-0\nevent: run_completed\ndata: {"output": "ok"}\n\n',
            )
        raise AssertionError(req.url)

    c = HaasClient(
        "http://api", transport=httpx.MockTransport(handler), reconnect_delay=0
    )
    got = [e.event async for e in c.stream_events("run_1")]
    assert got == ["step_started", "run_completed"]
    assert calls == ["0-0", "1-0"]


async def test_stream_gives_up_after_max_reconnects():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    c = HaasClient(
        "http://api",
        transport=httpx.MockTransport(handler),
        max_reconnects=2,
        reconnect_delay=0,
    )
    with pytest.raises(HaasAPIError, match="Lost connection"):
        async for _ in c.stream_events("run_1"):
            pass


async def make_session(fake: FakeHaas) -> ChatSession:
    return ChatSession(
        HaasClient("http://api", "t1", transport=fake.transport()),
        ChatSettings(secrets={"K": "v"}),
    )


async def test_turn_with_approval_resumes_from_pause_cursor():
    fake = FakeHaas()
    session = await make_session(fake)
    seen, asked = [], []

    async def on_approval(calls):
        asked.append([c.name for c in calls])
        assert calls[0].pretty_args.startswith("{\n")
        return Decision(True)

    result = await session.run_turn("go", lambda e: seen.append(e.event), on_approval)
    assert result.status == "completed" and "Refund prepared" in result.output
    assert asked == [["prepare_refund_batch"]]
    assert fake.resumes == [{"approved": True, "secrets": {"K": "v"}}]
    assert fake.created[0]["secrets"] == {"K": "v"}
    assert fake.stream_headers[-1]["last-event-id"] == "5-0"  # resumed after the pause
    assert seen.count("input_required") == 1 and seen[-1] == "run_completed"
    assert session.settings.thread_id == "thd_1"
    assert session.total_cost_usd == pytest.approx(0.0123)
    assert session.active_skills == ["revenue-ops"]


async def test_turn_rejection_sends_reason_and_reuses_thread():
    fake = FakeHaas()
    session = await make_session(fake)

    async def reject(calls):
        return Decision(False, "too risky")

    result = await session.run_turn("go", lambda e: None, reject)
    assert result.output == "Refund was rejected."
    assert fake.resumes[0]["rejection_reason"] == "too risky"

    fake.script = "simple"
    await session.run_turn("again", lambda e: None, reject)
    assert fake.created[1]["thread_id"] == "thd_1"


async def test_commands():
    fake = FakeHaas()
    session = await make_session(fake)
    run = lambda line: handle_command(session, line)  # noqa: E731

    assert "revenue-ops" in "\n".join((await run("/skills")).lines)
    assert any("⚠ prepare_refund_batch" in line for line in (await run("/tools")).lines)
    assert (await run("/preload nope")).error
    assert session.settings.preload_skills == []
    await run("/preload weather revenue-ops")
    assert session.settings.preload_skills == ["weather", "revenue-ops"]
    await run("/secret set GITHUB_TOKEN ghp_abcdefghijkl")
    listing = "\n".join((await run("/secret list")).lines)
    assert "GITHUB_TOKEN" in listing and "ghp_abcdefghijkl" not in listing
    await run("/secret unset GITHUB_TOKEN")
    assert "GITHUB_TOKEN" not in session.settings.secrets
    assert (await run("/steps 99")).error and (await run("/budget 0")).error
    await run("/steps 5")
    await run("/budget $0.5")
    await run("/model foo/bar")
    assert (
        session.settings.max_steps,
        session.settings.max_cost_usd,
        session.settings.primary_model,
    ) == (5, 0.5, "foo/bar")
    await run("/thread thd_x")
    assert session.settings.thread_id == "thd_x"
    assert (await run("/new")).new_conversation and session.settings.thread_id is None
    assert (await run("/bogus")).error
    assert (await run("/quit")).quit
    status = "\n".join((await run("/status")).lines)
    assert "[ok]" in status and "secrets     K" in status

    fake.healthy = False
    assert "unreachable" in "\n".join((await run("/status")).lines)
    assert (await run("/skills")).error
