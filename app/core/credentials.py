"""Credential broker: single-use, audience-bound secret grants for tool invocations (owner: SEC).

Dispatch (``prepare_tool_dispatch``, runs wherever the outbox relay runs):
  * resolves the invocation's allowed secret names from tenant ``secret_bindings``
    (filtered by allowed plugins/tools) and the run's ephemeral vault (which wins);
  * seals each value with a fresh random AES-256-GCM key whose AAD binds the grant id,
    audience and plugin digest, stores the ciphertext in Redis with the grant TTL and
    records the grant (no value) in ``credential_grants``;
  * returns Celery kwargs carrying only opaque grant references ``<grant_id>.<key>``
    plus the tenant egress policy.

Execution (``redeem_grant``, runs in the tool worker): atomic GETDEL makes every grant
single-use; audience, plugin digest, invocation and TTL must match or the value is never
decrypted. Redemptions and denials are queued in Redis and drained by
``AccessStore.maintenance_access`` into ``credential_grants`` + ``audit_events``.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import os
import time
from typing import Any
from uuid import UUID, uuid4

import redis.asyncio as aioredis
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings
from app.core.egress import EgressPolicy
from app.core.envelope import EnvelopeError, arguments_digest, sign_envelope, verify_envelope
from app.core.redaction import register_secret_values

log = logging.getLogger("haas.credentials")

GRANT_KEY_PREFIX = "haas:grant:"
REDEMPTION_QUEUE_KEY = "haas:grant-redemptions"
EGRESS_POLICY_KIND = "haas.egress_policy/1"


class GrantError(Exception):
    """A credential grant could not be redeemed; ``reason`` is a stable machine code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def plugin_identity(tool_name: str) -> tuple[str, str]:
    """(plugin id or "_", loaded plugin digest or "") for a registered tool."""
    from app.core.plugins import registry

    tool = registry.tools.get(tool_name)
    plugin_id = tool.plugin_id if tool is not None and tool.plugin_id else "_"
    plugin = registry.plugins.get(plugin_id) if plugin_id != "_" else None
    return plugin_id, plugin.digest if plugin is not None else ""


def tool_audience(tool_name: str) -> str:
    return f"tool:{plugin_identity(tool_name)[0]}/{tool_name}"


def _aad(grant_id: str, audience: str, plugin_digest: str, invocation_id: str) -> bytes:
    return f"haas:grant:{grant_id}|{audience}|{plugin_digest}|{invocation_id}".encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def parse_grant_ref(ref: str) -> tuple[str, bytes]:
    grant_id, sep, key_text = ref.partition(".")
    try:
        UUID(grant_id)
        key = _unb64(key_text)
    except (ValueError, binascii.Error) as exc:
        raise GrantError("malformed", "Malformed credential grant reference.") from exc
    if not sep or len(key) != 32:
        raise GrantError("malformed", "Malformed credential grant reference.")
    return grant_id, key


async def seal_grant(
    redis: Any,
    *,
    tenant_id: str,
    run_id: str,
    invocation_id: str,
    audience: str,
    plugin_digest: str,
    name: str,
    value: str,
    ttl_sec: int | None = None,
) -> tuple[str, str, float]:
    """Store one sealed secret in Redis; returns (grant_id, grant_ref, expires_at)."""
    ttl = ttl_sec or settings.credential_grant_ttl_sec
    grant_id = str(uuid4())
    key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(
        nonce,
        json.dumps({"name": name, "value": value}).encode(),
        _aad(grant_id, audience, plugin_digest, invocation_id),
    )
    expires_at = time.time() + ttl
    record = {
        "ct": _b64(nonce + ciphertext),
        "audience": audience,
        "plugin_digest": plugin_digest,
        "invocation_id": invocation_id,
        "tenant_id": tenant_id,
        "run_id": run_id,
        "name": name,
        "expires_at": expires_at,
    }
    await redis.set(GRANT_KEY_PREFIX + grant_id, json.dumps(record), ex=ttl)
    return grant_id, f"{grant_id}.{_b64(key)}", expires_at


async def _note(redis: Any, entry: dict[str, Any]) -> None:
    try:
        await redis.lpush(REDEMPTION_QUEUE_KEY, json.dumps(entry))
    except Exception:  # Audit drain is best effort; the grant is already consumed.
        log.warning("could not queue grant redemption record", exc_info=True)


async def redeem_grant(
    store: Any,
    grant_id: str,
    *,
    audience: str,
    plugin_digest: str,
    invocation_id: str | None = None,
    now: float | None = None,
) -> dict[str, str]:
    """Redeem one grant reference exactly once; returns ``{secret_name: value}``."""
    redis = store.rds if hasattr(store, "rds") else store
    gid, key = parse_grant_ref(grant_id)
    raw = await redis.getdel(GRANT_KEY_PREFIX + gid)
    if raw is None:
        await _note(redis, {"grant_id": gid, "outcome": "denied", "reason": "unavailable"})
        raise GrantError("unavailable", "Credential grant is unknown, expired or already used.")
    record = json.loads(raw)
    base = {
        "grant_id": gid,
        "tenant_id": record.get("tenant_id"),
        "run_id": record.get("run_id"),
        "at": time.time(),
    }
    checks = (
        ("audience", hmac.compare_digest(str(record["audience"]), audience)),
        (
            "plugin_digest",
            hmac.compare_digest(str(record["plugin_digest"]), plugin_digest),
        ),
        (
            "invocation",
            invocation_id is None
            or hmac.compare_digest(str(record["invocation_id"]), invocation_id),
        ),
        ("expired", float(record["expires_at"]) > (time.time() if now is None else now)),
    )
    for reason, ok in checks:
        if not ok:
            await _note(redis, {**base, "outcome": "denied", "reason": reason})
            raise GrantError(reason, f"Credential grant rejected: {reason} mismatch.")
    try:
        blob = _unb64(record["ct"])
        plain = AESGCM(key).decrypt(
            blob[:12],
            blob[12:],
            _aad(gid, audience, plugin_digest, str(record["invocation_id"])),
        )
    except (InvalidTag, ValueError, binascii.Error) as exc:
        await _note(redis, {**base, "outcome": "denied", "reason": "integrity"})
        raise GrantError("integrity", "Credential grant failed integrity check.") from exc
    data = json.loads(plain)
    await _note(redis, {**base, "outcome": "redeemed"})
    return {str(data["name"]): str(data["value"])}


async def redeem_grants(
    refs: list[str],
    *,
    tool_name: str,
    invocation_id: str,
    run_id: str,
    redis: Any | None = None,
) -> dict[str, str]:
    """Tool-worker helper: redeem every grant for one invocation, skipping invalid ones."""
    if not refs:
        return {}
    _, digest = plugin_identity(tool_name)
    audience = tool_audience(tool_name)
    owned = redis is None
    client = redis or aioredis.from_url(settings.redis_url, decode_responses=True)
    values: dict[str, str] = {}
    try:
        for ref in refs:
            try:
                values.update(
                    await redeem_grant(
                        client,
                        ref,
                        audience=audience,
                        plugin_digest=digest,
                        invocation_id=invocation_id,
                    )
                )
            except GrantError as exc:
                log.warning("credential grant refused (%s) for %s", exc.reason, tool_name)
    finally:
        if owned:
            await client.aclose()
    register_secret_values(run_id, values.values())
    return values


# ---------------------------------------------------------------- egress policy transport


def seal_egress_policy(
    policy: EgressPolicy, *, invocation_id: str, expires_at: float
) -> dict[str, Any]:
    """Policy document for the tool worker; signed when this process holds the signing key."""
    doc: dict[str, Any] = {
        "kind": EGRESS_POLICY_KIND,
        "invocation_id": invocation_id,
        "policy": policy.as_dict(),
        "signed": None,
    }
    try:
        doc["signed"] = sign_envelope(
            {
                "kind": EGRESS_POLICY_KIND,
                "invocation_id": invocation_id,
                "policy": policy.as_dict(),
                "expires_at": expires_at,
                "tool_call": {"name": "", "arguments": ""},
                "arguments_digest": arguments_digest("", ""),
            }
        )
    except EnvelopeError:
        pass
    return doc


def open_egress_policy(doc: dict[str, Any] | None, *, invocation_id: str) -> EgressPolicy:
    """Accept a signed policy verbatim; an unsigned one may only *tighten* the defaults."""
    if not doc:
        return EgressPolicy()
    signed = doc.get("signed")
    if signed:
        try:
            body = verify_envelope(signed)
        except EnvelopeError:
            body = None
        if (
            body is not None
            and body.get("kind") == EGRESS_POLICY_KIND
            and body.get("invocation_id") == invocation_id
        ):
            return EgressPolicy.from_value(body.get("policy"))
    try:
        claimed = EgressPolicy.from_value(doc.get("policy"))
    except (TypeError, ValueError):
        return EgressPolicy()
    cap = claimed.max_bytes_per_run
    return EgressPolicy.from_value(
        {
            "allow_hosts": list(claimed.allow_hosts),
            "max_bytes_per_run": (
                min(cap, settings.egress_default_max_bytes_per_run) if cap else None
            ),
        }
    )


# ---------------------------------------------------------------- dispatch


async def prepare_tool_dispatch(store: Any, message: dict[str, Any]) -> dict[str, Any]:
    """Extra ``execute_tool_task`` kwargs for one tool outbox message (no secret values)."""
    payload = message["payload"]
    body = payload["envelope"]["body"]
    tenant_id = str(message["tenant_id"])
    run_id = str(message["run_id"])
    invocation_id = str(body["invocation_id"])
    tool_name = str(body["tool_call"]["name"])
    plugin_id, _ = plugin_identity(tool_name)
    digest = str(body.get("plugin_digest") or "")
    audience = tool_audience(tool_name)
    names = sorted(set(payload.get("secret_keys") or []))

    resolved: dict[str, dict[str, Any]] = {}
    if names:
        resolved = await store.resolve_secret_bindings(
            tenant_id, run_id, names, plugin_id=plugin_id, tool_name=tool_name
        )
        for name, value in (await store.get_scoped_secrets(run_id, set(names))).items():
            resolved[name] = {"value": value, "source": "run"}

    refs: list[str] = []
    rows: list[dict[str, Any]] = []
    for name in sorted(resolved):
        item = resolved[name]
        grant_id, ref, expires_at = await seal_grant(
            store.rds,
            tenant_id=tenant_id,
            run_id=run_id,
            invocation_id=invocation_id,
            audience=audience,
            plugin_digest=digest,
            name=name,
            value=str(item["value"]),
        )
        refs.append(ref)
        rows.append(
            {
                "grant_id": grant_id,
                "invocation_id": invocation_id,
                "secret_name": name,
                "source": item.get("source", "binding"),
                "binding_id": item.get("binding_id"),
                "binding_version": item.get("version"),
                "audience": audience,
                "plugin_digest": digest,
                "expires_at": expires_at,
            }
        )
    register_secret_values(run_id, (str(v["value"]) for v in resolved.values()))
    if rows:
        await store.record_credential_grants(tenant_id, run_id, rows)
    policy = await store.egress_policy_for_run(tenant_id, run_id)
    return {
        "credential_grants": refs,
        "egress_policy": seal_egress_policy(
            policy,
            invocation_id=invocation_id,
            expires_at=float(body.get("expires_at") or time.time() + 600),
        ),
    }
