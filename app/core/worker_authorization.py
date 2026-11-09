"""One-shot HTTPS authorization checks; no grant/handler work and no retry fallback."""

from __future__ import annotations

import hashlib
import json
import math
import ssl
import time
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from app.core.config import Settings
from app.core.context import validate_safe_outbound_url
from app.core.run_restrictions import canonical_arguments
from app.core.worker_identity import PATH, sign_worker_request

BINDING_FIELDS = {
    "version", "tenant_id", "run_id", "invocation_id", "tool_name", "arguments_sha256",
    "intent_binding", "policy_revision_id", "registry_fingerprint", "plugin_digest",
    "envelope_expires_at",
}


class WorkerAuthorizationError(Exception):
    """Generic denial without URLs, arguments, credentials or response previews."""


def _unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _request(binding: dict[str, Any], operation: str) -> dict[str, Any]:
    body = json.loads(canonical_arguments(binding))
    if set(body) != BINDING_FIELDS or type(body["version"]) is not int or body["version"] != 1:
        raise ValueError()
    if operation not in ("pre_grant", "final_entry"):
        raise ValueError()
    for field in ("tenant_id", "invocation_id", "policy_revision_id"):
        if str(UUID(body[field])) != body[field]:
            raise ValueError()
    for field in ("arguments_sha256", "intent_binding", "registry_fingerprint", "plugin_digest"):
        value = body[field]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError()
    for field, limit in (("run_id", 128), ("tool_name", 255)):
        if not isinstance(body[field], str) or not 1 <= len(body[field]) <= limit:
            raise ValueError()
    expiry = body["envelope_expires_at"]
    if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= time.time():
        raise ValueError()
    return {**body, "request_id": uuid4().hex, "operation": operation}


def _allow(response: object, body: dict[str, Any], started: float) -> None:
    if not isinstance(response, dict) or set(response) != {
        "version", "request_id", "operation", "decision", "binding_sha256", "checked_at", "valid_until",
    }:
        raise ValueError()
    now = time.time()
    checked, until = response["checked_at"], response["valid_until"]
    if (
        type(response["version"]) is not int or response["version"] != 1
        or response["request_id"] != body["request_id"] or response["operation"] != body["operation"]
        or response["decision"] != "allow"
        or response["binding_sha256"] != hashlib.sha256(canonical_arguments(body).encode()).hexdigest()
        or type(checked) not in (int, float) or type(until) not in (int, float)
        or not math.isfinite(checked) or not math.isfinite(until)
        or checked < started - 5 or checked > now + 5 or not checked < until
        or until <= now or until - checked > 5 or until > body["envelope_expires_at"]
    ):
        raise ValueError()


async def check_worker_authorization(
    config: Settings, binding: dict[str, Any], operation: Literal["pre_grant", "final_entry"],
) -> None:
    """Return only for a fresh allow over verified TLS; every failure denies."""
    try:
        body = _request(binding, operation)
        url = config.internal_authorization_url
        if not url:
            raise ValueError()
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.path != PATH
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.port == 0
        ):
            raise ValueError()
        # Existing SSRF rules remain conjunctive. Internal deployments must use
        # the existing operator host allowlist; never infer a private-host bypass.
        validate_safe_outbound_url(url, resolve_dns=True)
        tls = ssl.create_default_context(cafile=(
            str(config.internal_authorization_ca_file) if config.internal_authorization_ca_file else None
        ))
        token = sign_worker_request(config, body)
        started = time.time()
        async with httpx.AsyncClient(
            verify=tls, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(2.0), limits=httpx.Limits(max_connections=1),
        ) as client:
            async with client.stream(
                "POST", url, content=canonical_arguments(body).encode("utf-8"),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            ) as response:
                if response.status_code != 200:
                    raise ValueError()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 8192:
                        raise ValueError()
                _allow(json.loads(data, object_pairs_hook=_unique), body, started)
    except Exception:
        raise WorkerAuthorizationError("restriction.authorization_denied") from None
