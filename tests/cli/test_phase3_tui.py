"""T3: approvals inbox, run inspector, budgets and durable-replay client behaviour."""

from __future__ import annotations

import httpx
import pytest
from textual.widgets import Button, DataTable, Static

from haas_cli.client import HaasAPIError, HaasClient, SSEEvent
from haas_cli.config import ChatSettings
from haas_cli.session import (
    ApprovalBinding,
    ChatSession,
    Decision,
    format_budget,
    handle_command,
)
from haas_cli.tui.app import ApprovalScreen, pending_calls_from

from fake_haas import BATCH_DIGEST, BATCH_ID, BUDGET, TRACEPARENT, FakeHaas
from test_tui import chat_text, make_app, wait_for


def session_for(fake: FakeHaas) -> ChatSession:
    return ChatSession(
        HaasClient("http://api", "t1", transport=fake.transport()), ChatSettings()
    )


async def test_stream_ignores_legacy_cursor_and_stops_on_reauth_failure():
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers["last-event-id"])
        return httpx.Response(
            200,
            content=(
                'id: 4\nevent: step_started\ndata: {"step": 1}\n\n'
                'event: stream_closed\ndata: {"reason": "Credential revoked."}\n\n'
            ),
        )

    client = HaasClient("http://api", transport=httpx.MockTransport(handler))
    got: list[str] = []
    with pytest.raises(HaasAPIError, match="Credential revoked") as excinfo:
        async for ev in client.stream_events("run_1", "7-0"):
            got.append(ev.event)
    assert seen == ["0"]
    assert got == ["step_started"]
    assert excinfo.value.status == 401 and client.auth_expired


async def test_stream_keeps_following_queued_and_cancelling_runs():
    statuses = iter(["queued", "cancelling", "cancelled"])
    streams = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal streams
        if req.url.path.endswith("/stream"):
            streams += 1
            return httpx.Response(200, content=": keep-alive\n\n")
        return httpx.Response(200, json={"status": next(statuses)})

    client = HaasClient(
        "http://api", transport=httpx.MockTransport(handler), reconnect_delay=0
    )
    assert [e async for e in client.stream_events("run_1")] == []
    assert streams == 3


async def test_client_approval_and_inspector_requests():
    fake = FakeHaas()
    client = HaasClient("http://api", "t1", transport=fake.transport())
    pending = await client.list_approvals()
    assert [b["batch_id"] for b in pending["items"]] == [BATCH_ID]
    assert (await client.get_approval(BATCH_ID))["batch_digest"] == BATCH_DIGEST
    with pytest.raises(HaasAPIError) as conflict:
        await client.decide_approval(BATCH_ID, approve=True, batch_digest="0" * 64)
    assert conflict.value.status == 409
    result = await client.decide_approval(
        BATCH_ID, approve=False, batch_digest=BATCH_DIGEST, reason="no", secrets={}
    )
    assert result["status"] == "queued"
    assert fake.decisions[-1] == (
        BATCH_ID,
        {
            "decision": "reject",
            "batch_digest": BATCH_DIGEST,
            "secrets": {},
            "reason": "no",
        },
    )
    assert (await client.list_approvals())["items"] == []
    assert len((await client.list_approvals(status="all"))["items"]) == 1
    assert fake.approval_queries[-1] == {"status": "all", "limit": "50"}
    events = await client.run_events("run_1", after=2)
    assert [e["sequence"] for e in events["items"]] == [3, 4]
    assert (await client.run_steps("run_1"))["steps"][0]["step_number"] == 1
    assert (await client.run_usage("run_1"))["budget"] == BUDGET


def test_approval_binding_parse_and_budget_format():
    assert ApprovalBinding.parse(None) is None
    assert ApprovalBinding.parse({"batch_id": 1, "batch_digest": "x"}) is None
    binding = ApprovalBinding.parse(
        {"batch_id": "b", "batch_digest": "d", "expires_at": 5}
    )
    assert binding == ApprovalBinding("b", "d", None)
    lines = format_budget(BUDGET)
    assert "settled $0.0010" in lines[0] and "remaining $0.2980" in lines[0]
    assert lines[1] == "steps       2 / 15"
    assert lines[2] == "tools       1 / 20"
    assert lines[3].startswith("deadline")
    assert format_budget({"max_cost_usd": "bad"})[0].count("$?") == 1
    assert pending_calls_from("nope") is None
    assert pending_calls_from([{"id": 1, "name": "x"}]) is None
    assert pending_calls_from([1]) is None
    calls = pending_calls_from([{"id": "c", "function": {"name": "t"}}])
    assert calls is not None and calls[0].name == "t"


async def test_session_inspection_commands():
    fake = FakeHaas()
    session = session_for(fake)

    no_run = await handle_command(session, "/inspect")
    assert no_run.error and "RUN_ID" in no_run.lines[0]
    bad = await handle_command(session, "/approvals nonsense")
    assert bad.error and "Usage: /approvals" in bad.lines[0]

    inbox = await handle_command(session, "/approvals")
    assert BATCH_ID in inbox.lines[0] and "pending" in inbox.lines[0]
    assert "prepare_refund_batch" in inbox.lines[1]

    session.last_run_id = "run_1"
    inspected = await handle_command(session, "/inspect")
    text = "\n".join(inspected.lines)
    assert not inspected.error
    assert "run_1  [input_required]" in text
    assert "version 7 · events 5" in text
    assert f"approval    batch {BATCH_ID}" in text
    assert "#2.1  failed" in text and "(unreported)" in text and "error: boom" in text
    assert "query_revenue_metrics  succeeded" in text
    assert f"trace       {TRACEPARENT}" in text
    assert "send_email  unknown" in text
    assert "never re-executes" in text and "e" * 64 in text
    assert "usage ledger: 1 entry" in text and "model_tokens" in text

    page = await handle_command(session, "/events")
    assert [line.split()[0] for line in page.lines[:2]] == ["1", "2"]
    assert page.lines[-1] == "More: /events run_1 2"
    tail = await handle_command(session, "/events run_1 4")
    assert tail.lines[0].split()[0] == "5" and not tail.lines[-1].startswith("More")
    assert (await handle_command(session, "/events run_1 x")).error
    searched = await handle_command(session, "/events run_1 type=STEP")
    assert searched.lines and all(
        "step" in line.split()[2] for line in searched.lines[:-1]
    )
    assert searched.lines[-1].endswith(" type=step")
    missing = await handle_command(session, "/events run_1 4 type=nomatch")
    assert missing.lines == ["No 'nomatch' events in this page."]
    assert (await handle_command(session, "/events type=")).error
    assert (await handle_command(session, "/events type=a type=b")).error
    assert (await handle_command(session, "/inspect run_1 extra")).error

    fake.approval_batches[0]["status"] = "approved"
    fake.approval_batches[0]["decision"] = {
        "decision": "approved",
        "decided_by": "p-2",
        "reason": "ok",
        "decided_at": "2030-01-01T00:00:00+00:00",
    }
    assert (await handle_command(session, "/approvals")).lines == [
        "No pending approvals."
    ]
    history = await handle_command(session, "/approvals all")
    assert (
        "decided approved by p-2" in history.lines[-1] and ": ok" in history.lines[-1]
    )


async def test_resume_conflict_keeps_following_durable_log():
    fake = FakeHaas()
    session = session_for(fake)
    original = session.client.resume

    async def conflicting(*args, **kwargs):
        await original(*args, **kwargs)
        raise HaasAPIError("Batch digest mismatch.", 409)

    session.client.resume = conflicting  # type: ignore[method-assign]

    async def approve(calls):
        return Decision(True)

    result = await session.run_turn("go", lambda e: None, approve)
    assert result.status == "completed"
    assert fake.resumes[0]["batch_digest"] == BATCH_DIGEST
    assert session.pending_approval is None


async def test_tui_approval_inbox_review_and_filter(monkeypatch):
    fake = FakeHaas()
    fake.roles = ["approver"]
    app = make_app(fake, credential="hk_test_approver")
    async with app.run_test(size=(140, 44)) as pilot:
        await app.action_show_approvals()
        table = app.query_one("#approvals-table", DataTable)
        assert table.row_count == 1
        assert not app.query_one("#approvals-review", Button).disabled
        assert BATCH_DIGEST in str(app.query_one("#approval-detail", Static).render())

        screens: list[ApprovalScreen] = []

        async def approve(screen):
            screens.append(screen)
            return Decision(True)

        monkeypatch.setattr(app, "push_screen_wait", approve)
        await app.action_review_selected_approval()
        assert screens[0].binding == ApprovalBinding(
            BATCH_ID, BATCH_DIGEST, "2030-01-01T00:00:00+00:00"
        )
        assert fake.decisions[-1][1]["decision"] == "approve"
        assert fake.decisions[-1][1]["batch_digest"] == BATCH_DIGEST
        await wait_for(pilot, lambda: f"Approved batch {BATCH_ID}" in chat_text(app))
        assert table.row_count == 0
        assert "No pending approvals" in str(
            app.query_one("#approval-detail", Static).render()
        )

        await app.action_cycle_approval_filter()
        assert app._approval_filter == "all"
        assert str(app.query_one("#approvals-filter", Button).label) == "Filter: all"
        assert table.row_count == 1
        assert app.query_one("#approvals-review", Button).disabled
        assert "approved by principa" in str(
            app.query_one("#approval-detail", Static).render()
        )


async def test_tui_review_requires_approver_and_reports_conflicts(monkeypatch):
    fake = FakeHaas()
    app = make_app(fake, credential="hk_test_submitter")
    async with app.run_test(size=(140, 44)):
        await app.action_show_approvals()
        await app.action_review_selected_approval()
        assert "requires the approver role" in str(
            app.query_one("#approval-detail", Static).render()
        )
        assert fake.decisions == []

        fake.roles = ["approver"]
        app.identity = {**(app.identity or {}), "roles": ["approver"]}
        fake.approval_batches[0]["batch_digest"] = "cd" * 32

        async def approve(screen):
            screen.binding = ApprovalBinding(BATCH_ID, BATCH_DIGEST)
            return Decision(True)

        original_get = app.session.client.get_approval

        async def stale_get(batch_id: str):
            batch = await original_get(batch_id)
            return {**batch, "batch_digest": BATCH_DIGEST}

        monkeypatch.setattr(app.session.client, "get_approval", stale_get)
        monkeypatch.setattr(app, "push_screen_wait", approve)
        await app.action_review_selected_approval()
        assert "Decision was not accepted" in str(
            app.query_one("#approval-detail", Static).render()
        )

        fake.approval_batches[0]["expired"] = True
        await app.action_review_selected_approval()
        assert "no longer pending" in str(
            app.query_one("#approval-detail", Static).render()
        )


async def test_tui_run_inspector_and_cancellable_states():
    fake = FakeHaas()
    fake.run_status = "queued"
    app = make_app(fake)
    async with app.run_test(size=(140, 44)):
        await app.action_show_runs()
        assert not app.query_one("#runs-cancel", Button).disabled
        assert not app.query_one("#runs-inspect", Button).disabled
        await app.action_inspect_selected()
        body = str(app.query_one("#run-inspector-body", Static).render())
        assert "run_1  [queued]" in body and "usage ledger: 1 entry" in body
        assert "settled $0.0010" in body

        fake.run_status = "cancelling"
        await app.action_refresh_runs()
        assert app.query_one("#runs-cancel", Button).disabled


async def test_tui_renders_recovery_cancellation_and_expiry_events():
    app = make_app(FakeHaas())
    async with app.run_test(size=(140, 44)) as pilot:
        for event in (
            SSEEvent("run_cancelling", {"outstanding_tools": 2}),
            SSEEvent("run_recovering", {"reason": "lease_expired", "attempt": 1}),
            SSEEvent("approval_expired", {"run_id": "run_1"}),
            SSEEvent("stream_gap", {"requested": 9, "latest": 4}),
        ):
            await app._on_event(event)
        await wait_for(pilot, lambda: "replay restarted" in chat_text(app))
        text = chat_text(app)
        assert "waiting for 2 in-flight tool(s)" in text
        assert "lease_expired, attempt 1" in text
        assert "approval expired" in text
        assert app._phase == "cancelling"


async def test_tui_attached_approval_shows_digest_binding(monkeypatch):
    fake = FakeHaas()
    app = make_app(fake)
    async with app.run_test(size=(140, 44)) as pilot:
        bindings: list[ApprovalBinding | None] = []

        async def capture(screen):
            if isinstance(screen, ApprovalScreen):
                bindings.append(screen.binding)
                return Decision(True)
            return None

        monkeypatch.setattr(app, "push_screen_wait", capture)
        await app._submit("refund it")
        await wait_for(pilot, lambda: "Refund prepared" in chat_text(app))
        assert bindings == [
            ApprovalBinding(BATCH_ID, BATCH_DIGEST, "2030-01-01T00:00:00+00:00")
        ]
        assert fake.resumes[0]["batch_id"] == BATCH_ID
