from __future__ import annotations

import asyncio
from typing import Any

from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Markdown, Static, TextArea

from haas_cli.client import HaasAPIError, SSEEvent
from haas_cli.completion import Completion, common_prefix, suggest
from haas_cli.session import (
    ChatSession,
    Decision,
    PendingCall,
    handle_command,
)

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BANNER = "[b #7aa2f7]◆ HaaS Chat[/]  [dim]distributed agent harness[/]"


class PromptInput(TextArea):
    """
    Enter sends; Ctrl+J / Shift+Enter inserts a newline; Up/Down browse history.
    Slash-command completion: Tab completes, Up/Down choose between several matches,
    and Enter on a partially typed command completes it instead of sending.
    """

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    class CompletionMoved(Message):
        pass

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.prompt_history: list[str] = []
        self._hist_idx: int | None = None
        self.completion = Completion()
        self.sel = 0

    def _token(self) -> str:
        return self.text[self.completion.start :]

    def _accept(self, value: str) -> None:
        self.load_text(self.text[: self.completion.start] + value)
        self.move_cursor(self.document.end)

    def _tab_complete(self) -> None:
        items = self.completion.items
        if not items:
            return
        token = self._token()
        common = common_prefix(i.value for i in items)
        if len(items) > 1 and len(common) > len(token):
            self._accept(common)
        else:
            self._accept(items[self.sel].value)

    def _enter_completes(self) -> bool:
        items = self.completion.items
        return (
            bool(items)
            and self._token().lower() != items[self.sel].value.strip().lower()
        )

    def remember(self, text: str) -> None:
        if text and (not self.prompt_history or self.prompt_history[-1] != text):
            self.prompt_history.append(text)
        self._hist_idx = None

    async def _on_key(self, event: events.Key) -> None:
        items = self.completion.items
        if event.key == "tab":  # never move focus out of the prompt
            event.prevent_default()
            event.stop()
            self._tab_complete()
        elif event.key in ("up", "down") and len(items) > 1:
            event.prevent_default()
            event.stop()
            step = -1 if event.key == "up" else 1
            self.sel = (self.sel + step) % len(items)
            self.post_message(self.CompletionMoved())
        elif event.key == "enter" and self._enter_completes():
            event.prevent_default()
            event.stop()
            self._accept(items[self.sel].value)
        elif event.key == "enter":
            event.prevent_default()
            event.stop()
            self.post_message(self.Submitted(self.text))
        elif event.key in ("ctrl+j", "shift+enter"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
        elif event.key == "up" and self.cursor_location[0] == 0 and self.prompt_history:
            event.prevent_default()
            event.stop()
            self._hist_idx = (
                len(self.prompt_history) - 1
                if self._hist_idx is None
                else max(0, self._hist_idx - 1)
            )
            self.load_text(self.prompt_history[self._hist_idx])
            self.move_cursor(self.document.end)
        elif (
            event.key == "down"
            and self._hist_idx is not None
            and self.cursor_location[0] == self.document.line_count - 1
        ):
            event.prevent_default()
            event.stop()
            if self._hist_idx >= len(self.prompt_history) - 1:
                self._hist_idx = None
                self.load_text("")
            else:
                self._hist_idx += 1
                self.load_text(self.prompt_history[self._hist_idx])
                self.move_cursor(self.document.end)
        else:
            await super()._on_key(event)


class ApprovalScreen(ModalScreen[Decision]):
    """Human-in-the-loop gate: approve or reject the tool calls a run wants to make."""

    BINDINGS = [
        Binding("y", "approve", "Approve"),
        Binding("n", "reject", "Reject"),
        Binding("escape", "reject", "Reject"),
    ]

    def __init__(self, calls: list[PendingCall]) -> None:
        super().__init__()
        self.calls = calls

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-box"):
            yield Static("⚠  Approval required", id="approval-title")
            yield Static(
                "The agent wants to run the following action(s). Nothing runs until you approve.",
                id="approval-sub",
            )
            with VerticalScroll(id="approval-calls"):
                for c in self.calls:
                    yield Static(
                        Text(f"▸ {c.name}", style="bold yellow"), classes="call-name"
                    )
                    yield Static(
                        Syntax(
                            c.pretty_args, "json", theme="ansi_dark", word_wrap=True
                        ),
                        classes="call-args",
                    )
            yield Input(placeholder="Rejection reason (optional)", id="reason")
            with Horizontal(id="approval-buttons"):
                yield Button("Approve  (y)", variant="success", id="approve")
                yield Button("Reject  (n / esc)", variant="error", id="reject")

    def on_mount(self) -> None:
        self.query_one("#reject", Button).focus()

    def action_approve(self) -> None:
        if not isinstance(self.focused, Input):
            self.dismiss(Decision(True))

    def action_reject(self) -> None:
        reason = self.query_one("#reason", Input).value.strip() or None
        self.dismiss(Decision(False, reason))

    @on(Button.Pressed, "#approve")
    def _approve(self) -> None:
        self.dismiss(Decision(True))

    @on(Button.Pressed, "#reject")
    def _reject(self) -> None:
        self.action_reject()


class ChatApp(App[None]):
    TITLE = "HaaS Chat"

    CSS = """
    Screen { layout: vertical; }
    #topbar { height: 1; background: $primary-darken-2; color: $text; padding: 0 1; }
    #chat { height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }
    .user { margin: 1 0 0 0; color: $text; background: $boost; padding: 0 1; }
    .assistant { margin: 1 0 0 0; padding: 0 1; border-left: thick $success; }
    .assistant MarkdownBlock { margin-bottom: 0; }
    .note { color: $text-muted; margin: 0 0 0 2; }
    .tool { margin: 0 0 0 2; color: $text-muted; }
    .tool.error { color: $error; }
    .tool.done { color: $text-muted; }
    .error-box { margin: 1 0 0 0; padding: 0 1; border-left: thick $error; color: $error; }
    .cmd-out { margin: 1 0 0 0; padding: 0 1; border-left: thick $accent; }
    #activity { height: 1; color: $warning; padding: 0 1; }
    #hints { height: auto; color: $text-muted; padding: 0 1; }
    #prompt { height: auto; min-height: 3; max-height: 10; border: round $primary; }
    #prompt:focus { border: round $accent; }
    #status { height: 1; background: $surface-darken-1; color: $text-muted; padding: 0 1; }

    ApprovalScreen { align: center middle; background: $background 70%; }
    #approval-box { width: 80%; max-width: 100; height: auto; max-height: 85%;
        border: thick $warning; background: $surface; padding: 1 2; }
    #approval-title { text-style: bold; color: $warning; }
    #approval-sub { color: $text-muted; margin-bottom: 1; }
    #approval-calls { height: auto; max-height: 20; }
    .call-args { margin: 0 0 1 2; }
    #reason { margin-top: 1; }
    #approval-buttons { height: auto; margin-top: 1; align-horizontal: right; }
    #approval-buttons Button { margin-left: 2; }
    """

    BINDINGS = [
        Binding("escape", "detach", "Detach run", show=True),
        Binding("ctrl+l", "clear_screen", "Clear"),
        Binding("ctrl+n", "new_chat", "New chat"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(
        self, session: ChatSession, *, initial_prompt: str | None = None
    ) -> None:
        super().__init__()
        self.session = session
        self.initial_prompt = initial_prompt
        self.busy = False
        self.healthy: bool | None = None
        self._step = 0
        self._phase = ""
        self._spin = 0
        self._tool_cards: dict[str, Static] = {}
        self._tool_names: dict[str, str] = {}

    # ---- layout -------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        yield VerticalScroll(id="chat")
        yield Static(id="activity")
        yield Static(id="hints")
        yield PromptInput(id="prompt", show_line_numbers=False, soft_wrap=True)
        yield Static(id="status")
        yield Footer()

    async def on_mount(self) -> None:
        self._main("#prompt", PromptInput).focus()
        self.set_interval(0.1, self._tick)
        self.sub_title = self.session.settings.base_url
        await self._add(Static(BANNER, classes="note"))
        await self._check_health()
        await self._note(
            "Type a task and press Enter · Ctrl+J newline · /help for commands · Esc detaches from a running task"
        )
        self._refresh_bars()
        if self.initial_prompt:
            await self._submit(self.initial_prompt)

    async def on_unmount(self) -> None:
        await self.session.client.aclose()

    def _main(self, selector: str, widget_type: type):
        """Query the base screen so timers keep working while a modal is open."""
        return self.screen_stack[0].query_one(selector, widget_type)

    # ---- helpers ------------------------------------------------------------------------

    async def _add(self, widget: Static | Markdown) -> None:
        chat = self._main("#chat", VerticalScroll)
        await chat.mount(widget)
        chat.scroll_end(animate=False)

    async def _note(self, text: str, *, style: str = "") -> Static:
        w = Static(Text(text, style=style), classes="note")
        await self._add(w)
        return w

    async def _check_health(self) -> None:
        try:
            await self.session.client.health()
            self.healthy = True
            data = await self.session.plugins()
            await self._note(
                f"Connected to {self.session.settings.base_url} as tenant "
                f"'{self.session.settings.tenant}' — {len(data['skills'])} skills, "
                f"{len(data['tools'])} tools.",
                style="green",
            )
        except HaasAPIError as exc:
            self.healthy = False
            await self._add(
                Static(
                    Text(
                        f"✘ {exc}\n  Start the stack with `docker compose up -d`, then send a message to retry.",
                        style="red",
                    ),
                    classes="error-box",
                )
            )

    def _refresh_bars(self) -> None:
        s = self.session.settings
        dot = {True: "[green]●[/]", False: "[red]●[/]", None: "[yellow]●[/]"}[
            self.healthy
        ]
        self._main("#topbar", Static).update(
            f"{dot} {escape(s.base_url)}  tenant [b]{escape(s.tenant)}[/]"
        )
        parts = [
            f"thread {s.thread_id or '(new)'}",
            f"step {self._step}" if self.busy else "idle",
            f"${self.session.total_cost_usd:.4f}",
            f"skills: {', '.join(self.session.active_skills) or '-'}",
        ]
        if s.preload_skills:
            parts.append(f"preload: {', '.join(s.preload_skills)}")
        if s.secrets:
            parts.append(f"secrets: {len(s.secrets)}")
        self._main("#status", Static).update(Text(" │ ".join(parts)))

    def _tick(self) -> None:
        try:
            act = self._main("#activity", Static)
        except NoMatches:  # timer can fire before the screen finishes composing
            return
        if self.busy:
            self._spin = (self._spin + 1) % len(SPINNER)
            act.update(
                Text(
                    f"{SPINNER[self._spin]} {self._phase or 'working'}…  (esc to detach)"
                )
            )
        else:
            act.update("")

    # ---- input handling -----------------------------------------------------------------

    @on(TextArea.Changed, "#prompt")
    def _on_changed(self, event: TextArea.Changed) -> None:
        prompt = self._main("#prompt", PromptInput)
        prompt.completion = suggest(
            event.text_area.text,
            skills=self.session.skill_names,
            secrets=self.session.settings.secrets,
        )
        prompt.sel = 0
        self._render_hints()

    @on(PromptInput.CompletionMoved)
    def _on_completion_moved(self) -> None:
        self._render_hints()

    def _render_hints(self) -> None:
        prompt = self._main("#prompt", PromptInput)
        hints = self._main("#hints", Static)
        items = prompt.completion.items
        if not items:
            hints.update("")
            return
        size = 8
        first = max(0, min(prompt.sel - size // 2, len(items) - size))
        shown = items[first : first + size]
        width = max(len(i.value.strip()) for i in shown)
        text = Text()
        for offset, item in enumerate(shown):
            selected = first + offset == prompt.sel
            line = f"{'▸' if selected else ' '} {item.value.strip().ljust(width)}  {item.description}"
            text.append(line + "\n", style="bold reverse" if selected else "")
        more = f" ({len(items)} matches)" if len(items) > size else ""
        text.append(
            "Tab complete · ↑↓ choose · Enter runs a full command" + more, style="dim"
        )
        hints.update(text)

    @on(PromptInput.Submitted)
    async def _on_submitted(self, event: PromptInput.Submitted) -> None:
        text = event.value.strip()
        prompt = self._main("#prompt", PromptInput)
        if not text:
            return
        if self.busy and not text.startswith("/"):
            await self._note(
                "A task is still running — wait for it or press Esc to detach.",
                style="yellow",
            )
            return
        if not text.lower().startswith("/secret"):  # keep secret values out of history
            prompt.remember(text)
        prompt.load_text("")
        await self._submit(text)

    async def _submit(self, text: str) -> None:
        if text.startswith("/"):
            await self._run_command(text)
        else:
            self.run_worker(self._turn(text), exclusive=True, group="turn")

    async def _run_command(self, line: str) -> None:
        shown = (
            line.split()[0] + " …" if line.lower().startswith("/secret set") else line
        )
        await self._add(Static(Text(shown, style="bold magenta"), classes="user"))
        result = await handle_command(self.session, line)
        if result.quit:
            self.exit()
            return
        if result.clear:
            await self.action_clear_screen()
        if result.new_conversation:
            self._step = 0
        if result.lines:
            await self._add(
                Static(
                    Text("\n".join(result.lines), style="red" if result.error else ""),
                    classes="cmd-out",
                )
            )
        self._refresh_bars()

    # ---- run turn -----------------------------------------------------------------------

    async def _turn(self, prompt: str) -> None:
        await self._add(Static(Text(f"❯ {prompt}"), classes="user"))
        self.busy, self._step, self._phase = True, 0, "submitting"
        self._tool_cards.clear()
        self._tool_names.clear()
        self._refresh_bars()
        try:
            result = await self.session.run_turn(
                prompt, self._on_event, self._on_approval
            )
            if result.status == "failed":
                self.healthy = True
            self._refresh_bars()
        except asyncio.CancelledError:
            await self._note(
                f"Detached from run {self.session.last_run_id}. It keeps running on the server; "
                "send a new message to continue the thread.",
                style="yellow",
            )
            raise
        except HaasAPIError as exc:
            self.healthy = exc.status is not None
            await self._add(Static(Text(f"✘ {exc}", style="red"), classes="error-box"))
        finally:
            self.busy = False
            self._phase = ""
            self._refresh_bars()
            self._main("#prompt", PromptInput).focus()

    async def _on_approval(self, calls: list[PendingCall]) -> Decision:
        self._phase = "waiting for approval"
        decision = await self.push_screen_wait(ApprovalScreen(calls))
        names = ", ".join(c.name for c in calls)
        if decision.approved:
            await self._note(f"✔ approved: {names}", style="green")
        else:
            why = f" ({decision.reason})" if decision.reason else ""
            await self._note(f"✘ rejected: {names}{why}", style="yellow")
        self._phase = "resuming"
        return decision

    async def _on_event(self, ev: SSEEvent) -> None:
        d = ev.data
        if ev.event == "run_queued":
            self.healthy = True
            self._phase = "queued"
        elif ev.event == "step_started":
            self._step = int(d.get("step", self._step))
            model = str(d.get("model", ""))
            self._phase = f"step {self._step} · {model.split('/')[-1]}"
            skills = ", ".join(d.get("active_skills", [])) or "no skills"
            await self._note(f"· step {self._step} · {model} · {skills}")
        elif ev.event == "tool_dispatched":
            await self._tool_card(
                d["tool_call_id"], d["tool"], f"running on {d.get('queue', '')}"
            )
        elif ev.event == "tool_result":
            await self._tool_result(d)
        elif ev.event == "run_completed":
            await self._add(
                Markdown(
                    str(d.get("output", "")) or "_(empty response)_",
                    classes="assistant",
                )
            )
            await self._note(
                f"✓ done · {d.get('steps_used', '?')} step(s) · ${float(d.get('total_cost_usd', 0)):.4f}"
                f" · {d.get('cached_tokens', 0)} cached tokens"
            )
        elif ev.event == "run_error":
            await self._add(
                Static(
                    Text(
                        f"✘ Run failed: {d.get('error', 'unknown error')}", style="red"
                    ),
                    classes="error-box",
                )
            )
        elif ev.event in ("hitl_approved", "hitl_rejected"):
            self._phase = "running tools"
        self._refresh_bars()

    async def _tool_card(self, call_id: str, name: str, detail: str) -> Static:
        card = self._tool_cards.get(call_id)
        self._tool_names[call_id] = name
        text = Text(f"⏺ {name}", style="bold")
        text.append(f"  {detail}", style="dim")
        if card is None:
            card = Static(text, classes="tool")
            self._tool_cards[call_id] = card
            await self._add(card)
        else:
            card.update(text)
        return card

    async def _tool_result(self, d: dict[str, Any]) -> None:
        call_id, name = d["tool_call_id"], d.get("tool", "tool")
        card = self._tool_cards.get(call_id) or await self._tool_card(call_id, name, "")
        err = bool(d.get("is_error"))
        preview = " ".join(str(d.get("preview", "")).split())
        if len(preview) > 140:
            preview = preview[:137] + "…"
        text = Text("✘ " if err else "✔ ", style="red" if err else "green")
        text.append(name, style="bold")
        text.append(f"  {preview}", style="red" if err else "dim")
        card.update(text)
        card.set_class(err, "error")
        card.set_class(not err, "done")

    # ---- actions ------------------------------------------------------------------------

    def action_detach(self) -> None:
        if self.busy:
            self.workers.cancel_group(self, "turn")

    async def action_clear_screen(self) -> None:
        await self._main("#chat", VerticalScroll).remove_children()

    async def action_new_chat(self) -> None:
        if self.busy:
            await self._note("Detach (Esc) before starting a new chat.", style="yellow")
            return
        await self._submit("/new")
