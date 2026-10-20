"""Phase 5 webhooks, quotas, telemetry, DR and Phase 6 cells (owner: OPS)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import PrincipalInfo, requires, requires_any, tenant_scope
from app.core.authorization import Action
from app.main import store

router = APIRouter()


# Webhook Endpoints
class WebhookEndpointCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(..., pattern=r"^https?://[^\s]+$", max_length=2048)
    events: list[str] = Field(default_factory=lambda: ["run.terminal"], min_length=1, max_length=16)
    enabled: bool = True
    description: str = Field("", max_length=500)


class WebhookEndpointResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint_id: str
    tenant_id: str
    url: str
    events: list[str]
    enabled: bool
    description: str
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class WebhookEndpointUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str | None = Field(None, pattern=r"^https?://[^\s]+$", max_length=2048)
    events: list[str] | None = Field(None, min_length=1, max_length=16)
    enabled: bool | None = None
    description: str | None = Field(None, max_length=500)


# Webhook Signing Keys
class WebhookSigningKeyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_id: str = Field(..., pattern=r"^whk_[0-9a-f]{24}$")
    secret_envelope: str = Field(..., min_length=8, max_length=4096)


class WebhookSigningKeyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_id: str
    tenant_id: str
    status: str
    retire_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class WebhookSigningKeyRotateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_key_id: str = Field(..., pattern=r"^whk_[0-9a-f]{24}$")
    new_secret_envelope: str = Field(..., min_length=8, max_length=4096)
    retire_at: datetime | None = None


# Webhook Deliveries
class WebhookDeliveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delivery_id: str
    tenant_id: str
    run_id: str | None
    endpoint_id: str | None
    event_type: str
    url: str
    body: dict[str, Any]
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: datetime
    last_status_code: int | None
    last_error: str | None
    last_latency_ms: int | None
    replay_count: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class WebhookDeliveryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[WebhookDeliveryResponse]
    next_cursor: str | None = None


class WebhookReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delivery_ids: list[str] = Field(..., min_length=1, max_length=100)


# Quotas
class TenantQuotaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_concurrent_runs: int | None = Field(None, gt=0)
    max_inflight_runs: int | None = Field(None, gt=0)
    requests_per_minute: int | None = Field(None, gt=0)
    principal_requests_per_minute: int | None = Field(None, gt=0)
    sse_connections: int | None = Field(None, gt=0)
    weight: int | None = Field(None, ge=1, le=100)


class TenantQuotaResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    max_concurrent_runs: int | None
    max_inflight_runs: int | None
    requests_per_minute: int | None
    principal_requests_per_minute: int | None
    sse_connections: int | None
    weight: int
    updated_at: datetime


# Platform Operations
class RestoreEpochRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(..., min_length=1, max_length=500)
    actor: str = Field(..., min_length=1, max_length=200)


class RestoreEpochResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_epoch: int
    fenced_runs: int
    reset_deliveries: int


class CellCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cell_id: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    region: str = Field(..., min_length=1, max_length=64)
    base_url: str = Field(..., pattern=r"^https?://[^\s]+$")
    dedicated: bool = False


class CellResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cell_id: str
    region: str
    base_url: str
    status: str
    dedicated: bool
    created_at: datetime
    updated_at: datetime


class TenantPlacementResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    slug: str
    cell_id: str
    placement_epoch: int
    state: str
    target_cell_id: str | None
    active_runs: int


class TenantMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_cell_id: str
    expected_epoch: int
    force: bool = False


class TenantMoveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    cell_id: str
    placement_epoch: int
    state: str
    target_cell_id: str | None
    moved_at: datetime | None


class TenantMoveFinishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_epoch: int
    commit: bool = True


# Metrics
class MetricsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metrics: list[dict[str, Any]]


# Webhook Endpoint CRUD
@router.post("/v1/webhooks/endpoints", response_model=WebhookEndpointResponse)
async def create_webhook_endpoint(request: WebhookEndpointCreateRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires_any(Action.RUN_SUBMIT, Action.RUN_READ)) -> WebhookEndpointResponse:
    """Create a webhook endpoint."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO webhook_endpoints (
                endpoint_id, tenant_id, url, events, enabled, description, created_by
            ) VALUES (
                gen_random_uuid(),
                principal.tenant_id,
                $1, $2, $3, $4, $5
            )
            RETURNING endpoint_id::text, tenant_id::text, url, events, enabled, description,
                      created_by::text, created_at, updated_at
            """,
            request.url,
            request.events,
            request.enabled,
            request.description,
            principal.principal_id,
        )
    
    return WebhookEndpointResponse(**dict(row))


@router.get("/v1/webhooks/endpoints", response_model=list[WebhookEndpointResponse])
async def list_webhook_endpoints(
    enabled_only: bool = Query(True),
    principal: PrincipalInfo = Depends(tenant_scope),
    _auth: None = requires_any(Action.RUN_SUBMIT, Action.RUN_READ),
) -> list[WebhookEndpointResponse]:
    """List webhook endpoints."""
    async with store.connection() as conn:
        query = """
            SELECT endpoint_id::text, tenant_id::text, url, events, enabled, description,
                   created_by::text, created_at, updated_at
            FROM webhook_endpoints
            WHERE TRUE  -- tenant isolation via RLS
        """
        if enabled_only:
            query += " AND enabled = true"
        query += " ORDER BY created_at DESC"
        
        rows = await conn.fetch(query)
    
    return [WebhookEndpointResponse(**dict(row)) for row in rows]


@router.get("/v1/webhooks/endpoints/{endpoint_id}", response_model=WebhookEndpointResponse)
async def get_webhook_endpoint(endpoint_id: str, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires_any(Action.RUN_SUBMIT, Action.RUN_READ)) -> WebhookEndpointResponse:
    """Get a webhook endpoint."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT endpoint_id::text, tenant_id::text, url, events, enabled, description,
                   created_by::text, created_at, updated_at
            FROM webhook_endpoints
            WHERE endpoint_id = $1
            """,
            uuid.UUID(endpoint_id),
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    
    return WebhookEndpointResponse(**dict(row))


@router.patch("/v1/webhooks/endpoints/{endpoint_id}", response_model=WebhookEndpointResponse)
async def update_webhook_endpoint(endpoint_id: str, request: WebhookEndpointUpdateRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires_any(Action.RUN_SUBMIT, Action.RUN_READ)) -> WebhookEndpointResponse:
    """Update a webhook endpoint."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE webhook_endpoints SET
                url = COALESCE($1, url),
                events = COALESCE($2, events),
                enabled = COALESCE($3, enabled),
                description = COALESCE($4, description),
                updated_at = now()
            WHERE endpoint_id = $5
            RETURNING endpoint_id::text, tenant_id::text, url, events, enabled, description,
                      created_by::text, created_at, updated_at
            """,
            request.url,
            request.events,
            request.enabled,
            request.description,
            uuid.UUID(endpoint_id),
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    
    return WebhookEndpointResponse(**dict(row))


@router.delete("/v1/webhooks/endpoints/{endpoint_id}")
async def delete_webhook_endpoint(endpoint_id: str, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires_any(Action.RUN_SUBMIT, Action.RUN_READ)) -> dict[str, str]:
    """Delete a webhook endpoint."""
    async with store.connection() as conn:
        result = await conn.execute(
            "DELETE FROM webhook_endpoints WHERE endpoint_id = $1",
            uuid.UUID(endpoint_id),
        )
    
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    
    return {"status": "deleted"}


# Webhook Signing Keys
@router.post("/v1/webhooks/signing-keys", response_model=WebhookSigningKeyResponse)
async def create_webhook_signing_key(request: WebhookSigningKeyCreateRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.RUN_SUBMIT)) -> WebhookSigningKeyResponse:
    """Create a webhook signing key."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO webhook_signing_keys (
                key_id, tenant_id, secret_envelope, status
            ) VALUES (
                $1,
                principal.tenant_id,
                $2, 'active'
            )
            RETURNING key_id, tenant_id::text, status, retire_at, revoked_at, created_at
            """,
            request.key_id,
            request.secret_envelope,
        )
    
    return WebhookSigningKeyResponse(**dict(row))


@router.get("/v1/webhooks/signing-keys", response_model=list[WebhookSigningKeyResponse])
async def list_webhook_signing_keys(
    active_only: bool = Query(True),
    principal: PrincipalInfo = Depends(tenant_scope),
    _auth: None = requires(Action.RUN_SUBMIT)

) -> list[WebhookSigningKeyResponse]:
    """List webhook signing keys."""
    async with store.connection() as conn:
        query = """
            SELECT key_id, tenant_id::text, status, retire_at, revoked_at, created_at
            FROM webhook_signing_keys
            WHERE TRUE  -- tenant isolation via RLS
        """
        if active_only:
            query += " AND status <> 'revoked'"
        query += " ORDER BY created_at DESC"
        
        rows = await conn.fetch(query)
    
    return [WebhookSigningKeyResponse(**dict(row)) for row in rows]


@router.post("/v1/webhooks/signing-keys/{key_id}/rotate", response_model=WebhookSigningKeyResponse)
async def rotate_webhook_signing_key(key_id: str, request: WebhookSigningKeyRotateRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.RUN_SUBMIT)) -> WebhookSigningKeyResponse:
    """Rotate a webhook signing key (mark old as retiring, create new)."""
    async with store.connection() as conn:
        async with conn.transaction():
            # Mark old key as retiring
            await conn.execute(
                """
                UPDATE webhook_signing_keys
                SET status = 'retiring', retire_at = COALESCE($1, now() + interval '30 days')
                WHERE key_id = $2
                """,
                request.retire_at,
                key_id,
            )
            
            # Create new key
            row = await conn.fetchrow(
                """
                INSERT INTO webhook_signing_keys (
                    key_id, tenant_id, secret_envelope, status
                ) VALUES (
                    $1,
                    principal.tenant_id,
                    $2, 'active'
                )
                RETURNING key_id, tenant_id::text, status, retire_at, revoked_at, created_at
                """,
                request.new_key_id,
                request.new_secret_envelope,
            )
    
    return WebhookSigningKeyResponse(**dict(row))


@router.post("/v1/webhooks/signing-keys/{key_id}/revoke")
async def revoke_webhook_signing_key(key_id: str, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.RUN_SUBMIT)) -> dict[str, str]:
    """Revoke a webhook signing key immediately."""
    async with store.connection() as conn:
        result = await conn.execute(
            """
            UPDATE webhook_signing_keys
            SET status = 'revoked', revoked_at = now()
            WHERE key_id = $1
            """,
            key_id,
        )
    
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Signing key not found")
    
    return {"status": "revoked"}


# Webhook Deliveries
@router.get("/v1/webhooks/deliveries", response_model=WebhookDeliveryListResponse)
async def list_webhook_deliveries(
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
    principal: PrincipalInfo = Depends(tenant_scope),
    _auth: None = requires(Action.RUN_SUBMIT)

) -> WebhookDeliveryListResponse:
    """List webhook deliveries with cursor pagination."""
    async with store.connection() as conn:
        if cursor:
            cursor_parts = cursor.split(":")
            cursor_created_at = cursor_parts[0]
            cursor_delivery_id = cursor_parts[1] if len(cursor_parts) > 1 else None
            rows = await conn.fetch(
                """
                SELECT delivery_id::text, tenant_id::text, run_id, endpoint_id::text,
                       event_type, url, body, status, attempts, max_attempts,
                       next_attempt_at, last_status_code, last_error, last_latency_ms,
                       replay_count, created_at, updated_at, completed_at
                FROM webhook_deliveries
                WHERE TRUE  -- tenant isolation via RLS
                  AND ($1::text IS NULL OR status = $1)
                  AND (created_at, delivery_id) > ($2::timestamptz, $3::uuid)
                ORDER BY created_at, delivery_id
                LIMIT $4
                """,
                status,
                cursor_created_at,
                uuid.UUID(cursor_delivery_id) if cursor_delivery_id else None,
                limit + 1,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT delivery_id::text, tenant_id::text, run_id, endpoint_id::text,
                       event_type, url, body, status, attempts, max_attempts,
                       next_attempt_at, last_status_code, last_error, last_latency_ms,
                       replay_count, created_at, updated_at, completed_at
                FROM webhook_deliveries
                WHERE TRUE  -- tenant isolation via RLS
                  AND ($1::text IS NULL OR status = $1)
                ORDER BY created_at, delivery_id
                LIMIT $2
                """,
                status,
                limit + 1,
            )
    
    items = [WebhookDeliveryResponse(**dict(row)) for row in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = f"{last['created_at'].isoformat()}:{last['delivery_id']}"
    
    return WebhookDeliveryListResponse(items=items, next_cursor=next_cursor)


@router.get("/v1/webhooks/deliveries/{delivery_id}", response_model=WebhookDeliveryResponse)
async def get_webhook_delivery(delivery_id: str, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.RUN_SUBMIT)) -> WebhookDeliveryResponse:
    """Get a webhook delivery."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT delivery_id::text, tenant_id::text, run_id, endpoint_id::text,
                   event_type, url, body, status, attempts, max_attempts,
                   next_attempt_at, last_status_code, last_error, last_latency_ms,
                   replay_count, created_at, updated_at, completed_at
            FROM webhook_deliveries
            WHERE delivery_id = $1
            """,
            uuid.UUID(delivery_id),
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Webhook delivery not found")
    
    return WebhookDeliveryResponse(**dict(row))


@router.post("/v1/webhooks/deliveries/replay", response_model=dict[str, int])
async def replay_webhook_deliveries(request: WebhookReplayRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.RUN_SUBMIT)) -> dict[str, int]:
    """Replay webhook deliveries."""
    async with store.connection() as conn:
        async with conn.transaction():
            # Reset deliveries to pending for replay
            result = await conn.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'pending', claim_token = NULL, claim_expires_at = NULL,
                    next_attempt_at = now(), attempts = 0, replay_count = replay_count + 1,
                    updated_at = now()
                WHERE delivery_id = ANY($1)
                """,
                [uuid.UUID(did) for did in request.delivery_ids],
            )
            
            # Extract count from result
            replayed = int(result.split()[-1]) if result else 0
    
    return {"replayed": replayed}


# Quotas
@router.get("/v1/quotas", response_model=TenantQuotaResponse)
async def get_tenant_quota(principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires_any(Action.TENANT_ADMIN, Action.RUN_SUBMIT)) -> TenantQuotaResponse:
    """Get the tenant's quota configuration."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT tenant_id::text, max_concurrent_runs, max_inflight_runs,
                   requests_per_minute, principal_requests_per_minute,
                   sse_connections, weight, updated_at
            FROM tenant_quotas
            WHERE TRUE  -- tenant isolation via RLS
            """,
        )
    
    if not row:
        # Return defaults
        return TenantQuotaResponse(
            tenant_id="default",
            max_concurrent_runs=None,
            max_inflight_runs=None,
            requests_per_minute=None,
            principal_requests_per_minute=None,
            sse_connections=None,
            weight=1,
            updated_at=datetime.now(),
        )
    
    return TenantQuotaResponse(**dict(row))


@router.put("/v1/quotas", response_model=TenantQuotaResponse)
async def set_tenant_quota(request: TenantQuotaRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.TENANT_ADMIN)) -> TenantQuotaResponse:
    """Set or update the tenant's quota configuration."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT haas_ops_set_quota(
                principal.tenant_id,
                $1, $2, $3, $4, $5, $6
            )
            """,
            request.max_concurrent_runs,
            request.max_inflight_runs,
            request.requests_per_minute,
            request.principal_requests_per_minute,
            request.sse_connections,
            request.weight,
        )
    
    return TenantQuotaResponse(**dict(row))


# Platform Operations (admin only)
@router.post("/v1/ops/restore-epoch", response_model=RestoreEpochResponse)
async def bump_restore_epoch(request: RestoreEpochRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.TENANT_ADMIN)) -> RestoreEpochResponse:
    """Bump the platform restore epoch (fences all runs, resets in-flight deliveries)."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM haas_ops_bump_restore_epoch($1, $2)",
            request.reason,
            request.actor,
        )
    
    return RestoreEpochResponse(
        new_epoch=row["new_epoch"],
        fenced_runs=row["fenced_runs"],
        reset_deliveries=row["reset_deliveries"],
    )


@router.get("/v1/ops/metrics", response_model=MetricsResponse)
async def get_platform_metrics(principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.PLATFORM_OPERATE)) -> MetricsResponse:
    """Get platform metrics (admin only)."""
    async with store.connection() as conn:
        rows = await conn.fetch("SELECT * FROM haas_ops_metrics()")
    
    return MetricsResponse(metrics=[dict(row) for row in rows])


# Cells
@router.post("/v1/ops/cells", response_model=CellResponse)
async def create_cell(request: CellCreateRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.PLATFORM_OPERATE)) -> CellResponse:
    """Register a new cell."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO cells (cell_id, region, base_url, dedicated)
            VALUES ($1, $2, $3, $4)
            RETURNING cell_id, region, base_url, status, dedicated, created_at, updated_at
            """,
            request.cell_id,
            request.region,
            request.base_url,
            request.dedicated,
        )
    
    return CellResponse(**dict(row))


@router.get("/v1/ops/cells", response_model=list[CellResponse])
async def list_cells(principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.PLATFORM_OPERATE)) -> list[CellResponse]:
    """List all cells."""
    async with store.connection() as conn:
        rows = await conn.fetch(
            "SELECT cell_id, region, base_url, status, dedicated, created_at, updated_at FROM cells ORDER BY cell_id"
        )
    
    return [CellResponse(**dict(row)) for row in rows]


@router.get("/v1/ops/cells/{cell_id}", response_model=CellResponse)
async def get_cell(cell_id: str, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.PLATFORM_OPERATE)) -> CellResponse:
    """Get a cell."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            "SELECT cell_id, region, base_url, status, dedicated, created_at, updated_at FROM cells WHERE cell_id = $1",
            cell_id,
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Cell not found")
    
    return CellResponse(**dict(row))


@router.patch("/v1/ops/cells/{cell_id}", response_model=CellResponse)
async def update_cell(cell_id: str, status: str = Query(..., pattern="^(active|draining|drained)$"), principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.PLATFORM_OPERATE)) -> CellResponse:
    """Update cell status."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE cells SET status = $1, updated_at = now()
            WHERE cell_id = $2
            RETURNING cell_id, region, base_url, status, dedicated, created_at, updated_at
            """,
            status,
            cell_id,
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Cell not found")
    
    return CellResponse(**dict(row))


# Tenant Placements
@router.get("/v1/ops/placements", response_model=list[TenantPlacementResponse])
async def list_placements(
    cell_id: str | None = Query(None),
    is_default: bool = Query(False),
    principal: PrincipalInfo = Depends(tenant_scope),
    _auth: None = requires(Action.PLATFORM_OPERATE)

) -> list[TenantPlacementResponse]:
    """List tenant placements."""
    async with store.connection() as conn:
        rows = await conn.fetch(
            "SELECT * FROM haas_ops_list_placements($1, $2)",
            cell_id,
            is_default,
        )
    
    return [TenantPlacementResponse(**dict(row)) for row in rows]


@router.post("/v1/ops/placements/{tenant_id}/move", response_model=TenantMoveResponse)
async def begin_tenant_move(tenant_id: str, request: TenantMoveRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.PLATFORM_OPERATE)) -> TenantMoveResponse:
    """Begin moving a tenant to a different cell."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM haas_ops_begin_move($1, $2, $3, $4, $5)",
            uuid.UUID(tenant_id),
            # Current cell will be fetched from tenant_placements
            (await conn.fetchrow("SELECT cell_id FROM tenant_placements WHERE tenant_id = $1", uuid.UUID(tenant_id)))["cell_id"],
            request.target_cell_id,
            request.expected_epoch,
            request.force,
        )
    
    return TenantMoveResponse(**dict(row))


@router.post("/v1/ops/placements/{tenant_id}/move/finish", response_model=TenantMoveResponse)
async def finish_tenant_move(tenant_id: str, request: TenantMoveFinishRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.PLATFORM_OPERATE)) -> TenantMoveResponse:
    """Finish moving a tenant (commit or rollback)."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM haas_ops_finish_move($1, $2, $3)",
            uuid.UUID(tenant_id),
            request.expected_epoch,
            request.commit,
        )
    
    return TenantMoveResponse(**dict(row))


@router.post("/v1/ops/fence-runs")
async def fence_runs(tenant_id: str | None = None, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.PLATFORM_OPERATE)) -> dict[str, int]:
    """Fence runs (increment lease epoch) for a tenant or all tenants."""
    async with store.connection() as conn:
        count = await conn.fetchval(
            "SELECT haas_ops_fence_runs($1)",
            uuid.UUID(tenant_id) if tenant_id else None,
        )
    
    return {"fenced_runs": count}
