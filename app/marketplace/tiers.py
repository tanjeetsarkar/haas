"""Trust tiers and tenant consent material for marketplace installs."""

from __future__ import annotations

from typing import Any

from app.marketplace.canonical import digest_json

# Highest trust first. "official" is accepted as an alias for "platform".
TIERS = ("platform", "verified", "community", "private", "unreviewed")
TIER_ALIASES = {"official": "platform"}
DEFAULT_ALLOWED_TIERS = ("platform", "verified", "private")
PUBLISHABLE_TIERS = ("platform", "verified", "community", "private")


def normalize_tier(value: str) -> str:
    tier = TIER_ALIASES.get(value.strip().lower(), value.strip().lower())
    if tier not in TIERS:
        raise ValueError(f"unknown trust tier '{value}'; expected one of {list(TIERS)}")
    return tier


def tier_for_publisher(
    *, requested: str | None, verification: str, visibility: str
) -> str:
    """Tier a new revision lands in, given the publisher's verification level."""
    if visibility == "private":
        return "private"
    if requested is not None:
        wanted = normalize_tier(requested)
        if wanted == "platform" and verification != "official":
            raise ValueError("only official (platform) publishers can publish platform-tier plugins")
        if wanted == "verified" and verification not in ("verified", "official"):
            raise ValueError("verified tier requires a verified publisher namespace")
        if wanted in ("private", "unreviewed"):
            raise ValueError("use visibility 'private' for private plugins")
        return wanted
    if verification == "official":
        return "platform"
    if verification == "verified":
        return "verified"
    return "community"


def consent_material(
    *, listing_id: str, version: str, digest: str, permissions: dict[str, Any]
) -> dict[str, Any]:
    """The exact thing a tenant admin consents to; its digest is echoed back on install."""
    material = {
        "listing_id": listing_id,
        "version": version,
        "digest": digest,
        "permissions": permissions,
    }
    return {**material, "consent_digest": digest_json(material)}
