"""Signed tool execution envelopes (SDD §5.4, threat TM-12/SEC-06).

The orchestrator signs each tool dispatch with an Ed25519 key; tool workers hold only the
public key, so a broker writer cannot forge or alter a tool execution request.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from app.core.config import Settings, settings

ENVELOPE_VERSION = 1
DEV_SIGNING_SEED_B64 = base64.b64encode(
    hashlib.sha256(b"haas-development-envelope-key").digest()
).decode()


class EnvelopeError(Exception):
    """The tool envelope is malformed, expired, or carries an invalid signature."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(value: Any) -> str:
    data = value if isinstance(value, bytes) else canonical_json(value)
    return hashlib.sha256(data).hexdigest()


def _decode_key(value: str, name: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise EnvelopeError(f"{name} must be base64.") from exc
    if len(raw) != 32:
        raise EnvelopeError(f"{name} must decode to 32 bytes.")
    return raw


def _private_key(config: Settings) -> Ed25519PrivateKey:
    seed = config.envelope_signing_key or (
        DEV_SIGNING_SEED_B64 if config.haas_profile == "dev" else None
    )
    if seed is None:
        raise EnvelopeError("HAAS_ENVELOPE_SIGNING_KEY is not configured.")
    return Ed25519PrivateKey.from_private_bytes(
        _decode_key(seed, "HAAS_ENVELOPE_SIGNING_KEY")
    )


def _public_key(config: Settings) -> Ed25519PublicKey:
    if config.envelope_verify_key:
        return Ed25519PublicKey.from_public_bytes(
            _decode_key(config.envelope_verify_key, "HAAS_ENVELOPE_VERIFY_KEY")
        )
    if config.haas_profile != "dev":
        raise EnvelopeError("HAAS_ENVELOPE_VERIFY_KEY is not configured.")
    return _private_key(config).public_key()


def dev_verify_key_b64() -> str:
    key = Ed25519PrivateKey.from_private_bytes(
        base64.b64decode(DEV_SIGNING_SEED_B64)
    ).public_key()
    return base64.b64encode(key.public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def arguments_digest(tool_name: str, arguments: str) -> str:
    return sha256_hex({"name": tool_name, "arguments": arguments})


def sign_envelope(body: dict[str, Any], config: Settings = settings) -> dict[str, Any]:
    body = {**body, "v": ENVELOPE_VERSION}
    signature = _private_key(config).sign(canonical_json(body))
    return {"body": body, "signature": base64.b64encode(signature).decode()}


def verify_envelope(
    envelope: dict[str, Any],
    config: Settings = settings,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    try:
        body = envelope["body"]
        signature = base64.b64decode(envelope["signature"], validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise EnvelopeError("Malformed tool envelope.") from exc
    if not isinstance(body, dict) or body.get("v") != ENVELOPE_VERSION:
        raise EnvelopeError("Unsupported tool envelope version.")
    try:
        _public_key(config).verify(signature, canonical_json(body))
    except InvalidSignature as exc:
        raise EnvelopeError("Tool envelope signature is invalid.") from exc
    if float(body.get("expires_at", 0)) < (time.time() if now is None else now):
        raise EnvelopeError("Tool envelope has expired.")
    tool_call = body.get("tool_call") or {}
    if arguments_digest(
        str(tool_call.get("name", "")), str(tool_call.get("arguments", ""))
    ) != body.get("arguments_digest"):
        raise EnvelopeError("Tool envelope arguments digest does not match.")
    return body


def generate_keypair() -> tuple[str, str]:
    """Returns (signing seed, verify key) as base64 for HAAS_ENVELOPE_* settings."""
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(seed).decode(), base64.b64encode(public).decode()


if __name__ == "__main__":
    signing, verify = generate_keypair()
    print("# Orchestrator worker only:")
    print(f"HAAS_ENVELOPE_SIGNING_KEY={signing}")
    print("# Tool workers only:")
    print(f"HAAS_ENVELOPE_VERIFY_KEY={verify}")
