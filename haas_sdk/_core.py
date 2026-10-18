"""Transport-independent plumbing shared by the sync and async clients."""

from __future__ import annotations

import os
import random
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from haas_sdk._version import __version__
from haas_sdk.errors import APIError, error_class

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TENANT = "default-tenant"
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_RETRY_AFTER = 60.0


@dataclass
class ClientOptions:
    base_url: str
    api_key: str | None
    tenant: str
    timeout: float
    max_retries: int
    backoff_base: float = 0.5
    backoff_cap: float = 8.0
    stream_max_reconnects: int = 5
    stream_reconnect_delay: float = 1.0
    default_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def resolve(
        cls,
        base_url: str | None,
        api_key: str | None,
        tenant: str | None,
        *,
        timeout: float,
        max_retries: int,
        stream_max_reconnects: int,
        stream_reconnect_delay: float,
        default_headers: dict[str, str] | None,
    ) -> ClientOptions:
        """Explicit arguments win over ``HAAS_URL`` / ``HAAS_API_KEY`` / ``HAAS_TENANT``."""
        return cls(
            base_url=(base_url or os.environ.get("HAAS_URL") or DEFAULT_BASE_URL).rstrip(
                "/"
            ),
            api_key=api_key
            or os.environ.get("HAAS_API_KEY")
            or os.environ.get("HAAS_BEARER_TOKEN")
            or None,
            tenant=tenant or os.environ.get("HAAS_TENANT") or DEFAULT_TENANT,
            timeout=timeout,
            max_retries=max(0, max_retries),
            stream_max_reconnects=max(0, stream_max_reconnects),
            stream_reconnect_delay=max(0.0, stream_reconnect_delay),
            default_headers=dict(default_headers or {}),
        )

    def headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": f"haas-sdk-python/{__version__}",
            "X-Tenant-ID": self.tenant,
            **self.default_headers,
        }
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        _inject_trace_context(headers)
        return headers


def _inject_trace_context(headers: dict[str, str]) -> None:
    """Propagate the caller's OpenTelemetry context (``traceparent``) when available."""
    try:
        from opentelemetry import propagate
    except ImportError:  # pragma: no cover - optional dependency
        return
    try:
        propagate.inject(headers)
    except Exception:  # pragma: no cover - never fail a request over tracing
        return


@dataclass
class Call:
    method: str
    path: str
    params: dict[str, Any] | None = None
    json: Any = None
    files: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    # Safe to resend after a response was lost: GET/DELETE or keyed by Idempotency-Key.
    idempotent: bool = False
    raw: bool = False


def seg(value: str) -> str:
    return quote(str(value), safe="")


def idempotency_headers(key: str | None) -> dict[str, str]:
    return {"Idempotency-Key": key or f"sdk-{uuid.uuid4()}"}


def clean(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


def retry_after_seconds(response: httpx.Response | None) -> float | None:
    if response is None:
        return None
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return min(max(float(value), 0.0), MAX_RETRY_AFTER)
    except ValueError:
        return None


def retry_delay(
    options: ClientOptions,
    call: Call,
    attempt: int,
    *,
    response: httpx.Response | None = None,
    error: Exception | None = None,
) -> float | None:
    """Seconds to wait before retry number ``attempt + 1``; ``None`` means do not retry."""
    if attempt >= options.max_retries:
        return None
    if error is not None:
        # A refused connection never reached the server, so any method may be resent.
        if not (call.idempotent or isinstance(error, httpx.ConnectError)):
            return None
    elif response is None or response.status_code not in RETRYABLE_STATUSES:
        return None
    elif not call.idempotent and response.status_code != 429:
        return None
    hinted = retry_after_seconds(response)
    if hinted is not None:
        return hinted
    # Exponential backoff with full jitter.
    return random.uniform(0, min(options.backoff_cap, options.backoff_base * 2**attempt))


def api_error(response: httpx.Response) -> APIError:
    body: Any = None
    message = response.reason_phrase or "HTTP error"
    code = None
    request_id = response.headers.get("x-request-id")
    try:
        body = response.json()
    except ValueError:
        text = response.text
        if text:
            message = text[:500]
    if isinstance(body, dict):
        detail = body.get("detail", body.get("title"))
        if isinstance(detail, str) and detail:
            message = detail
        elif detail is not None:
            message = str(detail)
        code = body.get("code")
        request_id = body.get("request_id") or request_id
    return error_class(response.status_code)(
        message,
        status=response.status_code,
        code=code if isinstance(code, str) else None,
        request_id=request_id,
        body=body,
        retry_after=retry_after_seconds(response),
    )


def stream_cursor(last_event_id: str | int | None) -> str:
    value = "" if last_event_id is None else str(last_event_id)
    return value if value.isdigit() and len(value) <= 18 else "0"
