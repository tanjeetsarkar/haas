"""Exception hierarchy and error parsing for the HaaS SDK.

`api_error()` handles all three server body shapes (SDK-ERR-01):

1. RFC 9457 problem: ``{type,title,status,detail,instance,errors?}``.
2. OpenAI facade envelope: ``{"error": {message,type,param,code,haas?}}``.
3. Non-JSON: truncated text (500 chars).
"""

from __future__ import annotations

import json
from typing import Any


class HaasError(Exception):
    """Base class for every SDK error."""


class ConfigError(HaasError):
    """Invalid client configuration (bad base URL, conflicting credentials, ...)."""


class IncompatibleServerError(HaasError):
    """`require_compat()` failed: the server does not speak `/v1` as this SDK expects."""


class HaasConnectionError(HaasError):
    """The API could not be reached (raised after retries are exhausted)."""


class APIError(HaasError):
    """The API answered with a non-2xx response."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        code: str | None = None,
        request_id: str | None = None,
        body: Any = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id
        self.body = body
        self.retry_after = retry_after

    def __str__(self) -> str:
        suffix = f" (request_id={self.request_id})" if self.request_id else ""
        return f"{self.status}: {self.args[0]}{suffix}"

    def __repr__(self) -> str:
        from haas_sdk.secrets import redact

        return f"{type(self).__name__}({redact(str(self))!r})"


class BadRequestError(APIError):
    """400."""


class AuthenticationError(APIError):
    """401. The SDK clears its credential and never retries a 401 (§6.6)."""


class PermissionDeniedError(APIError):
    """403 (includes separation-of-duties refusals)."""


class NotFoundError(APIError):
    """404. Also returned for foreign-tenant resources, which are not discoverable."""


class ConflictError(APIError):
    """409."""


class ThreadBusyError(ConflictError):
    """The thread has an active run. Wait, cancel it, or branch."""


class HistoryChangedError(ConflictError):
    """Thread history changed while paging or branching; refresh and retry."""


class ApprovalStaleError(ConflictError):
    """The approval batch is stale or its digest does not match what was reviewed."""


class ApprovalExpiredError(ConflictError):
    """The approval batch expired."""


class ApprovalAlreadyDecidedError(ConflictError):
    """There is no pending approval batch (someone already decided, or the run moved on)."""


class PolicyChangedError(ConflictError):
    """The tenant policy changed since the draft/decision was prepared."""


class IdempotencyConflictError(ConflictError):
    """The Idempotency-Key was already used with a different request."""


class ValidationError(APIError):
    """422."""


class RateLimitError(APIError):
    """429. `.retry_after` carries the server hint (capped at 60 s)."""


class ServerError(APIError):
    """5xx."""


class StreamClosedError(HaasError):
    """The server sent `stream_closed` (authorization revoked or expired)."""


class WebhookVerificationError(HaasError):
    """A webhook delivery failed signature, timestamp or payload verification."""


class RunFailed(HaasError):
    """A run reached a failed or cancelled terminal state."""

    def __init__(self, run: Any, code: str | None) -> None:
        self.run = run
        self.code = code
        super().__init__(f"run {run.run_id} failed: {code or 'unknown'}")


class ApprovalRequired(HaasError):
    """`wait()` hit `input_required` with no approval handler."""

    def __init__(self, run: Any) -> None:
        self.run = run
        super().__init__(f"run {run.run_id} is waiting for approval")


# --- 409 subclass selection (SDK-ERR-01) ----------------------------------------------
# Selected by `code` when the body carries one, else by `detail` prefix. One test per row.
_CONFLICT_PREFIXES: tuple[tuple[str, type[ConflictError]], ...] = (
    ("Thread has an active run", ThreadBusyError),
    ("Thread history changed", HistoryChangedError),
    ("Approval batch is stale", ApprovalStaleError),
    ("Approval batch digest does not match", ApprovalStaleError),
    ("Approval batch expired", ApprovalExpiredError),
    ("No pending approval batch", ApprovalAlreadyDecidedError),
    ("Policy changed", PolicyChangedError),
    ("Tenant policy changed", PolicyChangedError),
    ("Idempotency-Key was already used", IdempotencyConflictError),
    ("Idempotency-Key was used for a different decision", IdempotencyConflictError),
)

_CONFLICT_CODES: dict[str, type[ConflictError]] = {
    "thread_busy": ThreadBusyError,
    "history_changed": HistoryChangedError,
    "approval_stale": ApprovalStaleError,
    "approval_expired": ApprovalExpiredError,
    "approval_already_decided": ApprovalAlreadyDecidedError,
    "policy_changed": PolicyChangedError,
    "idempotency_conflict": IdempotencyConflictError,
}

_BY_STATUS: dict[int, type[APIError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: ValidationError,
    429: RateLimitError,
}


def error_class(status: int) -> type[APIError]:
    if status >= 500:
        return ServerError
    return _BY_STATUS.get(status, APIError)


def _extract(body: Any) -> tuple[str | None, str | None]:
    """Return ``(message, code)`` from a parsed JSON body, or ``(None, None)``."""
    if not isinstance(body, dict):
        return None, None
    envelope = body.get("error")
    if isinstance(envelope, dict):  # OpenAI facade envelope
        message = envelope.get("message")
        code = envelope.get("code")
        return (
            message if isinstance(message, str) and message else None,
            code if isinstance(code, str) and code else None,
        )
    detail = body.get("detail", body.get("title", body.get("message")))
    if isinstance(detail, (list, dict)):  # FastAPI validation error: {"detail": [...]}
        detail = json.dumps(detail)
    message = str(detail) if detail is not None else None
    code = body.get("code")
    return message, code if isinstance(code, str) and code else None


def _conflict_class(
    status: int, code: str | None, message: str | None
) -> type[APIError]:
    if status != 409:
        return error_class(status)
    if code is not None:
        mapped = _CONFLICT_CODES.get(code)
        if mapped is not None:
            return mapped
    if message is not None:
        for prefix, cls in _CONFLICT_PREFIXES:
            if message.startswith(prefix):
                return cls
    return ConflictError


def api_error(response: Any) -> APIError:
    """Build the most specific exception from an HTTP response (SDK-ERR-01)."""
    status = response.status_code
    request_id = response.headers.get("x-request-id")
    body: Any = None
    message: str | None = None
    code: str | None = None
    try:
        body = response.json()
    except Exception:
        text = (response.text or "").strip()
        if text:
            message = text[:500]
    if body is not None:
        parsed_message, parsed_code = _extract(body)
        message = parsed_message or message or (response.reason_phrase or "HTTP error")
        code = parsed_code
        body_request_id = body.get("request_id")
        if isinstance(body_request_id, str) and body_request_id:
            request_id = body_request_id
    final_message = message or response.reason_phrase or "HTTP error"
    cls = _conflict_class(status, code, final_message)
    return cls(
        final_message,
        status=status,
        code=code,
        request_id=request_id,
        body=body,
        retry_after=_retry_after(response),
    )


def _retry_after(response: Any) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def run_error_code(raw: Any) -> str | None:
    """Normalize a `run_error.code` payload value against the stable catalog.

    Unknown codes pass through unchanged (P7: `/v1` is additive).
    """
    if not isinstance(raw, str) or not raw:
        return None
    return raw
