from __future__ import annotations

import asyncio

from textual.widgets import Button, DataTable, Input, Markdown, Static

from haas_cli.client import HaasClient
from haas_cli.config import ChatSettings
from haas_cli.session import ChatSession, Decision
from haas_cli.profiles import EnvironmentProfile, ProfileStore
from haas_cli.tui.app import (
    ApprovalScreen,
    AdminFormScreen,
    CancelRunScreen,
    ChatApp,
    LoginScreen,
    PromptInput,
)

from fake_haas import FakeHaas


def make_app(
    fake: FakeHaas,
    *,
    ascii: bool = False,
    no_color: bool = False,
    credential: str | None = None,
    profile_store: ProfileStore | None = None,
    environment: str = "unknown",
) -> ChatApp:
    session = ChatSession(
        HaasClient("http://api", "t1", transport=fake.transport(), reconnect_delay=0),
        ChatSettings(
            base_url="http://api",
            tenant="t1",
            credential=credential,
            environment=environment,
            secrets={"TOK": "supersecretvalue"},
            ascii=ascii,
            no_color=no_color,
        ),
    )
    return ChatApp(session, profile_store=profile_store)


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
    await pilot.pause()
    await pilot.press("enter")


async def test_connects_and_shows_banner():
    app = make_app(FakeHaas())
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: "2 skills" in chat_text(app))
        assert "api" in str(app.query_one("#topbar", Static).render())


async def test_login_logout_and_production_identity_banner():
    fake = FakeHaas()
    app = make_app(fake, environment="prod")
    async with app.run_test(size=(120, 40)) as pilot:
        assert "PRODUCTION" in str(app.query_one("#topbar", Static).render())
        app.run_worker(app.action_login(), group="login")
        await wait_for(pilot, lambda: isinstance(app.screen, LoginScreen))
        app.screen.query_one("#credential", Input).value = "hk_test_memory_only"
        await pilot.press("enter")
        await wait_for(pilot, lambda: app.identity is not None)
        assert app.session.client.credential == "hk_test_memory_only"
        assert (
            fake.identity_headers[-1]["authorization"] == "Bearer hk_test_memory_only"
        )
        assert "as principa" in str(app.query_one("#topbar", Static).render())

        await app.action_logout()
        assert app.session.client.credential is None
        assert app.session.settings.credential is None
        assert app.identity is None


async def test_tenant_switch_is_server_verified_and_clears_thread():
    fake = FakeHaas()
    app = make_app(fake, credential="hk_test_session")
    async with app.run_test(size=(120, 40)):
        assert app.identity is not None
        app.session.settings.thread_id = "old-thread"
        await app.action_switch_tenant("t2")
        assert app.session.settings.tenant == "t2"
        assert app.session.settings.thread_id is None

        await app.action_switch_tenant("not-authorized")
        assert app.session.settings.tenant == "t2"
        assert app.session.client.tenant == "t2"


async def test_expired_session_clears_credential_and_updates_banner():
    fake = FakeHaas()
    app = make_app(fake, credential="hk_test_expiring")
    async with app.run_test(size=(120, 40)):
        assert app.identity is not None
        fake.auth_rejected = True
        await app.action_show_runs()

        assert app.session.client.credential is None
        assert app.session.settings.credential is None
        assert app.identity is None
        assert "auth required" in str(app.query_one("#topbar", Static).render())


async def test_profile_switch_updates_context_without_persisting_credentials(tmp_path):
    fake = FakeHaas()
    fake.environment = "staging"
    profiles = ProfileStore(tmp_path / "profiles.json")
    profiles.save(
        EnvironmentProfile("staging", "https://staging.example.test", "t2", "staging")
    )
    app = make_app(fake, credential="hk_test_memory_only", profile_store=profiles)
    async with app.run_test(size=(120, 40)):
        await app.action_switch_profile("staging")
        assert app.session.settings.profile_name == "staging"
        assert app.session.settings.environment == "staging"
        assert app.session.settings.tenant == "t2"
        assert app.session.client.credential == "hk_test_memory_only"
        assert profiles.active() is not None
        assert "hk_test_memory_only" not in profiles.path.read_text()


async def test_admin_workspace_is_role_gated_and_shows_one_time_api_key(monkeypatch):
    fake = FakeHaas()
    fake.roles = ["tenant_admin"]
    app = make_app(fake, credential="hk_test_admin")
    async with app.run_test(size=(120, 40)):
        assert app.query_one("#nav-admin", Button).display
        await app.action_show_admin()
        assert app.query_one("#workspaces").current == "admin-workspace"

        await app.action_admin_list_memberships()
        assert "principal-1" in str(app.query_one("#admin-result", Static).render())
        await app.action_admin_plugin_inventory()
        assert "revenue-ops" in str(app.query_one("#admin-result", Static).render())

        async def submit_form(_screen):
            assert isinstance(_screen, AdminFormScreen)
            return {
                "principal": "principal-1",
                "name": "tui-test",
                "expires": "30",
            }

        monkeypatch.setattr(app, "push_screen_wait", submit_form)
        await app.action_admin_create_api_key()
        output = str(app.query_one("#admin-result", Static).render())
        assert "hk_test_one_time" in output
        assert "shown once" in output
        assert fake.admin_requests[-1] == (
            "POST",
            "/v1/admin/api-keys",
            {
                "principal_id": "principal-1",
                "name": "tui-test",
                "expires_in_days": 30,
            },
        )


async def test_auditor_can_open_audit_but_not_tenant_admin_controls():
    fake = FakeHaas()
    fake.roles = ["auditor"]
    app = make_app(fake, credential="hk_test_auditor")
    async with app.run_test(size=(120, 40)):
        assert app.query_one("#nav-admin", Button).display
        assert not app.query_one("#admin-member-grant", Button).display
        await app.action_show_admin()
        await app.action_admin_audit_events()
        assert fake.admin_requests[-1][:2] == (
            "GET",
            "/v1/audit-events",
        )


async def test_non_admin_cannot_call_tenant_admin_endpoints_from_tui():
    fake = FakeHaas()
    app = make_app(fake, credential="hk_test_submitter")
    async with app.run_test(size=(120, 40)):
        assert not app.query_one("#nav-admin", Button).display
        await app.action_admin_list_memberships()
        assert fake.admin_requests == []


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
        assert "Run ID: run_1" in chat_text(app)
        assert "$0.0010" in str(app.query_one("#status", Static).render())
        assert app.query_one("#prompt", PromptInput).text == ""


async def test_authenticated_submitter_cannot_approve_own_run():
    fake = FakeHaas()
    app = make_app(fake, credential="hk_test_submitter")
    async with app.run_test(size=(120, 40)) as pilot:
        await send(pilot, app, "refund please")
        await wait_for(pilot, lambda: isinstance(app.screen, ApprovalScreen))
        assert app.screen.query_one("#approve", Button).disabled
        assert app.screen.query_one("#reject", Button).disabled
        assert "separate approver credential" in str(app.screen.query(".error-box")[0].render())
        await pilot.press("y")
        assert isinstance(app.screen, ApprovalScreen)
        await pilot.press("n")
        assert isinstance(app.screen, ApprovalScreen)
        await pilot.press("escape")
        await wait_for(pilot, lambda: not app.busy)
        assert fake.resumes == []


async def test_approver_can_review_and_approve_known_pending_run(monkeypatch):
    fake = FakeHaas()
    fake.roles = ["approver"]
    app = make_app(fake, credential="hk_test_approver")
    async with app.run_test(size=(120, 40)):
        screens: list[ApprovalScreen] = []

        async def approve(screen):
            assert isinstance(screen, ApprovalScreen)
            screens.append(screen)
            return Decision(True)

        monkeypatch.setattr(app, "push_screen_wait", approve)
        await app._run_command("/approve run_1")
        assert len(screens) == 1
        assert [call.name for call in screens[0].calls] == ["prepare_refund_batch"]
        assert fake.resumes == [{"approved": True, "secrets": {}}]


async def test_approve_command_rejects_unauthorized_identity():
    fake = FakeHaas()
    app = make_app(fake, credential="hk_test_submitter")
    async with app.run_test(size=(120, 40)):
        await app._run_command("/approve run_1")
        assert "requires the approver role" in chat_text(app)
        assert fake.resumes == []


async def test_approve_command_rejects_nonpending_and_malformed_data():
    fake = FakeHaas()
    fake.roles = ["approver"]
    fake.run_status = "completed"
    app = make_app(fake, credential="hk_test_approver")
    async with app.run_test(size=(120, 40)):
        await app._run_command("/approve run_1")
        assert "has no pending approval" in chat_text(app)

        fake.run_status = "input_required"
        fake.pending_tool_calls = [{"name": "missing-call-id"}]
        await app._run_command("/approve run_1")
        assert "approval data from the API is malformed" in chat_text(app)
        assert fake.resumes == []


async def test_approver_can_reject_or_leave_pending(monkeypatch):
    fake = FakeHaas()
    fake.roles = ["approver"]
    app = make_app(fake, credential="hk_test_approver")
    async with app.run_test(size=(120, 40)):
        async def reject(screen):
            assert isinstance(screen, ApprovalScreen)
            return Decision(False, "not today")

        monkeypatch.setattr(app, "push_screen_wait", reject)
        await app._run_command("/approve run_1")
        assert fake.resumes == [
            {"approved": False, "secrets": {}, "rejection_reason": "not today"}
        ]

        fake.resumes.clear()

        async def leave_pending(screen):
            assert isinstance(screen, ApprovalScreen)
            return None

        monkeypatch.setattr(app, "push_screen_wait", leave_pending)
        await app._run_command("/approve run_1")
        assert fake.resumes == []


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
        await pilot.click("#reject")
        await wait_for(
            pilot, lambda: "Refund was rejected" in chat_text(app) and not app.busy
        )
        assert fake.resumes[0] == {
            "approved": False,
            "secrets": {"TOK": "supersecretvalue"},
            "rejection_reason": "not today",
        }


async def test_approval_escape_leaves_run_pending():
    fake = FakeHaas()
    app = make_app(fake)
    async with app.run_test(size=(120, 40)) as pilot:
        await send(pilot, app, "refund please")
        await wait_for(pilot, lambda: isinstance(app.screen, ApprovalScreen))
        await pilot.press("escape")
        await wait_for(
            pilot,
            lambda: "left pending on the server" in chat_text(app) and not app.busy,
        )
        assert fake.resumes == []


async def test_runs_pagination_and_attach_pending_approval():
    fake = FakeHaas()
    app = make_app(fake)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.action_show_runs()
        table = app.query_one("#runs-table", DataTable)
        assert table.row_count == 1
        assert not app.query_one("#runs-more", Button).disabled
        await pilot.pause()
        await pilot.click("#runs-more")
        await wait_for(pilot, lambda: table.row_count == 2)
        assert table.row_count == 2
        await app.action_attach_selected()
        await wait_for(pilot, lambda: isinstance(app.screen, ApprovalScreen))
        await pilot.press("escape")
        await wait_for(pilot, lambda: not app.busy and "left pending" in chat_text(app))
        assert fake.resumes == []


async def test_run_cancellation_requires_confirmation():
    fake = FakeHaas()
    app = make_app(fake)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.action_show_runs()
        app.run_worker(app.action_cancel_selected(), group="cancel")
        await wait_for(pilot, lambda: isinstance(app.screen, CancelRunScreen))
        await pilot.press("escape")
        assert fake.cancelled == []
        app.run_worker(app.action_cancel_selected(), group="cancel")
        await wait_for(pilot, lambda: isinstance(app.screen, CancelRunScreen))
        await pilot.click("#confirm-cancel")
        await wait_for(pilot, lambda: fake.cancelled == ["run_1"])
        assert fake.run_status == "cancelled"


async def test_run_browser_surfaces_api_failures():
    fake = FakeHaas()
    fake.list_error = "run listing unavailable"
    app = make_app(fake)
    async with app.run_test(size=(120, 40)):
        await app.action_show_runs()
        assert "run listing unavailable" in str(
            app.query_one("#run-detail", Static).render()
        )


async def test_cancel_failure_is_visible_and_does_not_claim_success():
    fake = FakeHaas()
    fake.cancel_error = "Run is already terminal."
    app = make_app(fake)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.action_show_runs()
        app.run_worker(app.action_cancel_selected(), group="cancel")
        await wait_for(pilot, lambda: isinstance(app.screen, CancelRunScreen))
        await pilot.press("y")
        await wait_for(
            pilot,
            lambda: (
                "Run is already terminal"
                in str(app.query_one("#run-detail", Static).render())
            ),
        )
        assert fake.cancelled == []


async def test_tui_renders_ascii_mode_at_supported_sizes():
    for size in ((80, 24), (120, 40), (160, 50)):
        app = make_app(FakeHaas("simple"), ascii=True, no_color=True)
        async with app.run_test(size=size) as pilot:
            assert app.query_one("#prompt", PromptInput).region.width > 0
            await app.action_show_runs()
            await pilot.pause()
            assert app.query_one("#runs-table", DataTable).region.width > 0
            assert "UP" in str(app.query_one("#topbar", Static).render())
            assert app.console.no_color


async def test_theme_cycle_and_navigation():
    app = make_app(FakeHaas("simple"))
    async with app.run_test(size=(120, 40)) as pilot:
        assert app.session.settings.theme == "auto"
        await app.action_cycle_theme()
        assert app.session.settings.theme == "dark"
        await pilot.press("ctrl+2")
        await pilot.pause()
        assert app.query_one("#workspaces").current == "runs-workspace"
        await pilot.press("ctrl+1")
        await pilot.pause()
        assert app.query_one("#workspaces").current == "chat-workspace"


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
