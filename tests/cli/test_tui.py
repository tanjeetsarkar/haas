from __future__ import annotations

import asyncio

from textual.widgets import Markdown, Static

from haas_cli.client import HaasClient
from haas_cli.config import ChatSettings
from haas_cli.session import ChatSession
from haas_cli.tui.app import ApprovalScreen, ChatApp, PromptInput

from fake_haas import FakeHaas


def make_app(fake: FakeHaas) -> ChatApp:
    session = ChatSession(
        HaasClient("http://api", "t1", transport=fake.transport(), reconnect_delay=0),
        ChatSettings(
            base_url="http://api", tenant="t1", secrets={"TOK": "supersecretvalue"}
        ),
    )
    return ChatApp(session)


async def wait_for(pilot, predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not reached in time")


def chat_text(app: ChatApp) -> str:
    out = []
    for w in app.query("#chat > *"):
        if isinstance(w, Markdown):
            out.append(w.source)
        elif isinstance(w, Static):
            out.append(str(w.render()))
    return "\n".join(out)


async def send(pilot, app: ChatApp, text: str) -> None:
    app.query_one("#prompt", PromptInput).load_text(text)
    await pilot.press("enter")


async def test_connects_and_shows_banner():
    app = make_app(FakeHaas())
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: "2 skills" in chat_text(app))
        assert "api" in str(app.query_one("#topbar", Static).render())


async def test_unreachable_api_shows_error_banner():
    app = make_app(FakeHaas(healthy=False))
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: "Cannot reach" in chat_text(app))
        await send(pilot, app, "hello")
        await wait_for(pilot, lambda: chat_text(app).count("Cannot reach") >= 2)
        assert not app.busy


async def test_simple_chat_turn_renders_markdown_answer():
    fake = FakeHaas("simple")
    app = make_app(fake)
    async with app.run_test(size=(120, 40)) as pilot:
        await send(pilot, app, "hi")
        await wait_for(pilot, lambda: "Hello there" in chat_text(app) and not app.busy)
        assert "❯ hi" in chat_text(app)
        assert "$0.0010" in str(app.query_one("#status", Static).render())
        assert app.query_one("#prompt", PromptInput).text == ""


async def test_approval_flow_approve_with_key():
    fake = FakeHaas()
    app = make_app(fake)
    async with app.run_test(size=(120, 40)) as pilot:
        await send(pilot, app, "refund please")
        await wait_for(pilot, lambda: isinstance(app.screen, ApprovalScreen))
        assert "prepare_refund_batch" in str([c.name for c in app.screen.calls])
        await pilot.press("y")
        await wait_for(
            pilot, lambda: "Refund prepared" in chat_text(app) and not app.busy
        )
        text = chat_text(app)
        assert "approved: prepare_refund_batch" in text
        assert "✔ query_revenue_metrics" in text and "✔ prepare_refund_batch" in text
        assert fake.resumes[0]["approved"] is True


async def test_approval_flow_reject_with_reason():
    fake = FakeHaas()
    app = make_app(fake)
    async with app.run_test(size=(120, 40)) as pilot:
        await send(pilot, app, "refund please")
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, ApprovalScreen) and app.screen.query("#reason")
            ),
        )
        app.screen.query_one("#reason").value = "not today"
        await pilot.press("escape")
        await wait_for(
            pilot, lambda: "Refund was rejected" in chat_text(app) and not app.busy
        )
        assert fake.resumes[0] == {
            "approved": False,
            "secrets": {"TOK": "supersecretvalue"},
            "rejection_reason": "not today",
        }


async def test_slash_commands_and_secret_masking():
    app = make_app(FakeHaas("simple"))
    async with app.run_test(size=(120, 40)) as pilot:
        await send(pilot, app, "/secret set NEW_KEY topsecretvalue123")
        await wait_for(pilot, lambda: "NEW_KEY set" in chat_text(app))
        assert "topsecretvalue123" not in chat_text(app)
        assert app.session.settings.secrets["NEW_KEY"] == "topsecretvalue123"
        assert "NEW_KEY" not in " ".join(
            app.query_one("#prompt", PromptInput).prompt_history
        )
        await send(pilot, app, "/skills")
        await wait_for(pilot, lambda: "revenue-ops" in chat_text(app))
        await send(pilot, app, "/bogus")
        await wait_for(pilot, lambda: "Unknown command" in chat_text(app))
        await pilot.press("ctrl+l")
        await wait_for(pilot, lambda: chat_text(app) == "")


async def test_prompt_newline_history_and_hints():
    app = make_app(FakeHaas("simple"))
    async with app.run_test(size=(120, 40)) as pilot:
        prompt = app.query_one("#prompt", PromptInput)
        await pilot.press("a", "ctrl+j", "b")
        assert prompt.text == "a\nb"
        prompt.load_text("/st")
        await pilot.pause()
        assert "/status" in str(app.query_one("#hints", Static).render())
        prompt.load_text("")
        await send(pilot, app, "first")
        await wait_for(pilot, lambda: not app.busy and "Hello there" in chat_text(app))
        await pilot.press("up")
        assert prompt.text == "first"


async def test_esc_detaches_from_running_task():
    fake = FakeHaas()
    app = make_app(fake)
    async with app.run_test(size=(120, 40)) as pilot:
        await send(pilot, app, "refund please")
        await wait_for(pilot, lambda: isinstance(app.screen, ApprovalScreen))
        app.pop_screen()  # leave the gate unanswered, then detach
        app.action_detach()
        await wait_for(
            pilot, lambda: "Detached from run" in chat_text(app) and not app.busy
        )


async def type_text(pilot, app: ChatApp, text: str) -> PromptInput:
    prompt = app.query_one("#prompt", PromptInput)
    prompt.load_text(text)
    prompt.move_cursor(prompt.document.end)
    await pilot.pause()
    return prompt


def hints_text(app: ChatApp) -> str:
    return str(app.query_one("#hints", Static).render())


async def test_tab_completes_single_command_and_keeps_focus():
    app = make_app(FakeHaas("simple"))
    async with app.run_test(size=(120, 40)) as pilot:
        prompt = await type_text(pilot, app, "/pre")
        assert "/preload" in hints_text(app) and "Load skills" not in hints_text(app)
        await pilot.press("tab")
        assert prompt.text == "/preload " and app.focused is prompt
        await pilot.press("tab")  # nothing to add: still in the prompt
        assert app.focused is prompt


async def test_tab_accepts_highlighted_match_when_ambiguous():
    app = make_app(FakeHaas("simple"))
    async with app.run_test(size=(120, 40)) as pilot:
        prompt = await type_text(pilot, app, "/s")
        shown = hints_text(app)
        assert "/status" in shown and "/skills" in shown and "/secret" in shown
        await pilot.press(
            "tab"
        )  # no longer common prefix: accepts the highlighted match
        assert prompt.text == "/status"


async def test_arrows_choose_between_matches():
    app = make_app(FakeHaas("simple"))
    async with app.run_test(size=(120, 40)) as pilot:
        prompt = await type_text(pilot, app, "/s")
        await pilot.press("down")
        assert prompt.sel == 1
        await pilot.press("tab")
        assert prompt.text == "/skills"
        await type_text(pilot, app, "/s")
        await pilot.press("up")  # wraps to the last match
        await pilot.press("tab")
        assert prompt.text == "/steps "


async def test_enter_completes_partial_but_runs_exact_command():
    app = make_app(FakeHaas("simple"))
    async with app.run_test(size=(120, 40)) as pilot:
        prompt = await type_text(pilot, app, "/stat")
        await pilot.press("enter")
        assert prompt.text == "/status"  # completed, not executed
        assert "tenant      t1" not in chat_text(app)
        await pilot.press("enter")
        await wait_for(pilot, lambda: "tenant      t1" in chat_text(app))
        assert prompt.text == ""


async def test_skill_name_and_secret_name_completion():
    app = make_app(FakeHaas("simple"))
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(
            pilot, lambda: "2 skills" in chat_text(app)
        )  # skill names cached
        prompt = await type_text(pilot, app, "/preload wea")
        await pilot.press("tab")
        assert prompt.text == "/preload weather"
        await type_text(pilot, app, "/secret unset T")
        await pilot.press("tab")
        assert prompt.text == "/secret unset TOK"


async def test_arrows_still_browse_history_with_single_or_no_match():
    app = make_app(FakeHaas("simple"))
    async with app.run_test(size=(120, 40)) as pilot:
        await send(pilot, app, "/status")
        await wait_for(pilot, lambda: "tenant      t1" in chat_text(app))
        prompt = app.query_one("#prompt", PromptInput)
        await pilot.press("up")
        assert (
            prompt.text == "/status"
        )  # single exact match must not capture the arrow keys
