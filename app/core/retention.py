"""Retention classes, policy resolution and erasure receipts (owner: DATA, ADR 0018).

Pure logic shared by the store (``app.core.areas.artifacts``) and the API. Tenants
carry an operator-assigned ``retention_class``; tenant administrators may only
shorten the class TTLs through ``retention_policies`` (data-model §6).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass
from typing import Any

from app.core.config import Settings, settings

DATA_CLASSES = ("run_events", "artifacts", "messages")
DEFAULT_CLASS = "standard"
TERMINAL_RUN_STATUSES = ("completed", "failed", "cancelled")
SYSTEM_ACTOR = "system:retention"


@dataclass(frozen=True)
class RetentionClass:
    """TTL in days after a run's last completed activity, per data class."""

    run_events: int
    artifacts: int
    messages: int


RETENTION_CLASSES: dict[str, RetentionClass] = {
    "minimal": RetentionClass(run_events=1, artifacts=1, messages=1),
    "short": RetentionClass(run_events=7, artifacts=7, messages=7),
    DEFAULT_CLASS: RetentionClass(run_events=30, artifacts=30, messages=30),
    "extended": RetentionClass(run_events=365, artifacts=365, messages=365),
}


def retention_class(name: str | None) -> tuple[str, RetentionClass]:
    """Resolve a tenant's class; unknown or missing names fall back to ``standard``."""
    if name in RETENTION_CLASSES:
        assert name is not None
        return name, RETENTION_CLASSES[name]
    return DEFAULT_CLASS, RETENTION_CLASSES[DEFAULT_CLASS]


def classes_json() -> str:
    """Class table as JSON for SQL-side TTL resolution."""
    return json.dumps({name: asdict(c) for name, c in RETENTION_CLASSES.items()})


def validate_overrides(class_name: str | None, overrides: dict[str, Any]) -> None:
    """Tenant overrides may only shorten (never extend) the class TTLs."""
    _, base = retention_class(class_name)
    for kind in DATA_CLASSES:
        value = overrides.get(f"{kind}_ttl_days")
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{kind}_ttl_days must be a positive integer")
        if value > getattr(base, kind):
            raise ValueError(
                f"{kind}_ttl_days may not exceed the tenant retention class "
                f"({getattr(base, kind)} days)"
            )
    quota = overrides.get("artifact_quota_bytes")
    if quota is not None and (
        not isinstance(quota, int) or isinstance(quota, bool) or quota < 0
    ):
        raise ValueError("artifact_quota_bytes must be a non-negative integer")


def effective_policy(
    class_name: str | None,
    overrides: dict[str, Any] | None,
    *,
    config: Settings = settings,
) -> dict[str, Any]:
    name, base = retention_class(class_name)
    overrides = overrides or {}
    policy: dict[str, Any] = {"retention_class": name}
    for kind in DATA_CLASSES:
        value = overrides.get(f"{kind}_ttl_days")
        policy[f"{kind}_ttl_days"] = (
            min(int(value), getattr(base, kind))
            if value is not None
            else getattr(base, kind)
        )
    quota = overrides.get("artifact_quota_bytes")
    policy["artifact_quota_bytes"] = (
        int(quota) if quota is not None else config.artifact_tenant_quota_bytes
    )
    return policy


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def sign_manifest(
    manifest: dict[str, Any], *, config: Settings = settings
) -> tuple[str, str | None, str | None]:
    """Return ``(manifest_sha256, hmac_sha256_hex | None, key_id | None)``.

    The HMAC covers the manifest digest; without a configured key the receipt is
    hashed but unsigned (development only).
    """
    manifest_sha = digest(manifest)
    key = config.retention_receipt_hmac_key
    if not key:
        return manifest_sha, None, None
    signature = hmac.new(
        key.encode("utf-8"), manifest_sha.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return manifest_sha, signature, config.retention_receipt_key_id


def verify_receipt(
    manifest: dict[str, Any],
    manifest_sha256: str,
    receipt_hmac: str | None,
    key: str | None,
) -> bool:
    if not hmac.compare_digest(digest(manifest), manifest_sha256):
        return False
    if receipt_hmac is None or key is None:
        return receipt_hmac is None and key is None
    expected = hmac.new(
        key.encode("utf-8"), manifest_sha256.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, receipt_hmac)
