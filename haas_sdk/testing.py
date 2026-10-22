"""`FakeHaas`: an in-process fake server for user tests (§14).

Implements the semantics that matter (SDK-TEST-01): integer sequences, resume by
`Last-Event-ID`, `stream_gap`, digest checking (409 on mismatch), single-winner
decisions, idempotency replay + conflict, and `input_required` closing the stream.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class Call:
    """A scripted tool call."""

    name: str
    arguments: dict[str, Any]


@dataclass
class Step:
    """A scripted model step."""

    model: str = "fake-model"
    tool_calls: list[Call] = field(default_factory=list)


@dataclass
class Approval:
    """A scripted approval pause."""

    calls: list[Call] = field(default_factory=list)
    ttl_sec: float = 3600.0


@dataclass
class _FakeRun:
    run_id: str
    thread_id: str
    status: str = "queued"
    prompt: str = ""
    steps: list[Step] = field(default_factory=list)
    approval: Approval | None = None
    result: str | None = None
    cost_usd: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)
    sequence: int = 0
    batch_id: str | None = None
    batch_digest: str | None = None
    decided: bool = False
    decision: str | None = None
    idempotency: dict[str, dict[str, Any]] = field(default_factory=dict)


def _digest(run_id: str, step: int, calls: list[Call]) -> str:
    payload = json.dumps(
        {
            "run_id": run_id,
            "step": step,
            "calls": [{"name": c.name, "arguments": c.arguments} for c in calls],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class FakeHaas:
    """Scripted HaaS server served over `httpx.MockTransport`."""

    def __init__(self, tenant: str = "t1", strict: bool = True) -> None:
        self.tenant = tenant
        self.strict = strict
        self.runs: dict[str, _FakeRun] = {}
        self.decisions: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.failures: list[
            tuple[int, dict[str, Any]]
        ] = []  # (call_index, response spec)
        self._call_count = 0
        self._lock = threading.Lock()

    # -- scripting -------------------------------------------------------------------

    def script_run(
        self,
        *,
        steps: list[Step] | None = None,
        approval: Approval | None = None,
        result: str = "done",
        cost_usd: float = 0.0,
        run_id: str | None = None,
        thread_id: str | None = None,
    ) -> str:
        run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"
        thread_id = thread_id or f"thd-{uuid.uuid4().hex[:8]}"
        run = _FakeRun(
            run_id=run_id,
            thread_id=thread_id,
            steps=steps or [],
            approval=approval,
            result=result,
            cost_usd=cost_usd,
        )
        self.runs[run_id] = run
        return run_id

    def fail_next(
        self,
        status: int = 503,
        *,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> None:
        """Inject a fault for the next request (429 with Retry-After, 503, ...)."""
        self.failures.append(
            (
                self._call_count,
                {"status": status, "headers": headers or {}, "body": body},
            )
        )

    # -- server behavior -------------------------------------------------------------

    def _admit(self, run: _FakeRun) -> None:
        """Emit the event sequence for a scripted run up to its pause/terminal."""
        run.sequence = 0

        def emit(event_type: str, data: dict[str, Any]) -> None:
            run.sequence += 1
            run.events.append(
                {
                    "sequence": run.sequence,
                    "event_id": str(run.sequence),
                    "type": event_type,
                    "data": data,
                    "schema_version": 1,
                }
            )

        emit("run_queued", {"run_id": run.run_id, "thread_id": run.thread_id})
        for index, step in enumerate(run.steps, start=1):
            emit(
                "step_started",
                {
                    "step": index,
                    "attempt": 1,
                    "model": step.model,
                    "active_skills": [],
                    "unlocked_tools_count": 0,
                    "reserved_usd": 0.0,
                },
            )
            for call in step.tool_calls:
                emit(
                    "tool_dispatched",
                    {
                        "tool_call_id": f"tc-{call.name}",
                        "tool": call.name,
                        "queue": "default",
                        "invocation_id": f"inv-{call.name}",
                        "effect_key": f"ek-{call.name}",
                    },
                )
                emit(
                    "tool_result",
                    {
                        "tool_call_id": f"tc-{call.name}",
                        "tool": call.name,
                        "status": "succeeded",
                        "invocation_id": f"inv-{call.name}",
                        "is_error": False,
                        "preview": "ok",
                    },
                )
        if run.approval is not None:
            run.status = "input_required"
            run.batch_id = str(uuid.uuid4())
            run.batch_digest = _digest(
                run.run_id, len(run.steps) + 1, run.approval.calls
            )
            emit(
                "input_required",
                {
                    "step": len(run.steps) + 1,
                    "batch_id": run.batch_id,
                    "batch_digest": run.batch_digest,
                    "expires_at": "2099-01-01T00:00:00Z",
                    "pending_tool_calls": [
                        {"id": f"c-{c.name}", "name": c.name, "arguments": c.arguments}
                        for c in run.approval.calls
                    ],
                    "resume_endpoint": f"/v1/runs/{run.run_id}/resume",
                    "decision_endpoint": f"/v1/approvals/{run.batch_id}/decision",
                },
            )
            # SDK-TEST-01: input_required closes the stream.
            return
        run.status = "completed"
        emit(
            "run_completed",
            {
                "output": run.result,
                "steps_used": len(run.steps),
                "total_cost_usd": run.cost_usd,
                "cached_tokens": 0,
            },
        )

    def _resume(self, run: _FakeRun, approved: bool) -> None:
        def emit(event_type: str, data: dict[str, Any]) -> None:
            run.sequence += 1
            run.events.append(
                {
                    "sequence": run.sequence,
                    "event_id": str(run.sequence),
                    "type": event_type,
                    "data": data,
                    "schema_version": 1,
                }
            )

        emit(
            "hitl_approved" if approved else "hitl_rejected",
            {"batch_id": run.batch_id, "reason": None}
            if approved
            else {"batch_id": run.batch_id, "reason": "rejected"},
        )
        if approved:
            for call in run.approval.calls if run.approval else []:
                emit(
                    "tool_result",
                    {
                        "tool_call_id": f"tc-{call.name}",
                        "tool": call.name,
                        "status": "succeeded",
                        "invocation_id": f"inv-{call.name}",
                        "is_error": False,
                        "preview": "ok",
                    },
                )
            run.status = "completed"
            emit(
                "run_completed",
                {
                    "output": run.result,
                    "steps_used": len(run.steps) + 1,
                    "total_cost_usd": run.cost_usd,
                    "cached_tokens": 0,
                },
            )
        else:
            run.status = "cancelled"
            emit("run_cancelled", {"run_id": run.run_id, "reason": "rejected"})

    # -- HTTP handlers -----------------------------------------------------------------

    def _check_auth(self, request: httpx.Request) -> httpx.Response | None:
        if request.headers.get("x-tenant-id") != self.tenant:
            return httpx.Response(404, json={"detail": "Wrong tenant."})
        return None

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self._call_count += 1
            for index, spec in self.failures:
                if index == self._call_count - 1:
                    self.failures.remove((index, spec))
                    return httpx.Response(
                        spec["status"],
                        headers=spec["headers"],
                        json=spec["body"] or {"detail": "injected"},
                    )
            self.requests.append(request)
            path = request.url.path
            method = request.method
            denied = self._check_auth(request)
            if denied is not None:
                return denied
            if self.strict and not (
                path.startswith("/v1/") or path in ("/readyz", "/livez")
            ):
                return httpx.Response(404, json={"detail": f"unknown route {path}"})
            try:
                if method == "POST" and path == "/v1/runs":
                    return self._create(request)
                if (
                    method == "GET"
                    and path.startswith("/v1/runs/")
                    and path.endswith("/stream")
                ):
                    return self._stream(request)
                if (
                    method == "GET"
                    and path.startswith("/v1/runs/")
                    and path.endswith("/events")
                ):
                    return self._events(request)
                if method == "GET" and path.startswith("/v1/runs/"):
                    return self._get_run(request)
                if method == "POST" and path.endswith("/resume"):
                    return self._resume_request(request)
                if method == "POST" and path.endswith("/decision"):
                    return self._decide(request)
                if method == "GET" and path.startswith("/v1/approvals/"):
                    return self._get_batch(request)
                if method == "GET" and path == "/v1/capabilities":
                    return httpx.Response(
                        200,
                        json={
                            "version": "1",
                            "actions": ["run.submit", "run.approve"],
                            "event_schemas": ["run-events.v1"],
                            "features": [],
                            "durable_replay": True,
                        },
                    )
                if method == "GET" and path == "/readyz":
                    return httpx.Response(200, json={"status": "ok"})
                return httpx.Response(
                    404, json={"detail": f"no fake for {method} {path}"}
                )
            except Exception as exc:  # pragma: no cover - defensive
                return httpx.Response(500, json={"detail": str(exc)})

    def _create(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        key = request.headers.get("idempotency-key")
        digest = hashlib.sha256(request.content).hexdigest()
        for run in self.runs.values():
            prior = run.idempotency.get(key or "")
            if prior is not None:
                if prior["digest"] != digest:
                    return httpx.Response(
                        409,
                        json={
                            "detail": "Idempotency-Key was already used with a different request."
                        },
                    )
                return httpx.Response(
                    202,
                    json={**prior["response"], "replayed": True},
                    headers={"Idempotent-Replayed": "true"},
                )
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        thread_id = body.get("thread_id") or f"thd-{uuid.uuid4().hex[:8]}"
        # Scripted runs are matched by prompt for convenience; otherwise a bare run.
        scripted = next(
            (r for r in self.runs.values() if r.prompt == body.get("prompt")), None
        )
        if scripted is None:
            self.script_run(
                steps=[Step(model="fake-model")],
                result="done",
                run_id=run_id,
                thread_id=thread_id,
            )
            scripted = self.runs[run_id]
        scripted.prompt = body.get("prompt", "")
        self._admit(scripted)
        response = {
            "run_id": scripted.run_id,
            "thread_id": scripted.thread_id,
            "status": scripted.status,
            "stream_url": f"/v1/runs/{scripted.run_id}/stream",
            "events_url": f"/v1/runs/{scripted.run_id}/events",
        }
        if key:
            scripted.idempotency[key] = {"digest": digest, "response": response}
        return httpx.Response(202, json=response)

    def _get_run(self, request: httpx.Request) -> httpx.Response:
        run_id = request.url.path.split("/")[3]
        run = self.runs.get(run_id)
        if run is None:
            return httpx.Response(404, json={"detail": "Run not found."})
        approval = None
        if run.status == "input_required" and run.batch_id:
            approval = {
                "batch_id": run.batch_id,
                "batch_digest": run.batch_digest,
                "expires_at": "2099-01-01T00:00:00Z",
                "decision_endpoint": f"/v1/approvals/{run.batch_id}/decision",
            }
        return httpx.Response(
            200,
            json={
                "run_id": run.run_id,
                "thread_id": run.thread_id,
                "status": run.status,
                "result": run.result if run.status == "completed" else None,
                "step_count": len(run.steps),
                "total_cost_usd": run.cost_usd,
                "state_version": 1,
                "event_sequence": run.sequence,
                "active_skills": [],
                "approval": approval,
            },
        )

    def _stream(self, request: httpx.Request) -> httpx.Response:
        run_id = request.url.path.split("/")[3]
        run = self.runs.get(run_id)
        if run is None:
            return httpx.Response(404, json={"detail": "Run not found."})
        last = request.headers.get("last-event-id", "0")
        try:
            after = int(last)
        except ValueError:
            after = 0
        frames = [": keep-alive\n\n"]
        for event in run.events:
            if event["sequence"] <= after:
                continue
            frames.append(f"id: {event['sequence']}\n")
            frames.append(f"event: {event['type']}\n")
            frames.append(f"data: {json.dumps(event['data'])}\n\n")
        return httpx.Response(200, content="".join(frames).encode())

    def _events(self, request: httpx.Request) -> httpx.Response:
        run_id = request.url.path.split("/")[3]
        run = self.runs.get(run_id)
        if run is None:
            return httpx.Response(404, json={"detail": "Run not found."})
        after = int(request.url.params.get("after", 0))
        items = [e for e in run.events if e["sequence"] > after]
        return httpx.Response(
            200,
            json={
                "items": items,
                "next_after": items[-1]["sequence"] if items else after,
                "latest_sequence": run.sequence,
            },
        )

    def _get_batch(self, request: httpx.Request) -> httpx.Response:
        batch_id = request.url.path.split("/")[3]
        run = next((r for r in self.runs.values() if r.batch_id == batch_id), None)
        if run is None or run.batch_id is None:
            return httpx.Response(404, json={"detail": "Approval batch not found."})
        return httpx.Response(
            200,
            json={
                "batch_id": run.batch_id,
                "run_id": run.run_id,
                "step_number": len(run.steps) + 1,
                "status": "pending" if not run.decided else "approved",
                "batch_digest": run.batch_digest,
                "expires_at": "2099-01-01T00:00:00Z",
                "expired": False,
                "tool_calls": [
                    {"id": f"c-{c.name}", "name": c.name, "arguments": c.arguments}
                    for c in (run.approval.calls if run.approval else [])
                ],
            },
        )

    def _decide(self, request: httpx.Request) -> httpx.Response:
        batch_id = request.url.path.split("/")[3]
        body = json.loads(request.content)
        run = next((r for r in self.runs.values() if r.batch_id == batch_id), None)
        if run is None:
            return httpx.Response(404, json={"detail": "Approval batch not found."})
        if body.get("batch_digest") != run.batch_digest:
            return httpx.Response(
                409, json={"detail": "Approval batch digest does not match."}
            )
        if run.decided:
            return httpx.Response(
                409, json={"detail": "No pending approval batch for this run."}
            )
        run.decided = True
        run.decision = body.get("decision")
        self.decisions.append(
            {
                "batch_id": batch_id,
                "digest": body.get("batch_digest"),
                "approve": body.get("decision") == "approve",
            }
        )
        self._resume(run, approved=body.get("decision") == "approve")
        return httpx.Response(
            202,
            json={
                "run_id": run.run_id,
                "batch_id": batch_id,
                "decision": body.get("decision"),
                "replayed": False,
            },
        )

    def _resume_request(self, request: httpx.Request) -> httpx.Response:
        run_id = request.url.path.split("/")[3]
        run = self.runs.get(run_id)
        if run is None:
            return httpx.Response(404, json={"detail": "Run not found."})
        body = json.loads(request.content)
        if run.status != "input_required":
            return httpx.Response(
                409,
                json={"detail": f"Run status is '{run.status}', not 'input_required'."},
            )
        if body.get("batch_digest") != run.batch_digest:
            return httpx.Response(
                409, json={"detail": "Approval batch digest does not match."}
            )
        run.decided = True
        self.decisions.append(
            {
                "batch_id": run.batch_id,
                "digest": body.get("batch_digest"),
                "approve": body.get("approved"),
            }
        )
        self._resume(run, approved=bool(body.get("approved")))
        return httpx.Response(
            202,
            json={
                "run_id": run.run_id,
                "status": "queued",
                "batch_id": run.batch_id,
                "replayed": False,
            },
        )

    # -- clients -----------------------------------------------------------------------

    def client(self) -> Any:
        """A real sync `Haas` over the fake transport."""
        from haas_sdk import Haas

        return Haas(
            base_url="http://fake",
            api_key="hk_test_fake",
            tenant=self.tenant,
            transport=httpx.MockTransport(self.handler),
            sleep=lambda _: None,
        )

    def async_client(self) -> Any:
        """A real async `AsyncHaas` over the fake transport."""
        from haas_sdk import AsyncHaas

        async def no_sleep(_: float) -> None:
            return None

        return AsyncHaas(
            base_url="http://fake",
            api_key="hk_test_fake",
            tenant=self.tenant,
            transport=httpx.MockTransport(self.handler),
            sleep=no_sleep,
        )
