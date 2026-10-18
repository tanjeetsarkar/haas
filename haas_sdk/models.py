"""Typed views over HaaS API resources.

Every model keeps the full JSON body in ``raw`` so fields added by newer servers stay
reachable without an SDK upgrade (the ``/v1`` API is additive).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

TERMINAL_EVENTS = frozenset({"run_completed", "run_error", "run_cancelled"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
# A stream may close while a run is still active; `input_required` waits on a human.
ACTIVE_STATUSES = frozenset({"queued", "running", "cancelling"})

T = TypeVar("T")


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True)
class Run:
    run_id: str
    status: str
    thread_id: str | None = None
    result: str | None = None
    step_count: int | None = None
    total_cost_usd: float | None = None
    event_sequence: int | None = None
    approval: dict[str, Any] | None = None
    created_at: float | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Run:
        return cls(
            run_id=str(data["run_id"]),
            status=str(data.get("status", "unknown")),
            thread_id=_str(data.get("thread_id")),
            result=data.get("result"),
            step_count=data.get("step_count"),
            total_cost_usd=data.get("total_cost_usd"),
            event_sequence=data.get("event_sequence"),
            approval=data.get("approval"),
            created_at=data.get("created_at"),
            raw=data,
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def waiting_for_approval(self) -> bool:
        return self.status == "input_required"


@dataclass(frozen=True)
class RunEvent:
    """One SSE frame. ``id`` is the durable per-run sequence (absent on advisory deltas)."""

    type: str
    data: dict[str, Any]
    id: str | None = None

    @property
    def sequence(self) -> int | None:
        return int(self.id) if self.id is not None and self.id.isdigit() else None

    @property
    def is_terminal(self) -> bool:
        return self.type in TERMINAL_EVENTS

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> RunEvent:
        """Build from a ``GET /v1/runs/{id}/events`` item."""
        sequence = record.get("sequence")
        return cls(
            type=str(record.get("type", "message")),
            data=record.get("data") or {},
            id=str(sequence) if sequence is not None else None,
        )


@dataclass(frozen=True)
class ApprovalBatch:
    batch_id: str
    run_id: str
    status: str
    batch_digest: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    expires_at: str | None = None
    decision: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalBatch:
        return cls(
            batch_id=str(data["batch_id"]),
            run_id=str(data.get("run_id", "")),
            status=str(data.get("status", "pending")),
            batch_digest=str(data.get("batch_digest", "")),
            tool_calls=list(data.get("tool_calls") or []),
            expires_at=_str(data.get("expires_at")),
            decision=data.get("decision"),
            raw=data,
        )


@dataclass(frozen=True)
class Thread:
    thread_id: str
    history_version: int | None = None
    active_run_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Thread:
        return cls(
            thread_id=str(data["thread_id"]),
            history_version=data.get("history_version"),
            active_run_id=_str(data.get("active_run_id")),
            raw=data,
        )


@dataclass(frozen=True)
class AgentRevision:
    revision_id: str
    agent_name: str
    version: int | None = None
    state: str | None = None
    digest: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentRevision:
        return cls(
            revision_id=str(data["revision_id"]),
            agent_name=str(data.get("agent_name", "")),
            version=data.get("version"),
            state=_str(data.get("state")),
            digest=_str(data.get("digest")),
            raw=data,
        )


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    run_id: str | None = None
    name: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Artifact:
        return cls(
            artifact_id=str(data.get("artifact_id") or data.get("id")),
            run_id=_str(data.get("run_id")),
            name=_str(data.get("name") or data.get("filename")),
            content_type=_str(data.get("content_type")),
            size_bytes=data.get("size_bytes", data.get("size")),
            sha256=_str(data.get("sha256") or data.get("digest")),
            raw=data,
        )


@dataclass(frozen=True)
class Page(Generic[T]):
    items: list[T]
    next_cursor: str | None = None

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


@dataclass(frozen=True)
class WebhookEvent:
    """A verified webhook delivery."""

    delivery_id: str | None
    key_id: str | None
    timestamp: int
    type: str | None
    data: Any
    raw: bytes = field(repr=False, compare=False, default=b"")

    @classmethod
    def from_payload(
        cls,
        body: bytes,
        *,
        delivery_id: str | None,
        key_id: str | None,
        timestamp: int,
    ) -> WebhookEvent:
        payload = json.loads(body.decode("utf-8")) if body else None
        kind = None
        if isinstance(payload, dict):
            kind = payload.get("type") or payload.get("event")
        return cls(
            delivery_id=delivery_id,
            key_id=key_id,
            timestamp=timestamp,
            type=str(kind) if kind is not None else None,
            data=payload,
            raw=body,
        )
