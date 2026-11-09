from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Mapping
from typing import cast
from urllib.parse import urlsplit

from haas_plugin_sdk import ServiceInvocationClaims

_CLAIM_TYPES: dict[str, type] = {
    "iss": str,
    "aud": str,
    "tenant_id": str,
    "plugin_id": str,
    "plugin_digest": str,
    "run_id": str,
    "invocation_id": str,
    "tool_name": str,
    "operation": str,
    "effect_key": str,
    "policy_revision": str,
    "catalog_revision": str,
    "method": str,
    "destination": str,
    "content_type": str,
    "request_sha256": str,
    "iat": int,
    "exp": int,
    "jti": str,
}
_CLAIM_NAMES = frozenset({"v", *_CLAIM_TYPES})
_DOMAIN = b"HaaS-Service-Request-v1"
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_IPV4ISH_HOST = re.compile(r"(?:[0-9]+|0x[0-9a-f]+)(?:\.(?:[0-9]+|0x[0-9a-f]+)){0,3}")
_PATH = re.compile(r"/[A-Za-z0-9._~/-]*")
_METHOD = re.compile(r"[A-Z][A-Z0-9-]*")
_B64URL = re.compile(r"[A-Za-z0-9_-]+")


def _decode_canonical(value: str, size: int, name: str) -> bytes:
    if not _B64URL.fullmatch(value):
        raise ValueError(f"{name} must be unpadded base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except binascii.Error as exc:
        raise ValueError(f"{name} must be valid base64url") from exc
    if len(decoded) != size or _b64url(decoded) != value:
        raise ValueError(f"{name} has invalid size or noncanonical encoding")
    return decoded


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _validate_destination(destination: str) -> None:
    if not destination.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in destination):
        raise ValueError("destination is not a canonical service URL")
    parsed = urlsplit(destination)
    port = parsed.port
    host = parsed.hostname
    if (
        not destination.startswith("https://")
        or host is None
        or parsed.netloc != f"{host}:443"
        or port != 443
        or "?" in destination
        or "#" in destination
        or not host.isascii()
        or len(host) > 253
        or any(not _HOST_LABEL.fullmatch(label) for label in host.split("."))
    ):
        raise ValueError("destination is not a canonical service URL")
    if _IPV4ISH_HOST.fullmatch(host):
        raise ValueError("destination must use a DNS hostname")
    path = parsed.path
    if not _PATH.fullmatch(path) or any(part in {".", ".."} for part in path.split("/")):
        raise ValueError("destination path is not canonical")


def _validate_request_fields(method: str, destination: str, content_type: str) -> None:
    if type(method) is not str or not _METHOD.fullmatch(method):
        raise ValueError("method must be an uppercase HTTP token")
    if type(destination) is not str:
        raise ValueError("destination must be a string")
    _validate_destination(destination)
    if type(content_type) is not str or not content_type or any(
        ord(char) < 32 or ord(char) == 127 for char in content_type
    ):
        raise ValueError("content_type must be a non-empty header value")


def validate_service_invocation_claims(claims: object) -> ServiceInvocationClaims:
    """Validate exact v1 claim names, types, encodings and canonical request fields."""
    if not isinstance(claims, Mapping) or set(claims) != _CLAIM_NAMES:
        raise ValueError("claims must contain exactly the version-1 fields")
    if type(claims["v"]) is not int or claims["v"] != 1:
        raise ValueError("v must be integer 1")
    for name, expected in _CLAIM_TYPES.items():
        if type(claims[name]) is not expected:
            raise ValueError(f"{name} has an invalid type")
    _decode_canonical(cast(str, claims["request_sha256"]), 32, "request_sha256")
    _decode_canonical(cast(str, claims["jti"]), 16, "jti")
    _validate_request_fields(cast(str, claims["method"]), cast(str, claims["destination"]),
                             cast(str, claims["content_type"]))
    return cast(ServiceInvocationClaims, dict(claims))


def service_request_binding_bytes(
    method: str, destination: str, content_type: str, body: bytes
) -> bytes:
    """Build deterministic v1 binding bytes; body bytes are included unchanged."""
    _validate_request_fields(method, destination, content_type)
    if type(body) is not bytes:
        raise ValueError("body must be exact bytes")
    encoded = bytearray(_DOMAIN)
    for field in ("1", method, destination, content_type, body):
        value = field.encode("utf-8") if isinstance(field, str) else field
        encoded.extend(len(value).to_bytes(4, "big"))
        encoded.extend(value)
    return bytes(encoded)


def service_request_sha256(
    method: str, destination: str, content_type: str, body: bytes
) -> str:
    """Return the canonical unpadded base64url SHA-256 request binding."""
    binding = service_request_binding_bytes(method, destination, content_type, body)
    return _b64url(hashlib.sha256(binding).digest())
