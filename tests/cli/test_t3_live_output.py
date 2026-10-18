"""T3 live output: advisory delta previews, reconciliation and follow/pause."""

from __future__ import annotations

from textual.widgets import Markdown, Static

from haas_cli.client import OUTPUT_DELTA_EVENT, HaasClient, SSEEvent
from haas_cli.config import ChatSettings
from haas_cli.session import ChatSession, handle_command

from fake_haas import FakeHaas
from test_tui import chat_text, make_app, wait_for


def _delta(step: int, attempt: int, call: int, text: str, **extra) -> SSEEvent:
    return SSEEvent(
        OUTPUT_DELTA_EVENT,
        {"step": step, "attempt": attempt, "call": call, "text": text, **extra},
    )


async def test_client_requests_deltas_and_keeps_durable_cursor():
    fake = FakeHaas("live")
    client = HaasClient("http://api", "t1", transport=fake.transport())
    events = [e async for e in client.stream_events("run_1", output_deltas=True)]
    assert fake.stream_params[-1] == {"output_deltas": "true"}
    assert [e.event for e in events] == [
        "run_queued",
        "step_started",
        OUTPUT_DELTA_EVENT,
        OUTPUT_DELTA_EVENT,
        OUTPUT_DELTA_EVENT,
        "run_completed",
    ]
    assert [e.id for e in events if e.event == OUTPUT_DELTA_EVENT] == [None] * 3
    plain = [e.event async for e in client.stream_events("run_1")]
    assert fake.stream_params[-1] == {}
    assert OUTPUT_DELTA_EVENT not in plain


async def test_session_only_requests_deltas_when_offered_and_enabled():
    fake = FakeHaas("live")
    session = ChatSession(
        HaasClient("http://api", "t1", transport=fake.transport()), ChatSettings()
    )
    seen: list[str] = []
    result = await session.run_turn("hi", lambda ev: seen.append(ev.event), None)  # type: ignore[arg-type]
    assert result.output == "Hello there"
    assert fake.stream_params[-1] == {} and OUTPUT_DELTA_EVENT not in seen

    session.output_deltas = True
    await session.run_turn("hi", lambda ev: seen.append(ev.event), None)  # type: ignore[arg-type]
    assert fake.stream_params[-1] == {"output_deltas": "true"}

    out = await handle_command(session, "/live off")
    assert out.lines == ["Live output preview: off (offered)."]
    await session.run_turn("hi", lambda ev: None, None)  # type: ignore[arg-type]
    assert fake.stream_params[-1] == {}
    assert (await handle_command(session, "/live maybe")).error
    session.output_deltas = False
    out = await handle_command(session, "/live on")
    assert out.lines == ["Live output preview: on (not offered by this server)."]


async def test_tui_negotiates_deltas_and_reconciles_with_final_output():
    fake = FakeHaas("live")
    fake.features = ["run-inspector.v1", "output-deltas.v1"]
    app = make_app(fake, credential="sk-test")
    async with app.run_test(size=(140, 44)) as pilot:
        await wait_for(pilot, lambda: app.identity is not None)
        assert app.session.output_deltas is True
        await app._submit("hello")
        await wait_for(pilot, lambda: "done" in chat_text(app))
        assert fake.stream_params[-1] == {"output_deltas": "true"}
        # The advisory preview is replaced by the authoritative durable output.
        assert not app.query(".live-output")
        finals = [w.source for w in app.query("#chat > Markdown")]
        assert finals == ["Hello there"]
        assert app._preview_markdown is None and app._preview_text == ""


async def test_tui_without_capability_never_requests_deltas():
    fake = FakeHaas("live")
    fake.features = ["run-inspector.v1"]
    app = make_app(fake, credential="sk-test")
    async with app.run_test(size=(140, 44)) as pilot:
        await wait_for(pilot, lambda: app.identity is not None)
        assert app.session.output_deltas is False
        await app._submit("hello")
        await wait_for(pilot, lambda: "done" in chat_text(app))
        assert fake.stream_params[-1] == {}


async def test_tui_preview_resets_settles_and_drops_retries():
    app = make_app(FakeHaas())
    async with app.run_test(size=(140, 44)) as pilot:
        await app._on_event(SSEEvent("step_started", {"step": 1, "model": "m"}))
        await app._on_event(_delta(1, 1, 1, "Thinking"))
        await app._on_event(_delta(1, 1, 1, " about it", final=True))
        await pilot.pause()  # Let stream process
        [preview] = app.query(".live-output")
        # Markdown widget renders differently, check the source
        assert "Thinking about it" in preview.source
        await app._on_event(
            _delta(1, 1, 2, "Second call")
        )  # new call resets the buffer
        assert app._preview_text == "Second call"
        await pilot.pause()  # Let stream process
        # New call creates a new preview widget, check the last one (the new one)
        previews = app.query(".live-output")
        preview = previews[-1]
        assert "Thinking" not in preview.source
        assert "Second call" in preview.source

        # A durable tool dispatch settles the interim text as a dim transcript entry.
        await app._on_event(
            SSEEvent(
                "tool_dispatched", {"tool_call_id": "c1", "tool": "t", "queue": "q"}
            )
        )
        await pilot.pause()
        assert preview.has_class("settled") and app._preview_markdown is None
        assert "Second call" in chat_text(app)

        # A retry of the same step discards its stale partial output.
        await app._on_event(SSEEvent("step_started", {"step": 2, "model": "m"}))
        await app._on_event(_delta(2, 1, 1, "partial"))
        await pilot.pause()
        stale = app._preview_markdown
        await app._on_event(SSEEvent("step_started", {"step": 2, "model": "m"}))
        await pilot.pause()
        assert stale is not None and not stale.is_attached
        assert "partial" not in chat_text(app)

        # Malformed frames are ignored; truncation is surfaced.
        await app._on_event(SSEEvent(OUTPUT_DELTA_EVENT, {"step": "x"}))
        await app._on_event(_delta(2, 2, 1, "cut", truncated=True, final=True))
        await pilot.pause()
        assert app._preview_markdown is not None
        assert "truncated" in app._preview_markdown.source


async def test_tui_follow_toggle_pauses_autoscroll():
    app = make_app(FakeHaas())
    async with app.run_test(size=(140, 44)) as pilot:
        assert app._follow is True
        await pilot.press("ctrl+f")
        assert app._follow is False
        assert "paused" in str(app.query_one("#status", Static).render())
        for i in range(60):
            await app._note(f"line {i}")
        await pilot.pause()
        chat = app.query_one("#chat")
        assert chat.scroll_y < chat.max_scroll_y
        await pilot.press("ctrl+f")
        await pilot.pause()
        assert app._follow is True and "paused" not in str(
            app.query_one("#status", Static).render()
        )
        assert chat.scroll_y == chat.max_scroll_y
        assert isinstance(app.query("#chat > *").last(), (Static, Markdown))
