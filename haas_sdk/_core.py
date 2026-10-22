"""Transport-independent plumbing shared by the sync and async clients."""

from __future__ import annotations

import os
import random
import re
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from haas_sdk._version import __version__
from haas_sdk.errors import ConfigError, api_error
from haas_sdk.secrets import Secret

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TENANT = "default-tenant"
RETRYABLE_STATUSES = frozenset({408, 425, 500, 502, 503, 504})
MAX_RETRY_AFTER = 60.0
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")


@dataclass
class ClientOptions:
    """Resolved client configuration (explicit arguments beat environment)."""

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
    allow_test_keys: bool = False

    @classmethod
    def resolve(
        cls,
        base_url: str | None,
        api_key: str | Secret | None,
        tenant: str | None,
        *,
        timeout: float,
        max_retries: int,
        stream_max_reconnects: int,
        stream_reconnect_delay: float,
        default_headers: dict[str, str] | None,
        allow_test_keys: bool = False,
    ) -> ClientOptions:
        """Apply SDK-CFG-01..05: arguments override env; validate everything early."""
        resolved_url = _validate_base_url(base_url or os.environ.get("HAAS_URL"))
        env_api = os.environ.get("HAAS_API_KEY")
        env_bearer = os.environ.get("HAAS_BEARER_TOKEN")
        if env_api and env_bearer and env_api != env_bearer:
            raise ConfigError(
                "HAAS_API_KEY and HAAS_BEARER_TOKEN are both set to different values; "
                "set only one."
            )
        resolved_key = api_key if api_key is not None else env_api or env_bearer
        if isinstance(resolved_key, Secret):
            resolved_key = resolved_key.reveal()
        resolved_key = resolved_key or None
        _check_test_key(resolved_key, resolved_url, allow_test_keys)
        return cls(
            base_url=resolved_url,
            api_key=resolved_key,
            tenant=tenant or os.environ.get("HAAS_TENANT") or DEFAULT_TENANT,
            timeout=timeout,
            max_retries=max(0, max_retries),
            stream_max_reconnects=max(0, stream_max_reconnects),
            stream_reconnect_delay=max(0.0, stream_reconnect_delay),
            default_headers=dict(default_headers or {}),
            allow_test_keys=allow_test_keys,
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


def _validate_base_url(value: str | None) -> str:
    """SDK-CFG-03: absolute http(s), no credentials, path, query or fragment."""
    raw = value or DEFAULT_BASE_URL
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"base_url must be an absolute http(s) URL, got {raw!r}")
    if parts.username or parts.password:
        raise ConfigError("base_url must not contain credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ConfigError("base_url must not contain a path, query or fragment")
    return raw.rstrip("/")


def _is_loopback(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1", "[::1]")


def _check_test_key(api_key: str | None, base_url: str, allow_test_keys: bool) -> None:
    """SDK-CFG-05: `hk_test_` keys are rejected against non-loopback https hosts."""
    if api_key is None or not api_key.startswith("hk_test_") or allow_test_keys:
        return
    parts = urlsplit(base_url)
    if parts.scheme == "https" and not _is_loopback(parts.hostname or ""):
        raise ConfigError(
            "test keys are rejected in production (https non-loopback base URL); "
            "pass allow_test_keys=True to override"
        )


def _inject_trace_context(headers: dict[str, str]) -> None:
    """Propagate the caller's OpenTelemetry context (`traceparent`) when available."""
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
    """A single API operation: what to send and whether resending is safe."""

    method: str
    path: str
    params: dict[str, Any] | None = None
    json: Any = None
    files: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    # Safe to resend after a lost response: GET/DELETE, or keyed by Idempotency-Key.
    idempotent: bool = False


def seg(value: str) -> str:
    """URL-encode one path segment."""
    return quote(str(value), safe="")


def idempotency_headers(key: str | None) -> dict[str, str]:
    """SDK-NET/P9: generate `sdk-<uuid4>` once per logical call, reuse on retry."""
    if key is not None:
        if not IDEMPOTENCY_KEY_RE.fullmatch(key):
            raise ValueError("idempotency_key must match ^[A-Za-z0-9._:-]{1,255}$")
        return {"Idempotency-Key": key}
    return {"Idempotency-Key": f"sdk-{uuid.uuid4()}"}


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
    """Seconds to wait before retry number ``attempt + 1``; ``None`` means do not retry.

    Shared by sync and async clients (SDK-NET-01). 429 is always retried: the
    request was rejected, not executed. Other retryables only when idempotent.
    """
    if attempt >= options.max_retries:
        return None
    if error is not None:
        # A refused connection never reached the server, so any method may be resent.
        if not (call.idempotent or isinstance(error, httpx.ConnectError)):
            return None
    else:
        if response is None or response.status_code not in RETRYABLE_STATUSES:
            if response is None or response.status_code != 429:
                return None
        elif not call.idempotent and response.status_code != 429:
            return None
    hinted = retry_after_seconds(response)
    if hinted is not None:
        return hinted
    # Exponential backoff with full jitter.
    return random.uniform(
        0, min(options.backoff_cap, options.backoff_base * 2**attempt)
    )


def stream_cursor(last_event_id: str | int | None) -> str:
    """Normalize a `Last-Event-ID` value; non-numeric cursors become "0" (§10.3)."""
    value = "" if last_event_id is None else str(last_event_id)
    return value if value.isdigit() and len(value) <= 18 else "0"


__all__ = [
    "Call",
    "ClientOptions",
    "DEFAULT_BASE_URL",
    "DEFAULT_TENANT",
    "IDEMPOTENCY_KEY_RE",
    "REQUEST_ID_RE",
    "RETRYABLE_STATUSES",
    "api_error",
    "clean",
    "idempotency_headers",
    "retry_after_seconds",
    "retry_delay",
    "seg",
    "stream_cursor",
]
