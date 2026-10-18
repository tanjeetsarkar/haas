import json

import httpx
from textual.widgets import Button, Input, Static, TextArea

from fake_haas import FakeHaas
from haas_cli.client import HaasClient
from haas_cli.config import ChatSettings
from haas_cli.session import ChatSession, handle_command
from haas_cli.tui.app import AdminFormScreen, ChatApp


class Phase2Fake(FakeHaas):
    def __init__(self):
        super().__init__("simple")
        self.roles = ["tenant_admin"]
        self.revision = {
            "revision_id": "rev-1",
            "state": "draft",
            "configuration": {"skills": [], "max_steps": 3},
            "policy_revision_id": "policy-1",
        }
        self.requests = []

    def handle(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.requests.append((path, body, dict(request.url.params)))
        if path == "/v1/agents/revisions":
            if request.method == "POST":
                self.revision["configuration"] = body["configuration"]
                return httpx.Response(201, json=self.revision)
            return httpx.Response(
                200, json={"items": [self.revision], "next_cursor": None}
            )
        if path.startswith("/v1/agents/revisions/"):
            if path.endswith("/validate"):
                self.revision["state"] = "validated"
            elif path.endswith("/publish"):
                self.revision["state"] = "published"
            return httpx.Response(200, json=self.revision)
        if path == "/v1/threads":
            return httpx.Response(
                200, json={"items": [{"thread_id": "thd-1"}], "next_cursor": "cursor-2"}
            )
        if path.endswith("/branches"):
            if body["expected_history_version"] != 2:
                return httpx.Response(409, json={"detail": "Thread history changed."})
            return httpx.Response(
                201, json={"thread_id": "branch-1", "history_version": 0}
            )
        if path.startswith("/v1/threads/"):
            return httpx.Response(
                200,
                json={
                    "items": [{"role": "assistant", "content": "history"}],
                    "history_version": 2,
                    "active_run_id": None,
                    "next_offset": 50,
                },
            )
        return super().handle(request)


async def test_terminal_revision_thread_pages_diff_and_conflict_recovery():
    fake = Phase2Fake()
    client = HaasClient("http://api", transport=fake.transport())
    session = ChatSession(client, ChatSettings())
    try:
        assert not (await handle_command(session, "/agents")).error
        assert (await handle_command(session, "/agent use rev-1")).error
        for command in (
            "/agent validate rev-1",
            "/agent publish rev-1",
            "/agent diff rev-1 rev-1",
            "/agent use rev-1",
        ):
            assert not (await handle_command(session, command)).error
        assert session.settings.agent_revision_id == "rev-1"
        await session.client.create_run("pinned", agent_revision_id="rev-1")
        payload = fake.created[-1]
        assert payload["agent_revision_id"] == "rev-1"
        assert not {"primary_model", "max_steps", "preload_skills"} & payload.keys()
        await handle_command(session, "/threads find-me")
        await handle_command(session, "/threads next")
        assert fake.requests[-1][2] == {"search": "find-me", "cursor": "cursor-2"}
        await handle_command(session, "/thread thd-1")
        await handle_command(session, "/history")
        assert session.settings.history_version == 2
        await handle_command(session, "/history 50")
        assert fake.requests[-1][2]["history_version"] == "2"
        conflict = await handle_command(session, "/branch 1")
        assert conflict.error and "Refresh /history" in conflict.lines[0]
        assert not (await handle_command(session, "/branch 2")).error
        assert session.settings.thread_id == "branch-1"
        session.new_conversation()
        assert session.settings.agent_revision_id is None
        assert session.settings.history_version is None
    finally:
        await client.aclose()


async def test_agent_draft_keyboard_form_and_role_gate():
    fake = Phase2Fake()
    client = HaasClient(
        "http://api", "t1", credential="hk_test", transport=fake.transport()
    )
    session = ChatSession(client, ChatSettings(tenant="t1", credential="hk_test"))
    app = ChatApp(session)
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause(0.2)
        app.run_worker(app._run_command("/agent draft"), group="agent-draft")
        await pilot.pause(0.2)
        assert isinstance(app.screen, AdminFormScreen)
        app.screen.query_one("#admin-field-name", Input).value = "research"
        app.screen.query_one("#admin-form-json", TextArea).load_text(
            '{"primary_model":"model-a","planner_model":"model-b"}'
        )
        app.screen.query_one("#admin-form-submit", Button).focus()
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert any(
            path == "/v1/agents/revisions" and body["agent_name"] == "research"
            for path, body, _ in fake.requests
            if body
        )
        app.identity["roles"] = ["submitter"]
        await app._run_command("/agent draft")
        assert not isinstance(app.screen, AdminFormScreen)
        assert any(
            "requires a signed-in tenant administrator" in str(widget.render())
            for widget in app.query(Static)
        )
        app.identity["roles"] = ["tenant_admin"]
        app.identity["capabilities"] = {
            "version": "1",
            "actions": [],
            "event_schemas": ["run-events.v1"],
        }
        assert not app._has_admin_role()


def test_incompatible_capability_metadata_is_not_accepted():
    import pytest
    from haas_cli.client import HaasAPIError

    with pytest.raises(HaasAPIError, match="incompatible"):
        ChatApp._validate_identity(
            {
                "tenant_id": "t1",
                "principal_id": "p1",
                "roles": [],
                "credential_type": "oidc",
                "environment": "prod",
                "capabilities": {"version": "99", "actions": [], "event_schemas": []},
            }
        )
