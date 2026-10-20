"""Track M marketplace registry, trust and installs (owner: MKT)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import PrincipalInfo, requires, tenant_scope
from app.core.authorization import Action
from app.main import store
from app.marketplace.review import run_review, decide
from app.marketplace.signing import verify as verify_signature
from app.marketplace.canonical import canonical_json

import hashlib

router = APIRouter()


# Publisher models
class PublisherCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    namespace: str = Field(..., pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=64)
    display_name: str = Field(..., min_length=1, max_length=128)
    domain: str | None = Field(None, pattern=r"^[a-z0-9.-]+\.[a-z]{2,}$")


class PublisherResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    publisher_id: str
    namespace: str
    display_name: str
    owner_tenant_id: str
    verification: str
    domain: str | None
    status: str
    status_reason: str | None
    created_at: datetime
    updated_at: datetime


class PublisherUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(None, min_length=1, max_length=128)
    domain: str | None = Field(None, pattern=r"^[a-z0-9.-]+\.[a-z]{2,}$")
    status: str | None = Field(None, pattern="^(active|suspended)$")
    status_reason: str | None = None


# Publisher Keys
class PublisherKeyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_id: str = Field(..., pattern=r"^[0-9a-f]{32}$")
    public_key: str = Field(..., min_length=64, max_length=64)  # hex encoded 32 bytes
    label: str | None = None


class PublisherKeyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_id: str
    publisher_id: str
    label: str | None
    created_at: datetime
    revoked_at: datetime | None
    revoked_reason: str | None


# Listings
class ListingCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin_id: str = Field(..., pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    name: str = Field(..., pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    display_name: str = Field(..., min_length=1, max_length=128)
    summary: str = Field(..., min_length=1, max_length=500)
    categories: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    visibility: str = Field("public", pattern="^(public|private)$")


class ListingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    listing_id: str
    plugin_id: str
    publisher_id: str
    name: str
    display_name: str
    summary: str
    categories: list[str]
    keywords: list[str]
    visibility: str
    owner_tenant_id: str
    status: str
    status_reason: str | None
    install_count: int
    created_at: datetime
    updated_at: datetime


class ListingUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(None, min_length=1, max_length=128)
    summary: str | None = Field(None, min_length=1, max_length=500)
    categories: list[str] | None = None
    keywords: list[str] | None = None
    visibility: str | None = Field(None, pattern="^(public|private)$")
    status: str | None = Field(None, pattern="^(active|taken_down)$")
    status_reason: str | None = None


# Artifacts
class ArtifactUploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest: str
    size_bytes: int


# Revisions
class RevisionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    listing_id: str
    version: str
    major: int
    minor: int
    patch: int
    prerelease: str | None = None
    digest: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")
    manifest: dict[str, Any]
    manifest_raw: dict[str, Any]
    manifest_digest: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")
    source_manifest_version: int = Field(..., ge=1, le=2)
    permissions: dict[str, Any]
    signature: dict[str, Any]
    signing_key_id: str = Field(..., pattern=r"^[0-9a-f]{32}$")
    provenance: dict[str, Any] | None = None
    tier: str = Field(..., pattern="^(platform|verified|community|private|unreviewed)$")


class RevisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: str
    listing_id: str
    version: str
    major: int
    minor: int
    patch: int
    prerelease: str | None
    digest: str
    manifest: dict[str, Any]
    manifest_raw: dict[str, Any]
    manifest_digest: str
    source_manifest_version: int
    permissions: dict[str, Any]
    signature: dict[str, Any]
    signing_key_id: str
    provenance: dict[str, Any] | None
    tier: str
    review_state: str
    review_report: dict[str, Any]
    reviewed_by: str | None
    reviewed_at: datetime | None
    review_notes: str | None
    tlog_seq: int
    yanked_at: datetime | None
    yank_reason: str | None
    published_by: str | None
    created_at: datetime


class RevisionReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_state: str = Field(..., pattern="^(approved|rejected)$")
    review_report: dict[str, Any]
    review_notes: str | None = None


# Installations
class InstallationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    listing_id: str
    revision_id: str
    consent: dict[str, Any]
    consent_digest: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")


class InstallationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installation_id: str
    tenant_id: str
    listing_id: str
    revision_id: str
    plugin_id: str
    version: str
    digest: str
    consent: dict[str, Any]
    consent_digest: str
    status: str
    status_reason: str | None
    installed_by: str | None
    installed_at: datetime
    updated_at: datetime


class InstallationUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str | None = Field(None, pattern="^(active|uninstalled|revoked)$")
    status_reason: str | None = None


# Notifications
class NotificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notification_id: str
    tenant_id: str
    kind: str
    plugin_id: str
    version: str | None
    details: dict[str, Any]
    created_at: datetime
    acknowledged_at: datetime | None


# Abuse Reports
class AbuseReportCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    listing_id: str
    version: str | None = None
    reason: str = Field(..., pattern="^(malware|data_exfiltration|impersonation|prompt_injection|policy|other)$")
    details: str = Field(..., min_length=1, max_length=4000)


class AbuseReportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str
    listing_id: str
    version: str | None
    reporter_tenant_id: str | None
    reporter_principal_id: str | None
    reason: str
    details: str
    status: str
    resolution: str | None
    resolved_by: str | None
    resolved_at: datetime | None
    created_at: datetime


# Search
class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = ""
    categories: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    tiers: list[str] = Field(default_factory=lambda: ["platform", "verified", "community"])
    visibility: str | None = Field(None, pattern="^(public|private)$")
    limit: int = Field(20, ge=1, le=100)
    offset: int = Field(0, ge=0)


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    listing_id: str
    plugin_id: str
    publisher_id: str
    name: str
    display_name: str
    summary: str
    categories: list[str]
    keywords: list[str]
    visibility: str
    install_count: int
    latest_revision: RevisionResponse | None


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[SearchResult]
    total: int
    limit: int
    offset: int


# Publisher endpoints
@router.post("/v1/marketplace/publishers", response_model=PublisherResponse)
async def create_publisher(request: PublisherCreateRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.MARKETPLACE_PUBLISH)) -> PublisherResponse:
    """Create a publisher account."""
    async with store.connection() as conn:
        # Check if namespace is taken
        existing = await conn.fetchrow(
            "SELECT 1 FROM marketplace_publishers WHERE namespace = $1",
            request.namespace,
        )
        if existing:
            raise HTTPException(status_code=409, detail=f"Namespace '{request.namespace}' already taken")
        
        row = await conn.fetchrow(
            """
            INSERT INTO marketplace_publishers (
                publisher_id, namespace, display_name, owner_tenant_id, created_by,
                verification, domain, status
            ) VALUES (
                gen_random_uuid(), $1, $2,
                principal.tenant_id,
                $3, 'unverified', $4, 'active'
            )
            RETURNING publisher_id::text, namespace, display_name, owner_tenant_id::text,
                      verification, domain, status, status_reason, created_at, updated_at
            """,
            request.namespace,
            request.display_name,
            principal.principal_id,
            request.domain,
        )
    
    return PublisherResponse(**dict(row))


@router.get("/v1/marketplace/publishers", response_model=list[PublisherResponse])
async def list_publishers(
    status: str | None = Query(None, pattern="^(active|suspended)$"),
    verification: str | None = Query(None, pattern="^(unverified|verified|official)$"),
    principal: PrincipalInfo = Depends(tenant_scope),
    _auth: None = requires(Action.MARKETPLACE_READ)

) -> list[PublisherResponse]:
    """List publishers."""
    async with store.connection() as conn:
        query = """
            SELECT publisher_id::text, namespace, display_name, owner_tenant_id::text,
                   verification, domain, status, status_reason, created_at, updated_at
            FROM marketplace_publishers
            WHERE 1=1
        """
        params = []
        if status:
            query += " AND status = $1"
            params.append(status)
        if verification:
            query += f" AND verification = ${len(params) + 1}"
            params.append(verification)
        query += " ORDER BY created_at DESC"
        
        rows = await conn.fetch(query, *params)
    
    return [PublisherResponse(**dict(row)) for row in rows]


@router.get("/v1/marketplace/publishers/{publisher_id}", response_model=PublisherResponse)
async def get_publisher(publisher_id: str, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.MARKETPLACE_READ)) -> PublisherResponse:
    """Get a publisher."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT publisher_id::text, namespace, display_name, owner_tenant_id::text,
                   verification, domain, status, status_reason, created_at, updated_at
            FROM marketplace_publishers
            WHERE publisher_id = $1
            """,
            uuid.UUID(publisher_id),
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Publisher not found")
    
    return PublisherResponse(**dict(row))


@router.patch("/v1/marketplace/publishers/{publisher_id}", response_model=PublisherResponse)
async def update_publisher(publisher_id: str, request: PublisherUpdateRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.MARKETPLACE_PUBLISH)) -> PublisherResponse:
    """Update a publisher."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE marketplace_publishers SET
                display_name = COALESCE($1, display_name),
                domain = COALESCE($2, domain),
                status = COALESCE($3, status),
                status_reason = COALESCE($4, status_reason),
                updated_at = now()
            WHERE publisher_id = $5
            RETURNING publisher_id::text, namespace, display_name, owner_tenant_id::text,
                      verification, domain, status, status_reason, created_at, updated_at
            """,
            request.display_name,
            request.domain,
            request.status,
            request.status_reason,
            uuid.UUID(publisher_id),
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Publisher not found")
    
    return PublisherResponse(**dict(row))


# Publisher Keys
@router.post("/v1/marketplace/publishers/{publisher_id}/keys", response_model=PublisherKeyResponse)
async def create_publisher_key(publisher_id: str, request: PublisherKeyCreateRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.MARKETPLACE_PUBLISH)) -> PublisherKeyResponse:
    """Add a signing key to a publisher."""
    async with store.connection() as conn:
        # Verify publisher exists
        pub = await conn.fetchrow(
            "SELECT 1 FROM marketplace_publishers WHERE publisher_id = $1",
            uuid.UUID(publisher_id),
        )
        if not pub:
            raise HTTPException(status_code=404, detail="Publisher not found")
        
        # Check key_id uniqueness
        existing = await conn.fetchrow(
            "SELECT 1 FROM marketplace_publisher_keys WHERE key_id = $1",
            request.key_id,
        )
        if existing:
            raise HTTPException(status_code=409, detail=f"Key ID '{request.key_id}' already exists")
        
        row = await conn.fetchrow(
            """
            INSERT INTO marketplace_publisher_keys (
                key_id, publisher_id, public_key, label
            ) VALUES ($1, $2, decode($3, 'hex'), $4)
            RETURNING key_id, publisher_id::text, label, created_at, revoked_at, revoked_reason
            """,
            request.key_id,
            uuid.UUID(publisher_id),
            request.public_key,
            request.label,
        )
    
    return PublisherKeyResponse(**dict(row))


@router.get("/v1/marketplace/publishers/{publisher_id}/keys", response_model=list[PublisherKeyResponse])
async def list_publisher_keys(publisher_id: str, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.MARKETPLACE_READ)) -> list[PublisherKeyResponse]:
    """List publisher keys."""
    async with store.connection() as conn:
        rows = await conn.fetch(
            """
            SELECT key_id, publisher_id::text, label, created_at, revoked_at, revoked_reason
            FROM marketplace_publisher_keys
            WHERE publisher_id = $1
            ORDER BY created_at DESC
            """,
            uuid.UUID(publisher_id),
        )
    
    return [PublisherKeyResponse(**dict(row)) for row in rows]


@router.post("/v1/marketplace/publishers/{publisher_id}/keys/{key_id}/revoke")
async def revoke_publisher_key(publisher_id: str, key_id: str, reason: str | None = None, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.MARKETPLACE_PUBLISH)) -> dict[str, str]:
    """Revoke a publisher key."""
    async with store.connection() as conn:
        result = await conn.execute(
            """
            UPDATE marketplace_publisher_keys
            SET revoked_at = now(), revoked_reason = $1
            WHERE key_id = $2 AND publisher_id = $3
            """,
            reason,
            key_id,
            uuid.UUID(publisher_id),
        )
    
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Key not found")
    
    return {"status": "revoked"}


# Listings
@router.post("/v1/marketplace/listings", response_model=ListingResponse)
async def create_listing(request: ListingCreateRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.MARKETPLACE_PUBLISH)) -> ListingResponse:
    """Create a plugin listing."""
    async with store.connection() as conn:
        # Check plugin_id uniqueness
        existing = await conn.fetchrow(
            "SELECT 1 FROM marketplace_listings WHERE plugin_id = $1",
            request.plugin_id,
        )
        if existing:
            raise HTTPException(status_code=409, detail=f"Plugin ID '{request.plugin_id}' already exists")
        
        # Get publisher for current user (simplified)
        publisher = await conn.fetchrow(
            "SELECT publisher_id FROM marketplace_publishers WHERE owner_tenant_id = $1 LIMIT 1",
            principal.tenant_id,
        )
        if not publisher:
            raise HTTPException(status_code=400, detail="No publisher found for current tenant")
        
        row = await conn.fetchrow(
            """
            INSERT INTO marketplace_listings (
                listing_id, plugin_id, publisher_id, name, display_name, summary,
                categories, keywords, visibility, owner_tenant_id, status
            ) VALUES (
                gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, $8,
                principal.tenant_id, 'active'
            )
            RETURNING listing_id::text, plugin_id, publisher_id::text, name, display_name,
                      summary, categories, keywords, visibility, owner_tenant_id::text,
                      status, status_reason, install_count, created_at, updated_at
            """,
            request.plugin_id,
            publisher["publisher_id"],
            request.name,
            request.display_name,
            request.summary,
            request.categories,
            request.keywords,
            request.visibility,
        )
    
    return ListingResponse(**dict(row))


@router.get("/v1/marketplace/listings", response_model=list[ListingResponse])
async def list_listings(
    publisher_id: str | None = Query(None),
    status: str | None = Query(None, pattern="^(active|taken_down)$"),
    visibility: str | None = Query(None, pattern="^(public|private)$"),
    principal: PrincipalInfo = Depends(tenant_scope),
    _auth: None = requires(Action.MARKETPLACE_READ)

) -> list[ListingResponse]:
    """List plugin listings."""
    async with store.connection() as conn:
        query = """
            SELECT listing_id::text, plugin_id, publisher_id::text, name, display_name,
                   summary, categories, keywords, visibility, owner_tenant_id::text,
                   status, status_reason, install_count, created_at, updated_at
            FROM marketplace_listings
            WHERE 1=1
        """
        params = []
        if publisher_id:
            query += " AND publisher_id = $1"
            params.append(uuid.UUID(publisher_id))
        if status:
            query += f" AND status = ${len(params) + 1}"
            params.append(status)
        if visibility:
            query += f" AND visibility = ${len(params) + 1}"
            params.append(visibility)
        query += " ORDER BY created_at DESC"
        
        rows = await conn.fetch(query, *params)
    
    return [ListingResponse(**dict(row)) for row in rows]


@router.get("/v1/marketplace/listings/{listing_id}", response_model=ListingResponse)
async def get_listing(listing_id: str, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.MARKETPLACE_READ)) -> ListingResponse:
    """Get a plugin listing."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT listing_id::text, plugin_id, publisher_id::text, name, display_name,
                   summary, categories, keywords, visibility, owner_tenant_id::text,
                   status, status_reason, install_count, created_at, updated_at
            FROM marketplace_listings
            WHERE listing_id = $1
            """,
            uuid.UUID(listing_id),
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Listing not found")
    
    return ListingResponse(**dict(row))


@router.patch("/v1/marketplace/listings/{listing_id}", response_model=ListingResponse)
async def update_listing(listing_id: str, request: ListingUpdateRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.MARKETPLACE_PUBLISH)) -> ListingResponse:
    """Update a plugin listing."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE marketplace_listings SET
                display_name = COALESCE($1, display_name),
                summary = COALESCE($2, summary),
                categories = COALESCE($3, categories),
                keywords = COALESCE($4, keywords),
                visibility = COALESCE($5, visibility),
                status = COALESCE($6, status),
                status_reason = COALESCE($7, status_reason),
                updated_at = now()
            WHERE listing_id = $8
            RETURNING listing_id::text, plugin_id, publisher_id::text, name, display_name,
                      summary, categories, keywords, visibility, owner_tenant_id::text,
                      status, status_reason, install_count, created_at, updated_at
            """,
            request.display_name,
            request.summary,
            request.categories,
            request.keywords,
            request.visibility,
            request.status,
            request.status_reason,
            uuid.UUID(listing_id),
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Listing not found")
    
    return ListingResponse(**dict(row))


# Artifacts
@router.post("/v1/marketplace/artifacts", response_model=ArtifactUploadResponse)
async def upload_artifact(
    digest: str = Query(..., pattern=r"^sha256:[0-9a-f]{64}$"),
    content: bytes = b"",  # Would be file upload in real implementation,
    principal: PrincipalInfo = Depends(tenant_scope),
    _auth: None = requires(Action.MARKETPLACE_PUBLISH)

) -> ArtifactUploadResponse:
    """Upload a plugin artifact (content-addressed)."""
    # In real implementation, this would receive multipart/form-data
    # For now, return a mock response
    return ArtifactUploadResponse(
        digest=digest,
        size_bytes=len(content),
    )


# Revisions
@router.post("/v1/marketplace/revisions", response_model=RevisionResponse)
async def create_revision(request: RevisionCreateRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.MARKETPLACE_PUBLISH)) -> RevisionResponse:
    """Create a plugin revision (publish) with automated review, signature verification, and transparency log."""
    async with store.connection() as conn:
        # Verify listing exists
        listing = await conn.fetchrow(
            "SELECT * FROM marketplace_listings WHERE listing_id = $1",
            uuid.UUID(request.listing_id),
        )
        if not listing:
            raise HTTPException(status_code=404, detail="Listing not found")
        
        # Verify signing key exists and belongs to publisher
        key_row = await conn.fetchrow(
            "SELECT public_key FROM marketplace_publisher_keys WHERE key_id = $1 AND publisher_id = $2 AND revoked_at IS NULL",
            request.signing_key_id,
            listing["publisher_id"],
        )
        if not key_row:
            raise HTTPException(status_code=400, detail="Invalid or revoked signing key")
        
        # Verify artifact exists
        artifact = await conn.fetchrow(
            "SELECT 1 FROM marketplace_artifacts WHERE digest = $1",
            request.digest,
        )
        if not artifact:
            raise HTTPException(status_code=400, detail="Artifact not found in store")
        
        # Verify signature
        signature_verified = False
        from app.marketplace.manifest import parse_manifest
        parsed = parse_manifest(request.manifest)
        try:
            public_key = key_row["public_key"]
            verify_signature(
                public_key=public_key,
                signature=request.signature,
                artifact_digest=request.digest,
                manifest=request.manifest,
                provenance=request.provenance,
            )
            signature_verified = True
        except Exception:
            # Signature verification failed, but we still have parsed manifest for review
            pass
        
        # Get previous permissions for diff
        prev_revision = await conn.fetchrow(
            "SELECT permissions FROM marketplace_revisions WHERE listing_id = $1 AND review_state = 'approved' AND yanked_at IS NULL ORDER BY created_at DESC LIMIT 1",
            uuid.UUID(request.listing_id),
        )
        previous_permissions = json.loads(prev_revision["permissions"]) if prev_revision else None
        
        # Provenance verification
        provenance_verified = True
        if request.provenance:
            try:
                from app.marketplace.signing import validate_provenance
                validate_provenance(
                    request.provenance,
                    plugin_id=listing["plugin_id"],
                    version=request.version,
                    artifact_digest=request.digest,
                )
            except Exception:
                provenance_verified = False
        else:
            provenance_verified = None
        
        review_report = run_review(
            parsed=parsed,
            raw_manifest=request.manifest,
            tier=request.tier,
            files=None,
            previous_permissions=previous_permissions,
            signature_verified=signature_verified,
            provenance_verified=provenance_verified,
        )
        
        review_state, effective_tier = decide(request.tier, review_report, community_auto_approve=False)
        
        # Insert revision with review state
        revision_row = await conn.fetchrow(
            """
            INSERT INTO marketplace_revisions (
                revision_id, listing_id, version, major, minor, patch, prerelease,
                digest, manifest, manifest_raw, manifest_digest, source_manifest_version,
                permissions, signature, signing_key_id, provenance, tier,
                review_state, review_report, published_by
            ) VALUES (
                gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                $12, $13, $14, $15, $16, $17, $18, $19
            )
            RETURNING revision_id::text, listing_id::text, version, major, minor, patch,
                      prerelease, digest, manifest, manifest_raw, manifest_digest,
                      source_manifest_version, permissions, signature, signing_key_id,
                      provenance, tier, review_state, review_report, reviewed_by::text,
                      reviewed_at, review_notes, tlog_seq, yanked_at, yank_reason,
                      published_by::text, created_at
            """,

            uuid.UUID(request.listing_id),
            request.version,
            request.major,
            request.minor,
            request.patch,
            request.prerelease,
            request.digest,
            json.dumps(request.manifest),
            json.dumps(request.manifest_raw),
            request.manifest_digest,
            request.source_manifest_version,
            json.dumps(request.permissions),
            json.dumps(request.signature),
            request.signing_key_id,
            json.dumps(request.provenance) if request.provenance else None,
            effective_tier,
            review_state,
            json.dumps(review_report),
            principal.principal_id,
        )
        
        # Insert into transparency log and get the assigned sequence number
        tlog_entry = {
            "revision_id": revision_row["revision_id"],
            "listing_id": request.listing_id,
            "version": request.version,
            "digest": request.digest,
            "manifest_digest": request.manifest_digest,
            "signature": request.signature,
            "signing_key_id": request.signing_key_id,
            "provenance": request.provenance,
            "tier": effective_tier,
            "review_state": review_state,
            "published_by": principal.principal_id,
        }
        tlog_entry_bytes = canonical_json(tlog_entry)
        
        tlog_row = await conn.fetchrow(
            """
            INSERT INTO marketplace_transparency_log (entry, leaf_hash, chain_hash)
            VALUES ($1, $2, $3)
            RETURNING seq
            """,
            tlog_entry_bytes,
            # leaf_hash = SHA-256(0x00 || entry)
            bytes.fromhex("00") + hashlib.sha256(b"\x00" + tlog_entry_bytes).digest(),
            # placeholder for chain_hash, trigger will update
            bytes.fromhex("00") * 32,
        )
        tlog_seq = tlog_row["seq"]
        
        # Update revision with tlog_seq
        await conn.execute(
            "UPDATE marketplace_revisions SET tlog_seq = $1 WHERE revision_id = $2",
            tlog_seq, uuid.UUID(revision_row["revision_id"]),
        )
        
        revision_row["tlog_seq"] = tlog_seq
    
    return RevisionResponse(**dict(revision_row))


@router.get("/v1/marketplace/revisions", response_model=list[RevisionResponse])
async def list_revisions(
    listing_id: str | None = Query(None),
    review_state: str | None = Query(None, pattern="^(submitted|approved|rejected)$"),
    tier: str | None = Query(None, pattern="^(platform|verified|community|private|unreviewed)$"),
    principal: PrincipalInfo = Depends(tenant_scope),
    _auth: None = requires(Action.MARKETPLACE_READ)

) -> list[RevisionResponse]:
    """List plugin revisions."""
    async with store.connection() as conn:
        query = """
            SELECT revision_id::text, listing_id::text, version, major, minor, patch,
                   prerelease, digest, manifest, manifest_raw, manifest_digest,
                   source_manifest_version, permissions, signature, signing_key_id,
                   provenance, tier, review_state, review_report, reviewed_by::text,
                   reviewed_at, review_notes, tlog_seq, yanked_at, yank_reason,
                   published_by::text, created_at
            FROM marketplace_revisions
            WHERE 1=1
        """
        params = []
        if listing_id:
            query += " AND listing_id = $1"
            params.append(uuid.UUID(listing_id))
        if review_state:
            query += f" AND review_state = ${len(params) + 1}"
            params.append(review_state)
        if tier:
            query += f" AND tier = ${len(params) + 1}"
            params.append(tier)
        query += " ORDER BY created_at DESC"
        
        rows = await conn.fetch(query, *params)
    
    return [RevisionResponse(**dict(row)) for row in rows]


@router.get("/v1/marketplace/revisions/{revision_id}", response_model=RevisionResponse)
async def get_revision(revision_id: str, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.MARKETPLACE_READ)) -> RevisionResponse:
    """Get a plugin revision."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT revision_id::text, listing_id::text, version, major, minor, patch,
                   prerelease, digest, manifest, manifest_raw, manifest_digest,
                   source_manifest_version, permissions, signature, signing_key_id,
                   provenance, tier, review_state, review_report, reviewed_by::text,
                   reviewed_at, review_notes, tlog_seq, yanked_at, yank_reason,
                   published_by::text, created_at
            FROM marketplace_revisions
            WHERE revision_id = $1
            """,
            uuid.UUID(revision_id),
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Revision not found")
    
    return RevisionResponse(**dict(row))


@router.post("/v1/marketplace/revisions/{revision_id}/review", response_model=RevisionResponse)
async def review_revision(revision_id: str, request: RevisionReviewRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.MARKETPLACE_PUBLISH)) -> RevisionResponse:
    """Review a plugin revision (approve/reject)."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE marketplace_revisions SET
                review_state = $1,
                review_report = $2,
                review_notes = $3,
                reviewed_by = $4,
                reviewed_at = now()
            WHERE revision_id = $5
            RETURNING revision_id::text, listing_id::text, version, major, minor, patch,
                      prerelease, digest, manifest, manifest_raw, manifest_digest,
                      source_manifest_version, permissions, signature, signing_key_id,
                      provenance, tier, review_state, review_report, reviewed_by::text,
                      reviewed_at, review_notes, tlog_seq, yanked_at, yank_reason,
                      published_by::text, created_at
            """,
            request.review_state,
            json.dumps(request.review_report),
            request.review_notes,
            principal.principal_id,
            uuid.UUID(revision_id),
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Revision not found")
    
    return RevisionResponse(**dict(row))


@router.post("/v1/marketplace/revisions/{revision_id}/yank")
async def yank_revision(revision_id: str, reason: str, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.MARKETPLACE_PUBLISH)) -> dict[str, str]:
    """Yank a plugin revision."""
    async with store.connection() as conn:
        result = await conn.execute(
            """
            UPDATE marketplace_revisions
            SET yanked_at = now(), yank_reason = $1
            WHERE revision_id = $2
            """,
            reason,
            uuid.UUID(revision_id),
        )
    
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Revision not found")
    
    return {"status": "yanked"}


# Installations
@router.post("/v1/marketplace/installations", response_model=InstallationResponse)
async def create_installation(request: InstallationCreateRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.MARKETPLACE_INSTALL)) -> InstallationResponse:
    """Install a plugin revision."""
    async with store.connection() as conn:
        # Verify revision exists and is approved
        revision = await conn.fetchrow(
            """
            SELECT r.*, l.plugin_id, l.name
            FROM marketplace_revisions r
            JOIN marketplace_listings l ON l.listing_id = r.listing_id
            WHERE r.revision_id = $1 AND r.review_state = 'approved' AND r.yanked_at IS NULL
            """,
            uuid.UUID(request.revision_id),
        )
        if not revision:
            raise HTTPException(status_code=404, detail="Revision not found or not approved")
        
        # Check for existing active installation
        existing = await conn.fetchrow(
            """
            SELECT 1 FROM marketplace_installations
            WHERE TRUE  -- tenant isolation via RLS
              AND listing_id = $1 AND status = 'active'
            """,
            uuid.UUID(request.listing_id),
        )
        if existing:
            raise HTTPException(status_code=409, detail="Plugin already installed")
        
        row = await conn.fetchrow(
            """
            INSERT INTO marketplace_installations (
                installation_id, tenant_id, listing_id, revision_id, plugin_id,
                version, digest, consent, consent_digest, installed_by
            ) VALUES (
                gen_random_uuid(),
                principal.tenant_id,
                $1, $2, $3, $4, $5, $6, $7, $8
            )
            RETURNING installation_id::text, tenant_id::text, listing_id::text,
                      revision_id::text, plugin_id, version, digest, consent,
                      consent_digest, status, status_reason, installed_by::text,
                      installed_at, updated_at
            """,
            uuid.UUID(request.listing_id),
            uuid.UUID(request.revision_id),
            revision["plugin_id"],
            revision["version"],
            revision["digest"],
            json.dumps(request.consent),
            request.consent_digest,
            principal.principal_id,
        )
    
    return InstallationResponse(**dict(row))


@router.get("/v1/marketplace/installations", response_model=list[InstallationResponse])
async def list_installations(
    status: str | None = Query(None, pattern="^(active|uninstalled|revoked)$"),
    principal: PrincipalInfo = Depends(tenant_scope),
    _auth: None = requires(Action.MARKETPLACE_READ)

) -> list[InstallationResponse]:
    """List plugin installations."""
    async with store.connection() as conn:
        query = """
            SELECT installation_id::text, tenant_id::text, listing_id::text,
                   revision_id::text, plugin_id, version, digest, consent,
                   consent_digest, status, status_reason, installed_by::text,
                   installed_at, updated_at
            FROM marketplace_installations
            WHERE TRUE  -- tenant isolation via RLS
        """
        params = []
        if status:
            query += " AND status = $1"
            params.append(status)
        query += " ORDER BY installed_at DESC"
        
        rows = await conn.fetch(query, *params)
    
    return [InstallationResponse(**dict(row)) for row in rows]


@router.get("/v1/marketplace/installations/{installation_id}", response_model=InstallationResponse)
async def get_installation(installation_id: str, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.MARKETPLACE_READ)) -> InstallationResponse:
    """Get a plugin installation."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT installation_id::text, tenant_id::text, listing_id::text,
                   revision_id::text, plugin_id, version, digest, consent,
                   consent_digest, status, status_reason, installed_by::text,
                   installed_at, updated_at
            FROM marketplace_installations
            WHERE installation_id = $1
            """,
            uuid.UUID(installation_id),
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Installation not found")
    
    return InstallationResponse(**dict(row))


@router.patch("/v1/marketplace/installations/{installation_id}", response_model=InstallationResponse)
async def update_installation(installation_id: str, request: InstallationUpdateRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.MARKETPLACE_INSTALL)) -> InstallationResponse:
    """Update a plugin installation (uninstall/revoke)."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE marketplace_installations SET
                status = COALESCE($1, status),
                status_reason = COALESCE($2, status_reason),
                updated_at = now()
            WHERE installation_id = $3
            RETURNING installation_id::text, tenant_id::text, listing_id::text,
                      revision_id::text, plugin_id, version, digest, consent,
                      consent_digest, status, status_reason, installed_by::text,
                      installed_at, updated_at
            """,
            request.status,
            request.status_reason,
            uuid.UUID(installation_id),
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Installation not found")
    
    return InstallationResponse(**dict(row))


# Notifications
@router.get("/v1/marketplace/notifications", response_model=list[NotificationResponse])
async def list_notifications(
    unacknowledged_only: bool = Query(True),
    principal: PrincipalInfo = Depends(tenant_scope),
    _auth: None = requires(Action.MARKETPLACE_READ)

) -> list[NotificationResponse]:
    """List marketplace notifications."""
    async with store.connection() as conn:
        query = """
            SELECT notification_id::text, tenant_id::text, kind, plugin_id, version,
                   details, created_at, acknowledged_at
            FROM marketplace_notifications
            WHERE TRUE  -- tenant isolation via RLS
        """
        if unacknowledged_only:
            query += " AND acknowledged_at IS NULL"
        query += " ORDER BY created_at DESC"
        
        rows = await conn.fetch(query)
    
    return [NotificationResponse(**dict(row)) for row in rows]


@router.post("/v1/marketplace/notifications/{notification_id}/acknowledge")
async def acknowledge_notification(notification_id: str, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.MARKETPLACE_INSTALL)) -> dict[str, str]:
    """Acknowledge a notification."""
    async with store.connection() as conn:
        result = await conn.execute(
            """
            UPDATE marketplace_notifications
            SET acknowledged_at = now()
            WHERE notification_id = $1
            """,
            uuid.UUID(notification_id),
        )
    
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Notification not found")
    
    return {"status": "acknowledged"}


# Abuse Reports
@router.post("/v1/marketplace/abuse-reports", response_model=AbuseReportResponse)
async def create_abuse_report(request: AbuseReportCreateRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.MARKETPLACE_READ)) -> AbuseReportResponse:
    """Report abuse for a plugin."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO marketplace_abuse_reports (
                report_id, listing_id, version, reporter_tenant_id, reporter_principal_id,
                reason, details, status
            ) VALUES (
                gen_random_uuid(), $1, $2,
                principal.tenant_id,
                $3, $4, $5, 'open'
            )
            RETURNING report_id::text, listing_id::text, version,
                      reporter_tenant_id::text, reporter_principal_id::text,
                      reason, details, status, resolution, resolved_by::text,
                      resolved_at, created_at
            """,
            uuid.UUID(request.listing_id),
            request.version,
            principal.principal_id,
            request.reason,
            request.details,
        )
    
    return AbuseReportResponse(**dict(row))


@router.get("/v1/marketplace/abuse-reports", response_model=list[AbuseReportResponse])
async def list_abuse_reports(
    status: str | None = Query(None, pattern="^(open|actioned|dismissed)$"),
    principal: PrincipalInfo = Depends(tenant_scope),
    _auth: None = requires(Action.MARKETPLACE_READ)

) -> list[AbuseReportResponse]:
    """List abuse reports."""
    async with store.connection() as conn:
        query = """
            SELECT report_id::text, listing_id::text, version,
                   reporter_tenant_id::text, reporter_principal_id::text,
                   reason, details, status, resolution, resolved_by::text,
                   resolved_at, created_at
            FROM marketplace_abuse_reports
            WHERE 1=1
        """
        params = []
        if status:
            query += " AND status = $1"
            params.append(status)
        query += " ORDER BY created_at DESC"
        
        rows = await conn.fetch(query, *params)
    
    return [AbuseReportResponse(**dict(row)) for row in rows]


# Search
@router.post("/v1/marketplace/search", response_model=SearchResponse)
async def search_marketplace(request: SearchRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.MARKETPLACE_READ)) -> SearchResponse:
    """Search the marketplace."""
    async with store.connection() as conn:
        # Build search query using full-text search
        conditions = ["l.status = 'active'"]
        params = []
        
        if request.query:
            conditions.append("l.search_document @@ plainto_tsquery($1)")
            params.append(request.query)
        
        if request.categories:
            conditions.append("l.categories && $2")
            params.append(request.categories)
        
        if request.keywords:
            conditions.append("l.keywords && $3")
            params.append(request.keywords)
        
        if request.tiers:
            conditions.append("r.tier = ANY($4)")
            params.append(request.tiers)
        
        if request.visibility:
            conditions.append("l.visibility = $5")
            params.append(request.visibility)
        
        where_clause = " AND ".join(conditions)
        
        # Get total count
        count_query = f"""
            SELECT count(*)
            FROM marketplace_listings l
            JOIN marketplace_revisions r ON r.listing_id = l.listing_id
            WHERE {where_clause}
              AND r.review_state = 'approved'
              AND r.yanked_at IS NULL
        """
        total = await conn.fetchval(count_query, *params)
        
        # Get results with latest approved revision
        params.extend([request.limit, request.offset])
        limit_param = len(params) - 1
        offset_param = len(params)
        
        rows = await conn.fetch(
            f"""
            SELECT DISTINCT ON (l.listing_id)
                l.listing_id::text, l.plugin_id, l.publisher_id::text, l.name,
                l.display_name, l.summary, l.categories, l.keywords, l.visibility,
                l.install_count,
                r.revision_id::text, r.listing_id::text, r.version, r.major, r.minor,
                r.patch, r.prerelease, r.digest, r.manifest, r.manifest_raw,
                r.manifest_digest, r.source_manifest_version, r.permissions,
                r.signature, r.signing_key_id, r.provenance, r.tier, r.review_state,
                r.review_report, r.reviewed_by::text, r.reviewed_at, r.review_notes,
                r.tlog_seq, r.yanked_at, r.yank_reason, r.published_by::text, r.created_at
            FROM marketplace_listings l
            JOIN marketplace_revisions r ON r.listing_id = l.listing_id
            WHERE {where_clause}
              AND r.review_state = 'approved'
              AND r.yanked_at IS NULL
            ORDER BY l.listing_id, r.created_at DESC
            LIMIT ${limit_param} OFFSET ${offset_param}
            """,
            *params,
        )
    
    results = []
    for row in rows:
        revision = None
        if row["revision_id"]:
            revision = RevisionResponse(
                revision_id=row["revision_id"],
                listing_id=row["listing_id"],
                version=row["version"],
                major=row["major"],
                minor=row["minor"],
                patch=row["patch"],
                prerelease=row["prerelease"],
                digest=row["digest"],
                manifest=row["manifest"],
                manifest_raw=row["manifest_raw"],
                manifest_digest=row["manifest_digest"],
                source_manifest_version=row["source_manifest_version"],
                permissions=row["permissions"],
                signature=row["signature"],
                signing_key_id=row["signing_key_id"],
                provenance=row["provenance"],
                tier=row["tier"],
                review_state=row["review_state"],
                review_report=row["review_report"],
                reviewed_by=row["reviewed_by"],
                reviewed_at=row["reviewed_at"],
                review_notes=row["review_notes"],
                tlog_seq=row["tlog_seq"],
                yanked_at=row["yanked_at"],
                yank_reason=row["yank_reason"],
                published_by=row["published_by"],
                created_at=row["created_at"],
            )
        
        results.append(SearchResult(
            listing_id=row["listing_id"],
            plugin_id=row["plugin_id"],
            publisher_id=row["publisher_id"],
            name=row["name"],
            display_name=row["display_name"],
            summary=row["summary"],
            categories=row["categories"],
            keywords=row["keywords"],
            visibility=row["visibility"],
            install_count=row["install_count"],
            latest_revision=revision,
        ))
    
    return SearchResponse(
        results=results,
        total=total,
        limit=request.limit,
        offset=request.offset,
    )


# Transparency Log
@router.get("/v1/marketplace/transparency-log")
async def get_transparency_log(
    after_seq: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    principal: PrincipalInfo = Depends(tenant_scope),
    _auth: None = requires(Action.MARKETPLACE_READ)

) -> dict[str, Any]:
    """Get transparency log entries."""
    async with store.connection() as conn:
        rows = await conn.fetch(
            """
            SELECT seq, entry, encode(leaf_hash, 'hex') as leaf_hash,
                   encode(chain_hash, 'hex') as chain_hash, created_at
            FROM marketplace_transparency_log
            WHERE seq > $1
            ORDER BY seq
            LIMIT $2
            """,
            after_seq,
            limit,
        )
    
    return {
        "entries": [dict(row) for row in rows],
        "next_after": rows[-1]["seq"] if rows else after_seq,
    }
