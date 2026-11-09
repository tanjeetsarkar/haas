"""Dedicated request-bound worker identity. No tenant, envelope-key or dev fallback."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Protocol

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key

from app.core.config import Settings
from app.core.run_restrictions import canonical_arguments

ISSUER = SUBJECT = "haas:tool-worker"
AUDIENCE = "haas:internal:run-authorizations:v1"
METHOD = "POST"
PATH = "/internal/v1/run-authorizations/check"
IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")
KEY_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class WorkerIdentityError(Exception):
    """Deliberately omits tokens, key material, filesystem paths and claim values."""


class ReplayStore(Protocol):
    async def set(self, name: str, value: str, *, nx: bool, ex: int) -> Any: ...


def _read(path: Path | None, *, private: bool = False) -> bytes:
    if path is None:
        raise ValueError()
    # Follow trusted operator-mounted Secret symlinks, then inspect the open file.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & (0o077 if private else 0o022):
            raise ValueError()
        data = stream.read(65_537)
        if len(data) > 65_536:
            raise ValueError()
        return data


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _binding(body: object, method: str, path: str) -> tuple[str, str]:
    encoded = canonical_arguments(body)
    if method != METHOD or path != PATH or not isinstance(body, dict):
        raise ValueError()
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not IDENTIFIER.fullmatch(request_id):
        raise ValueError()
    return request_id, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def sign_worker_request(
    config: Settings, body: object, *, method: str = METHOD, path: str = PATH,
) -> str:
    """Load worker-only PEM on demand; never caches or logs private material."""
    try:
        request_id, digest = _binding(body, method, path)
        kid = config.worker_auth_key_id
        if not kid or not KEY_ID.fullmatch(kid):
            raise ValueError()
        key = load_pem_private_key(_read(config.worker_auth_private_key_file, private=True), None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError()
        now = int(time.time())
        return jwt.encode({
            "iss": ISSUER, "sub": SUBJECT, "aud": AUDIENCE, "iat": now, "exp": now + 20,
            "jti": request_id, "method": method, "path": path, "body_sha256": digest,
        }, key, algorithm="EdDSA", headers={"kid": kid, "typ": "JWT"})
    except Exception:
        raise WorkerIdentityError("worker_auth.unavailable") from None


async def authenticate_worker_request(
    config: Settings, replay: ReplayStore, token: str, body: object, *,
    method: str = METHOD, path: str = PATH,
) -> str:
    """Verify key-bound principal and consume request identity across core replicas."""
    try:
        if not isinstance(token, str) or len(token) > 8192:
            raise ValueError()
        request_id, digest = _binding(body, method, path)
        header = jwt.get_unverified_header(token)
        if set(header) != {"alg", "kid", "typ"} or header.get("alg") != "EdDSA" or header.get("typ") != "JWT":
            raise ValueError()
        kid = header["kid"]
        if not isinstance(kid, str) or not KEY_ID.fullmatch(kid):
            raise ValueError()
        # Reload on each check: removal/rotation never waits for a process cache.
        trust = json.loads(_read(config.worker_auth_trust_keys_file), object_pairs_hook=_pairs)
        entry = trust["keys"][kid]
        if entry["subject"] != SUBJECT:
            raise ValueError()
        key = load_pem_public_key(entry["public_key_pem"].encode("ascii"))
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError()
        claims = jwt.decode(token, key, algorithms=["EdDSA"], audience=AUDIENCE, issuer=ISSUER,
                            options={"verify_iat": False, "verify_exp": False,
                                     "strict_aud": True, "require": [
                                         "iss", "sub", "aud", "iat", "exp", "jti",
                                         "method", "path", "body_sha256"]})
        issued, expires, now = claims["iat"], claims["exp"], time.time()
        if (
            type(issued) is not int or type(expires) is not int
            or not 0 < expires - issued <= 20 or issued > now + 5 or expires <= now - 5
            or claims["sub"] != entry["subject"] or claims["jti"] != request_id
            or claims["method"] != method or claims["path"] != path
            or claims["body_sha256"] != digest
        ):
            raise ValueError()
        replay_key = hashlib.sha256(f"{SUBJECT}\0{request_id}".encode()).hexdigest()
        if not await replay.set(f"haas:worker-auth:{replay_key}", "1", nx=True, ex=60):
            raise ValueError()
        return SUBJECT
    except Exception:
        raise WorkerIdentityError("worker_auth.denied") from None
