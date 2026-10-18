from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
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

from haas_cli.client import (
    OUTPUT_DELTA_EVENT,
    OUTPUT_DELTA_FEATURE,
    HaasAPIError,
    SSEEvent,
)
from haas_cli.completion import Completion, common_prefix, suggest
from haas_cli.profiles import EnvironmentProfile, ProfileStore
from haas_cli.session import (
    ApprovalBinding,
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
MAX_CHAT_WIDGETS = 400
MAX_APPROVAL_ARGUMENTS = 12_000
APPROVAL_FILTERS = ("pending", "all", "approved", "rejected", "expired", "cancelled")
CANCELLABLE_STATUSES = ("queued", "running", "input_required")


def pending_calls_from(raw_calls: Any) -> list[PendingCall] | None:
    """Normalise API tool-call shapes; None when the payload is malformed."""
    if not isinstance(raw_calls, list):
        return None
    calls: list[PendingCall] = []
    for item in raw_calls:
        if not isinstance(item, dict):
            return None
        function = item.get("function")
        function_data = function if isinstance(function, dict) else {}
        call_id = item.get("id", item.get("tool_call_id"))
        name = item.get("name", function_data.get("name"))
        arguments = item.get("arguments", function_data.get("arguments", {}))
        if not isinstance(call_id, str) or not isinstance(name, str):
            return None
        calls.append(PendingCall(call_id, name, arguments))
    return calls


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

    def __init__(
        self,
        calls: list[PendingCall],
        *,
        ascii: bool = False,
        decisions_enabled: bool = True,
        decision_disabled_reason: str | None = None,
        binding: ApprovalBinding | None = None,
    ) -> None:
        super().__init__()
        self.calls = calls
        self.binding = binding
        self.ascii = ascii
        self.decisions_enabled = decisions_enabled
        self.decision_disabled_reason = decision_disabled_reason

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-box"):
            title = "Approval required" if self.ascii else "⚠  Approval required"
            yield Static(title, id="approval-title")
            yield Static(
                "The agent wants to run the following action(s). Nothing runs until you approve.",
                id="approval-sub",
            )
            if self.binding is not None:
                yield Static(
                    Text(
                        f"batch {sanitize_terminal_text(self.binding.batch_id, limit=40)}"
                        f" | digest {sanitize_terminal_text(self.binding.batch_digest, limit=64)[:16]}"
                        f" | expires {sanitize_terminal_text(self.binding.expires_at or '?', limit=40)}",
                        style="dim",
                    ),
                    id="approval-binding",
                )
            if self.decision_disabled_reason:
                yield Static(self.decision_disabled_reason, classes="error-box")
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
        if not self.decisions_enabled:
            self.query_one("#approve", Button).disabled = True
            self.query_one("#reject", Button).disabled = True
        if any(len(call.pretty_args) > MAX_APPROVAL_ARGUMENTS for call in self.calls):
            self.query_one("#approve", Button).disabled = True
        self.query_one("#leave-pending", Button).focus()

    def action_approve(self) -> None:
        if (
            self.decisions_enabled
            and not any(
                len(call.pretty_args) > MAX_APPROVAL_ARGUMENTS for call in self.calls
            )
            and not isinstance(self.focused, Input)
        ):
            self.dismiss(Decision(True))

    def action_reject(self) -> None:
        if not self.decisions_enabled:
            return
        reason = self.query_one("#reason", Input).value.strip() or None
        self.dismiss(Decision(False, reason))

    def action_leave_pending(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#approve")
    def _approve(self) -> None:
        self.action_approve()

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


class LoginScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="identity-box"):
            yield Static("Sign in to HaaS", id="identity-title")
            yield Static(
                "The bearer credential is kept in memory for this process only.",
                classes="note",
            )
            yield Input(
                placeholder="API key or OIDC bearer token",
                password=True,
                id="credential",
            )
            yield Static("", id="identity-error")
            with Horizontal(id="identity-buttons"):
                yield Button("Sign in", variant="primary", id="identity-submit")
                yield Button("Cancel", id="identity-cancel")

    def on_mount(self) -> None:
        self.query_one("#credential", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        credential = self.query_one("#credential", Input).value.strip()
        if not credential:
            self.query_one("#identity-error", Static).update("Enter a credential.")
            return
        self.dismiss(credential)

    @on(Input.Submitted, "#credential")
    def _on_submitted(self) -> None:
        self._submit()

    @on(Button.Pressed, "#identity-submit")
    def _on_submit_button(self) -> None:
        self._submit()

    @on(Button.Pressed, "#identity-cancel")
    def _on_cancel_button(self) -> None:
        self.action_cancel()


class TenantSwitchScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, current_tenant: str) -> None:
        super().__init__()
        self.current_tenant = current_tenant

    def compose(self) -> ComposeResult:
        with Vertical(id="identity-box"):
            yield Static("Switch tenant", id="identity-title")
            yield Static(
                "The selected tenant is verified by the server before the switch.",
                classes="note",
            )
            yield Input(value=self.current_tenant, placeholder="Tenant ID", id="tenant")
            with Horizontal(id="identity-buttons"):
                yield Button("Switch", variant="primary", id="tenant-submit")
                yield Button("Cancel", id="tenant-cancel")

    def on_mount(self) -> None:
        self.query_one("#tenant", Input).focus()
        self.query_one("#tenant", Input).select_all()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        tenant = self.query_one("#tenant", Input).value.strip()
        if tenant:
            self.dismiss(tenant)

    @on(Input.Submitted, "#tenant")
    def _on_submitted(self) -> None:
        self._submit()

    @on(Button.Pressed, "#tenant-submit")
    def _on_submit_button(self) -> None:
        self._submit()

    @on(Button.Pressed, "#tenant-cancel")
    def _on_cancel_button(self) -> None:
        self.action_cancel()


class ProfileSwitchScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self, profiles: list[EnvironmentProfile], current_profile: str
    ) -> None:
        super().__init__()
        self.profile_names = {profile.name for profile in profiles}
        self.current_profile = current_profile
        self.profiles = profiles

    def compose(self) -> ComposeResult:
        summary = "\n".join(
            f"{sanitize_terminal_text(profile.name, limit=64)}  |  "
            f"{profile.environment}  |  "
            f"{sanitize_terminal_text(profile.base_url, limit=120)}  |  "
            f"{sanitize_terminal_text(profile.tenant, limit=64)}"
            for profile in self.profiles
        )
        with Vertical(id="identity-box"):
            yield Static("Switch environment profile", id="identity-title")
            yield Static(summary, id="profile-list")
            yield Input(
                value=self.current_profile, placeholder="Profile name", id="profile"
            )
            with Horizontal(id="identity-buttons"):
                yield Button("Use profile", variant="primary", id="profile-submit")
                yield Button("Cancel", id="profile-cancel")

    def on_mount(self) -> None:
        self.query_one("#profile", Input).focus()
        self.query_one("#profile", Input).select_all()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        name = self.query_one("#profile", Input).value.strip()
        if name in self.profile_names:
            self.dismiss(name)
        else:
            self.query_one("#profile-list", Static).update(
                f"Unknown profile {sanitize_terminal_text(name, limit=64)!r}."
            )

    @on(Input.Submitted, "#profile")
    def _on_submitted(self) -> None:
        self._submit()

    @on(Button.Pressed, "#profile-submit")
    def _on_submit_button(self) -> None:
        self._submit()

    @on(Button.Pressed, "#profile-cancel")
    def _on_cancel_button(self) -> None:
        self.action_cancel()


class AdminFormScreen(ModalScreen[dict[str, str] | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        title: str,
        fields: list[tuple[str, str, str]],
        *,
        json_value: str | None = None,
        json_label: str = "Policy rules JSON",
    ) -> None:
        super().__init__()
        self.form_title = title
        self.fields = fields
        self.json_value = json_value
        self.json_label = json_label

    def compose(self) -> ComposeResult:
        with Vertical(id="admin-form-box"):
            yield Static(self.form_title, id="identity-title")
            for field_id, label, placeholder in self.fields:
                yield Static(label, classes="admin-field-label")
                yield Input(placeholder=placeholder, id=f"admin-field-{field_id}")
            if self.json_value is not None:
                yield Static(self.json_label, classes="admin-field-label")
                yield TextArea(self.json_value, id="admin-form-json", language="json")
            yield Static("", id="identity-error")
            with Horizontal(id="identity-buttons"):
                yield Button("Submit", variant="primary", id="admin-form-submit")
                yield Button("Cancel", id="admin-form-cancel")

    def on_mount(self) -> None:
        if self.fields:
            self.query_one(f"#admin-field-{self.fields[0][0]}", Input).focus()
        elif self.json_value is not None:
            self.query_one("#admin-form-json", TextArea).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        values = {
            field_id: self.query_one(f"#admin-field-{field_id}", Input).value.strip()
            for field_id, _, _ in self.fields
        }
        if any(not value for value in values.values()):
            self.query_one("#identity-error", Static).update(
                "Complete every required field."
            )
            return
        if self.json_value is not None:
            values["json"] = self.query_one("#admin-form-json", TextArea).text
        self.dismiss(values)

    @on(Input.Submitted)
    def _on_input_submitted(self) -> None:
        self._submit()

    @on(Button.Pressed, "#admin-form-submit")
    def _on_submit(self) -> None:
        self._submit()

    @on(Button.Pressed, "#admin-form-cancel")
    def _on_cancel(self) -> None:
        self.action_cancel()


class NavigationCommands(Provider):
    @staticmethod
    def _commands() -> list[tuple[str, str, str]]:
        return [
            ("Open chat", "action_show_chat", "Go to the chat workspace"),
            ("Browse runs", "action_show_runs", "List and attach to tenant runs"),
            (
                "Tenant administration",
                "action_show_admin",
                "Manage tenant access and policy",
            ),
            ("Sign in", "action_login", "Authenticate with an API key or OIDC token"),
            ("Sign out", "action_logout", "Clear the in-memory credential"),
            (
                "Switch tenant",
                "action_switch_tenant",
                "Verify and change tenant context",
            ),
            ("Switch profile", "action_switch_profile", "Choose a saved environment"),
            ("Refresh runs", "action_refresh_runs", "Reload the current run page"),
            (
                "Approval inbox",
                "action_show_approvals",
                "Review digest-bound pending approvals",
            ),
            (
                "Inspect selected run",
                "action_inspect_selected",
                "Budget, steps, tool invocations and usage ledger",
            ),
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
    #chat-workspace, #runs-workspace, #admin-workspace, #approvals-workspace {
        height: 1fr;
    }
    #chat { height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }
    .user { margin: 1 0 0 0; color: $text; background: $boost; padding: 0 1; }
    .assistant { margin: 1 0 0 0; padding: 0 1; border-left: thick $success; }
    .assistant MarkdownBlock { margin-bottom: 0; }
    .note { color: $text-muted; margin: 0 0 0 2; }
    .live-output { margin: 1 0 0 0; padding: 0 1; border-left: thick $warning; }
    .live-output.settled { border-left: thick $panel; color: $text-muted; }
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
    #run-inspector { height: 14; margin: 0 1; border: round $primary-darken-1; }
    #run-inspector-body { padding: 0 1; }
    #approvals-heading { height: 2; padding: 0 1; }
    #approvals-table { height: 1fr; }
    #approval-actions { height: 3; padding: 0 1; }
    #approval-actions Button { margin-right: 1; }
    #approval-detail { height: 4; padding: 0 1; color: $text-muted; }
    #nav-admin { display: none; }
    #admin-memberships, #admin-member-grant, #admin-member-revoke,
    #admin-api-keys, #admin-key-create, #admin-key-revoke, #admin-policy-view,
    #admin-policy-activate, #admin-plugin-inventory, #admin-audit,
    #admin-audit-next { display: none; }
    #admin-heading { height: 2; padding: 0 1; }
    .admin-actions { height: 3; padding: 0 1; }
    .admin-actions Button { margin-right: 1; }
    #admin-output { height: 1fr; margin: 0 1; border: round $primary-darken-1; }
    #admin-result { padding: 1; }
    #status { height: 1; background: $surface-darken-1; color: $text-muted; padding: 0 1; }

    ApprovalScreen, CancelRunScreen, LoginScreen, TenantSwitchScreen, ProfileSwitchScreen {
        align: center middle; background: $background 70%;
    }
    AdminFormScreen { align: center middle; background: $background 70%; }
    #identity-box { width: 80%; max-width: 90; height: auto; border: thick $primary;
        background: $surface; padding: 1 2; }
    #identity-title { text-style: bold; color: $accent; }
    #identity-buttons { height: auto; margin-top: 1; align-horizontal: right; }
    #identity-buttons Button { margin-left: 2; }
    #identity-error { color: $error; }
    #profile-list { height: auto; max-height: 10; margin: 1 0; }
    #admin-form-box { width: 80%; max-width: 90; height: auto; max-height: 90%;
        border: thick $primary; background: $surface; padding: 1 2; }
    .admin-field-label { margin-top: 1; color: $text-muted; }
    #admin-form-json { height: 12; min-height: 6; margin-top: 1; }
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
        Binding("ctrl+3", "show_admin", "Admin", show=True),
        Binding("ctrl+4", "show_approvals", "Approvals", show=True),
        Binding("ctrl+r", "refresh_runs", "Refresh"),
        Binding("ctrl+t", "cycle_theme", "Theme"),
        Binding("ctrl+shift+l", "login", "Sign in"),
        Binding("f1", "show_help", "Help"),
        Binding("f6", "focus_next", "Next focus"),
        Binding("ctrl+l", "clear_screen", "Clear"),
        Binding("ctrl+n", "new_chat", "New chat"),
        Binding("ctrl+f", "toggle_follow", "Follow"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(
        self,
        session: ChatSession,
        *,
        initial_prompt: str | None = None,
        profile_store: ProfileStore | None = None,
    ) -> None:
        super().__init__()
        self.session = session
        self.initial_prompt = initial_prompt
        self.profile_store = profile_store or ProfileStore()
        self.identity: dict[str, Any] | None = None
        self.busy = False
        self._submitting_run = False
        self.healthy: bool | None = None
        self._step = 0
        self._phase = ""
        self._spin = 0
        self._tool_cards: dict[str, Static] = {}
        self._tool_names: dict[str, str] = {}
        self._runs: dict[str, dict[str, Any]] = {}
        self._runs_cursor: str | None = None
        self._selected_run_id: str | None = None
        self._audit_cursor: str | None = None
        self._approvals: dict[str, dict[str, Any]] = {}
        self._approval_filter = "pending"
        self._selected_batch_id: str | None = None
        self._loading_approvals = False
        self._ascii = session.settings.ascii
        self._loading_runs = False
        self._system_theme = self.theme
        self._follow = True
        self._preview: Static | None = None
        self._preview_key: tuple[int, int, int] | None = None
        self._preview_text = ""
        self._preview_truncated = False
        self._preview_dirty = False
        self.transcript_trimmed = 0

    # ---- layout -------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        with Horizontal(id="navigation"):
            yield Button("Chat", id="nav-chat", variant="primary")
            yield Button("Runs", id="nav-runs")
            yield Button("Approvals", id="nav-approvals")
            yield Button("Admin", id="nav-admin")
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
                    yield Button("Inspect", id="runs-inspect", disabled=True)
                    yield Button(
                        "Cancel..." if self._ascii else "Cancel…",
                        id="runs-cancel",
                        disabled=True,
                    )
                yield Static("Select a run to inspect or attach.", id="run-detail")
                with VerticalScroll(id="run-inspector"):
                    yield Static(
                        "Inspect a run to see its budget, steps, tool invocations "
                        "and usage ledger.",
                        id="run-inspector-body",
                    )
            with Vertical(id="approvals-workspace"):
                yield Static(
                    "Approval inbox | decisions are bound to the reviewed batch digest",
                    id="approvals-heading",
                )
                yield DataTable(
                    id="approvals-table", cursor_type="row", zebra_stripes=True
                )
                with Horizontal(id="approval-actions"):
                    yield Button("Refresh", id="approvals-refresh")
                    yield Button("Filter: pending", id="approvals-filter")
                    yield Button(
                        "Review..." if self._ascii else "Review…",
                        id="approvals-review",
                        variant="warning",
                        disabled=True,
                    )
                yield Static(
                    "Select an approval batch to review.", id="approval-detail"
                )
            with Vertical(id="admin-workspace"):
                yield Static(
                    "Tenant administration | changes are authorized by the server",
                    id="admin-heading",
                )
                with Horizontal(classes="admin-actions"):
                    yield Button("List members", id="admin-memberships")
                    yield Button("Grant member", id="admin-member-grant")
                    yield Button("Revoke member", id="admin-member-revoke")
                with Horizontal(classes="admin-actions"):
                    yield Button("List API keys", id="admin-api-keys")
                    yield Button("Create API key", id="admin-key-create")
                    yield Button("Revoke API key", id="admin-key-revoke")
                with Horizontal(classes="admin-actions"):
                    yield Button("View policy", id="admin-policy-view")
                    yield Button("Activate policy…", id="admin-policy-activate")
                    yield Button("Scoped plugins", id="admin-plugin-inventory")
                with Horizontal(classes="admin-actions"):
                    yield Button("Audit events", id="admin-audit")
                    yield Button("Audit next", id="admin-audit-next", disabled=True)
                with VerticalScroll(id="admin-output"):
                    yield Static(
                        "Choose an administration action. Credentials are never "
                        "saved by the TUI.",
                        id="admin-result",
                    )
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
        self._main("#approvals-table", DataTable).add_columns(
            "Batch", "Status", "Run", "Step", "Tools", "Expires"
        )
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
        self._refresh_admin_visibility()
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
        # Bounded transcript: older entries remain on the server (/events, /history).
        overflow = len(chat.children) - MAX_CHAT_WIDGETS
        if overflow > 0:
            stale = [
                w
                for w in list(chat.children)[:overflow]
                if w is not self._preview and w not in self._tool_cards.values()
            ]
            if stale:
                await chat.remove_children(stale)
                self.transcript_trimmed += len(stale)
        if self._follow:
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
            return

        self.healthy = True
        if self.session.client.credential:
            try:
                self._accept_identity(await self.session.client.identity())
            except HaasAPIError as exc:
                self._sync_auth_state()
                await self._note(
                    "Session could not be verified: "
                    f"{sanitize_terminal_text(exc, limit=300)}",
                    style="yellow" if exc.status == 401 else "red",
                )
        try:
            data = await self.session.plugins()
            await self._note(
                f"Connected to {self.session.settings.base_url} as tenant "
                f"'{self.session.settings.tenant}' "
                f"{'-' if self._ascii else '—'} {len(data['skills'])} skills, "
                f"{len(data['tools'])} tools.",
                style="green",
            )
        except HaasAPIError as exc:
            if exc.status == 401:
                self._sync_auth_state()
                await self._note(
                    "Authentication required. Use Sign in or the /login command.",
                    style="yellow",
                )
            elif exc.status == 403:
                await self._note(
                    "Connected, but this identity cannot read the plugin catalog.",
                    style="yellow",
                )
            else:
                await self._note(
                    f"Connected, but the plugin catalog is unavailable: "
                    f"{sanitize_terminal_text(exc, limit=300)}",
                    style="red",
                )

    @staticmethod
    def _validate_identity(identity: Any) -> dict[str, Any]:
        if (
            not isinstance(identity, dict)
            or not isinstance(identity.get("tenant_id"), str)
            or not isinstance(identity.get("principal_id"), str)
            or not isinstance(identity.get("roles"), list)
            or not all(isinstance(role, str) for role in identity["roles"])
            or not isinstance(identity.get("credential_type"), str)
            or identity.get("environment") not in ("dev", "staging", "prod")
        ):
            raise HaasAPIError("Identity response is malformed.", 502)
        capabilities = identity.get("capabilities")
        if capabilities is not None and (
            not isinstance(capabilities, dict)
            or capabilities.get("version") != "1"
            or not isinstance(capabilities.get("actions"), list)
            or not all(isinstance(action, str) for action in capabilities["actions"])
            or not isinstance(capabilities.get("event_schemas"), list)
            or "run-events.v1" not in capabilities.get("event_schemas", [])
        ):
            raise HaasAPIError(
                "Server capability/event schema is incompatible; update the client.",
                502,
            )
        return identity

    def _accept_identity(self, identity: Any) -> dict[str, Any]:
        verified = self._validate_identity(identity)
        self.identity = verified
        capabilities = verified.get("capabilities")
        features = (
            capabilities.get("features") if isinstance(capabilities, dict) else None
        )
        self.session.output_deltas = isinstance(features, list) and (
            OUTPUT_DELTA_FEATURE in features
        )
        self.session.settings.environment = verified["environment"]
        self._refresh_admin_visibility()
        return verified

    def _sync_auth_state(self) -> None:
        if self.session.client.auth_expired:
            self.session.settings.credential = None
            self.identity = None
            self._refresh_admin_visibility()
            self._refresh_bars()

    def _refresh_bars(self) -> None:
        s = self.session.settings
        state = {True: "UP", False: "DOWN", None: "?"}[self.healthy]
        endpoint = sanitize_terminal_text(s.base_url, limit=160)
        tenant = sanitize_terminal_text(s.tenant, limit=100)
        environment = s.environment.upper()
        principal = (
            sanitize_terminal_text(
                str(self.identity.get("principal_id", ""))[:8], limit=8
            )
            if self.identity
            else "auth required"
            if self.session.client.auth_expired
            else "signed out"
        )
        identity_roles = self.identity.get("roles", []) if self.identity else []
        roles = (
            ",".join(role for role in identity_roles if isinstance(role, str))
            if isinstance(identity_roles, list)
            else ""
        )
        if self._ascii:
            text = Text(
                f"{state} {environment} {s.profile_name} {endpoint} | "
                f"tenant {tenant} | {principal} {roles} | theme {s.theme}"
            )
        else:
            dot = {True: "●", False: "●", None: "●"}[self.healthy]
            color = {True: "green", False: "red", None: "yellow"}[self.healthy]
            text = Text()
            text.append(f"{dot} ", style=color)
            if s.environment == "prod":
                text.append("PRODUCTION ", style="bold reverse red")
            else:
                text.append(f"{environment} ", style="dim")
            text.append(sanitize_terminal_text(s.profile_name, limit=40), style="bold")
            text.append("  ")
            text.append(endpoint)
            text.append("  tenant ", style="dim")
            text.append(tenant, style="bold")
            text.append("  as ", style="dim")
            text.append(principal, style="bold")
            if roles:
                text.append(
                    f" [{sanitize_terminal_text(roles, limit=80)}]", style="dim"
                )
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
        if not self._follow:
            parts.append("PAUSED (ctrl+f)" if self._ascii else "⏸ paused (ctrl+f)")
        elif self.session.output_deltas and self.session.live_output:
            parts.append("live" if self._ascii else "◉ live")
        separator = " | " if self._ascii else " │ "
        self._main("#status", Static).update(Text(separator.join(parts)))

    def _tick(self) -> None:
        if not self.screen_stack:
            return
        if self._preview_dirty:
            self._render_preview()
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
        parts = line.strip().split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1] if len(parts) == 2 else None
        if command == "/login":
            await self.action_login()
            return
        if command == "/logout":
            await self.action_logout()
            return
        if command == "/tenant":
            await self.action_switch_tenant(argument)
            return
        if command == "/profile":
            await self.action_switch_profile(argument)
            return
        if command == "/admin":
            await self.action_show_admin()
            return
        if command == "/approve":
            await self.action_review_approval(argument)
            return
        if command == "/agent" and argument == "draft":
            if not self._has_admin_role():
                await self._note(
                    "Agent authoring requires a signed-in tenant administrator.",
                    style="yellow",
                )
                return
            values = await self.push_screen_wait(
                AdminFormScreen(
                    "Create immutable agent draft",
                    [("name", "Agent name", "my-agent")],
                    json_label="Agent configuration JSON (no credentials)",
                    json_value=json.dumps(
                        {
                            "primary_model": "",
                            "planner_model": "",
                            "skills": [],
                            "preload_skills": [],
                            "max_steps": 15,
                            "max_cost_usd": 0.30,
                        },
                        indent=2,
                    ),
                )
            )
            if values is None:
                return
            try:
                config = json.loads(values["json"])
                if not isinstance(config, dict):
                    raise ValueError("Configuration must be a JSON object.")
                result = await self.session.client.draft_agent(values["name"], config)
                await self._note(json.dumps(result), style="green")
            except (ValueError, HaasAPIError) as exc:
                await self._note(sanitize_terminal_text(exc, limit=1000), style="red")
            return
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
        self._submitting_run = True
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
            self._sync_auth_state()
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
            self._submitting_run = False
            self._phase = ""
            self._refresh_bars()
            self._main("#prompt", PromptInput).focus()

    async def _on_approval(self, calls: list[PendingCall]) -> Decision | None:
        self._phase = "waiting for approval"
        roles = self.identity.get("roles", []) if self.identity else []
        decisions_enabled = self.identity is None or (
            isinstance(roles, list) and "approver" in roles and not self._submitting_run
        )
        decision = await self.push_screen_wait(
            ApprovalScreen(
                calls,
                ascii=self._ascii,
                decisions_enabled=decisions_enabled,
                decision_disabled_reason=(
                    "This identity cannot approve this action. Leave it pending, then use "
                    "/approve <run-id> with a separate approver credential."
                    if not decisions_enabled
                    else None
                ),
                binding=self.session.pending_approval,
            )
        )
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

    def _render_preview(self) -> None:
        self._preview_dirty = False
        if self._preview is None:
            return
        text = Text(
            sanitize_terminal_text(self._preview_text, limit=MAX_RENDERED_OUTPUT)
        )
        if self._preview_truncated:
            text.append(
                "\n[live preview truncated; final output follows]"
                if self._ascii
                else "\n… live preview truncated; final output follows",
                style="dim",
            )
        self._preview.update(text)
        if self._follow:
            self._main("#chat", VerticalScroll).scroll_end(animate=False)

    async def _on_output_delta(self, d: dict[str, Any]) -> None:
        try:
            key = (int(d["step"]), int(d["attempt"]), int(d["call"]))
        except (KeyError, TypeError, ValueError):
            return
        text = d.get("text")
        if not isinstance(text, str):
            return
        if key != self._preview_key:
            # A new step, retry or provider-call attempt restarts the live buffer.
            self._preview_key = key
            self._preview_text = ""
            self._preview_truncated = False
            if self._preview is None:
                self._preview = Static("", classes="live-output")
                await self._add(self._preview)
        if len(self._preview_text) < MAX_RENDERED_OUTPUT:
            self._preview_text += text[: MAX_RENDERED_OUTPUT - len(self._preview_text)]
        if d.get("truncated") or len(self._preview_text) >= MAX_RENDERED_OUTPUT:
            self._preview_truncated = True
        self._preview_dirty = True
        if d.get("final"):
            self._render_preview()

    async def _settle_preview(self, *, discard: bool) -> None:
        """Closes the live preview when the durable log moves past the streamed call."""
        preview = self._preview
        self._preview, self._preview_key = None, None
        self._preview_dirty = False
        if preview is None:
            return
        if discard or not self._preview_text.strip():
            await preview.remove()
        else:
            preview.update(
                Text(
                    sanitize_terminal_text(
                        self._preview_text, limit=MAX_RENDERED_OUTPUT
                    ),
                    style="dim",
                )
            )
            preview.add_class("settled")
        self._preview_text = ""
        self._preview_truncated = False

    async def _on_event(self, ev: SSEEvent) -> None:
        d = ev.data
        if ev.event == OUTPUT_DELTA_EVENT:
            if isinstance(d, dict):
                await self._on_output_delta(d)
            return
        if self._preview is not None:
            retry = (
                ev.event == "step_started"
                and self._preview_key is not None
                and str(d.get("step")) == str(self._preview_key[0])
            )
            # The durable run_completed output supersedes the advisory preview.
            await self._settle_preview(discard=retry or ev.event == "run_completed")
        if ev.event == "run_queued":
            self.healthy = True
            self._phase = "queued"
            run_id = d.get("run_id")
            if isinstance(run_id, str):
                await self._note(
                    f"Run ID: {sanitize_terminal_text(run_id, limit=64)}",
                    style="dim",
                )
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
        elif ev.event == "run_cancelling":
            self._phase = "cancelling"
            await self._note(
                f"Cancellation requested; waiting for {d.get('outstanding_tools', '?')} "
                "in-flight tool(s) to settle.",
                style="yellow",
            )
        elif ev.event == "run_recovering":
            await self._note(
                f"Run recovered by the reconciler "
                f"({sanitize_terminal_text(d.get('reason', 'unknown'), limit=40)}, "
                f"attempt {sanitize_terminal_text(d.get('attempt', '?'), limit=8)}).",
                style="dim",
            )
        elif ev.event == "approval_expired":
            await self._note(
                "The pending approval expired before a decision was recorded.",
                style="yellow",
            )
        elif ev.event == "stream_gap":
            await self._note(
                "Stream cursor was ahead of the server; replay restarted at the latest event.",
                style="dim",
            )
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

    async def action_login(self) -> None:
        if self.busy:
            await self._note(
                "Detach from the active run before changing identity.", style="yellow"
            )
            return
        credential = await self.push_screen_wait(LoginScreen())
        if credential is None:
            return
        old_credential = self.session.client.credential
        old_identity = self.identity
        self.session.client.set_credential(credential)
        try:
            identity = self._accept_identity(await self.session.client.identity())
        except HaasAPIError as exc:
            if old_credential is not None:
                self.session.client.set_credential(old_credential)
                self.session.settings.credential = old_credential
                self.identity = old_identity
            else:
                self.session.client.set_credential(None)
                self.session.settings.credential = None
                self.identity = None
            await self._note(
                f"Sign in failed: {sanitize_terminal_text(exc, limit=300)}",
                style="red",
            )
            self._refresh_bars()
            return
        self.session.settings.credential = credential
        self.identity = identity
        self.session.new_conversation()
        self._refresh_bars()
        await self._note(
            f"Signed in for tenant {self.session.settings.tenant} "
            f"with role(s): {', '.join(identity['roles']) or 'none'}.",
            style="green",
        )

    async def action_logout(self) -> None:
        if self.busy:
            await self._note(
                "Detach from the active run before changing identity.", style="yellow"
            )
            return
        self.session.client.set_credential(None)
        self.session.settings.credential = None
        self.identity = None
        self._refresh_admin_visibility()
        self.session.new_conversation()
        self._refresh_bars()
        await self._note(
            "Signed out. The credential was cleared from memory.", style="yellow"
        )

    async def action_switch_tenant(self, tenant: str | None = None) -> None:
        if self.busy:
            await self._note(
                "Detach from the active run before changing tenant.", style="yellow"
            )
            return
        if not self.session.client.credential or self.identity is None:
            await self._note("Sign in before switching tenants.", style="yellow")
            return
        selected = tenant or await self.push_screen_wait(
            TenantSwitchScreen(self.session.settings.tenant)
        )
        if not selected or selected == self.session.settings.tenant:
            return
        previous_tenant = self.session.client.tenant
        self.session.client.tenant = selected
        try:
            identity = self._accept_identity(await self.session.client.identity())
        except HaasAPIError as exc:
            self.session.client.tenant = previous_tenant
            self._sync_auth_state()
            await self._note(
                f"Tenant switch rejected: {sanitize_terminal_text(exc, limit=300)}",
                style="red",
            )
            self._refresh_bars()
            return
        self.session.settings.tenant = selected
        self.identity = identity
        self.session.new_conversation()
        self._refresh_bars()
        await self._note(f"Switched to tenant {selected}.", style="green")

    async def action_switch_profile(self, profile_name: str | None = None) -> None:
        if self.busy:
            await self._note(
                "Detach from the active run before changing profiles.", style="yellow"
            )
            return
        try:
            profiles = self.profile_store.profiles()
        except ValueError as exc:
            await self._note(str(exc), style="red")
            return
        if not profiles:
            await self._note(
                "No saved profiles. Create one with `haas profile add`.",
                style="yellow",
            )
            return
        selected = profile_name or await self.push_screen_wait(
            ProfileSwitchScreen(profiles, self.session.settings.profile_name)
        )
        if not selected:
            return
        try:
            profile = self.profile_store.get(selected)
        except ValueError as exc:
            await self._note(str(exc), style="red")
            return
        if profile is None:
            await self._note(f"Profile {selected!r} no longer exists.", style="red")
            return

        old_url = self.session.settings.base_url
        old_tenant = self.session.settings.tenant
        old_identity = self.identity
        self.session.client.set_connection(profile.base_url, profile.tenant)
        identity = None
        if self.session.client.credential:
            try:
                identity = self._validate_identity(await self.session.client.identity())
            except HaasAPIError as exc:
                self.session.client.set_connection(old_url, old_tenant)
                self._sync_auth_state()
                if not self.session.client.auth_expired:
                    self.identity = old_identity
                await self._note(
                    f"Profile could not be verified: "
                    f"{sanitize_terminal_text(exc, limit=300)}",
                    style="red",
                )
                self._refresh_bars()
                return
        try:
            self.profile_store.activate(profile.name)
        except (OSError, ValueError) as exc:
            self.session.client.set_connection(old_url, old_tenant)
            self.identity = old_identity
            await self._note(str(exc), style="red")
            self._refresh_bars()
            return
        self.session.settings.profile_name = profile.name
        self.session.settings.base_url = profile.base_url
        self.session.settings.tenant = profile.tenant
        self.session.settings.environment = profile.environment
        self.identity = None
        self._refresh_admin_visibility()
        if identity is not None:
            self._accept_identity(identity)
        self.session.new_conversation()
        await self._check_health()
        self._refresh_bars()

    def _has_admin_role(self) -> bool:
        return self._has_action("tenant.admin", {"tenant_admin"})

    def _has_audit_role(self) -> bool:
        return self._has_action("audit.read", {"tenant_admin", "auditor"})

    def _has_action(self, action: str, legacy_roles: set[str]) -> bool:
        if self.identity is None:
            return False
        metadata = self.identity.get("capabilities")
        if metadata is not None:
            return action in metadata["actions"]
        return bool(legacy_roles.intersection(self.identity["roles"]))

    def _refresh_admin_visibility(self) -> None:
        try:
            button = self._main("#nav-admin", Button)
            button.display = self._has_audit_role()
            for selector in (
                "#admin-memberships",
                "#admin-member-grant",
                "#admin-member-revoke",
                "#admin-api-keys",
                "#admin-key-create",
                "#admin-key-revoke",
                "#admin-policy-view",
                "#admin-policy-activate",
                "#admin-plugin-inventory",
            ):
                self._main(selector, Button).display = self._has_admin_role()
            for selector in ("#admin-audit", "#admin-audit-next"):
                self._main(selector, Button).display = self._has_audit_role()
            if (
                not self._has_audit_role()
                and self._main("#workspaces", ContentSwitcher).current
                == "admin-workspace"
            ):
                self._switch_workspace("chat-workspace")
        except NoMatches:
            return

    async def _admin_form(
        self,
        title: str,
        fields: list[tuple[str, str, str]],
        *,
        json_value: str | None = None,
    ) -> dict[str, str] | None:
        if not self._has_admin_role():
            await self._note(
                "Tenant administration requires the tenant_admin role.",
                style="yellow",
            )
            return None
        if self.busy:
            await self._note(
                "Detach from the active run before changing tenant administration.",
                style="yellow",
            )
            return None
        return await self.push_screen_wait(
            AdminFormScreen(title, fields, json_value=json_value)
        )

    async def _admin_execute(
        self,
        label: str,
        operation: Callable[[], Awaitable[dict[str, Any]]],
    ) -> None:
        if not self._has_admin_role():
            await self._note(
                "Tenant administration requires the tenant_admin role.",
                style="yellow",
            )
            return
        try:
            result = await operation()
            content = json.dumps(result, indent=2, default=str)
            if label == "Create API key":
                content = (
                    "The API credential is shown once. Copy it now; the TUI will not "
                    "save it.\n\n" + content
                )
            self._main("#admin-result", Static).update(
                Text(sanitize_terminal_text(content, limit=MAX_RENDERED_OUTPUT))
            )
        except HaasAPIError as exc:
            self._sync_auth_state()
            self._main("#admin-result", Static).update(
                Text(
                    f"{label} failed: {sanitize_terminal_text(exc, limit=500)}",
                    style="red",
                )
            )
        except (TypeError, ValueError) as exc:
            self._main("#admin-result", Static).update(
                Text(
                    f"{label} failed: {sanitize_terminal_text(exc, limit=500)}",
                    style="red",
                )
            )

    async def action_admin_list_memberships(self) -> None:
        await self._admin_execute(
            "List memberships", self.session.client.list_memberships
        )

    async def action_admin_grant_membership(self) -> None:
        values = await self._admin_form(
            "Grant tenant membership",
            [
                ("subject", "Principal subject", "OIDC subject or service identity"),
                ("kind", "Principal kind: human or service", "service"),
                (
                    "role",
                    "Role: tenant_admin, submitter, approver, auditor",
                    "submitter",
                ),
            ],
        )
        if values is None:
            return
        if values["kind"] not in ("human", "service"):
            await self._note("Kind must be human or service.", style="red")
            return
        if values["role"] not in (
            "tenant_admin",
            "submitter",
            "approver",
            "auditor",
        ):
            await self._note("Unknown tenant role.", style="red")
            return
        await self._admin_execute(
            "Grant membership",
            lambda: self.session.client.grant_membership(
                subject=values["subject"],
                kind=values["kind"],
                role=values["role"],
            ),
        )

    async def action_admin_revoke_membership(self) -> None:
        values = await self._admin_form(
            "Revoke tenant membership",
            [
                ("principal", "Principal ID", "UUID"),
                (
                    "role",
                    "Role to revoke",
                    "tenant_admin, submitter, approver, or auditor",
                ),
            ],
        )
        if values is None:
            return
        if values["role"] not in (
            "tenant_admin",
            "submitter",
            "approver",
            "auditor",
        ):
            await self._note("Unknown tenant role.", style="red")
            return
        await self._admin_execute(
            "Revoke membership",
            lambda: self.session.client.revoke_membership(
                values["principal"], values["role"]
            ),
        )

    async def action_admin_list_api_keys(self) -> None:
        await self._admin_execute("List API keys", self.session.client.list_api_keys)

    async def action_admin_create_api_key(self) -> None:
        values = await self._admin_form(
            "Create tenant API key",
            [
                ("principal", "Principal ID", "UUID of an active tenant member"),
                ("name", "Key name", "automation"),
                ("expires", "Expires in days (1–365)", "90"),
            ],
        )
        if values is None:
            return
        try:
            expires_in_days = int(values["expires"])
        except ValueError:
            await self._note("Expiration must be a number of days.", style="red")
            return
        if not 1 <= expires_in_days <= 365:
            await self._note("Expiration must be between 1 and 365 days.", style="red")
            return
        await self._admin_execute(
            "Create API key",
            lambda: self.session.client.create_api_key(
                principal_id=values["principal"],
                name=values["name"],
                expires_in_days=expires_in_days,
            ),
        )

    async def action_admin_revoke_api_key(self) -> None:
        values = await self._admin_form(
            "Revoke tenant API key",
            [("key", "API key ID", "UUID")],
        )
        if values is None:
            return
        await self._admin_execute(
            "Revoke API key",
            lambda: self.session.client.revoke_api_key(values["key"]),
        )

    async def action_admin_view_policy(self) -> None:
        await self._admin_execute(
            "View tenant policy", self.session.client.get_tenant_policy
        )

    async def action_admin_plugin_inventory(self) -> None:
        await self._admin_execute(
            "View scoped plugin inventory", self.session.client.plugins
        )

    async def action_admin_activate_policy(self) -> None:
        values = await self._admin_form(
            "Activate policy revision",
            [],
            json_value='{\n  "model_providers": [],\n  "models": []\n}',
        )
        if values is None:
            return
        try:
            rules = json.loads(values["json"])
        except json.JSONDecodeError as exc:
            await self._note(f"Policy JSON is invalid: {exc.msg}.", style="red")
            return
        if not isinstance(rules, dict):
            await self._note("Policy JSON must be an object.", style="red")
            return
        await self._admin_execute(
            "Activate policy",
            lambda: self.session.client.activate_tenant_policy(rules),
        )

    async def action_admin_audit_events(self, *, next_page: bool = False) -> None:
        if not self._has_audit_role():
            await self._note(
                "Audit access requires the tenant_admin or auditor role.",
                style="yellow",
            )
            return
        cursor = self._audit_cursor if next_page else None
        try:
            result = await self.session.client.list_audit_events(
                limit=50, cursor=cursor
            )
            next_cursor = result.get("next_cursor")
            if next_cursor is not None and not isinstance(next_cursor, str):
                raise ValueError("Audit response contains an invalid cursor.")
            if not isinstance(result.get("items"), list):
                raise ValueError("Audit response contains an invalid items field.")
            self._audit_cursor = next_cursor
            self._main("#admin-audit-next", Button).disabled = next_cursor is None
            self._main("#admin-result", Static).update(
                Text(
                    sanitize_terminal_text(
                        json.dumps(result, indent=2, default=str),
                        limit=MAX_RENDERED_OUTPUT,
                    )
                )
            )
        except HaasAPIError as exc:
            self._sync_auth_state()
            self._main("#admin-result", Static).update(
                Text(
                    f"Audit query failed: {sanitize_terminal_text(exc, limit=500)}",
                    style="red",
                )
            )
        except (TypeError, ValueError) as exc:
            self._main("#admin-result", Static).update(
                Text(
                    f"Audit query failed: {sanitize_terminal_text(exc, limit=500)}",
                    style="red",
                )
            )

    def _switch_workspace(self, workspace: str) -> None:
        self._main("#workspaces", ContentSwitcher).current = workspace
        self._main("#nav-chat", Button).variant = (
            "primary" if workspace == "chat-workspace" else "default"
        )
        self._main("#nav-runs", Button).variant = (
            "primary" if workspace == "runs-workspace" else "default"
        )
        self._main("#nav-admin", Button).variant = (
            "primary" if workspace == "admin-workspace" else "default"
        )
        self._main("#nav-approvals", Button).variant = (
            "primary" if workspace == "approvals-workspace" else "default"
        )

    async def action_show_admin(self) -> None:
        if not self._has_audit_role():
            await self._note(
                "Tenant administration requires tenant_admin or auditor access.",
                style="yellow",
            )
            return
        self._switch_workspace("admin-workspace")

    async def action_admin_audit_next(self) -> None:
        await self.action_admin_audit_events(next_page=True)

    async def action_review_approval(self, run_id: str | None = None) -> None:
        if self.busy:
            await self._note(
                "Detach from the active run before reviewing another approval.",
                style="yellow",
            )
            return
        if not self.identity or "approver" not in self.identity["roles"]:
            await self._note(
                "Reviewing pending actions requires the approver role.", style="yellow"
            )
            return
        if not run_id:
            await self._note("Usage: /approve <run-id>", style="yellow")
            return
        try:
            run = await self.session.client.get_run(run_id)
        except HaasAPIError as exc:
            self._sync_auth_state()
            await self._note(
                f"Could not inspect run {sanitize_terminal_text(run_id, limit=64)}: "
                f"{sanitize_terminal_text(exc, limit=400)}",
                style="red",
            )
            return
        raw_calls = run.get("pending_tool_calls")
        if run.get("status") != "input_required" or not isinstance(raw_calls, list):
            await self._note(
                f"Run {sanitize_terminal_text(run_id, limit=64)} has no pending approval.",
                style="yellow",
            )
            return
        calls = pending_calls_from(raw_calls)
        if calls is None:
            await self._note(
                "The approval data from the API is malformed.", style="red"
            )
            return
        binding = ApprovalBinding.parse(run.get("approval"))
        if not calls:
            await self._note(
                f"Run {sanitize_terminal_text(run_id, limit=64)} has no pending approval.",
                style="yellow",
            )
            return

        decision = await self.push_screen_wait(
            ApprovalScreen(calls, ascii=self._ascii, binding=binding)
        )
        if decision is None:
            await self._note(
                f"Approval for {sanitize_terminal_text(run_id, limit=64)} remains pending.",
                style="yellow",
            )
            return
        try:
            result = await self.session.client.resume(
                run_id,
                decision.approved,
                rejection_reason=decision.reason,
                batch_id=binding.batch_id if binding else None,
                batch_digest=binding.batch_digest if binding else None,
            )
        except HaasAPIError as exc:
            self._sync_auth_state()
            await self._note(
                f"Approval decision was not accepted: "
                f"{sanitize_terminal_text(exc, limit=400)}",
                style="red",
            )
            return
        verb = "Approved" if decision.approved else "Rejected"
        await self._note(
            f"{verb} pending actions for {sanitize_terminal_text(run_id, limit=64)}; "
            f"server status: {sanitize_terminal_text(result.get('status', 'unknown'), limit=32)}.",
            style="green" if decision.approved else "yellow",
        )

    async def action_show_approvals(self) -> None:
        self._switch_workspace("approvals-workspace")
        await self._load_approvals()
        self._main("#approvals-table", DataTable).focus()

    async def action_refresh_approvals(self) -> None:
        await self._load_approvals()

    async def action_cycle_approval_filter(self) -> None:
        index = APPROVAL_FILTERS.index(self._approval_filter)
        self._approval_filter = APPROVAL_FILTERS[(index + 1) % len(APPROVAL_FILTERS)]
        self._main(
            "#approvals-filter", Button
        ).label = f"Filter: {self._approval_filter}"
        await self._load_approvals()

    async def _load_approvals(self) -> None:
        if self._loading_approvals:
            return
        self._loading_approvals = True
        table = self._main("#approvals-table", DataTable)
        detail = self._main("#approval-detail", Static)
        table.clear(columns=False)
        self._approvals.clear()
        self._selected_batch_id = None
        try:
            response = await self.session.client.list_approvals(
                status=self._approval_filter
            )
            items = response.get("items")
            if not isinstance(items, list) or not all(
                isinstance(item, dict) for item in items
            ):
                raise HaasAPIError(
                    "Approval inbox response has an invalid items field."
                )
            for item in items:
                batch_id = str(item["batch_id"])
                self._approvals[batch_id] = item
                calls = item.get("tool_calls") or []
                tools = ", ".join(
                    str(c.get("name", "?")) for c in calls if isinstance(c, dict)
                )
                status = str(item.get("status", "?"))
                if item.get("expired"):
                    status += " (expired)"
                table.add_row(
                    sanitize_terminal_text(batch_id, limit=12),
                    sanitize_terminal_text(status, limit=24),
                    sanitize_terminal_text(item.get("run_id", ""), limit=40),
                    str(item.get("step_number", "?")),
                    sanitize_terminal_text(tools or "-", limit=60),
                    sanitize_terminal_text(item.get("expires_at", ""), limit=32),
                    key=batch_id,
                )
            if items:
                table.move_cursor(row=0)
                self._select_approval(str(items[0]["batch_id"]))
            else:
                detail.update(f"No {self._approval_filter} approvals.")
        except HaasAPIError as exc:
            self._sync_auth_state()
            detail.update(
                Text(
                    f"Could not load approvals: {sanitize_terminal_text(exc, limit=400)}",
                    style="red",
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            detail.update(
                Text(
                    f"Invalid approval response: {sanitize_terminal_text(exc, limit=300)}",
                    style="red",
                )
            )
        finally:
            self._loading_approvals = False
            self._update_approval_actions()

    def _select_approval(self, batch_id: str) -> None:
        self._selected_batch_id = batch_id if batch_id in self._approvals else None
        item = self._approvals.get(self._selected_batch_id or "")
        detail = self._main("#approval-detail", Static)
        if item is None:
            detail.update("Select an approval batch to review.")
        else:
            decision = item.get("decision") or {}
            decided = (
                f" | {decision.get('decision')} by "
                f"{str(decision.get('decided_by') or '?')[:8]}"
                f"{': ' + str(decision['reason']) if decision.get('reason') else ''}"
                if decision
                else ""
            )
            detail.update(
                Text(
                    sanitize_terminal_text(
                        f"batch {item.get('batch_id')} | run {item.get('run_id')} | "
                        f"thread {item.get('thread_id')}\n"
                        f"digest {item.get('batch_digest')}{decided}",
                        limit=600,
                    )
                )
            )
        self._update_approval_actions()

    def _update_approval_actions(self) -> None:
        try:
            item = self._approvals.get(self._selected_batch_id or "")
            self._main("#approvals-review", Button).disabled = (
                item is None
                or item.get("status") != "pending"
                or bool(item.get("expired"))
            )
        except NoMatches:
            return

    async def action_review_selected_approval(self) -> None:
        batch_id = self._selected_batch_id
        if batch_id is None:
            await self._note("Select an approval batch to review.", style="yellow")
            return
        if not self.identity or "approver" not in self.identity["roles"]:
            self._main("#approval-detail", Static).update(
                Text(
                    "Reviewing pending actions requires the approver role.",
                    style="yellow",
                )
            )
            return
        try:
            batch = await self.session.client.get_approval(batch_id)
        except HaasAPIError as exc:
            self._sync_auth_state()
            self._main("#approval-detail", Static).update(
                Text(
                    f"Could not load batch: {sanitize_terminal_text(exc, limit=400)}",
                    style="red",
                )
            )
            return
        binding = ApprovalBinding.parse(batch)
        calls = pending_calls_from(batch.get("tool_calls"))
        if batch.get("status") != "pending" or batch.get("expired"):
            await self._load_approvals()
            self._main("#approval-detail", Static).update(
                Text("This batch is no longer pending.", style="yellow")
            )
            return
        if binding is None or not calls:
            self._main("#approval-detail", Static).update(
                Text("The approval data from the API is malformed.", style="red")
            )
            return
        decision = await self.push_screen_wait(
            ApprovalScreen(calls, ascii=self._ascii, binding=binding)
        )
        if decision is None:
            return
        try:
            result = await self.session.client.decide_approval(
                binding.batch_id,
                approve=decision.approved,
                batch_digest=binding.batch_digest,
                reason=decision.reason,
            )
        except HaasAPIError as exc:
            self._sync_auth_state()
            self._main("#approval-detail", Static).update(
                Text(
                    "Decision was not accepted: "
                    f"{sanitize_terminal_text(exc, limit=400)}",
                    style="red",
                )
            )
            return
        verb = "Approved" if decision.approved else "Rejected"
        await self._note(
            f"{verb} batch {sanitize_terminal_text(binding.batch_id, limit=40)}; "
            f"run status: {sanitize_terminal_text(result.get('status', 'unknown'), limit=32)}.",
            style="green" if decision.approved else "yellow",
        )
        await self._load_approvals()

    async def action_inspect_selected(self) -> None:
        run_id = self._selected_run_id or self.session.last_run_id
        if run_id is None:
            await self._note("Select a run to inspect.", style="yellow")
            return
        self._switch_workspace("runs-workspace")
        body = self._main("#run-inspector-body", Static)
        result = await handle_command(self.session, f"/inspect {run_id}")
        self._sync_auth_state()
        body.update(
            Text(
                sanitize_terminal_text(
                    "\n".join(result.lines), limit=MAX_RENDERED_OUTPUT
                ),
                style="red" if result.error else "",
            )
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
            "Ctrl+1 Chat | Ctrl+2 Runs | Ctrl+3 Admin/audit (authorized roles) | "
            "Ctrl+4 Approval inbox (digest-bound decisions) | "
            "Ctrl+P command palette | F6 next focus | "
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
            self._sync_auth_state()
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
            self._sync_auth_state()
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
            self._sync_auth_state()
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
            self._main("#runs-inspect", Button).disabled = run is None
            self._main("#runs-cancel", Button).disabled = (
                run is None or run.get("status") not in CANCELLABLE_STATUSES
            )
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

    @on(Button.Pressed, "#nav-admin")
    async def _on_nav_admin(self) -> None:
        await self.action_show_admin()

    @on(Button.Pressed, "#nav-help")
    async def _on_nav_help(self) -> None:
        await self.action_show_help()

    @on(Button.Pressed, "#admin-memberships")
    async def _on_admin_memberships(self) -> None:
        await self.action_admin_list_memberships()

    @on(Button.Pressed, "#admin-member-grant")
    async def _on_admin_member_grant(self) -> None:
        await self.action_admin_grant_membership()

    @on(Button.Pressed, "#admin-member-revoke")
    async def _on_admin_member_revoke(self) -> None:
        await self.action_admin_revoke_membership()

    @on(Button.Pressed, "#admin-api-keys")
    async def _on_admin_api_keys(self) -> None:
        await self.action_admin_list_api_keys()

    @on(Button.Pressed, "#admin-key-create")
    async def _on_admin_key_create(self) -> None:
        await self.action_admin_create_api_key()

    @on(Button.Pressed, "#admin-key-revoke")
    async def _on_admin_key_revoke(self) -> None:
        await self.action_admin_revoke_api_key()

    @on(Button.Pressed, "#admin-policy-view")
    async def _on_admin_policy_view(self) -> None:
        await self.action_admin_view_policy()

    @on(Button.Pressed, "#admin-policy-activate")
    async def _on_admin_policy_activate(self) -> None:
        await self.action_admin_activate_policy()

    @on(Button.Pressed, "#admin-plugin-inventory")
    async def _on_admin_plugin_inventory(self) -> None:
        await self.action_admin_plugin_inventory()

    @on(Button.Pressed, "#admin-audit")
    async def _on_admin_audit(self) -> None:
        await self.action_admin_audit_events()

    @on(Button.Pressed, "#admin-audit-next")
    async def _on_admin_audit_next(self) -> None:
        await self.action_admin_audit_next()

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

    @on(Button.Pressed, "#runs-inspect")
    async def _on_runs_inspect(self) -> None:
        await self.action_inspect_selected()

    @on(Button.Pressed, "#nav-approvals")
    async def _on_nav_approvals(self) -> None:
        await self.action_show_approvals()

    @on(Button.Pressed, "#approvals-refresh")
    async def _on_approvals_refresh(self) -> None:
        await self.action_refresh_approvals()

    @on(Button.Pressed, "#approvals-filter")
    async def _on_approvals_filter(self) -> None:
        await self.action_cycle_approval_filter()

    @on(Button.Pressed, "#approvals-review")
    async def _on_approvals_review(self) -> None:
        await self.action_review_selected_approval()

    @on(DataTable.RowHighlighted, "#approvals-table")
    def _on_approval_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._select_approval(str(event.row_key.value))

    def action_detach(self) -> None:
        if self.busy:
            self.workers.cancel_group(self, "turn")

    async def action_clear_screen(self) -> None:
        self._preview, self._preview_key = None, None
        self._preview_text, self._preview_dirty = "", False
        await self._main("#chat", VerticalScroll).remove_children()

    def action_toggle_follow(self) -> None:
        self._follow = not self._follow
        if self._follow:
            self._main("#chat", VerticalScroll).scroll_end(animate=False)
        self._refresh_bars()

    async def action_new_chat(self) -> None:
        if self.busy:
            await self._note("Detach (Esc) before starting a new chat.", style="yellow")
            return
        await self._submit("/new")
