from __future__ import annotations

import asyncio
from typing import Any, cast

from rich.syntax import Syntax
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.css.query import NoMatches
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    ContentSwitcher,
    DataTable,
    Footer,
    Input,
    Markdown,
    Static,
    TextArea,
)

from haas_cli.client import HaasAPIError, SSEEvent
from haas_cli.completion import Completion, common_prefix, suggest
from haas_cli.session import (
    ChatSession,
    Decision,
    PendingCall,
    handle_command,
)
from haas_cli.tui.safety import sanitize_terminal_text

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BANNER = "[b #7aa2f7]◆ HaaS Chat[/]  [dim]distributed agent harness[/]"


ASCII_SPINNER = "|/-\\"
MAX_RENDERED_OUTPUT = 20_000
MAX_APPROVAL_ARGUMENTS = 12_000


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


class ApprovalScreen(ModalScreen[Decision | None]):
    """Human-in-the-loop gate: approve or reject the tool calls a run wants to make."""

    BINDINGS = [
        Binding("y", "approve", "Approve"),
        Binding("n", "reject", "Reject"),
        Binding("escape", "leave_pending", "Leave pending"),
    ]

    def __init__(self, calls: list[PendingCall], *, ascii: bool = False) -> None:
        super().__init__()
        self.calls = calls
        self.ascii = ascii

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-box"):
            title = "Approval required" if self.ascii else "⚠  Approval required"
            yield Static(title, id="approval-title")
            yield Static(
                "The agent wants to run the following action(s). Nothing runs until you approve.",
                id="approval-sub",
            )
            with VerticalScroll(id="approval-calls"):
                for c in self.calls:
                    arguments = sanitize_terminal_text(
                        c.pretty_args, limit=MAX_APPROVAL_ARGUMENTS
                    )
                    yield Static(
                        Text(
                            f"{'>' if self.ascii else '▸'} "
                            f"{sanitize_terminal_text(c.name, limit=160)}",
                            style="bold yellow",
                        ),
                        classes="call-name",
                    )
                    yield Static(
                        Syntax(arguments, "json", theme="ansi_dark", word_wrap=True),
                        classes="call-args",
                    )
                    if len(c.pretty_args) > MAX_APPROVAL_ARGUMENTS:
                        yield Static(
                            "Arguments exceed the safe review limit; this TUI cannot approve this action.",
                            classes="error-box",
                        )
            yield Input(placeholder="Rejection reason (optional)", id="reason")
            with Horizontal(id="approval-buttons"):
                yield Button("Approve  (y)", variant="success", id="approve")
                yield Button("Reject  (n)", variant="error", id="reject")
                yield Button("Leave pending  (Esc)", id="leave-pending")

    def on_mount(self) -> None:
        if any(len(call.pretty_args) > MAX_APPROVAL_ARGUMENTS for call in self.calls):
            self.query_one("#approve", Button).disabled = True
        self.query_one("#leave-pending", Button).focus()

    def action_approve(self) -> None:
        if not isinstance(self.focused, Input):
            self.dismiss(Decision(True))

    def action_reject(self) -> None:
        reason = self.query_one("#reason", Input).value.strip() or None
        self.dismiss(Decision(False, reason))

    def action_leave_pending(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#approve")
    def _approve(self) -> None:
        self.dismiss(Decision(True))

    @on(Button.Pressed, "#reject")
    def _reject(self) -> None:
        self.action_reject()

    @on(Button.Pressed, "#leave-pending")
    def _leave_pending(self) -> None:
        self.action_leave_pending()


class CancelRunScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "confirm", "Request cancellation"),
        Binding("n", "keep_running", "Keep running"),
        Binding("escape", "keep_running", "Keep running"),
    ]

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def compose(self) -> ComposeResult:
        with Vertical(id="cancel-box"):
            yield Static("Request cooperative cancellation?", id="cancel-title")
            yield Static(
                f"Run {sanitize_terminal_text(self.run_id)} will be marked cancelled. "
                "A tool or external request already in progress may still finish.",
                id="cancel-detail",
            )
            with Horizontal(id="cancel-buttons"):
                yield Button("Keep running  (Esc)", id="keep-running")
                yield Button(
                    "Request cancellation  (y)", variant="error", id="confirm-cancel"
                )

    def on_mount(self) -> None:
        self.query_one("#keep-running", Button).focus()

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_keep_running(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm-cancel")
    def _confirm(self) -> None:
        self.action_confirm()

    @on(Button.Pressed, "#keep-running")
    def _keep_running(self) -> None:
        self.action_keep_running()


class NavigationCommands(Provider):
    @staticmethod
    def _commands() -> list[tuple[str, str, str]]:
        return [
            ("Open chat", "action_show_chat", "Go to the chat workspace"),
            ("Browse runs", "action_show_runs", "List and attach to tenant runs"),
            ("Refresh runs", "action_refresh_runs", "Reload the current run page"),
            (
                "Attach to selected run",
                "action_attach_selected",
                "Follow the selected run",
            ),
            (
                "Cancel selected run",
                "action_cancel_selected",
                "Request cooperative cancellation",
            ),
            ("Show help", "action_show_help", "Keyboard and safety guidance"),
            (
                "Change theme",
                "action_cycle_theme",
                "Cycle between dark and light themes",
            ),
        ]

    async def discover(self) -> Hits:
        for label, action, description in self._commands():

            def invoke(action_name: str = action) -> None:
                target = cast(ChatApp, self.app)
                target.run_worker(
                    getattr(target, action_name)(), group="navigation-command"
                )

            yield DiscoveryHit(label, invoke, help=description)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        app = cast(ChatApp, self.app)
        for label, action, description in self._commands():
            if (match := matcher.match(label)) > 0:

                def invoke(action_name: str = action, target: ChatApp = app) -> None:
                    target.run_worker(
                        getattr(target, action_name)(), group="navigation-command"
                    )

                yield Hit(
                    match,
                    matcher.highlight(label),
                    invoke,
                    help=description,
                )


class ChatApp(App[None]):
    TITLE = "HaaS Workbench"
    COMMANDS = App.COMMANDS | {NavigationCommands}

    CSS = """
    Screen { layout: vertical; }
    #topbar { height: 1; background: $primary-darken-2; color: $text; padding: 0 1; }
    #navigation { height: 3; padding: 0 1; align-vertical: middle; }
    #navigation Button { margin-right: 1; min-width: 12; }
    #workspaces { height: 1fr; }
    #chat-workspace, #runs-workspace { height: 1fr; }
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
    #runs-heading { height: 2; padding: 0 1; }
    #runs-table { height: 1fr; }
    #run-actions { height: 3; padding: 0 1; }
    #run-actions Button { margin-right: 1; }
    #run-detail { height: 3; padding: 0 1; color: $text-muted; }
    #status { height: 1; background: $surface-darken-1; color: $text-muted; padding: 0 1; }

    ApprovalScreen, CancelRunScreen { align: center middle; background: $background 70%; }
    #approval-box { width: 80%; max-width: 100; height: auto; max-height: 85%;
        border: thick $warning; background: $surface; padding: 1 2; }
    #approval-title { text-style: bold; color: $warning; }
    #approval-sub { color: $text-muted; margin-bottom: 1; }
    #approval-calls { height: auto; max-height: 20; }
    .call-args { margin: 0 0 1 2; }
    #reason { margin-top: 1; }
    #approval-buttons { height: auto; margin-top: 1; align-horizontal: right; }
    #approval-buttons Button { margin-left: 2; }
    #cancel-box { width: 80%; max-width: 90; height: auto; border: thick $error;
        background: $surface; padding: 1 2; }
    #cancel-title { text-style: bold; color: $error; }
    #cancel-detail { margin: 1 0; }
    #cancel-buttons { height: auto; align-horizontal: right; }
    #cancel-buttons Button { margin-left: 2; }
    """

    BINDINGS = [
        Binding("escape", "detach", "Detach run", show=True),
        Binding("ctrl+1", "show_chat", "Chat", show=True),
        Binding("ctrl+2", "show_runs", "Runs", show=True),
        Binding("ctrl+r", "refresh_runs", "Refresh"),
        Binding("ctrl+t", "cycle_theme", "Theme"),
        Binding("f1", "show_help", "Help"),
        Binding("f6", "focus_next", "Next focus"),
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
        self._runs: dict[str, dict[str, Any]] = {}
        self._runs_cursor: str | None = None
        self._selected_run_id: str | None = None
        self._ascii = session.settings.ascii
        self._loading_runs = False
        self._system_theme = self.theme

    # ---- layout -------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        with Horizontal(id="navigation"):
            yield Button("Chat", id="nav-chat", variant="primary")
            yield Button("Runs", id="nav-runs")
            yield Button("Help", id="nav-help")
        with ContentSwitcher(initial="chat-workspace", id="workspaces"):
            with Vertical(id="chat-workspace"):
                yield VerticalScroll(id="chat")
                yield Static(id="activity")
                yield Static(id="hints")
                yield PromptInput(id="prompt", show_line_numbers=False, soft_wrap=True)
            with Vertical(id="runs-workspace"):
                yield Static("Runs for the current tenant", id="runs-heading")
                yield DataTable(id="runs-table", cursor_type="row", zebra_stripes=True)
                with Horizontal(id="run-actions"):
                    yield Button("Refresh", id="runs-refresh")
                    yield Button("Load more", id="runs-more", disabled=True)
                    yield Button("Attach", id="runs-attach", disabled=True)
                    yield Button(
                        "Cancel..." if self._ascii else "Cancel…",
                        id="runs-cancel",
                        disabled=True,
                    )
                yield Static("Select a run to inspect or attach.", id="run-detail")
        yield Static(id="status")
        yield Footer()

    async def on_mount(self) -> None:
        if self.session.settings.no_color:
            self.console.no_color = True
        if self.session.settings.theme in ("dark", "light"):
            self.theme = f"textual-{self.session.settings.theme}"
        else:
            self.theme = self._system_theme
        table = self._main("#runs-table", DataTable)
        table.add_columns("Run ID", "Status", "Thread", "Steps", "Cost")
        self._main("#prompt", PromptInput).focus()
        self.set_interval(0.1, self._tick)
        self.sub_title = self.session.settings.base_url
        if self._ascii:
            await self._add(
                Static("HaaS Workbench | distributed agent harness", classes="note")
            )
        else:
            await self._add(Static(BANNER, classes="note"))
        await self._check_health()
        await self._note(
            (
                "Type a task and press Enter | Ctrl+J newline | /help for commands "
                "| Esc detaches from a running task"
                if self._ascii
                else "Type a task and press Enter · Ctrl+J newline · /help for commands "
                "· Esc detaches from a running task"
            )
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
        w = Static(
            Text(sanitize_terminal_text(text, limit=MAX_RENDERED_OUTPUT), style=style),
            classes="note",
        )
        await self._add(w)
        return w

    def _symbol(self, unicode_symbol: str, ascii_symbol: str) -> str:
        return ascii_symbol if self._ascii else unicode_symbol

    async def _check_health(self) -> None:
        try:
            await self.session.client.health()
            self.healthy = True
            data = await self.session.plugins()
            await self._note(
                f"Connected to {self.session.settings.base_url} as tenant "
                f"'{self.session.settings.tenant}' "
                f"{'-' if self._ascii else '—'} {len(data['skills'])} skills, "
                f"{len(data['tools'])} tools.",
                style="green",
            )
        except HaasAPIError as exc:
            self.healthy = False
            await self._add(
                Static(
                    Text(
                        sanitize_terminal_text(
                            f"{self._symbol('✘', 'ERROR')} {exc}\n  "
                            "Start the stack with `docker compose -f docker-compose.yml "
                            "-f docker-compose.dev.yml up -d`, then send a message to retry."
                        ),
                        style="red",
                    ),
                    classes="error-box",
                )
            )

    def _refresh_bars(self) -> None:
        s = self.session.settings
        state = {True: "UP", False: "DOWN", None: "?"}[self.healthy]
        endpoint = sanitize_terminal_text(s.base_url, limit=160)
        tenant = sanitize_terminal_text(s.tenant, limit=100)
        if self._ascii:
            self._main("#topbar", Static).update(
                Text(f"{state} {endpoint} | tenant {tenant} | theme {s.theme}")
            )
        else:
            dot = {True: "●", False: "●", None: "●"}[self.healthy]
            color = {True: "green", False: "red", None: "yellow"}[self.healthy]
            text = Text()
            text.append(f"{dot} ", style=color)
            text.append(endpoint)
            text.append("  tenant ", style="dim")
            text.append(tenant, style="bold")
            self._main("#topbar", Static).update(text)
        parts = [
            f"thread {sanitize_terminal_text(s.thread_id or '(new)', limit=48)}",
            f"step {self._step}" if self.busy else "idle",
            f"${self.session.total_cost_usd:.4f}",
            f"skills: {sanitize_terminal_text(', '.join(self.session.active_skills), limit=80) or '-'}",
        ]
        if s.preload_skills:
            parts.append(
                f"preload: {sanitize_terminal_text(', '.join(s.preload_skills), limit=80)}"
            )
        if s.secrets:
            parts.append(f"secrets: {len(s.secrets)}")
        separator = " | " if self._ascii else " │ "
        self._main("#status", Static).update(Text(separator.join(parts)))

    def _tick(self) -> None:
        try:
            act = self._main("#activity", Static)
        except NoMatches:  # timer can fire before the screen finishes composing
            return
        if self.busy:
            spinner = ASCII_SPINNER if self._ascii else SPINNER
            self._spin = (self._spin + 1) % len(spinner)
            act.update(
                Text(
                    f"{spinner[self._spin]} {self._phase or 'working'}"
                    f"{'  (esc to detach)' if self._ascii else '…  (Esc to detach)'}"
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
            marker = (
                (">" if selected else " ")
                if self._ascii
                else ("▸" if selected else " ")
            )
            value = sanitize_terminal_text(item.value.strip())
            description = sanitize_terminal_text(item.description)
            line = f"{marker} {value.ljust(width)}  {description}"
            text.append(line + "\n", style="bold reverse" if selected else "")
        more = f" ({len(items)} matches)" if len(items) > size else ""
        guidance = (
            "Tab complete | Up/Down choose | Enter runs a full command"
            if self._ascii
            else "Tab complete · ↑↓ choose · Enter runs a full command"
        )
        text.append(guidance + more, style="dim")
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
        await self._add(
            Static(
                Text(
                    sanitize_terminal_text(shown, limit=MAX_RENDERED_OUTPUT),
                    style="bold magenta",
                ),
                classes="user",
            )
        )
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
                    Text(
                        sanitize_terminal_text(
                            "\n".join(result.lines), limit=MAX_RENDERED_OUTPUT
                        ),
                        style="red" if result.error else "",
                    ),
                    classes="cmd-out",
                )
            )
        self._refresh_bars()

    # ---- run turn -----------------------------------------------------------------------

    async def _turn(self, prompt: str) -> None:
        await self._add(
            Static(
                Text(
                    f"{self._symbol('❯', '>')} "
                    f"{sanitize_terminal_text(prompt, limit=MAX_RENDERED_OUTPUT)}",
                    style="bold",
                ),
                classes="user",
            )
        )
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
            await self._add(
                Static(
                    Text(
                        sanitize_terminal_text(
                            f"{self._symbol('✘', 'ERROR')} {exc}",
                            limit=MAX_RENDERED_OUTPUT,
                        ),
                        style="red",
                    ),
                    classes="error-box",
                )
            )
        finally:
            self.busy = False
            self._phase = ""
            self._refresh_bars()
            self._main("#prompt", PromptInput).focus()

    async def _on_approval(self, calls: list[PendingCall]) -> Decision | None:
        self._phase = "waiting for approval"
        decision = await self.push_screen_wait(ApprovalScreen(calls, ascii=self._ascii))
        if decision is None:
            await self._note(
                "Approval left pending on the server. Attach to the run again to review it.",
                style="yellow",
            )
            self._phase = ""
            return None
        names = ", ".join(c.name for c in calls)
        if decision.approved:
            mark = "APPROVED" if self._ascii else "✔ approved"
            await self._note(f"{mark}: {names}", style="green")
        else:
            why = f" ({decision.reason})" if decision.reason else ""
            mark = "REJECTED" if self._ascii else "✘ rejected"
            await self._note(f"{mark}: {names}{why}", style="yellow")
        self._phase = "resuming"
        return decision

    async def _on_event(self, ev: SSEEvent) -> None:
        d = ev.data
        if ev.event == "run_queued":
            self.healthy = True
            self._phase = "queued"
        elif ev.event == "step_started":
            self._step = int(d.get("step", self._step))
            model = sanitize_terminal_text(d.get("model", ""), limit=160)
            self._phase = f"step {self._step} · {model.split('/')[-1]}"
            skills = (
                sanitize_terminal_text(
                    ", ".join(str(skill) for skill in d.get("active_skills", [])),
                    limit=240,
                )
                or "no skills"
            )
            await self._note(
                f"step {self._step} | {model} | {skills}"
                if self._ascii
                else f"· step {self._step} · {model} · {skills}"
            )
        elif ev.event == "tool_dispatched":
            await self._tool_card(
                str(d["tool_call_id"]),
                sanitize_terminal_text(d["tool"], limit=160),
                f"running on {sanitize_terminal_text(d.get('queue', ''), limit=100)}",
            )
        elif ev.event == "tool_result":
            await self._tool_result(d)
        elif ev.event == "run_completed":
            await self._add(
                Markdown(
                    sanitize_terminal_text(
                        d.get("output", ""), limit=MAX_RENDERED_OUTPUT
                    )
                    or "_(empty response)_",
                    classes="assistant",
                )
            )
            await self._note(
                (
                    f"DONE | {d.get('steps_used', '?')} step(s) | "
                    f"${float(d.get('total_cost_usd', 0)):.4f} | "
                    f"{d.get('cached_tokens', 0)} cached tokens"
                    if self._ascii
                    else f"✓ done · {d.get('steps_used', '?')} step(s) · "
                    f"${float(d.get('total_cost_usd', 0)):.4f} · "
                    f"{d.get('cached_tokens', 0)} cached tokens"
                )
            )
        elif ev.event == "run_error":
            await self._add(
                Static(
                    Text(
                        sanitize_terminal_text(
                            f"{self._symbol('✘', 'ERROR')} Run failed: "
                            f"{d.get('error', 'unknown error')}",
                            limit=MAX_RENDERED_OUTPUT,
                        ),
                        style="red",
                    ),
                    classes="error-box",
                )
            )
        elif ev.event == "run_cancelled":
            await self._note(
                f"Run {sanitize_terminal_text(d.get('run_id', self.session.last_run_id), limit=64)} was cancelled.",
                style="yellow",
            )
        elif ev.event in ("hitl_approved", "hitl_rejected"):
            self._phase = "running tools"
        self._refresh_bars()

    async def _tool_card(self, call_id: str, name: str, detail: str) -> Static:
        card = self._tool_cards.get(call_id)
        self._tool_names[call_id] = name
        text = Text(
            f"{self._symbol('⏺', '*')} {sanitize_terminal_text(name, limit=160)}",
            style="bold",
        )
        text.append(f"  {sanitize_terminal_text(detail, limit=240)}", style="dim")
        if card is None:
            card = Static(text, classes="tool")
            self._tool_cards[call_id] = card
            await self._add(card)
        else:
            card.update(text)
        return card

    async def _tool_result(self, d: dict[str, Any]) -> None:
        call_id = str(d["tool_call_id"])
        name = sanitize_terminal_text(d.get("tool", "tool"), limit=160)
        card = self._tool_cards.get(call_id) or await self._tool_card(call_id, name, "")
        err = bool(d.get("is_error"))
        preview = " ".join(
            sanitize_terminal_text(
                d.get("preview", ""), limit=MAX_RENDERED_OUTPUT
            ).split()
        )
        if len(preview) > 140:
            preview = preview[:137] + ("..." if self._ascii else "…")
        status = (
            ("ERROR " if err else "OK ") if self._ascii else ("✘ " if err else "✔ ")
        )
        text = Text(status, style="red" if err else "green")
        text.append(name, style="bold")
        text.append(f"  {preview}", style="red" if err else "dim")
        card.update(text)
        card.set_class(err, "error")
        card.set_class(not err, "done")

    # ---- actions ------------------------------------------------------------------------

    def _switch_workspace(self, workspace: str) -> None:
        self._main("#workspaces", ContentSwitcher).current = workspace
        self._main("#nav-chat", Button).variant = (
            "primary" if workspace == "chat-workspace" else "default"
        )
        self._main("#nav-runs", Button).variant = (
            "primary" if workspace == "runs-workspace" else "default"
        )

    async def action_show_chat(self) -> None:
        self._switch_workspace("chat-workspace")
        self._main("#prompt", PromptInput).focus()

    async def action_show_runs(self) -> None:
        self._switch_workspace("runs-workspace")
        await self._load_runs(reset=True)
        self._main("#runs-table", DataTable).focus()

    async def action_show_help(self) -> None:
        self._switch_workspace("chat-workspace")
        await self._note(
            "Ctrl+1 Chat | Ctrl+2 Runs | Ctrl+P command palette | F6 next focus | "
            "Esc detaches locally (server run continues). In Runs, select a row to "
            "attach or request cooperative cancellation. Cancellation cannot stop an "
            "external call already in progress.",
            style="cyan",
        )
        self._main("#prompt", PromptInput).focus()

    async def action_refresh_runs(self) -> None:
        await self._load_runs(reset=True)

    async def action_cycle_theme(self) -> None:
        themes = ("auto", "dark", "light")
        current = self.session.settings.theme
        theme = themes[(themes.index(current) + 1) % len(themes)]
        self.session.settings.theme = theme
        self.theme = self._system_theme if theme == "auto" else f"textual-{theme}"
        self._refresh_bars()
        await self._note(f"Theme: {theme}", style="cyan")

    async def action_attach_selected(self) -> None:
        run_id = self._selected_run_id
        if run_id is None:
            await self._note("Select a run to attach.", style="yellow")
            return
        if self.busy:
            await self._note("A run is already attached in this TUI.", style="yellow")
            return
        self._switch_workspace("chat-workspace")
        self.run_worker(
            self._attach_run(run_id), exclusive=True, group="turn", name="attach-run"
        )

    async def _attach_run(self, run_id: str) -> None:
        self.busy, self._step, self._phase = True, 0, "attaching"
        self._tool_cards.clear()
        self._tool_names.clear()
        try:
            result = await self.session.attach_run(
                run_id, self._on_event, self._on_approval
            )
            if result.status == "paused":
                await self._note(
                    f"Run {run_id} remains paused for approval.", style="yellow"
                )
            elif result.status == "failed":
                await self._note(
                    f"Run {run_id} did not complete successfully.", style="red"
                )
            else:
                self.healthy = True
        except asyncio.CancelledError:
            await self._note(
                f"Detached from run {run_id}. It continues on the server; "
                "use Runs to attach again.",
                style="yellow",
            )
            raise
        except HaasAPIError as exc:
            await self._note(f"{self._symbol('✘', 'ERROR')} {exc}", style="red")
        finally:
            self.busy = False
            self._phase = ""
            self._refresh_bars()
            self._update_run_actions()
            self._main("#prompt", PromptInput).focus()

    async def action_cancel_selected(self) -> None:
        run_id = self._selected_run_id
        if run_id is None:
            await self._note("Select a run to cancel.", style="yellow")
            return
        decision = await self.push_screen_wait(CancelRunScreen(run_id))
        if not decision:
            return
        try:
            await self.session.client.cancel_run(run_id)
        except HaasAPIError as exc:
            self._main("#run-detail", Static).update(
                Text(
                    f"Cancellation request failed: {sanitize_terminal_text(exc, limit=500)}",
                    style="red",
                )
            )
            return
        await self._note(
            f"Cancellation requested for run {run_id}. In-flight external calls may "
            "still finish.",
            style="yellow",
        )
        await self._load_runs(reset=True)

    async def _load_runs(self, *, reset: bool) -> None:
        if self._loading_runs:
            return
        table = self._main("#runs-table", DataTable)
        more = self._main("#runs-more", Button)
        self._loading_runs = True
        more.disabled = True
        if reset:
            table.clear(columns=False)
            self._runs.clear()
            self._runs_cursor = None
            self._selected_run_id = None
            self._render_run_detail()
            self._update_run_actions()
        try:
            response = await self.session.client.list_runs(cursor=self._runs_cursor)
            items = response.get("items")
            if not isinstance(items, list) or not all(
                isinstance(item, dict) for item in items
            ):
                raise HaasAPIError("Run list response has an invalid items field.")
            for run in items:
                run_id = str(run["run_id"])
                self._runs[run_id] = run
                table.add_row(
                    sanitize_terminal_text(run_id, limit=40),
                    sanitize_terminal_text(run.get("status", "unknown"), limit=24),
                    sanitize_terminal_text(run.get("thread_id", ""), limit=32),
                    str(run.get("step_count", 0)),
                    f"${float(run.get('total_cost_usd') or 0.0):.4f}",
                    key=run_id,
                )
            cursor = response.get("next_cursor")
            if cursor is not None and not isinstance(cursor, str):
                raise HaasAPIError("Run list response has an invalid next_cursor.")
            self._runs_cursor = cursor
            more.disabled = cursor is None
            if items and self._selected_run_id is None:
                table.move_cursor(row=0)
                self._select_run(str(items[0]["run_id"]))
        except HaasAPIError as exc:
            self._main("#run-detail", Static).update(
                Text(
                    f"Could not load runs: {sanitize_terminal_text(exc, limit=500)}",
                    style="red",
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            self._main("#run-detail", Static).update(
                Text(
                    f"Invalid run-list response: {sanitize_terminal_text(exc, limit=300)}",
                    style="red",
                )
            )
        finally:
            self._loading_runs = False
            more.disabled = self._runs_cursor is None

    def _select_run(self, run_id: str) -> None:
        self._selected_run_id = run_id if run_id in self._runs else None
        self._render_run_detail()
        self._update_run_actions()

    def _render_run_detail(self) -> None:
        detail = self._main("#run-detail", Static)
        run = self._runs.get(self._selected_run_id or "")
        if run is None:
            detail.update("Select a run to inspect or attach.")
            return
        run_id = sanitize_terminal_text(run.get("run_id", ""), limit=64)
        status = sanitize_terminal_text(run.get("status", "unknown"), limit=24)
        thread_id = sanitize_terminal_text(run.get("thread_id", ""), limit=48)
        detail.update(
            f"{run_id} | {status} | thread {thread_id} | "
            f"{run.get('step_count', 0)} step(s) | "
            f"${float(run.get('total_cost_usd') or 0.0):.4f}"
        )

    def _update_run_actions(self) -> None:
        try:
            run = self._runs.get(self._selected_run_id or "")
            self._main("#runs-attach", Button).disabled = run is None or self.busy
            self._main("#runs-cancel", Button).disabled = run is None or run.get(
                "status"
            ) not in ("running", "input_required")
        except NoMatches:
            return

    @on(DataTable.RowHighlighted, "#runs-table")
    def _on_run_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._select_run(str(event.row_key.value))

    @on(Button.Pressed, "#nav-chat")
    async def _on_nav_chat(self) -> None:
        await self.action_show_chat()

    @on(Button.Pressed, "#nav-runs")
    async def _on_nav_runs(self) -> None:
        await self.action_show_runs()

    @on(Button.Pressed, "#nav-help")
    async def _on_nav_help(self) -> None:
        await self.action_show_help()

    @on(Button.Pressed, "#runs-refresh")
    async def _on_runs_refresh(self) -> None:
        await self.action_refresh_runs()

    @on(Button.Pressed, "#runs-more")
    async def _on_runs_more(self) -> None:
        await self._load_runs(reset=False)

    @on(Button.Pressed, "#runs-attach")
    async def _on_runs_attach(self) -> None:
        await self.action_attach_selected()

    @on(Button.Pressed, "#runs-cancel")
    async def _on_runs_cancel(self) -> None:
        await self.action_cancel_selected()

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
