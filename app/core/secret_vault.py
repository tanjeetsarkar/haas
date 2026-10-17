from __future__ import annotations

import base64
import binascii
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

ENVELOPE_VERSION = "v1"


def _key_bytes() -> bytes:
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


def encrypt_secrets(run_id: str, secrets: dict[str, str]) -> str:
    nonce = os.urandom(12)
    aad = f"haas:secrets:{run_id}".encode()
    ciphertext = AESGCM(_key_bytes()).encrypt(
        nonce, json.dumps(secrets, separators=(",", ":")).encode(), aad
    )
    envelope = base64.b64encode(nonce + ciphertext).decode("ascii")
    return f"{ENVELOPE_VERSION}:{envelope}"


def decrypt_secrets(run_id: str, envelope: str) -> dict[str, str]:
    version, separator, encoded = envelope.partition(":")
    if not separator or version != ENVELOPE_VERSION:
        raise ValueError("Unsupported encrypted secret envelope")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid encrypted secret envelope") from exc
    if len(payload) < 12 + 16:
        raise ValueError("Invalid encrypted secret envelope")

    aad = f"haas:secrets:{run_id}".encode()
    plaintext = AESGCM(_key_bytes()).decrypt(payload[:12], payload[12:], aad)
    value = json.loads(plaintext)
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(secret, str)
        for key, secret in value.items()
    ):
        raise ValueError("Encrypted secret payload must be an object of strings")
    return value
