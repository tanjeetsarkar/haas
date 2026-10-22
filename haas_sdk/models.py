"""Typed, frozen models over HaaS `/v1` resources.

Every model keeps the full JSON body in ``raw`` (excluded from ``repr`` and ``==``)
so fields added by newer servers stay reachable without an SDK upgrade (P7).
Parsing is total: missing optionals yield ``None``, unknown fields land in ``raw``,
and a malformed required field raises ``HaasError("malformed <Model>")``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Generic, Mapping, TypeVar

from haas_sdk.constants import ACTIVE_STATUSES, TERMINAL_EVENTS, TERMINAL_STATUSES
from haas_sdk.errors import HaasError

T = TypeVar("T")


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise HaasError(f"malformed number: {value!r}") from exc


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HaasError(f"malformed integer: {value!r}") from exc


def _datetime(value: Any) -> datetime | None:
    """Parse an ISO timestamp or unix epoch into an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        text = value.replace("Z", "+00:00") if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise HaasError(f"malformed timestamp: {value!r}") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    raise HaasError(f"malformed timestamp: {value!r}")


def _require(data: dict[str, Any], key: str, model: str) -> Any:
    value = data.get(key)
    if value is None:
        raise HaasError(f"malformed {model}: missing {key!r}")
    return value


def _strs(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(str(item) for item in value)


@dataclass(frozen=True, slots=True)
class Budget:
    """Truthful money view (P8): settled, uncertain and reserved stay separate."""

    max_cost_usd: float
    settled_usd: float
    uncertain_usd: float
    reserved_usd: float
    remaining_usd: float
    steps_used: int
    max_steps: int
    tool_invocations_used: int
    max_tool_invocations: int
    deadline_at: datetime | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def spent_usd(self) -> float:
        """Settled plus uncertain: lost model responses count as spent."""
        return self.settled_usd + self.uncertain_usd

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Budget:
        try:
            return cls(
                max_cost_usd=float(_require(data, "max_cost_usd", "Budget")),
                settled_usd=float(data.get("settled_usd") or 0.0),
                uncertain_usd=float(data.get("uncertain_usd") or 0.0),
                reserved_usd=float(data.get("reserved_usd") or 0.0),
                remaining_usd=float(_require(data, "remaining_usd", "Budget")),
                steps_used=int(data.get("steps_used") or 0),
                max_steps=int(_require(data, "max_steps", "Budget")),
                tool_invocations_used=int(data.get("tool_invocations_used") or 0),
                max_tool_invocations=int(data.get("max_tool_invocations") or 0),
                deadline_at=_datetime(data.get("deadline_at")),
                raw=data,
            )
        except HaasError:
            raise
        except (TypeError, ValueError) as exc:
            raise HaasError(f"malformed Budget: {exc}") from exc


@dataclass(frozen=True, slots=True)
class PendingCall:
    """A tool call awaiting a human decision (P2: this is what was reviewed)."""

    id: str
    name: str
    arguments: dict[str, Any] | str
    requires_approval: bool | None
    queue: str | None
    allowed_secrets: tuple[str, ...]
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def parsed_arguments(self) -> dict[str, Any]:
        """JSON-decode string arguments; never raises (returns ``{"_raw": s}``)."""
        if isinstance(self.arguments, dict):
            return self.arguments
        if isinstance(self.arguments, str):
            try:
                parsed = json.loads(self.arguments)
            except json.JSONDecodeError:
                return {"_raw": self.arguments}
            return parsed if isinstance(parsed, dict) else {"_raw": self.arguments}
        return {}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingCall:
        arguments = data.get("arguments")
        if arguments is not None and not isinstance(arguments, dict | str):
            arguments = str(arguments)
        return cls(
            id=str(_require(data, "id", "PendingCall")),
            name=str(_require(data, "name", "PendingCall")),
            arguments=arguments if arguments is not None else {},
            requires_approval=data.get("requires_approval"),
            queue=_str(data.get("queue")),
            allowed_secrets=_strs(data.get("allowed_secrets")),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class ApprovalRef:
    """Embedded in `Run`: binds a decision to a specific batch + digest (P2)."""

    batch_id: str
    batch_digest: str
    expires_at: datetime
    decision_endpoint: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalRef:
        expires = _datetime(_require(data, "expires_at", "ApprovalRef"))
        return cls(
            batch_id=str(_require(data, "batch_id", "ApprovalRef")),
            batch_digest=str(_require(data, "batch_digest", "ApprovalRef")),
            expires_at=expires if expires is not None else datetime.now(tz=UTC),
            decision_endpoint=str(data.get("decision_endpoint") or ""),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class Run:
    """A run as returned by `GET /v1/runs/{id}`."""

    run_id: str
    status: str
    thread_id: str | None
    result: str | None
    step_count: int | None
    total_cost_usd: float | None
    state_version: int | None
    event_sequence: int | None
    active_skills: tuple[str, ...]
    pending_calls: tuple[PendingCall, ...] | None
    approval: ApprovalRef | None
    budget: Budget | None
    deadline_at: datetime | None
    parent_run_id: str | None
    traceparent: str | None
    created_at: float | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def waiting_for_approval(self) -> bool:
        return self.status == "input_required"

    @property
    def succeeded(self) -> bool:
        return self.status == "completed"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Run:
        pending = data.get("pending_tool_calls")
        approval = data.get("approval")
        budget = data.get("budget")
        return cls(
            run_id=str(_require(data, "run_id", "Run")),
            status=str(_require(data, "status", "Run")),
            thread_id=_str(data.get("thread_id")),
            result=_str(data.get("result")),
            step_count=_int(data.get("step_count")),
            total_cost_usd=_float(data.get("total_cost_usd")),
            state_version=_int(data.get("state_version")),
            event_sequence=_int(data.get("event_sequence")),
            active_skills=_strs(data.get("active_skills")),
            pending_calls=tuple(PendingCall.from_dict(c) for c in pending)
            if isinstance(pending, list)
            else None,
            approval=ApprovalRef.from_dict(approval)
            if isinstance(approval, dict)
            else None,
            budget=Budget.from_dict(budget) if isinstance(budget, dict) else None,
            deadline_at=_datetime(data.get("deadline_at")),
            parent_run_id=_str(data.get("parent_run_id")),
            traceparent=_str(data.get("traceparent")),
            created_at=_float(data.get("created_at")),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class RunReceipt:
    """Response of `POST /v1/runs` (202)."""

    run_id: str
    thread_id: str
    status: str
    stream_url: str
    events_url: str
    replayed: bool
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunReceipt:
        return cls(
            run_id=str(_require(data, "run_id", "RunReceipt")),
            thread_id=str(data.get("thread_id") or ""),
            status=str(data.get("status") or "queued"),
            stream_url=str(data.get("stream_url") or ""),
            events_url=str(data.get("events_url") or ""),
            replayed=bool(data.get("replayed", False)),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class ApprovalReview:
    """What a human was shown, frozen with its digest (P2).

    A `Decision` can only be built from this object; there is no
    "approve whatever is pending" API.
    """

    batch_id: str
    batch_digest: str
    run_id: str
    step_number: int
    calls: tuple[PendingCall, ...]
    expires_at: datetime
    submitted_by_principal_id: str | None
    status: str
    expired: bool
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def is_pending(self) -> bool:
        return self.status == "pending" and not self.expired

    def approve(self, *, secrets: Mapping[str, str] | None = None) -> Decision:
        """Build an approve `Decision` bound to this review."""
        return Decision(review=self, approve=True, reason=None, secrets=secrets)

    def reject(self, reason: str | None = None) -> Decision:
        """Build a reject `Decision` bound to this review."""
        return Decision(review=self, approve=False, reason=reason, secrets=None)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalReview:
        calls = data.get("tool_calls") or data.get("calls") or []
        expires = _datetime(_require(data, "expires_at", "ApprovalReview"))
        return cls(
            batch_id=str(_require(data, "batch_id", "ApprovalReview")),
            batch_digest=str(_require(data, "batch_digest", "ApprovalReview")),
            run_id=str(_require(data, "run_id", "ApprovalReview")),
            step_number=int(data.get("step_number") or 0),
            calls=tuple(PendingCall.from_dict(c) for c in calls if isinstance(c, dict)),
            expires_at=expires if expires is not None else datetime.now(tz=UTC),
            submitted_by_principal_id=_str(data.get("submitted_by_principal_id")),
            status=str(data.get("status") or "pending"),
            expired=bool(data.get("expired", False)),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class Decision:
    """An intent bound to a specific review (P2)."""

    review: ApprovalReview
    approve: bool
    reason: str | None
    secrets: Mapping[str, str] | None


@dataclass(frozen=True, slots=True)
class DecisionReceipt:
    """Response of `POST /v1/approvals/{id}/decision` (202)."""

    run_id: str
    batch_id: str
    decision: str
    replayed: bool
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DecisionReceipt:
        return cls(
            run_id=str(data.get("run_id") or ""),
            batch_id=str(data.get("batch_id") or ""),
            decision=str(data.get("decision") or ""),
            replayed=bool(data.get("replayed", False)),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class Invocation:
    """A tool invocation with its `effect_key` for reconciliation."""

    invocation_id: str
    step_number: int
    ordinal: int
    tool_name: str
    status: str
    queue: str
    effect_key: str
    attempt: int
    preview: str | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def needs_reconciliation(self) -> bool:
        """`unknown` means dispatched with no recorded outcome: never retried (P3)."""
        return self.status == "unknown"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Invocation:
        return cls(
            invocation_id=str(_require(data, "invocation_id", "Invocation")),
            step_number=int(data.get("step_number") or 0),
            ordinal=int(data.get("ordinal") or 0),
            tool_name=str(data.get("tool_name") or ""),
            status=str(data.get("status") or ""),
            queue=str(data.get("queue") or ""),
            effect_key=str(data.get("effect_key") or ""),
            attempt=int(data.get("attempt") or 0),
            preview=_str(data.get("preview")),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class StepAttempt:
    """One model-call attempt within a step."""

    step_number: int
    attempt: int
    model: str
    status: str
    cost_usd: float | None
    cost_reported: bool | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    error: str | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StepAttempt:
        return cls(
            step_number=int(data.get("step_number") or 0),
            attempt=int(data.get("attempt") or 0),
            model=str(data.get("model") or ""),
            status=str(data.get("status") or ""),
            cost_usd=_float(data.get("cost_usd")),
            cost_reported=data.get("cost_reported"),
            prompt_tokens=_int(data.get("prompt_tokens")),
            completion_tokens=_int(data.get("completion_tokens")),
            cached_tokens=_int(data.get("cached_tokens")),
            error=_str(data.get("error")),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class UsageEntry:
    """One usage-ledger row."""

    unit: str
    quantity: float
    cost_usd: float
    settlement_status: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UsageEntry:
        return cls(
            unit=str(data.get("unit") or ""),
            quantity=_float(data.get("quantity")) or 0.0,
            cost_usd=_float(data.get("cost_usd")) or 0.0,
            settlement_status=str(data.get("settlement_status") or ""),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class RunSteps:
    """Response of `GET /v1/runs/{id}/steps`."""

    steps: tuple[StepAttempt, ...]
    invocations: tuple[Invocation, ...]
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunSteps:
        steps = data.get("steps") or []
        invocations = data.get("invocations") or []
        return cls(
            steps=tuple(StepAttempt.from_dict(s) for s in steps if isinstance(s, dict)),
            invocations=tuple(
                Invocation.from_dict(i) for i in invocations if isinstance(i, dict)
            ),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class RunUsage:
    """Response of `GET /v1/runs/{id}/usage`."""

    budget: Budget | None
    entries: tuple[UsageEntry, ...]
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunUsage:
        entries = data.get("entries") or []
        budget = data.get("budget")
        return cls(
            budget=Budget.from_dict(budget) if isinstance(budget, dict) else None,
            entries=tuple(
                UsageEntry.from_dict(e) for e in entries if isinstance(e, dict)
            ),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class RunEvent:
    """One durable event (SSE frame or `GET .../events` item)."""

    type: str
    data: dict[str, Any]
    sequence: int | None
    event_id: str | None = None
    occurred_at: datetime | None = None
    schema_version: int | None = None

    @property
    def is_terminal(self) -> bool:
        return self.type in TERMINAL_EVENTS

    @property
    def is_delta(self) -> bool:
        return self.type == "run.output.delta"

    @property
    def is_pause(self) -> bool:
        return self.type == "input_required"

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> RunEvent:
        """Build from a `GET /v1/runs/{id}/events` item."""
        data = record.get("data")
        if data is None:
            data = record.get("payload")
        if not isinstance(data, dict):
            data = {"value": data} if data is not None else {}
        return cls(
            type=str(record.get("type", "message")),
            data=data,
            sequence=_int(record.get("sequence")),
            event_id=_str(record.get("event_id")),
            occurred_at=_datetime(record.get("occurred_at")),
            schema_version=_int(record.get("schema_version")),
        )


@dataclass(frozen=True, slots=True)
class EventPage:
    """Response of `GET /v1/runs/{id}/events`."""

    items: tuple[RunEvent, ...]
    next_after: int
    latest_sequence: int

    @property
    def has_more(self) -> bool:
        return self.next_after < self.latest_sequence

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EventPage:
        items = data.get("items") or []
        return cls(
            items=tuple(RunEvent.from_record(i) for i in items if isinstance(i, dict)),
            next_after=int(data.get("next_after") or 0),
            latest_sequence=int(data.get("latest_sequence") or 0),
        )


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """A cursor-paged list response."""

    items: tuple[T, ...]
    next_cursor: str | None

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


@dataclass(frozen=True, slots=True)
class Identity:
    """Response of `GET /v1/me`."""

    tenant_id: str
    principal_id: str
    roles: tuple[str, ...]
    credential_type: str | None
    environment: str | None
    capabilities: Capabilities | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Identity:
        caps = data.get("capabilities")
        return cls(
            tenant_id=str(data.get("tenant_id") or ""),
            principal_id=str(data.get("principal_id") or ""),
            roles=_strs(data.get("roles")),
            credential_type=_str(data.get("credential_type")),
            environment=_str(data.get("environment")),
            capabilities=Capabilities.from_dict(caps)
            if isinstance(caps, dict)
            else None,
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Response of `GET /v1/capabilities` (UX hints only; P1)."""

    version: str
    actions: tuple[str, ...]
    event_schemas: tuple[str, ...]
    features: tuple[str, ...]
    durable_replay: bool
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def has_feature(self, feature: str) -> bool:
        return feature in self.features

    def can(self, action: str) -> bool:
        return action in self.actions

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Capabilities:
        return cls(
            version=str(data.get("version") or ""),
            actions=_strs(data.get("actions")),
            event_schemas=_strs(data.get("event_schemas")),
            features=_strs(data.get("features")),
            durable_replay=bool(data.get("durable_replay", False)),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class Thread:
    """A conversation thread."""

    thread_id: str
    history_version: int | None
    active_run_id: str | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Thread:
        return cls(
            thread_id=str(_require(data, "thread_id", "Thread")),
            history_version=_int(data.get("history_version")),
            active_run_id=_str(data.get("active_run_id")),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class ThreadHistory:
    """Response of `GET /v1/threads/{id}`: messages plus paging state."""

    thread_id: str
    history_version: int
    active_run_id: str | None
    items: tuple[dict[str, Any], ...]
    next_offset: int | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThreadHistory:
        items = data.get("items") or []
        return cls(
            thread_id=str(_require(data, "thread_id", "ThreadHistory")),
            history_version=int(_require(data, "history_version", "ThreadHistory")),
            active_run_id=_str(data.get("active_run_id")),
            items=tuple(items),
            next_offset=_int(data.get("next_offset")),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class AgentRevision:
    """An agent revision (`draft -> validated -> published`)."""

    revision_id: str
    agent_name: str
    version: int | None
    state: str | None
    digest: str | None
    configuration: dict[str, Any] | None
    policy_rules: dict[str, Any] | None
    registry_fingerprint: str | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentRevision:
        configuration = data.get("configuration")
        policy_rules = data.get("policy_rules")
        return cls(
            revision_id=str(_require(data, "revision_id", "AgentRevision")),
            agent_name=str(data.get("agent_name") or ""),
            version=_int(data.get("version")),
            state=_str(data.get("state")),
            digest=_str(data.get("digest")),
            configuration=configuration if isinstance(configuration, dict) else None,
            policy_rules=policy_rules if isinstance(policy_rules, dict) else None,
            registry_fingerprint=_str(data.get("registry_fingerprint")),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class Skill:
    """A skill from the plugin catalog."""

    name: str
    version: str | None
    description: str | None
    required_tools: tuple[str, ...]
    allowed_secrets: tuple[str, ...]
    references: tuple[str, ...]
    plugin: str | None
    category: str | None
    hidden: bool
    ready: bool
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Skill:
        return cls(
            name=str(_require(data, "name", "Skill")),
            version=_str(data.get("version")),
            description=_str(data.get("description")),
            required_tools=_strs(data.get("required_tools")),
            allowed_secrets=_strs(data.get("allowed_secrets")),
            references=_strs(data.get("references")),
            plugin=_str(data.get("plugin")),
            category=_str(data.get("category")),
            hidden=bool(data.get("hidden", False)),
            ready=bool(data.get("ready", True)),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class Tool:
    """A tool from the plugin catalog."""

    name: str
    queue: str | None
    requires_approval: bool
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Tool:
        return cls(
            name=str(_require(data, "name", "Tool")),
            queue=_str(data.get("queue")),
            requires_approval=bool(data.get("requires_approval", False)),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class PluginRecord:
    """A plugin entry from the tenant-scoped catalog."""

    id: str
    status: str | None
    version: str | None
    category: str | None
    skills: tuple[str, ...]
    tools: tuple[str, ...]
    error: str | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PluginRecord:
        return cls(
            id=str(_require(data, "id", "PluginRecord")),
            status=_str(data.get("status")),
            version=_str(data.get("version")),
            category=_str(data.get("category")),
            skills=_strs(data.get("skills")),
            tools=_strs(data.get("tools")),
            error=_str(data.get("error")),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class PluginCatalog:
    """Response of `GET /v1/plugins`."""

    fingerprint: str
    plugins: tuple[PluginRecord, ...]
    skills: tuple[Skill, ...]
    tools: tuple[Tool, ...]
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def skill(self, name: str) -> Skill:
        """Look up a skill by name; `KeyError` if absent."""
        for skill in self.skills:
            if skill.name == name:
                return skill
        raise KeyError(name)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PluginCatalog:
        plugins = data.get("plugins") or []
        skills = data.get("skills") or []
        tools = data.get("tools") or []
        return cls(
            fingerprint=str(data.get("fingerprint") or ""),
            plugins=tuple(
                PluginRecord.from_dict(p) for p in plugins if isinstance(p, dict)
            ),
            skills=tuple(Skill.from_dict(s) for s in skills if isinstance(s, dict)),
            tools=tuple(Tool.from_dict(t) for t in tools if isinstance(t, dict)),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class Membership:
    """A tenant membership row."""

    principal_id: str
    issuer: str | None
    subject: str | None
    kind: str | None
    role: str | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Membership:
        return cls(
            principal_id=str(_require(data, "principal_id", "Membership")),
            issuer=_str(data.get("issuer")),
            subject=_str(data.get("subject")),
            kind=_str(data.get("kind")),
            role=_str(data.get("role")),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class ApiKey:
    """A tenant API key (metadata only; the credential is never returned again)."""

    key_id: str
    principal_id: str | None
    key_prefix: str | None
    name: str | None
    expires_at: datetime | None
    revoked_at: datetime | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApiKey:
        return cls(
            key_id=str(_require(data, "key_id", "ApiKey")),
            principal_id=_str(data.get("principal_id")),
            key_prefix=_str(data.get("key_prefix")),
            name=_str(data.get("name")),
            expires_at=_datetime(data.get("expires_at")),
            revoked_at=_datetime(data.get("revoked_at")),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class IssuedApiKey:
    """Response of `POST /v1/admin/api-keys`: the credential is shown exactly once."""

    key_id: str
    name: str | None
    credential: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IssuedApiKey:
        return cls(
            key_id=str(_require(data, "key_id", "IssuedApiKey")),
            name=_str(data.get("name")),
            credential=str(_require(data, "credential", "IssuedApiKey")),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class PolicyRevision:
    """The active tenant policy revision."""

    policy_revision_id: str
    version: int | None
    digest: str | None
    rules: dict[str, Any] | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PolicyRevision:
        rules = data.get("rules")
        return cls(
            policy_revision_id=str(
                _require(data, "policy_revision_id", "PolicyRevision")
            ),
            version=_int(data.get("version")),
            digest=_str(data.get("digest")),
            rules=rules if isinstance(rules, dict) else None,
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One audit-log row."""

    event_id: str
    actor_principal_id: str | None
    action: str | None
    target_type: str | None
    target_id: str | None
    request_id: str | None
    details: dict[str, Any] | None
    created_at: datetime | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuditEvent:
        details = data.get("details")
        return cls(
            event_id=str(_require(data, "event_id", "AuditEvent")),
            actor_principal_id=_str(data.get("actor_principal_id")),
            action=_str(data.get("action")),
            target_type=_str(data.get("target_type")),
            target_id=_str(data.get("target_id")),
            request_id=_str(data.get("request_id")),
            details=details if isinstance(details, dict) else None,
            created_at=_datetime(data.get("created_at")),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
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
