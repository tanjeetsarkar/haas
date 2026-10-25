from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, HttpUrl, ValidationError

from app.core.context import (
    RunContext,
    validate_safe_outbound_url,
)

TrustTier = Literal[
    "verified_official",
    "extracted_document",
    "aggregated_directory",
    "estimated_range",
    "unverified",
]

ComponentType = Literal[
    "entity_card",
    "comparison_row",
    "pricing_tier_card",
    "specialist_profile",
    "checklist_card",
]

DimensionKey = Literal[
    "root",
    "facilities",
    "pricing",
    "specialists",
    "credentials",
    "action",
]

DomainCategory = Literal[
    "healthcare",
    "education",
    "government",
    "commerce",
    "general",
]


class ProvenanceModel(BaseModel):
    trust_tier: TrustTier
    source_urls: list[str] = Field(min_length=1, max_length=10)
    observed_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    citation_snippet: str | None = Field(default=None, max_length=500)


class LayerNodeModel(BaseModel):
    node_id: str = Field(min_length=3, max_length=64)
    canonical_key: str = Field(pattern=r"^[a-z0-9_:-]{3,128}$")
    title: str = Field(min_length=1, max_length=160)
    subtitle: str | None = Field(default=None, max_length=240)
    component_type: ComponentType
    attributes: dict[str, str | int | float | bool | None]
    provenance: ProvenanceModel


class DrilldownChipModel(BaseModel):
    dimension_key: str = Field(pattern=r"^[a-z0-9_]{2,48}$")
    label: str = Field(min_length=2, max_length=64)
    prompt_template: str = Field(min_length=5, max_length=500)
    recommended_skills: list[str] = Field(default_factory=lambda: ["atlas-explorer", "web-research"])


class ActionChipModel(BaseModel):
    action_id: str = Field(pattern=r"^[a-z0-9_]{2,48}$")
    label: str = Field(min_length=2, max_length=64)
    tool_name: str
    requires_approval: bool = True


class AtlasLayerEnvelope(BaseModel):
    schema_version: Literal["atlas-layer.v1"] = "atlas-layer.v1"
    layer_depth: int = Field(ge=1, le=10)
    parent_node_id: str | None = None
    domain_category: DomainCategory
    nodes: list[LayerNodeModel] = Field(min_length=1, max_length=25)
    available_drilldowns: list[DrilldownChipModel] = Field(max_length=8)
    available_actions: list[ActionChipModel] = Field(max_length=6)


# TTL configuration by dimension_key and trust_tier (in seconds)
TTL_CONFIG: dict[str, dict[TrustTier, int]] = {
    "root": {
        "verified_official": 30 * 86400,
        "extracted_document": 30 * 86400,
        "aggregated_directory": 7 * 86400,
        "estimated_range": 3 * 86400,
        "unverified": 3 * 86400,
    },
    "facilities": {
        "verified_official": 14 * 86400,
        "extracted_document": 14 * 86400,
        "aggregated_directory": 7 * 86400,
        "estimated_range": 2 * 86400,
        "unverified": 2 * 86400,
    },
    "pricing": {
        "verified_official": 7 * 86400,
        "extracted_document": 7 * 86400,
        "aggregated_directory": 3 * 86400,
        "estimated_range": 86400,
        "unverified": 86400,
    },
    "specialists": {
        "verified_official": 7 * 86400,
        "extracted_document": 7 * 86400,
        "aggregated_directory": 3 * 86400,
        "estimated_range": 86400,
        "unverified": 86400,
    },
    "credentials": {
        "verified_official": 3 * 86400,
        "extracted_document": 3 * 86400,
        "aggregated_directory": 86400,
        "estimated_range": 12 * 3600,
        "unverified": 12 * 3600,
    },
    "action": {
        "verified_official": 86400,
        "extracted_document": 86400,
        "aggregated_directory": 86400,
        "estimated_range": 86400,
        "unverified": 86400,
    },
}


def canonicalize_entity_key(domain: str, location: str, slug: str) -> str:
    """Deterministic key generator for PostgreSQL atlas_entity_cache."""
    clean = f"{domain.strip().lower()}:{location.strip().lower()}:{slug.strip().lower()}"
    return "".join(c if c.isalnum() or c in "_:-" else "_" for c in clean)[:128]


def compute_content_sha256(attributes: dict[str, Any], provenance: ProvenanceModel) -> str:
    """Compute deterministic SHA256 of node content for deduplication."""
    payload = {
        "attributes": attributes,
        "provenance": provenance.model_dump(mode="json"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def compute_ttl(dimension_key: str, trust_tier: TrustTier) -> int:
    """Get TTL in seconds for a dimension_key and trust_tier combination."""
    return TTL_CONFIG.get(dimension_key, {}).get(trust_tier, 86400)


class CacheLookupArgs(BaseModel):
    canonical_key: str = Field(pattern=r"^[a-z0-9_:-]{3,128}$")
    dimension_key: DimensionKey


class CacheLookupResult(BaseModel):
    found: bool
    attributes: dict[str, Any] | None = None
    provenance: ProvenanceModel | None = None
    expires_at: str | None = None
    content_sha256: str | None = None


class CacheUpsertArgs(BaseModel):
    canonical_key: str = Field(pattern=r"^[a-z0-9_:-]{3,128}$")
    dimension_key: DimensionKey
    domain_category: DomainCategory
    attributes: dict[str, str | int | float | bool | None]
    provenance: ProvenanceModel
    trust_tier: TrustTier


class CacheUpsertResult(BaseModel):
    ok: bool
    cached: bool
    content_sha256: str
    expires_at: str


class EmitLayerArgs(BaseModel):
    payload: dict[str, Any]


class EmitLayerResult(BaseModel):
    ok: bool
    layer_digest: str | None = None
    envelope: dict[str, Any] | None = None
    error: str | None = None
    details: list[dict[str, Any]] | None = None


class DispatchInquiryArgs(BaseModel):
    destination_url: HttpUrl
    method: Literal["GET", "POST"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] | None = None
    idempotency_key: str = Field(min_length=32, max_length=64)


class DispatchInquiryResult(BaseModel):
    ok: bool
    status_code: int | None = None
    response_body: dict[str, Any] | None = None
    error: str | None = None


def register(registry) -> None:
    @registry.tool(
        name="atlas_cache_lookup",
        description=(
            "Look up cached entity attributes in the shared atlas_entity_cache. "
            "Returns cached data if found and not expired. Read-only."
        ),
        args_schema=CacheLookupArgs,
        sandbox=False,
    )
    async def atlas_cache_lookup(ctx: RunContext, args: CacheLookupArgs) -> str:
        """Query the atlas_entity_cache table for a cached entry."""
        if ctx.store is None:
            return CacheLookupResult(
                found=False,
                attributes=None,
                provenance=None,
                expires_at=None,
                content_sha256=None,
            ).model_dump_json()

        try:
            async with ctx.store.connection() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT attributes_json, provenance_json, expires_at, content_sha256
                    FROM atlas_entity_cache
                    WHERE tenant_id = $1
                      AND canonical_key = $2
                      AND dimension_key = $3
                      AND expires_at > now()
                    """,
                    ctx.tenant_id,
                    args.canonical_key,
                    args.dimension_key,
                )
                if row is None:
                    return CacheLookupResult(
                        found=False,
                        attributes=None,
                        provenance=None,
                        expires_at=None,
                        content_sha256=None,
                    ).model_dump_json()

                return CacheLookupResult(
                    found=True,
                    attributes=row["attributes_json"],
                    provenance=row["provenance_json"],
                    expires_at=row["expires_at"].isoformat(),
                    content_sha256=row["content_sha256"],
                ).model_dump_json()
        except Exception as exc:
            return CacheLookupResult(
                found=False,
                attributes=None,
                provenance=None,
                expires_at=None,
                content_sha256=None,
            ).model_dump_json()

    @registry.tool(
        name="atlas_cache_upsert",
        description=(
            "Insert or update entity attributes in the shared atlas_entity_cache with TTL based on "
            "dimension_key and trust_tier. Computes content_sha256 for deduplication. "
            "Does not require approval."
        ),
        args_schema=CacheUpsertArgs,
        sandbox=False,
    )
    async def atlas_cache_upsert(ctx: RunContext, args: CacheUpsertArgs) -> str:
        """Upsert into the atlas_entity_cache table."""
        if ctx.store is None:
            # Fallback to mock behavior if no store available
            content_sha256 = compute_content_sha256(args.attributes, args.provenance)
            ttl_seconds = compute_ttl(args.dimension_key, args.trust_tier)
            expires_at = datetime.now(timezone.utc).timestamp() + ttl_seconds
            expires_at_iso = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()

            return CacheUpsertResult(
                ok=True,
                cached=True,
                content_sha256=content_sha256,
                expires_at=expires_at_iso,
            ).model_dump_json()

        try:
            content_sha256 = compute_content_sha256(args.attributes, args.provenance)
            ttl_seconds = compute_ttl(args.dimension_key, args.trust_tier)
            expires_at = datetime.now(timezone.utc).timestamp() + ttl_seconds
            expires_at_iso = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()

            async with ctx.store.connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO atlas_entity_cache (
                        tenant_id, canonical_key, dimension_key, domain_category,
                        attributes_json, provenance_json, trust_tier,
                        content_sha256, expires_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (tenant_id, canonical_key, dimension_key) DO UPDATE SET
                        domain_category = EXCLUDED.domain_category,
                        attributes_json = EXCLUDED.attributes_json,
                        provenance_json = EXCLUDED.provenance_json,
                        trust_tier = EXCLUDED.trust_tier,
                        content_sha256 = EXCLUDED.content_sha256,
                        expires_at = EXCLUDED.expires_at,
                        fetched_at = now()
                    """,
                    ctx.tenant_id,
                    args.canonical_key,
                    args.dimension_key,
                    args.domain_category,
                    args.attributes,
                    args.provenance.model_dump(mode="json"),
                    args.trust_tier,
                    content_sha256,
                    expires_at_iso,
                )

            return CacheUpsertResult(
                ok=True,
                cached=True,
                content_sha256=content_sha256,
                expires_at=expires_at_iso,
            ).model_dump_json()
        except Exception as exc:
            return CacheUpsertResult(
                ok=False,
                cached=False,
                content_sha256="",
                expires_at="",
            ).model_dump_json()

    @registry.tool(
        name="atlas_emit_layer",
        description=(
            "Validates and normalizes a layer response against the atlas-layer.v1 contract. "
            "Rejects any node lacking valid HTTPS/HTTP provenance URLs. "
            "Returns validated envelope with SHA256 digest for HITL binding. Does not require approval."
        ),
        args_schema=EmitLayerArgs,
        sandbox=False,
    )
    async def atlas_emit_layer(ctx: RunContext, args: EmitLayerArgs) -> str:
        """
        Validates and normalizes a layer response against the atlas-layer.v1 contract.
        Rejects any node lacking valid HTTPS/HTTP provenance URLs.
        """
        try:
            envelope = AtlasLayerEnvelope.model_validate(args.payload)
        except ValidationError as exc:
            return EmitLayerResult(
                ok=False,
                error="schema_validation_failed",
                details=exc.errors(),
            ).model_dump_json()

        # Verify every URL uses http/https scheme to prevent javascript:/data: XSS in SDUI
        for node in envelope.nodes:
            for url in node.provenance.source_urls:
                parsed = urlparse(str(url))
                if parsed.scheme not in ("http", "https"):
                    return EmitLayerResult(
                        ok=False,
                        error=f"Disallowed URI scheme in provenance for {node.node_id}: {parsed.scheme}",
                    ).model_dump_json()

        serialized = envelope.model_dump(mode="json")
        digest = hashlib.sha256(
            json.dumps(serialized, sort_keys=True).encode("utf-8")
        ).hexdigest()

        return EmitLayerResult(
            ok=True,
            layer_digest=f"sha256:{digest}",
            envelope=serialized,
        ).model_dump_json()

    @registry.tool(
        name="atlas_dispatch_inquiry",
        description=(
            "Dispatches an external inquiry/action (e.g., hospital OPD inquiry, appointment request) "
            "to a destination URL. REQUIRES HUMAN APPROVAL — run will pause at input_required. "
            "The approval gate binds to the exact SHA256 digest of the tool arguments."
        ),
        args_schema=DispatchInquiryArgs,
        requires_approval=True,
        sandbox=False,
    )
    async def atlas_dispatch_inquiry(ctx: RunContext, args: DispatchInquiryArgs) -> str:
        if ctx.http_client is None:
            return DispatchInquiryResult(
                ok=False,
                error="HTTP client not available in context",
            ).model_dump_json()

        # Validate destination URL through SSRF guard at call time
        safe_url = await asyncio.to_thread(
            validate_safe_outbound_url, str(args.destination_url), resolve_dns=True
        )

        # Prepare headers with idempotency key
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": args.idempotency_key,
            **args.headers,
        }

        try:
            if args.method == "GET":
                resp = await ctx.http_client.get(
                    safe_url,
                    params=args.body,
                    headers=headers,
                    timeout=15.0,
                    follow_redirects=False,
                )
            else:
                resp = await ctx.http_client.request(
                    args.method,
                    safe_url,
                    json=args.body,
                    headers=headers,
                    timeout=15.0,
                    follow_redirects=False,
                )

            if resp.status_code >= 400:
                return DispatchInquiryResult(
                    ok=False,
                    status_code=resp.status_code,
                    error=f"HTTP {resp.status_code}: {resp.text[:500]}",
                ).model_dump_json()

            try:
                response_body = resp.json()
            except Exception:
                response_body = {"raw": resp.text[:2000]}

            return DispatchInquiryResult(
                ok=True,
                status_code=resp.status_code,
                response_body=response_body,
            ).model_dump_json()

        except Exception as exc:
            return DispatchInquiryResult(
                ok=False,
                error=f"Request failed: {type(exc).__name__}: {exc}",
            ).model_dump_json()