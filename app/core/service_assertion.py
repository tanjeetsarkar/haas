from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from typing import cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.core.service_invocation import validate_service_invocation_claims
from haas_plugin_sdk import ServiceInvocationClaims

_HEADER_TYPE = "haas-service-invocation+jws"
_KID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_CLAIM_LIMIT = 2048
_TOKEN_LIMIT = 16384

def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise ValueError from None
    if _b64(decoded) != value:
        raise ValueError
    return decoded

def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError
    return result

def _constant(_: str) -> None:
    raise ValueError


def _json_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=_constant)
        if not isinstance(value, dict) or _json_bytes(value) != raw:
            raise ValueError
        return value
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError from None

def _kid(value: object) -> str:
    if type(value) is not str or not value.isascii() or not _KID.fullmatch(value):
        raise ValueError
    return value

def _bounded_claims(value: object) -> ServiceInvocationClaims:
    claims = validate_service_invocation_claims(value)
    if any(not item or len(item) > _CLAIM_LIMIT for item in claims.values() if type(item) is str):
        raise ValueError
    if claims["iat"] < 0 or claims["exp"] < 0 or not 0 < claims["exp"] - claims["iat"] <= 60:
        raise ValueError
    return claims

def sign_service_assertion(
    claims: ServiceInvocationClaims, *, kid: str, private_key: Ed25519PrivateKey
) -> str:
    """Sign canonical v1 claims with an explicitly injected Ed25519 private key."""
    try:
        key_id = _kid(kid)
        if not isinstance(private_key, Ed25519PrivateKey):
            raise ValueError
        checked = _bounded_claims(dict(claims))
        header = {"alg": "EdDSA", "kid": key_id, "typ": _HEADER_TYPE}
        header_bytes, payload_bytes = _json_bytes(header), _json_bytes(checked)
        header_part, payload_part = _b64(header_bytes), _b64(payload_bytes)
        token = f"{header_part}.{payload_part}.{_b64(private_key.sign(f'{header_part}.{payload_part}'.encode('ascii')))}"
        if len(token) > _TOKEN_LIMIT:
            raise ValueError
        return token
    except Exception:
        raise ValueError("invalid service assertion input") from None

def verify_service_assertion(
    token: str,
    *,
    trusted_keys: Mapping[str, Ed25519PublicKey],
    expected_issuer: str,
    expected_audience: str,
    now: int,
    clock_skew: int = 5,
) -> ServiceInvocationClaims:
    """Verify canonical v1 bytes and explicit time/audience bounds; this is not replay protection."""
    try:
        if (
            type(token) is not str
            or not token.isascii()
            or len(token) > _TOKEN_LIMIT
            or type(expected_issuer) is not str
            or not expected_issuer
            or type(expected_audience) is not str
            or not expected_audience
            or type(now) is not int
            or now < 0
            or type(clock_skew) is not int
            or not 0 <= clock_skew <= 5
            or not isinstance(trusted_keys, Mapping)
        ):
            raise ValueError
        keys = dict(trusted_keys)
        if not keys:
            raise ValueError
        for key_id, public_key in keys.items():
            _kid(key_id)
            if not isinstance(public_key, Ed25519PublicKey):
                raise ValueError
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError
        header_raw, payload_raw, signature = map(_unb64, parts)
        if len(signature) != 64:
            raise ValueError
        header = _json_object(header_raw)
        if header != {"alg": "EdDSA", "kid": header.get("kid"), "typ": _HEADER_TYPE}:
            raise ValueError
        key_id = _kid(header["kid"])
        if key_id not in keys:
            raise ValueError
        keys[key_id].verify(signature, f"{parts[0]}.{parts[1]}".encode("ascii"))
        claims = _bounded_claims(_json_object(payload_raw))
        if claims["iss"] != expected_issuer or claims["aud"] != expected_audience:
            raise ValueError
        if claims["iat"] > now + clock_skew or claims["exp"] <= now - clock_skew:
            raise ValueError
        return cast(ServiceInvocationClaims, dict(claims))
    except Exception:
        raise ValueError("invalid service assertion") from None
