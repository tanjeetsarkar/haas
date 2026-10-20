"""Webhook signing and delivery utilities.

Implements Stripe-style HMAC-SHA256 signing for webhook deliveries.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings


SIGNATURE_HEADER = "HaaS-Signature"
WEBHOOK_ID_HEADER = "HaaS-Webhook-Id"
KEY_ID_HEADER = "HaaS-Key-Id"

ENVELOPE_VERSION = "v1"

def _signing_key_bytes() -> bytes:
    """Get the AES-256 key for signing key envelopes."""
    encoded = settings.secret_encryption_key
    if not encoded:
        raise ValueError(
            "HAAS_SECRET_ENCRYPTION_KEY must be configured to store secrets"
        )
    try:
        key = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(
            "HAAS_SECRET_ENCRYPTION_KEY must be base64-encoded 32-byte AES-256 key"
        ) from exc
    if len(key) != 32:
        raise ValueError(
            "HAAS_SECRET_ENCRYPTION_KEY must be base64-encoded 32-byte AES-256 key"
        )
    return key

def decrypt_signing_key(envelope: str) -> str:
    """Decrypt a webhook signing key envelope.
    
    The envelope format is: v1:base64(nonce || ciphertext)
    Unlike run secrets, these use a fixed AAD.
    """
    version, separator, encoded = envelope.partition(":")
    if not separator or version != ENVELOPE_VERSION:
        raise ValueError("Unsupported encrypted secret envelope")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid encrypted secret envelope") from exc
    if len(payload) < 12 + 16:
        raise ValueError("Invalid encrypted secret envelope")

    # Use fixed AAD for signing keys (not bound to a run)
    aad = b"haas:webhook:signing_key"
    plaintext = AESGCM(_signing_key_bytes()).decrypt(payload[:12], payload[12:], aad)
    return plaintext.decode("utf-8")


def compute_signature(payload: bytes, secret: str, timestamp: int) -> str:
    """Compute HMAC-SHA256 signature for webhook payload.
    
    Format: t=<timestamp>,v1=<hex>
    """
    key = secret.encode("utf-8")
    msg = f"{timestamp}.".encode() + payload
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def build_signature_header(payload: bytes, secrets: list[str], timestamp: int | None = None) -> str:
    """Build HaaS-Signature header value with multiple secrets for rotation."""
    ts = int(time.time()) if timestamp is None else int(timestamp)
    parts = [f"t={ts}"]
    for secret in secrets:
        sig = compute_signature(payload, secret, ts)
        parts.append(f"v1={sig}")
    return ",".join(parts)


def parse_signature_header(header: str) -> tuple[int, list[str]]:
    """Parse HaaS-Signature header into timestamp and list of v1 signatures."""
    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        name, _, value = part.strip().partition("=")
        if name == "t":
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise ValueError("invalid signature timestamp") from exc
        elif name == "v1" and value:
            signatures.append(value.strip().lower())
    if timestamp is None:
        raise ValueError("signature header has no timestamp")
    if not signatures:
        raise ValueError("signature header has no v1 signature")
    return timestamp, signatures


def verify_signature(
    payload: bytes,
    header: str | None,
    secrets: list[str],
    *,
    tolerance: int = 300,
    now: float | None = None,
) -> int:
    """Verify webhook signature.
    
    Raises ValueError if invalid. Returns the signed timestamp.
    """
    if not header:
        raise ValueError(f"missing {SIGNATURE_HEADER} header")
    timestamp, signatures = parse_signature_header(header)
    current = time.time() if now is None else now
    if tolerance > 0 and abs(current - timestamp) > tolerance:
        raise ValueError("signature timestamp outside the tolerance window")
    for secret in secrets:
        expected = compute_signature(payload, secret, timestamp)
        matched = False
        for candidate in signatures:
            # Evaluate every candidate so timing does not reveal which one matched.
            matched |= hmac.compare_digest(expected, candidate)
        if matched:
            return timestamp
    raise ValueError("no matching signature")


def get_active_signing_keys(conn, tenant_id: str) -> list[tuple[str, str]]:
    """Get active signing keys for a tenant.
    
    Returns list of (key_id, secret_envelope) tuples.
    """
    rows = conn.execute(
        """
        SELECT key_id, secret_envelope
        FROM webhook_signing_keys
        WHERE tenant_id = $1 AND status = 'active' AND (retire_at IS NULL OR retire_at > now())
        ORDER BY created_at DESC
        """,
        (tenant_id,),
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def create_delivery_record(
    conn,
    tenant_id: str,
    run_id: str | None,
    endpoint_id: str,
    event_type: str,
    url: str,
    body: dict[str, Any],
    max_attempts: int = 5,
) -> str:
    """Create a webhook delivery record."""
    import uuid
    delivery_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO webhook_deliveries (
            delivery_id, tenant_id, run_id, endpoint_id, event_type, url, body,
            max_attempts, next_attempt_at, restore_epoch
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, now(), 
            (SELECT restore_epoch FROM platform_state WHERE singleton)
        )
        """,
        (delivery_id, tenant_id, run_id, endpoint_id, event_type, url, json.dumps(body), max_attempts),
    )
    return delivery_id


def record_delivery_attempt(
    conn,
    delivery_id: str,
    tenant_id: str,
    attempt: int,
    outcome: str,
    status_code: int | None,
    latency_ms: int | None,
    error: str | None,
    key_ids: list[str],
    restore_epoch: int,
) -> None:
    """Record a webhook delivery attempt."""
    conn.execute(
        """
        INSERT INTO webhook_delivery_attempts (
            delivery_id, attempt, tenant_id, outcome, status_code, latency_ms,
            error, key_ids, restore_epoch
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
        (delivery_id, attempt, tenant_id, outcome, status_code, latency_ms,
         error, key_ids, restore_epoch),
    )


def update_delivery_status(
    conn,
    delivery_id: str,
    status: str,
    attempt: int | None = None,
    next_attempt_at: str | None = None,
    status_code: int | None = None,
    error: str | None = None,
    latency_ms: int | None = None,
    completed_at: str | None = None,
) -> None:
    """Update webhook delivery status."""
    sets = ["status = $2", "updated_at = now()"]
    params = [delivery_id, status]
    idx = 3
    if attempt is not None:
        sets.append(f"attempts = ${idx}")
        params.append(str(attempt))
        idx += 1
    if next_attempt_at is not None:
        sets.append(f"next_attempt_at = ${idx}")
        params.append(next_attempt_at)
        idx += 1
    if status_code is not None:
        sets.append(f"last_status_code = ${idx}")
        params.append(str(status_code))
        idx += 1
    if error is not None:
        sets.append(f"last_error = ${idx}")
        params.append(error)
        idx += 1
    if latency_ms is not None:
        sets.append(f"last_latency_ms = ${idx}")
        params.append(str(latency_ms))
        idx += 1
    if completed_at is not None:
        sets.append(f"completed_at = ${idx}")
        params.append(completed_at)
        idx += 1
    conn.execute(
        f"UPDATE webhook_deliveries SET {', '.join(sets)} WHERE delivery_id = $1",
        *params,
    )
