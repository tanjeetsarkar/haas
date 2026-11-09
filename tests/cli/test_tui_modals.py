from __future__ import annotations

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Button, Input, Static, TextArea

from fake_haas import FakeHaas
from haas_cli.profiles import EnvironmentProfile
from haas_cli.session import ApprovalBinding, Decision, PendingCall
from haas_cli.tui.app import (
    AdminFormScreen,
    ApprovalScreen,
    CancelRunScreen,
    LoginScreen,
    MAX_APPROVAL_ARGUMENTS,
    ProfileSwitchScreen,
    TenantSwitchScreen,
)
from test_tui import make_app, wait_for


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
@pytest.mark.parametrize("modal", ["login", "tenant", "profile", "cancel", "approval"])
async def test_modal_buttons_are_usable(size, modal):
    profiles = [
        EnvironmentProfile(f"profile-{i}", "https://example.test", "tenant")
        for i in range(15)
    ]
    screens = {
        "login": (LoginScreen(), "#identity-submit", "credential"),
        "tenant": (TenantSwitchScreen("tenant"), "#tenant-submit", "tenant"),
        "profile": (
            ProfileSwitchScreen(profiles, "profile-14"),
            "#profile-submit",
            "profile-14",
        ),
        "cancel": (CancelRunScreen("run-id"), "#confirm-cancel", True),
        "approval": (
            ApprovalScreen(
                [PendingCall("1", "tool", {"data": "x" * 1000})],
                binding=ApprovalBinding("batch", "d" * 64, "tomorrow"),
            ),
            "#approve",
            Decision(True),
        ),
    }
    screen, button, expected = screens[modal]
    app = make_app(FakeHaas())
    results = []
    async with app.run_test(size=size) as pilot:
        app.push_screen(screen, results.append)
        await pilot.pause()
        if modal == "login":
            screen.query_one("#credential", Input).value = "credential"
        assert await pilot.click(button)
        await pilot.pause()
        assert results == [expected]


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
@pytest.mark.parametrize("with_json", [False, True])
async def test_admin_form_fields_and_buttons_are_accessible(size, with_json):
    app = make_app(FakeHaas())
    screen = AdminFormScreen(
        "Test form",
        [("one", "First", ""), ("two", "Second", ""), ("three", "Third", "")],
        json_value='{"enabled": true}' if with_json else None,
    )
    results = []
    async with app.run_test(size=size) as pilot:
        app.push_screen(screen, results.append)
        await pilot.pause()
        for field in ("one", "two", "three"):
            screen.query_one(f"#admin-field-{field}", Input).value = field
        if with_json:
            assert (
                screen.query_one("#admin-form-json", TextArea).text
                == '{"enabled": true}'
            )
        screen.query_one("#admin-form-box", VerticalScroll).scroll_end(animate=False)
        await pilot.pause()
        assert await pilot.click("#admin-form-submit")
        await pilot.pause()
        expected = {"one": "one", "two": "two", "three": "three"}
        if with_json:
            expected["json"] = '{"enabled": true}'
        assert results == [expected]


async def test_approval_reason_accepts_shortcut_letters():
    app = make_app(FakeHaas())
    screen = ApprovalScreen([PendingCall("1", "tool", {})])
    results = []
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(screen, results.append)
        await pilot.pause()
        screen.query_one("#reason", Input).focus()
        await pilot.press("n", "y")
        assert screen.query_one("#reason", Input).value == "ny"
        assert results == []
        await pilot.click("#reject")
        await pilot.pause()
        assert results == [Decision(False, "ny")]


@pytest.mark.parametrize(
    "screen",
    [
        LoginScreen(),
        TenantSwitchScreen("tenant"),
        ProfileSwitchScreen([], ""),
        CancelRunScreen("run"),
        ApprovalScreen([PendingCall("1", "tool", {})]),
        AdminFormScreen("Form", [("name", "Name", "")]),
    ],
)
async def test_modal_escape_cancels_without_submitting(screen):
    app = make_app(FakeHaas())
    results = []
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(screen, results.append)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert results == [False if isinstance(screen, CancelRunScreen) else None]


async def test_profile_list_scrolls_and_invalid_entry_preserves_profiles():
    profiles = [
        EnvironmentProfile(f"profile-{i}", "https://example.test", "tenant")
        for i in range(20)
    ]
    app = make_app(FakeHaas())
    screen = ProfileSwitchScreen(profiles, "profile-0")
    results = []
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(screen, results.append)
        await pilot.pause()
        profile_list = screen.query_one("#profile-list-scroll", VerticalScroll)
        assert profile_list.max_scroll_y > 0
        profile_list.scroll_end(animate=False)
        await pilot.pause()
        assert profile_list.scroll_y == profile_list.max_scroll_y
        screen.query_one("#profile", Input).value = "missing"
        await pilot.press("enter")
        await pilot.pause()
        assert "Unknown profile" in str(
            screen.query_one("#identity-error", Static).render()
        )
        assert "profile-19" in str(screen.query_one("#profile-list", Static).render())
        assert results == []
        screen.query_one("#profile", Input).value = "profile-19"
        await pilot.press("enter")
        await pilot.pause()
        assert results == ["profile-19"]


async def test_empty_tenant_displays_validation_error():
    app = make_app(FakeHaas())
    screen = TenantSwitchScreen("tenant")
    results = []
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(screen, results.append)
        await pilot.pause()
        screen.query_one("#tenant", Input).value = " "
        await pilot.press("enter")
        await pilot.pause()
        assert "Enter a tenant ID" in str(
            screen.query_one("#identity-error", Static).render()
        )
        assert results == []


@pytest.mark.parametrize("disabled", [True, False])
async def test_approval_safety_controls_remain_accessible(disabled):
    app = make_app(FakeHaas())
    screen = ApprovalScreen(
        [PendingCall("1", "tool", {"data": "x" * (MAX_APPROVAL_ARGUMENTS + 1)})],
        decisions_enabled=not disabled,
        decision_disabled_reason="This identity cannot decide." if disabled else None,
        binding=ApprovalBinding("batch", "d" * 64, "tomorrow"),
    )
    results = []
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(screen, results.append)
        await pilot.pause()
        assert screen.query_one("#approve", Button).disabled
        assert screen.query_one("#reject", Button).disabled is disabled
        await pilot.press("y")
        assert results == []
        assert await pilot.click("#leave-pending")
        await pilot.pause()
        assert results == [None]


async def test_admin_form_validation_then_keyboard_navigation():
    app = make_app(FakeHaas())
    screen = AdminFormScreen(
        "Form", [("one", "First", ""), ("two", "Second", ""), ("three", "Third", "")]
    )
    results = []
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(screen, results.append)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert "Complete every required field" in str(
            screen.query_one("#identity-error", Static).render()
        )
        assert results == []
        await pilot.press(
            "o", "n", "e", "tab", "t", "w", "o", "tab", "t", "h", "r", "e", "e"
        )
        await pilot.press("tab", "enter")
        await pilot.pause()
        assert results == [{"one": "one", "two": "two", "three": "three"}]


async def test_long_approval_calls_can_be_reviewed_before_deciding():
    app = make_app(FakeHaas())
    screen = ApprovalScreen(
        [
            PendingCall("1", "first-tool", {"data": "x" * 1000}),
            PendingCall("2", "last-tool", {"data": "last argument"}),
        ]
    )
    results = []
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(screen, results.append)
        await pilot.pause()
        calls = screen.query_one("#approval-calls", VerticalScroll)
        assert calls.max_scroll_y > 0
        calls.scroll_end(animate=False)
        await pilot.pause()
        assert calls.scroll_y == calls.max_scroll_y
        assert await pilot.click("#approve")
        await pilot.pause()
        assert results == [Decision(True)]


async def test_admin_api_key_form_submits_through_real_action():
    fake = FakeHaas()
    fake.roles = ["tenant_admin"]
    app = make_app(fake, credential="hk_test_admin")
    async with app.run_test(size=(80, 24)) as pilot:
        app.run_worker(app.action_admin_create_api_key())
        await wait_for(pilot, lambda: isinstance(app.screen, AdminFormScreen))
        screen = app.screen
        screen.query_one("#admin-field-principal", Input).value = "principal-1"
        screen.query_one("#admin-field-name", Input).value = "key-name"
        screen.query_one("#admin-field-expires", Input).value = "30"
        screen.query_one("#admin-form-box", VerticalScroll).scroll_end(animate=False)
        await pilot.pause()
        assert await pilot.click("#admin-form-submit")
        await wait_for(pilot, lambda: bool(fake.admin_requests))
        assert fake.admin_requests[-1] == (
            "POST",
            "/v1/admin/api-keys",
            {"principal_id": "principal-1", "name": "key-name", "expires_in_days": 30},
        )
