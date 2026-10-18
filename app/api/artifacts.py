"""Phase 4 artifact service and Phase 5 retention (owner: DATA)."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from app.main import store

router = APIRouter()

ARTIFACTS_STORAGE_ROOT = Path(os.getenv("HAAS_ARTIFACTS_STORAGE_DIR", "/var/lib/haas/artifacts")).resolve()


class ArtifactCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    name: str = Field(..., min_length=1, max_length=255)
    content_type: str = Field(..., min_length=1, max_length=255)
    source: str = Field(..., pattern="^(api|tool)$")
    created_by: str | None = Field(None, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None


class ArtifactCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: str
    object_key: str
    size_bytes: int
    sha256: str
    upload_url: str | None = None


class ArtifactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: str
    tenant_id: str
    run_id: str
    name: str
    content_type: str
    size_bytes: int
    sha256: str
    object_key: str
    source: str
    created_by: str | None
    metadata: dict[str, Any]
    created_at: datetime
    expires_at: datetime | None
    deleted_at: datetime | None


class ArtifactListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ArtifactResponse]
    next_cursor: str | None = None


class RetentionPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_events_ttl_days: int | None = Field(None, ge=0)
    artifacts_ttl_days: int | None = Field(None, ge=0)
    messages_ttl_days: int | None = Field(None, ge=0)
    artifact_quota_bytes: int | None = Field(None, ge=0)


class RetentionPolicyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_uuid: str
    run_events_ttl_days: int | None
    artifacts_ttl_days: int | None
    messages_ttl_days: int | None
    artifact_quota_bytes: int | None
    updated_by: str | None
    updated_at: datetime


class LegalHoldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str = Field(..., pattern="^(tenant|run|thread)$")
    subject_id: str | None = Field(None, min_length=1, max_length=255)
    reason: str = Field(..., min_length=1, max_length=1000)


class LegalHoldResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hold_id: str
    tenant_uuid: str
    scope: str
    subject_id: str | None
    reason: str
    placed_by: str | None
    placed_at: datetime
    released_by: str | None
    released_at: datetime | None
    release_reason: str | None


class ErasureRequestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(..., pattern="^(subject|offboarding)$")
    subject_type: str | None = Field(None, pattern="^(run|thread)$")
    subject_ids: list[str] = Field(default_factory=list)
    reason: str | None = Field(None, max_length=1000)


class ErasureRequestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    tenant_uuid: str
    kind: str
    subject_type: str | None
    subject_ids: list[str]
    reason: str | None
    status: str
    requested_by: str | None
    requested_at: datetime
    attempts: int
    last_error: str | None
    completed_at: datetime | None
    manifest_sha256: str | None
    receipt_hmac: str | None
    receipt_key_id: str | None


class RetentionTombstoneResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tombstone_id: str
    tenant_id: str
    tenant_uuid: str
    data_class: str
    subject_type: str
    subject_id: str
    reason: str
    policy: dict[str, Any]
    records_erased: int
    bytes_erased: int
    manifest_sha256: str
    request_id: str | None
    erased_by: str
    erased_at: datetime


def _compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _generate_object_key(tenant_id: str, run_id: str, artifact_id: str, name: str) -> str:
    return f"{tenant_id}/{run_id}/{artifact_id}/{name}"


@router.post("/v1/artifacts", response_model=ArtifactCreateResponse)
async def create_artifact(
    request: ArtifactCreateRequest,
    file: UploadFile = File(...),
) -> ArtifactCreateResponse:
    """Create a new artifact by uploading content."""
    # Read file content
    content = await file.read()
    size_bytes = len(content)
    sha256 = _compute_sha256(content)
    
    # Generate artifact ID and object key
    artifact_id = str(uuid.uuid4())
    
    # Get tenant_id from database
    async with store.connection() as conn:
        tenant_row = await conn.fetchrow(
            "SELECT tenant_id::text FROM tenants WHERE status = 'active' LIMIT 1"
        )
        tenant_id = tenant_row["tenant_id"] if tenant_row else "default"
    
    object_key = _generate_object_key(tenant_id, request.run_id, artifact_id, request.name)
    
    # Store the artifact content
    storage_path = ARTIFACTS_STORAGE_ROOT / object_key
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_bytes(content)
    
    # Insert artifact record
    async with store.connection() as conn:
        await conn.execute(
            """
            INSERT INTO artifacts (
                artifact_id, tenant_id, tenant_uuid, run_id, name, content_type,
                size_bytes, sha256, object_key, source, created_by, metadata,
                created_at, expires_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, now(), $13)
            """,
            uuid.UUID(artifact_id),
            tenant_id,
            uuid.UUID(tenant_id),
            request.run_id,
            request.name,
            request.content_type,
            size_bytes,
            sha256,
            object_key,
            request.source,
            request.created_by,
            json.dumps(request.metadata),
            request.expires_at,
        )
    
    return ArtifactCreateResponse(
        artifact_id=artifact_id,
        object_key=object_key,
        size_bytes=size_bytes,
        sha256=sha256,
        upload_url=None,  # Could implement presigned URLs for large files
    )


@router.get("/v1/artifacts/{artifact_id}", response_model=ArtifactResponse)
async def get_artifact(artifact_id: str) -> ArtifactResponse:
    """Get artifact metadata."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT artifact_id::text, tenant_id, tenant_uuid::text, run_id, name,
                   content_type, size_bytes, sha256, object_key, source, created_by,
                   metadata, created_at, expires_at, deleted_at
            FROM artifacts
            WHERE artifact_id = $1
            """,
            uuid.UUID(artifact_id),
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Artifact not found")
    
    return ArtifactResponse(**dict(row))


@router.get("/v1/artifacts", response_model=ArtifactListResponse)
async def list_artifacts(
    run_id: str = Query(...),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
) -> ArtifactListResponse:
    """List artifacts for a run with cursor pagination."""
    async with store.connection() as conn:
        if cursor:
            cursor_parts = cursor.split(":")
            cursor_created_at = cursor_parts[0]
            cursor_artifact_id = cursor_parts[1] if len(cursor_parts) > 1 else None
            rows = await conn.fetch(
                """
                SELECT artifact_id::text, tenant_id, tenant_uuid::text, run_id, name,
                       content_type, size_bytes, sha256, object_key, source, created_by,
                       metadata, created_at, expires_at, deleted_at
                FROM artifacts
                WHERE run_id = $1
                  AND (created_at, artifact_id) > ($2::timestamptz, $3::uuid)
                ORDER BY created_at, artifact_id
                LIMIT $4
                """,
                run_id,
                cursor_created_at,
                uuid.UUID(cursor_artifact_id) if cursor_artifact_id else None,
                limit + 1,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT artifact_id::text, tenant_id, tenant_uuid::text, run_id, name,
                       content_type, size_bytes, sha256, object_key, source, created_by,
                       metadata, created_at, expires_at, deleted_at
                FROM artifacts
                WHERE run_id = $1
                ORDER BY created_at, artifact_id
                LIMIT $2
                """,
                run_id,
                limit + 1,
            )
    
    items = [ArtifactResponse(**dict(row)) for row in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = f"{last['created_at'].isoformat()}:{last['artifact_id']}"
    
    return ArtifactListResponse(items=items, next_cursor=next_cursor)


@router.get("/v1/artifacts/{artifact_id}/download")
async def download_artifact(artifact_id: str):
    """Download artifact content."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            "SELECT object_key, content_type, name FROM artifacts WHERE artifact_id = $1",
            uuid.UUID(artifact_id),
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Artifact not found")
    
    storage_path = ARTIFACTS_STORAGE_ROOT / row["object_key"]
    if not storage_path.exists():
        raise HTTPException(status_code=404, detail="Artifact content not found")
    
    from fastapi.responses import FileResponse
    return FileResponse(
        storage_path,
        media_type=row["content_type"],
        filename=row["name"],
    )


@router.delete("/v1/artifacts/{artifact_id}")
async def delete_artifact(artifact_id: str) -> dict[str, str]:
    """Soft delete an artifact (marks deleted_at)."""
    async with store.connection() as conn:
        result = await conn.execute(
            "UPDATE artifacts SET deleted_at = now() WHERE artifact_id = $1",
            uuid.UUID(artifact_id),
        )
    
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Artifact not found")
    
    return {"status": "deleted"}


# Retention Policy endpoints
@router.get("/v1/retention/policy", response_model=RetentionPolicyResponse)
async def get_retention_policy() -> RetentionPolicyResponse:
    """Get the tenant's retention policy."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT tenant_uuid::text, run_events_ttl_days, artifacts_ttl_days,
                   messages_ttl_days, artifact_quota_bytes, updated_by::text, updated_at
            FROM retention_policies
            WHERE tenant_uuid = (SELECT tenant_id FROM tenants WHERE status = 'active' LIMIT 1)
            """,
        )
    
    if not row:
        # Return default policy
        return RetentionPolicyResponse(
            tenant_uuid="default",
            run_events_ttl_days=None,
            artifacts_ttl_days=None,
            messages_ttl_days=None,
            artifact_quota_bytes=None,
            updated_by=None,
            updated_at=datetime.now(),
        )
    
    return RetentionPolicyResponse(**dict(row))


@router.put("/v1/retention/policy", response_model=RetentionPolicyResponse)
async def set_retention_policy(request: RetentionPolicyRequest) -> RetentionPolicyResponse:
    """Set or update the tenant's retention policy."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO retention_policies (
                tenant_uuid, run_events_ttl_days, artifacts_ttl_days,
                messages_ttl_days, artifact_quota_bytes, updated_by, updated_at
            ) VALUES (
                (SELECT tenant_id FROM tenants WHERE status = 'active' LIMIT 1),
                $1, $2, $3, $4, $5, now()
            )
            ON CONFLICT (tenant_uuid) DO UPDATE SET
                run_events_ttl_days = EXCLUDED.run_events_ttl_days,
                artifacts_ttl_days = EXCLUDED.artifacts_ttl_days,
                messages_ttl_days = EXCLUDED.messages_ttl_days,
                artifact_quota_bytes = EXCLUDED.artifact_quota_bytes,
                updated_by = EXCLUDED.updated_by,
                updated_at = now()
            RETURNING tenant_uuid::text, run_events_ttl_days, artifacts_ttl_days,
                      messages_ttl_days, artifact_quota_bytes, updated_by::text, updated_at
            """,
            request.run_events_ttl_days,
            request.artifacts_ttl_days,
            request.messages_ttl_days,
            request.artifact_quota_bytes,
            "current_user",  # Would come from auth context
        )
    
    return RetentionPolicyResponse(**dict(row))


# Legal Hold endpoints
@router.post("/v1/retention/legal-holds", response_model=LegalHoldResponse)
async def create_legal_hold(request: LegalHoldRequest) -> LegalHoldResponse:
    """Create a legal hold."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO legal_holds (
                hold_id, tenant_uuid, scope, subject_id, reason, placed_by, placed_at
            ) VALUES (
                gen_random_uuid(),
                (SELECT tenant_id FROM tenants WHERE status = 'active' LIMIT 1),
                $1, $2, $3, $4, now()
            )
            RETURNING hold_id::text, tenant_uuid::text, scope, subject_id, reason,
                      placed_by::text, placed_at, released_by::text, released_at, release_reason
            """,
            request.scope,
            request.subject_id,
            request.reason,
            "current_user",  # Would come from auth context
        )
    
    return LegalHoldResponse(**dict(row))


@router.get("/v1/retention/legal-holds", response_model=list[LegalHoldResponse])
async def list_legal_holds(
    scope: str | None = Query(None),
    active_only: bool = Query(True),
) -> list[LegalHoldResponse]:
    """List legal holds."""
    async with store.connection() as conn:
        query = """
            SELECT hold_id::text, tenant_uuid::text, scope, subject_id, reason,
                   placed_by::text, placed_at, released_by::text, released_at, release_reason
            FROM legal_holds
            WHERE tenant_uuid = (SELECT tenant_id FROM tenants WHERE status = 'active' LIMIT 1)
        """
        params = []
        if scope:
            query += " AND scope = $1"
            params.append(scope)
        if active_only:
            query += " AND released_at IS NULL"
        query += " ORDER BY placed_at DESC"
        
        rows = await conn.fetch(query, *params)
    
    return [LegalHoldResponse(**dict(row)) for row in rows]


@router.post("/v1/retention/legal-holds/{hold_id}/release", response_model=LegalHoldResponse)
async def release_legal_hold(hold_id: str, release_reason: str | None = None) -> LegalHoldResponse:
    """Release a legal hold."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE legal_holds
            SET released_by = $1, released_at = now(), release_reason = $2
            WHERE hold_id = $3
            RETURNING hold_id::text, tenant_uuid::text, scope, subject_id, reason,
                      placed_by::text, placed_at, released_by::text, released_at, release_reason
            """,
            "current_user",  # Would come from auth context
            release_reason,
            uuid.UUID(hold_id),
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Legal hold not found")
    
    return LegalHoldResponse(**dict(row))


# Erasure Request endpoints
@router.post("/v1/retention/erasure-requests", response_model=ErasureRequestResponse)
async def create_erasure_request(request: ErasureRequestRequest) -> ErasureRequestResponse:
    """Create an erasure request."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO erasure_requests (
                request_id, tenant_uuid, kind, subject_type, subject_ids,
                reason, requested_by, requested_at
            ) VALUES (
                gen_random_uuid(),
                (SELECT tenant_id FROM tenants WHERE status = 'active' LIMIT 1),
                $1, $2, $3, $4, $5, now()
            )
            RETURNING request_id::text, tenant_uuid::text, kind, subject_type, subject_ids,
                      reason, status, requested_by::text, requested_at, attempts,
                      last_error, completed_at, manifest_sha256, receipt_hmac, receipt_key_id
            """,
            request.kind,
            request.subject_type,
            request.subject_ids,
            request.reason,
            "current_user",  # Would come from auth context
        )
    
    return ErasureRequestResponse(**dict(row))


@router.get("/v1/retention/erasure-requests", response_model=list[ErasureRequestResponse])
async def list_erasure_requests(
    status: str | None = Query(None),
) -> list[ErasureRequestResponse]:
    """List erasure requests."""
    async with store.connection() as conn:
        query = """
            SELECT request_id::text, tenant_uuid::text, kind, subject_type, subject_ids,
                   reason, status, requested_by::text, requested_at, attempts,
                   last_error, completed_at, manifest_sha256, receipt_hmac, receipt_key_id
            FROM erasure_requests
            WHERE tenant_uuid = (SELECT tenant_id FROM tenants WHERE status = 'active' LIMIT 1)
        """
        params = []
        if status:
            query += " AND status = $1"
            params.append(status)
        query += " ORDER BY requested_at DESC"
        
        rows = await conn.fetch(query, *params)
    
    return [ErasureRequestResponse(**dict(row)) for row in rows]


@router.get("/v1/retention/tombstones", response_model=list[RetentionTombstoneResponse])
async def list_retention_tombstones(
    data_class: str | None = Query(None),
    subject_type: str | None = Query(None),
    subject_id: str | None = Query(None),
) -> list[RetentionTombstoneResponse]:
    """List retention tombstones."""
    async with store.connection() as conn:
        query = """
            SELECT tombstone_id::text, tenant_id, tenant_uuid::text, data_class,
                   subject_type, subject_id, reason, policy, records_erased,
                   bytes_erased, manifest_sha256, request_id::text, erased_by, erased_at
            FROM retention_tombstones
            WHERE tenant_uuid = (SELECT tenant_id FROM tenants WHERE status = 'active' LIMIT 1)
        """
        params = []
        if data_class:
            query += " AND data_class = $1"
            params.append(data_class)
        if subject_type:
            query += " AND subject_type = $2"
            params.append(subject_type)
        if subject_id:
            query += " AND subject_id = $3"
            params.append(subject_id)
        query += " ORDER BY erased_at DESC"
        
        rows = await conn.fetch(query, *params)
    
    return [RetentionTombstoneResponse(**dict(row)) for row in rows]
