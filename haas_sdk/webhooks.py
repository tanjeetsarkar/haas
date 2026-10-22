"""Webhook signature verification (stable, §13).

Deliveries carry ``HaaS-Webhook-Id``, ``HaaS-Key-Id`` and
``HaaS-Signature: t=<unix>,v1=<hex>`` where ``hex = HMAC-SHA256(secret, f"{t}." + body)``.
During secret rotation the header may contain several ``v1=`` values; any match is
accepted. Verify against the *raw* request body, before JSON parsing (SDK-WH-01).

Server deliveries are unsigned today (§13.1): treat callbacks as hints and
reconcile with `runs.get(run_id)` before acting.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Iterable, Mapping

from haas_sdk.errors import WebhookVerificationError
from haas_sdk.models import WebhookEvent

SIGNATURE_HEADER = "HaaS-Signature"
WEBHOOK_ID_HEADER = "HaaS-Webhook-Id"
KEY_ID_HEADER = "HaaS-Key-Id"
DEFAULT_TOLERANCE = 300

Secrets = str | bytes | Iterable[str | bytes]


def _secrets(secrets: Secrets) -> list[bytes]:
    items = [secrets] if isinstance(secrets, str | bytes) else list(secrets)
    out = [s.encode("utf-8") if isinstance(s, str) else bytes(s) for s in items]
    if not out or not all(out):
        raise ValueError("at least one non-empty webhook secret is required")
    return out


def _body(payload: str | bytes) -> bytes:
    return payload.encode("utf-8") if isinstance(payload, str) else payload


def compute_signature(payload: str | bytes, secret: str | bytes, timestamp: int) -> str:
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    return hmac.new(
        key, f"{timestamp}.".encode() + _body(payload), hashlib.sha256
    ).hexdigest()


def sign(
    payload: str | bytes, secrets: Secrets, *, timestamp: int | None = None
) -> str:
    """Build a ``HaaS-Signature`` header value (tests and local tooling)."""
    ts = int(time.time()) if timestamp is None else int(timestamp)
    parts = [f"t={ts}"] + [
        f"v1={compute_signature(payload, s, ts)}" for s in _secrets(secrets)
    ]
    return ",".join(parts)


def parse_signature_header(header: str) -> tuple[int, list[str]]:
    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        name, _, value = part.strip().partition("=")
        if name == "t":
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise WebhookVerificationError("invalid signature timestamp") from exc
        elif name == "v1" and value:
            signatures.append(value.strip().lower())
    if timestamp is None:
        raise WebhookVerificationError("signature header has no timestamp")
    if not signatures:
        raise WebhookVerificationError("signature header has no v1 signature")
    return timestamp, signatures


def verify_signature(
    payload: str | bytes,
    header: str | None,
    secrets: Secrets,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    now: float | None = None,
) -> int:
    """Raise `WebhookVerificationError` unless valid; return the signed timestamp."""
    if not header:
        raise WebhookVerificationError(f"missing {SIGNATURE_HEADER} header")
    timestamp, signatures = parse_signature_header(header)
    current = time.time() if now is None else now
    # SDK-WH-03: tolerance 0 disables the timestamp check (documented as unsafe).
    if tolerance > 0 and abs(current - timestamp) > tolerance:
        raise WebhookVerificationError(
            "signature timestamp outside the tolerance window"
        )
    for secret in _secrets(secrets):
        expected = compute_signature(payload, secret, timestamp)
        matched = False
        for candidate in signatures:
            # SDK-WH-02: evaluate every candidate so timing reveals nothing.
            matched |= hmac.compare_digest(expected, candidate)
        if matched:
            return timestamp
    raise WebhookVerificationError("no matching signature")


def verify(
    payload: str | bytes,
    header: str | None,
    secrets: Secrets,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    now: float | None = None,
) -> bool:
    try:
        verify_signature(payload, header, secrets, tolerance=tolerance, now=now)
    except WebhookVerificationError:
        return False
    return True


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def construct_event(
    payload: str | bytes,
    headers: Mapping[str, str],
    secrets: Secrets,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    now: float | None = None,
) -> WebhookEvent:
    """Verify a delivery and return the parsed `WebhookEvent` (SDK-WH-05)."""
    body = _body(payload)
    timestamp = verify_signature(
        body, _header(headers, SIGNATURE_HEADER), secrets, tolerance=tolerance, now=now
    )
    try:
        return WebhookEvent.from_payload(
            body,
            delivery_id=_header(headers, WEBHOOK_ID_HEADER),
            key_id=_header(headers, KEY_ID_HEADER),
            timestamp=timestamp,
        )
    except ValueError as exc:
        raise WebhookVerificationError("webhook body is not valid JSON") from exc
