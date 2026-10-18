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


BATCH_ID = "11111111-2222-3333-4444-555555555555"
BATCH_DIGEST = "ab" * 32
TRACEPARENT = "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01"


def ev(idx: int, event: str, data: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    return (str(idx), event, data)


def sse(events: list[tuple[str, str, dict[str, Any]]]) -> str:
    return "".join(
        f"id: {i}\nevent: {e}\ndata: {json.dumps(d)}\n\n" for i, e, d in events
    )


def delta(step: int, attempt: int, call: int, text: str) -> str:
    data = {
        "schema": "run-output-delta.v1",
        "step": step,
        "attempt": attempt,
        "call": call,
        "index": 0,
        "text": text,
        "final": False,
        "truncated": False,
    }
    return f"event: run.output.delta\ndata: {json.dumps(data)}\n\n"


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
            "batch_id": BATCH_ID,
            "batch_digest": BATCH_DIGEST,
            "expires_at": "2030-01-01T00:00:00+00:00",
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


BUDGET = {
    "max_cost_usd": 0.3,
    "settled_usd": 0.001,
    "uncertain_usd": 0.0,
    "reserved_usd": 0.001,
    "remaining_usd": 0.298,
    "max_steps": 15,
    "steps_used": 2,
    "max_tool_invocations": 20,
    "tool_invocations_used": 1,
    "deadline_at": "2030-01-01T00:00:00+00:00",
}


class FakeHaas:
    def __init__(self, script: str = "approval", healthy: bool = True) -> None:
        self.script = script
        self.healthy = healthy
        self.created: list[dict[str, Any]] = []
        self.resumes: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.stream_headers: list[dict[str, str]] = []
        self.stream_params: list[dict[str, str]] = []
        self.features: list[str] | None = None
        self.after_pause: list | None = None
        self.run_status = "input_required"
        self.list_error: str | None = None
        self.cancel_error: str | None = None
        self.auth_rejected = False
        self.allowed_tenants = {"t1", "t2"}
        self.environment = "dev"
        self.identity_headers: list[dict[str, str]] = []
        self.roles = ["submitter"]
        self.admin_requests: list[tuple[str, str, Any]] = []
        self.approval_queries: list[dict[str, str]] = []
        self.decisions: list[tuple[str, dict[str, Any]]] = []
        self.approval_batches: list[dict[str, Any]] = [
            {
                "batch_id": BATCH_ID,
                "run_id": "run_1",
                "thread_id": "thd_1",
                "step_number": 2,
                "batch_digest": BATCH_DIGEST,
                "status": "pending",
                "expired": False,
                "expires_at": "2030-01-01T00:00:00+00:00",
                "tool_calls": [
                    {
                        "id": "c2",
                        "name": "prepare_refund_batch",
                        "arguments": '{"invoice_ids": ["inv_3002"]}',
                    }
                ],
                "decision": None,
            }
        ]
        self.pending_tool_calls: Any = [
            {
                "id": "c2",
                "function": {
                    "name": "prepare_refund_batch",
                    "arguments": '{"invoice_ids": ["inv_3002"]}',
                },
            }
        ]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not self.healthy:
            raise httpx.ConnectError("connection refused")
        if path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/v1/me":
            self.identity_headers.append(dict(request.headers))
            if self.auth_rejected or not request.headers.get("authorization"):
                return httpx.Response(401, json={"detail": "Authentication required."})
            tenant = request.headers.get("x-tenant-id", "")
            if tenant not in self.allowed_tenants:
                return httpx.Response(403, json={"detail": "Tenant access denied."})
            return httpx.Response(
                200,
                json={
                    "tenant_id": tenant,
                    "principal_id": "principal-1234",
                    "roles": self.roles,
                    "credential_type": "api_key",
                    "environment": self.environment,
                    **(
                        {
                            "capabilities": {
                                "version": "1",
                                "actions": ["run:submit", "run:read"],
                                "event_schemas": ["run-events.v1"],
                                "features": self.features,
                            }
                        }
                        if self.features is not None
                        else {}
                    ),
                },
            )
        if self.auth_rejected and path.startswith("/v1/"):
            return httpx.Response(401, json={"detail": "Authentication expired."})
        if path == "/v1/plugins":
            return httpx.Response(200, json=PLUGINS)
        if path.startswith("/v1/admin/") or path == "/v1/audit-events":
            body = json.loads(request.content) if request.content else None
            self.admin_requests.append((request.method, path, body))
            if path == "/v1/admin/memberships" and request.method == "GET":
                result = {
                    "items": [{"principal_id": "principal-1", "role": "submitter"}]
                }
            elif path == "/v1/admin/api-keys" and request.method == "POST":
                result = {
                    "key_id": "key-1",
                    "credential": "hk_test_one_time",
                    "name": body["name"],
                }
            elif path == "/v1/admin/policy" and request.method == "GET":
                result = {"active": None}
            elif path == "/v1/audit-events":
                result = {"items": [], "next_cursor": None}
            else:
                result = {"ok": True}
            return httpx.Response(200, json=result)
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
            self.stream_params.append(dict(request.url.params))
            cursor = request.headers.get("last-event-id", "0")
            if self.script == "live":
                live_deltas = request.url.params.get("output_deltas") == "true"
                body = sse(SIMPLE[:2]) + (
                    delta(1, 1, 1, "Hel")
                    + delta(1, 1, 1, "lo wor")
                    + delta(1, 1, 2, "Hello there")
                    if live_deltas
                    else ""
                )
                return httpx.Response(
                    200,
                    content=body + sse(SIMPLE[2:]),
                    headers={"content-type": "text/event-stream"},
                )
            base = (
                []
                if self.script == "no_events"
                else SIMPLE
                if self.script == "simple"
                else PRE_PAUSE + (self.after_pause or [])
            )
            events = [e for e in base if int(e[0]) > int(cursor)]
            return httpx.Response(
                200, content=sse(events), headers={"content-type": "text/event-stream"}
            )
        if path == "/v1/approvals":
            self.approval_queries.append(dict(request.url.params))
            status = request.url.params.get("status", "pending")
            items = [b for b in self.approval_batches if status in ("all", b["status"])]
            return httpx.Response(200, json={"items": items})
        if path.startswith("/v1/approvals/"):
            batch_id = path.split("/")[3]
            batch = next(
                (b for b in self.approval_batches if b["batch_id"] == batch_id), None
            )
            if batch is None:
                return httpx.Response(404, json={"detail": "Approval batch not found."})
            if path.endswith("/decision"):
                body = json.loads(request.content)
                self.decisions.append((batch_id, body))
                if body["batch_digest"] != batch["batch_digest"]:
                    return httpx.Response(
                        409, json={"detail": "Batch digest mismatch."}
                    )
                batch["status"] = (
                    "approved" if body["decision"] == "approve" else "rejected"
                )
                batch["decision"] = {
                    "decision": batch["status"],
                    "decided_by": "principal-1234",
                    "reason": body.get("reason"),
                    "decided_at": "2030-01-01T00:00:00+00:00",
                }
                return httpx.Response(
                    202, json={"run_id": batch["run_id"], "status": "queued"}
                )
            return httpx.Response(200, json=batch)
        if path.endswith("/events") and path.startswith("/v1/runs/"):
            after = int(request.url.params.get("after", "0"))
            items = [
                {
                    "sequence": int(i),
                    "type": e,
                    "occurred_at": "2030-01-01T00:00:00+00:00",
                    "data": d,
                }
                for i, e, d in PRE_PAUSE
                if int(i) > after
            ][:2]
            return httpx.Response(
                200,
                json={
                    "items": items,
                    "next_after": items[-1]["sequence"] if items else after,
                    "latest_sequence": len(PRE_PAUSE),
                },
            )
        if path.endswith("/steps") and path.startswith("/v1/runs/"):
            return httpx.Response(
                200,
                json={
                    "run_id": "run_1",
                    "steps": [
                        {
                            "step_number": 1,
                            "attempt": 1,
                            "status": "tools_dispatched",
                            "model": "m/primary",
                            "cost_usd": 0.001,
                            "cost_reported": True,
                        },
                        {
                            "step_number": 2,
                            "attempt": 1,
                            "status": "failed",
                            "model": "m/primary",
                            "cost_usd": 0,
                            "cost_reported": False,
                            "error": "boom",
                        },
                    ],
                    "invocations": [
                        {
                            "step_number": 1,
                            "ordinal": 0,
                            "tool_name": "query_revenue_metrics",
                            "status": "succeeded",
                            "effect_key": "f" * 64,
                        },
                        {
                            "step_number": 1,
                            "ordinal": 1,
                            "tool_name": "send_email",
                            "status": "unknown",
                            "effect_key": "e" * 64,
                        },
                    ],
                },
            )
        if path.endswith("/usage") and path.startswith("/v1/runs/"):
            return httpx.Response(
                200,
                json={
                    "run_id": "run_1",
                    "budget": BUDGET,
                    "entries": [
                        {
                            "unit": "model_tokens",
                            "quantity": 120,
                            "cost_usd": 0.001,
                            "settlement_status": "settled",
                        }
                    ],
                },
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
                    "pending_tool_calls": self.pending_tool_calls
                    if self.run_status == "input_required"
                    else None,
                    "result": "Run cancelled."
                    if self.run_status == "cancelled"
                    else "done",
                    "state_version": 7,
                    "event_sequence": 5,
                    "traceparent": TRACEPARENT,
                    "budget": BUDGET,
                    "approval": {
                        "batch_id": BATCH_ID,
                        "batch_digest": BATCH_DIGEST,
                        "expires_at": "2030-01-01T00:00:00+00:00",
                    }
                    if self.run_status == "input_required"
                    else None,
                },
            )
        return httpx.Response(404, json={"detail": "nope"})
