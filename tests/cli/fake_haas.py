"""Scriptable in-memory HaaS API (httpx.MockTransport) shared by the CLI/TUI tests."""

from __future__ import annotations

import json
from typing import Any

import httpx

PLUGINS = {
    "skills": [
        {
            "name": "revenue-ops",
            "version": "1.0.0",
            "description": "Revenue anomalies and refunds.",
            "required_tools": ["query_revenue_metrics", "prepare_refund_batch"],
            "allowed_secrets": ["ANALYTICS_DB_PATH"],
            "references": [],
        },
        {
            "name": "weather",
            "version": "1.0.0",
            "description": "Forecasts.",
            "required_tools": [],
            "allowed_secrets": [],
            "references": [],
        },
    ],
    "tools": [
        {"name": "activate_skill", "queue": "q_tools_fast", "requires_approval": False},
        {
            "name": "prepare_refund_batch",
            "queue": "q_tools_sandbox",
            "requires_approval": True,
        },
    ],
}


def ev(idx: int, event: str, data: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    return (f"{idx}-0", event, data)


def sse(events: list[tuple[str, str, dict[str, Any]]]) -> str:
    return "".join(
        f"id: {i}\nevent: {e}\ndata: {json.dumps(d)}\n\n" for i, e, d in events
    )


PRE_PAUSE = [
    ev(1, "run_queued", {"run_id": "run_1", "thread_id": "thd_1"}),
    ev(
        2,
        "step_started",
        {"step": 1, "model": "m/primary", "active_skills": ["revenue-ops"]},
    ),
    ev(
        3,
        "tool_dispatched",
        {
            "tool_call_id": "c1",
            "tool": "query_revenue_metrics",
            "queue": "q_tools_sandbox",
        },
    ),
    ev(
        4,
        "tool_result",
        {
            "tool_call_id": "c1",
            "tool": "query_revenue_metrics",
            "is_error": False,
            "preview": '{"anomaly": true}',
        },
    ),
    ev(
        5,
        "input_required",
        {
            "step": 2,
            "pending_tool_calls": [
                {
                    "id": "c2",
                    "name": "prepare_refund_batch",
                    "arguments": json.dumps(
                        {"invoice_ids": ["inv_3002"], "reason": "dup"}
                    ),
                }
            ],
        },
    ),
]
APPROVED = [
    ev(6, "hitl_approved", {"dispatched_tools": 1}),
    ev(
        7,
        "tool_result",
        {
            "tool_call_id": "c2",
            "tool": "prepare_refund_batch",
            "is_error": False,
            "preview": '{"batch_id": "rb_1"}',
        },
    ),
    ev(
        8,
        "run_completed",
        {
            "output": "**Refund prepared** rb_1",
            "steps_used": 3,
            "total_cost_usd": 0.0123,
            "cached_tokens": 5,
        },
    ),
]
REJECTED = [
    ev(6, "hitl_rejected", {"reason": "no"}),
    ev(
        7,
        "run_completed",
        {
            "output": "Refund was rejected.",
            "steps_used": 3,
            "total_cost_usd": 0.01,
            "cached_tokens": 0,
        },
    ),
]
SIMPLE = [
    ev(1, "run_queued", {"run_id": "run_1", "thread_id": "thd_1"}),
    ev(2, "step_started", {"step": 1, "model": "m/primary", "active_skills": []}),
    ev(
        3,
        "run_completed",
        {
            "output": "Hello there",
            "steps_used": 1,
            "total_cost_usd": 0.001,
            "cached_tokens": 0,
        },
    ),
]


class FakeHaas:
    def __init__(self, script: str = "approval", healthy: bool = True) -> None:
        self.script = script
        self.healthy = healthy
        self.created: list[dict[str, Any]] = []
        self.resumes: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.stream_headers: list[dict[str, str]] = []
        self.after_pause: list | None = None
        self.run_status = "input_required"
        self.list_error: str | None = None
        self.cancel_error: str | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not self.healthy:
            raise httpx.ConnectError("connection refused")
        if path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/v1/plugins":
            return httpx.Response(200, json=PLUGINS)
        if path == "/v1/runs" and request.method == "GET":
            if self.list_error:
                return httpx.Response(503, json={"detail": self.list_error})
            if request.url.params.get("cursor") == "page2":
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "run_id": "run_2",
                                "thread_id": "thd_2",
                                "status": "completed",
                                "step_count": 2,
                                "total_cost_usd": 0.002,
                            }
                        ],
                        "next_cursor": None,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "run_id": "run_1",
                            "thread_id": "thd_1",
                            "status": self.run_status,
                            "step_count": 2,
                            "total_cost_usd": 0.001,
                        }
                    ],
                    "next_cursor": "page2",
                },
            )
        if path == "/v1/runs" and request.method == "POST":
            self.created.append(json.loads(request.content))
            return httpx.Response(
                202,
                json={
                    "run_id": "run_1",
                    "thread_id": self.created[-1].get("thread_id") or "thd_1",
                    "status": "running",
                },
            )
        if path.endswith("/resume"):
            body = json.loads(request.content)
            self.resumes.append(body)
            self.after_pause = APPROVED if body["approved"] else REJECTED
            return httpx.Response(202, json={"run_id": "run_1", "status": "running"})
        if path.endswith("/cancel") and request.method == "POST":
            if self.cancel_error:
                return httpx.Response(409, json={"detail": self.cancel_error})
            run_id = path.rsplit("/", 2)[-2]
            self.cancelled.append(run_id)
            self.run_status = "cancelled"
            return httpx.Response(202, json={"run_id": run_id, "status": "cancelled"})
        if path.endswith("/stream"):
            self.stream_headers.append(dict(request.headers))
            cursor = request.headers.get("last-event-id", "0-0")
            base = (
                []
                if self.script == "no_events"
                else SIMPLE
                if self.script == "simple"
                else PRE_PAUSE + (self.after_pause or [])
            )
            events = [
                e for e in base if int(e[0].split("-")[0]) > int(cursor.split("-")[0])
            ]
            return httpx.Response(
                200, content=sse(events), headers={"content-type": "text/event-stream"}
            )
        if path.startswith("/v1/runs/"):
            if path.endswith("/stream"):
                raise AssertionError("stream route must be handled before run detail")
            return httpx.Response(
                200,
                json={
                    "run_id": "run_1",
                    "thread_id": "thd_1",
                    "status": self.run_status,
                    "step_count": 2,
                    "total_cost_usd": 0.0,
                    "active_skills": ["revenue-ops"],
                    "pending_tool_calls": [
                        {
                            "id": "c2",
                            "function": {
                                "name": "prepare_refund_batch",
                                "arguments": '{"invoice_ids": ["inv_3002"]}',
                            },
                        }
                    ]
                    if self.run_status == "input_required"
                    else None,
                    "result": "Run cancelled."
                    if self.run_status == "cancelled"
                    else "done",
                },
            )
        return httpx.Response(404, json={"detail": "nope"})
