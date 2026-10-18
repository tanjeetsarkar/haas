"""Request builders and response parsers shared by :class:`Haas` and :class:`AsyncHaas`.

Each builder returns an :class:`Op` (what to send, how to read the answer) so the
sync and async clients only differ in how they perform I/O.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, BinaryIO, Generic, TypeVar

import httpx

from haas_sdk._core import Call, clean, idempotency_headers, seg
from haas_sdk.models import AgentRevision, ApprovalBatch, Artifact, Page, Run, RunEvent, Thread

T = TypeVar("T")

APPROVAL_DECISIONS = frozenset({"approve", "reject"})


@dataclass
class Op(Generic[T]):
    call: Call
    parse: Callable[[httpx.Response], T]


def _json(response: httpx.Response) -> Any:
    if not response.content:
        return {}
    return response.json()


def _dict(response: httpx.Response) -> dict[str, Any]:
    body = _json(response)
    return body if isinstance(body, dict) else {"value": body}


def _page(factory: Callable[[dict[str, Any]], T]) -> Callable[[httpx.Response], Page[T]]:
    def parse(response: httpx.Response) -> Page[T]:
        body = _dict(response)
        return Page(
            items=[factory(item) for item in body.get("items") or []],
            next_cursor=body.get("next_cursor"),
        )

    return parse


def _when(value: datetime | float | int | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _multi(value: str | Iterable[str] | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return ",".join(value)


# --- identity -------------------------------------------------------------------------


def me() -> Op[dict[str, Any]]:
    return Op(Call("GET", "/v1/me", idempotent=True), _dict)


def capabilities() -> Op[dict[str, Any]]:
    return Op(Call("GET", "/v1/capabilities", idempotent=True), _dict)


def health(path: str = "/readyz") -> Op[dict[str, Any]]:
    return Op(Call("GET", path, idempotent=True), _dict)


# --- runs -----------------------------------------------------------------------------


def run_create(
    prompt: str,
    *,
    idempotency_key: str | None,
    extra: Mapping[str, Any],
) -> Op[dict[str, Any]]:
    body = clean({"prompt": prompt, **extra})
    return Op(
        Call(
            "POST",
            "/v1/runs",
            json=body,
            headers=idempotency_headers(idempotency_key),
            idempotent=True,
        ),
        _dict,
    )


def run_get(run_id: str) -> Op[Run]:
    return Op(
        Call("GET", f"/v1/runs/{seg(run_id)}", idempotent=True),
        lambda r: Run.from_dict(_dict(r)),
    )


def run_list(
    *,
    limit: int | None,
    cursor: str | None,
    status: str | Iterable[str] | None,
    thread_id: str | None,
    created_after: datetime | float | str | None,
    created_before: datetime | float | str | None,
) -> Op[Page[Run]]:
    params = clean(
        {
            "limit": limit,
            "cursor": cursor,
            "status": _multi(status),
            "thread_id": thread_id,
            "created_after": _when(created_after),
            "created_before": _when(created_before),
        }
    )
    return Op(Call("GET", "/v1/runs", params=params, idempotent=True), _page(Run.from_dict))


def run_cancel(run_id: str) -> Op[dict[str, Any]]:
    # Cancelling twice is harmless (the second answer is 202 or 409), so retries are safe.
    return Op(Call("POST", f"/v1/runs/{seg(run_id)}/cancel", idempotent=True), _dict)


def run_resume(
    run_id: str,
    *,
    approved: bool,
    batch_id: str | None,
    batch_digest: str | None,
    rejection_reason: str | None,
    secrets: Mapping[str, str] | None,
    idempotency_key: str | None,
) -> Op[dict[str, Any]]:
    body = clean(
        {
            "approved": approved,
            "batch_id": batch_id,
            "batch_digest": batch_digest,
            "rejection_reason": rejection_reason,
            "secrets": dict(secrets) if secrets else None,
        }
    )
    return Op(
        Call(
            "POST",
            f"/v1/runs/{seg(run_id)}/resume",
            json=body,
            headers=idempotency_headers(idempotency_key),
            idempotent=True,
        ),
        _dict,
    )


def run_events(run_id: str, *, after: int, limit: int | None) -> Op[list[RunEvent]]:
    params = clean({"after": after, "limit": limit})
    return Op(
        Call("GET", f"/v1/runs/{seg(run_id)}/events", params=params, idempotent=True),
        lambda r: [RunEvent.from_record(item) for item in _dict(r).get("items") or []],
    )


def run_subresource(run_id: str, name: str) -> Op[dict[str, Any]]:
    return Op(Call("GET", f"/v1/runs/{seg(run_id)}/{name}", idempotent=True), _dict)


def run_stream_call(run_id: str, *, last_event_id: str | None, output_deltas: bool) -> Call:
    headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
    if last_event_id is not None:
        headers["Last-Event-ID"] = last_event_id
    params = {"output_deltas": "true"} if output_deltas else None
    return Call(
        "GET", f"/v1/runs/{seg(run_id)}/stream", params=params, headers=headers, idempotent=True
    )


# --- approvals ------------------------------------------------------------------------


def approval_list(
    *, status: str | None, run_id: str | None, limit: int | None, cursor: str | None
) -> Op[Page[ApprovalBatch]]:
    params = clean({"status": status, "run_id": run_id, "limit": limit, "cursor": cursor})
    return Op(
        Call("GET", "/v1/approvals", params=params, idempotent=True),
        _page(ApprovalBatch.from_dict),
    )


def approval_get(batch_id: str) -> Op[ApprovalBatch]:
    return Op(
        Call("GET", f"/v1/approvals/{seg(batch_id)}", idempotent=True),
        lambda r: ApprovalBatch.from_dict(_dict(r)),
    )


def approval_decide(
    batch_id: str,
    *,
    decision: str,
    batch_digest: str,
    reason: str | None,
    secrets: Mapping[str, str] | None,
    idempotency_key: str | None,
) -> Op[dict[str, Any]]:
    if decision not in APPROVAL_DECISIONS:
        raise ValueError("decision must be 'approve' or 'reject'")
    body = clean(
        {
            "decision": decision,
            "batch_digest": batch_digest,
            "reason": reason,
            "secrets": dict(secrets) if secrets else None,
        }
    )
    return Op(
        Call(
            "POST",
            f"/v1/approvals/{seg(batch_id)}/decision",
            json=body,
            headers=idempotency_headers(idempotency_key),
            idempotent=True,
        ),
        _dict,
    )


# --- threads --------------------------------------------------------------------------


def thread_list(
    *, search: str | None, limit: int | None, cursor: str | None
) -> Op[Page[Thread]]:
    params = clean({"search": search, "limit": limit, "cursor": cursor})
    return Op(
        Call("GET", "/v1/threads", params=params, idempotent=True), _page(Thread.from_dict)
    )


def thread_get(
    thread_id: str, *, offset: int | None, limit: int | None, history_version: int | None
) -> Op[dict[str, Any]]:
    params = clean({"offset": offset, "limit": limit, "history_version": history_version})
    return Op(
        Call("GET", f"/v1/threads/{seg(thread_id)}", params=params, idempotent=True), _dict
    )


def thread_branch(thread_id: str, *, expected_history_version: int) -> Op[dict[str, Any]]:
    return Op(
        Call(
            "POST",
            f"/v1/threads/{seg(thread_id)}/branches",
            json={"expected_history_version": expected_history_version},
        ),
        _dict,
    )


# --- agents ---------------------------------------------------------------------------


def agent_list(
    *, agent_name: str | None, state: str | None, limit: int | None, cursor: str | None
) -> Op[Page[AgentRevision]]:
    params = clean(
        {"agent_name": agent_name, "state": state, "limit": limit, "cursor": cursor}
    )
    return Op(
        Call("GET", "/v1/agents/revisions", params=params, idempotent=True),
        _page(AgentRevision.from_dict),
    )


def agent_get(revision_id: str) -> Op[AgentRevision]:
    return Op(
        Call("GET", f"/v1/agents/revisions/{seg(revision_id)}", idempotent=True),
        lambda r: AgentRevision.from_dict(_dict(r)),
    )


def agent_create(spec: Mapping[str, Any]) -> Op[AgentRevision]:
    return Op(
        Call("POST", "/v1/agents/revisions", json=dict(spec)),
        lambda r: AgentRevision.from_dict(_dict(r)),
    )


def agent_action(revision_id: str, action: str) -> Op[dict[str, Any]]:
    return Op(Call("POST", f"/v1/agents/revisions/{seg(revision_id)}/{action}"), _dict)


# --- artifacts ------------------------------------------------------------------------


def _artifact_items(response: httpx.Response) -> list[Artifact]:
    body = _json(response)
    items = body.get("items", []) if isinstance(body, dict) else body
    return [Artifact.from_dict(item) for item in items or []]


def artifact_list(run_id: str) -> Op[list[Artifact]]:
    return Op(
        Call("GET", f"/v1/runs/{seg(run_id)}/artifacts", idempotent=True), _artifact_items
    )


def artifact_upload(
    run_id: str,
    *,
    filename: str,
    content: bytes | BinaryIO,
    content_type: str,
) -> Op[Artifact]:
    return Op(
        Call(
            "POST",
            f"/v1/runs/{seg(run_id)}/artifacts",
            files={"file": (filename, content, content_type)},
        ),
        lambda r: Artifact.from_dict(_dict(r)),
    )


def artifact_get(artifact_id: str) -> Op[Artifact]:
    return Op(
        Call("GET", f"/v1/artifacts/{seg(artifact_id)}", idempotent=True),
        lambda r: Artifact.from_dict(_dict(r)),
    )


def artifact_content(
    artifact_id: str, *, start: int | None, end: int | None
) -> Op[bytes]:
    headers: dict[str, str] = {"Accept": "*/*"}
    if start is not None or end is not None:
        headers["Range"] = f"bytes={start or 0}-{'' if end is None else end}"
    return Op(
        Call(
            "GET",
            f"/v1/artifacts/{seg(artifact_id)}/content",
            headers=headers,
            idempotent=True,
            raw=True,
        ),
        lambda r: r.content,
    )


def artifact_delete(artifact_id: str) -> Op[None]:
    return Op(
        Call("DELETE", f"/v1/artifacts/{seg(artifact_id)}", idempotent=True), lambda r: None
    )
