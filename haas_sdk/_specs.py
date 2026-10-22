"""Request builders and response parsers shared by `Haas` and `AsyncHaas`.

Each builder returns an `Op` (what to send, how to read the answer) so the sync
and async clients only differ in how they perform I/O. No I/O happens here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Generic, TypeVar

import httpx

from haas_sdk._core import Call, clean, idempotency_headers, seg
from haas_sdk.constants import DECISIONS, LIMITS, PRINCIPAL_KINDS, ROLES
from haas_sdk.errors import HaasError
from haas_sdk.models import (
    AgentRevision,
    ApiKey,
    ApprovalReview,
    AuditEvent,
    DecisionReceipt,
    EventPage,
    Identity,
    IssuedApiKey,
    Membership,
    Page,
    PluginCatalog,
    PolicyRevision,
    Run,
    RunEvent,
    RunReceipt,
    RunSteps,
    RunUsage,
    Thread,
    ThreadHistory,
)

T = TypeVar("T")

MONEY = float | Decimal


@dataclass
class Op(Generic[T]):
    """A request plus its response parser."""

    call: Call
    parse: Callable[[httpx.Response], T]


def _json(response: httpx.Response) -> Any:
    if not response.content:
        return {}
    return response.json()


def _dict(response: httpx.Response) -> dict[str, Any]:
    body = _json(response)
    return body if isinstance(body, dict) else {"value": body}


def _page(
    factory: Callable[[dict[str, Any]], T],
) -> Callable[[httpx.Response], Page[T]]:
    def parse(response: httpx.Response) -> Page[T]:
        body = _dict(response)
        items = body.get("items") or []
        return Page(
            items=tuple(factory(item) for item in items if isinstance(item, dict)),
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


def _money(value: MONEY | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _secrets_body(secrets: Mapping[str, str] | None) -> dict[str, str] | None:
    """SDK-SEC-07: secrets are sent once in the body, never logged."""
    if not secrets:
        return None
    from haas_sdk.secrets import Secret

    return {k: (v.reveal() if isinstance(v, Secret) else v) for k, v in secrets.items()}


def _validate_limit(name: str, value: MONEY | int | None) -> MONEY | int | None:
    """SDK-RUN-04: client-side bounds from `constants.LIMITS`, before any request."""
    if value is None:
        return None
    low, high = LIMITS[name]
    if not low < value <= high:
        raise ValueError(f"{name} must satisfy {low} < x <= {high}, got {value}")
    return value


def validate_run_limits(
    *,
    max_steps: int | None,
    max_cost_usd: MONEY | None,
    deadline_sec: int | None,
    max_tool_invocations: int | None,
) -> None:
    _validate_limit("max_steps", max_steps)
    _validate_limit("max_cost_usd", max_cost_usd)
    _validate_limit("deadline_sec", deadline_sec)
    _validate_limit("max_tool_invocations", max_tool_invocations)


# --- identity -------------------------------------------------------------------------


def me() -> Op[Identity]:
    return Op(
        Call("GET", "/v1/me", idempotent=True), lambda r: Identity.from_dict(_dict(r))
    )


def capabilities() -> Op[Any]:
    return Op(Call("GET", "/v1/capabilities", idempotent=True), _dict)


def health(path: str = "/readyz") -> Op[dict[str, Any]]:
    return Op(Call("GET", path, idempotent=True), _dict)


# --- runs -----------------------------------------------------------------------------


def run_create(
    prompt: str,
    *,
    idempotency_key: str | None,
    extra: Mapping[str, Any],
) -> Op[RunReceipt]:
    body = clean({"prompt": prompt, **extra})
    return Op(
        Call(
            "POST",
            "/v1/runs",
            json=body,
            headers=idempotency_headers(idempotency_key),
            idempotent=True,
        ),
        lambda r: RunReceipt.from_dict(_dict(r)),
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
    return Op(
        Call("GET", "/v1/runs", params=params, idempotent=True), _page(Run.from_dict)
    )


def run_cancel(run_id: str) -> Op[dict[str, Any]]:
    # Cancelling twice is harmless (202 or 409), so retries are safe.
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
) -> Op[DecisionReceipt]:
    body = clean(
        {
            "approved": approved,
            "batch_id": batch_id,
            "batch_digest": batch_digest,
            "rejection_reason": rejection_reason,
            "secrets": _secrets_body(secrets),
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
        lambda r: DecisionReceipt.from_dict(_dict(r)),
    )


def run_events(run_id: str, *, after: int, limit: int | None) -> Op[EventPage]:
    params = clean({"after": after, "limit": limit})
    return Op(
        Call("GET", f"/v1/runs/{seg(run_id)}/events", params=params, idempotent=True),
        lambda r: EventPage.from_dict(_dict(r)),
    )


def run_subresource(run_id: str, name: str) -> Op[Any]:
    return Op(Call("GET", f"/v1/runs/{seg(run_id)}/{name}", idempotent=True), _dict)


def run_steps(run_id: str) -> Op[RunSteps]:
    return Op(
        run_subresource(run_id, "steps").call,
        lambda r: RunSteps.from_dict(_dict(r)),
    )


def run_usage(run_id: str) -> Op[RunUsage]:
    return Op(
        run_subresource(run_id, "usage").call,
        lambda r: RunUsage.from_dict(_dict(r)),
    )


def run_stream_call(
    run_id: str,
    *,
    last_event_id: str | None,
    output_deltas: bool,
) -> Call:
    headers = {"Accept": "text/event-stream"}
    if last_event_id is not None:
        headers["Last-Event-ID"] = last_event_id
    params = {"output_deltas": "true"} if output_deltas else None
    return Call(
        "GET",
        f"/v1/runs/{seg(run_id)}/stream",
        params=params,
        headers=headers,
        idempotent=True,
    )


# --- approvals ------------------------------------------------------------------------


def approval_list(
    *,
    status: str | None,
    run_id: str | None,
    limit: int | None,
    cursor: str | None,
) -> Op[Page[ApprovalReview]]:
    params = clean(
        {"status": status, "run_id": run_id, "limit": limit, "cursor": cursor}
    )
    return Op(
        Call("GET", "/v1/approvals", params=params, idempotent=True),
        _page(ApprovalReview.from_dict),
    )


def approval_get(batch_id: str) -> Op[ApprovalReview]:
    return Op(
        Call("GET", f"/v1/approvals/{seg(batch_id)}", idempotent=True),
        lambda r: ApprovalReview.from_dict(_dict(r)),
    )


def approval_decide(
    batch_id: str,
    *,
    decision: str,
    batch_digest: str,
    reason: str | None,
    secrets: Mapping[str, str] | None,
    idempotency_key: str | None,
) -> Op[DecisionReceipt]:
    if decision not in DECISIONS:
        raise ValueError(
            f"decision must be one of {sorted(DECISIONS)}, got {decision!r}"
        )
    if reason is not None and len(reason) > 2000:
        raise ValueError("reason must be at most 2000 characters (SDK-APR-05)")
    body = clean(
        {
            "decision": decision,
            "batch_digest": batch_digest,
            "reason": reason,
            "secrets": _secrets_body(secrets),
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
        lambda r: DecisionReceipt.from_dict(_dict(r)),
    )


# --- threads --------------------------------------------------------------------------


def thread_list(
    *, search: str | None, limit: int | None, cursor: str | None
) -> Op[Page[Thread]]:
    params = clean({"search": search, "limit": limit, "cursor": cursor})
    return Op(
        Call("GET", "/v1/threads", params=params, idempotent=True),
        _page(Thread.from_dict),
    )


def thread_get(
    thread_id: str,
    *,
    offset: int | None,
    limit: int | None,
    history_version: int | None,
) -> Op[ThreadHistory]:
    params = clean(
        {"offset": offset, "limit": limit, "history_version": history_version}
    )
    return Op(
        Call("GET", f"/v1/threads/{seg(thread_id)}", params=params, idempotent=True),
        lambda r: ThreadHistory.from_dict(_dict(r)),
    )


def thread_branch(thread_id: str, *, expected_history_version: int) -> Op[Thread]:
    # Not auto-retried: the server does not support an idempotency key here (§A).
    return Op(
        Call(
            "POST",
            f"/v1/threads/{seg(thread_id)}/branches",
            json={"expected_history_version": expected_history_version},
        ),
        lambda r: Thread.from_dict(_dict(r)),
    )


# --- agents ---------------------------------------------------------------------------


def agent_list(
    *,
    agent_name: str | None,
    state: str | Iterable[str] | None,
    limit: int | None,
    cursor: str | None,
) -> Op[Page[AgentRevision]]:
    params = clean(
        {
            "agent_name": agent_name,
            "state": _multi(state),
            "limit": limit,
            "cursor": cursor,
        }
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


def agent_create(
    agent_name: str, configuration: Mapping[str, Any]
) -> Op[AgentRevision]:
    return Op(
        Call(
            "POST",
            "/v1/agents/revisions",
            json={"agent_name": agent_name, "configuration": dict(configuration)},
        ),
        lambda r: AgentRevision.from_dict(_dict(r)),
    )


def agent_action(revision_id: str, operation: str) -> Op[AgentRevision]:
    return Op(
        Call(
            "POST",
            f"/v1/agents/revisions/{seg(revision_id)}/{seg(operation)}",
            json={},
        ),
        lambda r: AgentRevision.from_dict(_dict(r)),
    )


# --- plugins --------------------------------------------------------------------------


def plugin_catalog() -> Op[PluginCatalog]:
    return Op(
        Call("GET", "/v1/plugins", idempotent=True),
        lambda r: PluginCatalog.from_dict(_dict(r)),
    )


# --- admin ----------------------------------------------------------------------------


def admin_memberships_list() -> Op[list[Membership]]:
    def parse(response: httpx.Response) -> list[Membership]:
        body = _dict(response)
        items = body.get("items") or []
        return [Membership.from_dict(i) for i in items if isinstance(i, dict)]

    return Op(Call("GET", "/v1/admin/memberships", idempotent=True), parse)


def admin_memberships_grant(*, subject: str, role: str, kind: str) -> Op[Membership]:
    if role not in ROLES:
        raise ValueError(f"role must be one of {sorted(ROLES)}, got {role!r}")
    if kind not in PRINCIPAL_KINDS:
        raise ValueError(f"kind must be one of {sorted(PRINCIPAL_KINDS)}, got {kind!r}")
    return Op(
        Call(
            "POST",
            "/v1/admin/memberships",
            json={"subject": subject, "role": role, "kind": kind},
        ),
        lambda r: Membership.from_dict(_dict(r)),
    )


def admin_memberships_revoke(principal_id: str, role: str) -> Op[None]:
    if role not in ROLES:
        raise ValueError(f"role must be one of {sorted(ROLES)}, got {role!r}")
    return Op(
        Call("DELETE", f"/v1/admin/memberships/{seg(principal_id)}/{seg(role)}"),
        lambda _r: None,
    )


def admin_api_keys_list() -> Op[list[ApiKey]]:
    def parse(response: httpx.Response) -> list[ApiKey]:
        body = _dict(response)
        items = body.get("items") or []
        return [ApiKey.from_dict(i) for i in items if isinstance(i, dict)]

    return Op(Call("GET", "/v1/admin/api-keys", idempotent=True), parse)


def admin_api_keys_create(
    *, principal_id: str, name: str, expires_in_days: int
) -> Op[IssuedApiKey]:
    if not 1 <= expires_in_days <= 365:
        raise ValueError("expires_in_days must be between 1 and 365")
    return Op(
        Call(
            "POST",
            "/v1/admin/api-keys",
            json=clean(
                {
                    "principal_id": principal_id,
                    "name": name,
                    "expires_in_days": expires_in_days,
                }
            ),
        ),
        lambda r: IssuedApiKey.from_dict(_dict(r)),
    )


def admin_api_keys_revoke(key_id: str) -> Op[None]:
    return Op(Call("DELETE", f"/v1/admin/api-keys/{seg(key_id)}"), lambda _r: None)


def admin_policy_get() -> Op[PolicyRevision | None]:
    def parse(response: httpx.Response) -> PolicyRevision | None:
        body = _dict(response)
        active = body.get("active")
        return PolicyRevision.from_dict(active) if isinstance(active, dict) else None

    return Op(Call("GET", "/v1/admin/policy", idempotent=True), parse)


def admin_policy_activate(rules: Mapping[str, Any]) -> Op[PolicyRevision]:
    return Op(
        Call("PUT", "/v1/admin/policy", json={"rules": dict(rules)}),
        lambda r: PolicyRevision.from_dict(_dict(r)),
    )


def admin_audit_list(*, limit: int, cursor: str | None) -> Op[Page[AuditEvent]]:
    params = clean({"limit": limit, "cursor": cursor})
    return Op(
        Call("GET", "/v1/audit-events", params=params, idempotent=True),
        _page(AuditEvent.from_dict),
    )


# --- helpers --------------------------------------------------------------------------


def parse_events(response: httpx.Response) -> list[RunEvent]:
    """Parse a plain list of event records (used by tests and the fake server)."""
    body = _json(response)
    if not isinstance(body, list):
        raise HaasError("expected a JSON array of events")
    return [RunEvent.from_record(item) for item in body if isinstance(item, dict)]
