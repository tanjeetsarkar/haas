"""Phase 4 credential broker, egress policy and redaction (owner: SEC)."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import PrincipalInfo, requires, tenant_scope
from app.core.authorization import Action

from app.main import store

router = APIRouter()


class SecretBindingCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    provider: str = Field(..., pattern="^(inline|env)$")
    provider_reference: str | None = None
    encrypted_value: str | None = None
    allowed_plugins: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    description: str = Field("", max_length=500)


class SecretBindingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding_id: str
    tenant_id: str
    name: str
    provider: str
    provider_reference: str | None
    version: int
    allowed_plugins: list[str]
    allowed_tools: list[str]
    description: str
    created_by: str | None
    created_at: datetime
    rotated_at: datetime | None
    deleted_at: datetime | None


class SecretBindingUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str | None = Field(None, pattern="^(inline|env)$")
    provider_reference: str | None = None
    encrypted_value: str | None = None
    version: int | None = None
    allowed_plugins: list[str] | None = None
    allowed_tools: list[str] | None = None
    description: str | None = Field(None, max_length=500)


class CredentialGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    invocation_id: str
    secret_name: str
    source: str = Field(..., pattern="^(binding|run)$")
    binding_id: str | None = None
    binding_version: int | None = None
    audience: str
    plugin_digest: str = ""


class CredentialGrantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grant_id: str
    tenant_id: str
    run_id: str
    invocation_id: str
    secret_name: str
    source: str
    binding_id: str | None
    binding_version: int | None
    audience: str
    plugin_digest: str
    created_at: datetime
    expires_at: datetime
    redeemed_at: datetime | None
    denied_reason: str | None


class EgressPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_hosts: list[str] = Field(default_factory=list)
    allow_cidrs: list[str] = Field(default_factory=list)
    max_bytes_per_run: int | None = Field(None, gt=0)


class EgressPolicyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    allow_hosts: list[str]
    allow_cidrs: list[str]
    max_bytes_per_run: int | None
    version: int
    updated_by: str | None
    updated_at: datetime


class RedactionRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=128)
    pattern: str = Field(..., min_length=1)
    replacement: str = "[REDACTED]"
    applies_to: list[str] = Field(default_factory=lambda: ["model", "events", "logs", "callbacks"])
    enabled: bool = True


class RedactionRuleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str
    tenant_id: str
    name: str
    pattern: str
    replacement: str
    applies_to: list[str]
    enabled: bool
    created_at: datetime
    updated_at: datetime


class RedactionTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    rules: list[str] | None = None  # rule IDs to test, None = all enabled


class RedactionTestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original: str
    redacted: str
    matches: list[dict[str, Any]]


# Secret Binding endpoints
@router.post("/v1/secrets/bindings", response_model=SecretBindingResponse)
async def create_secret_binding(request: SecretBindingCreateRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.SECRET_ADMIN)) -> SecretBindingResponse:
    """Create a new secret binding."""
    # Validate provider shape
    if request.provider == "inline":
        if not request.encrypted_value:
            raise HTTPException(status_code=400, detail="encrypted_value required for inline provider")
        if request.provider_reference:
            raise HTTPException(status_code=400, detail="provider_reference not allowed for inline provider")
    elif request.provider == "env":
        if not request.provider_reference:
            raise HTTPException(status_code=400, detail="provider_reference required for env provider")
        if request.encrypted_value:
            raise HTTPException(status_code=400, detail="encrypted_value not allowed for env provider")
    
    async with store.connection() as conn:
        # Check for duplicate name
        existing = await conn.fetchrow(
            "SELECT 1 FROM secret_bindings WHERE TRUE  -- tenant isolation via RLS AND name = $1 AND deleted_at IS NULL",
            request.name,
        )
        if existing:
            raise HTTPException(status_code=409, detail=f"Secret binding '{request.name}' already exists")
        
        row = await conn.fetchrow(
            """
            INSERT INTO secret_bindings (
                binding_id, tenant_id, name, provider, provider_reference,
                encrypted_value, allowed_plugins, allowed_tools, description, created_by
            ) VALUES (
                gen_random_uuid(),
                principal.tenant_id,
                $1, $2, $3, $4, $5, $6, $7, $8
            )
            RETURNING binding_id::text, tenant_id::text, name, provider, provider_reference,
                      version, allowed_plugins, allowed_tools, description,
                      created_by::text, created_at, rotated_at, deleted_at
            """,
            request.name,
            request.provider,
            request.provider_reference,
            request.encrypted_value,
            request.allowed_plugins,
            request.allowed_tools,
            request.description,
            principal.principal_id,  # Would come from auth context
        )
    
    return SecretBindingResponse(**dict(row))


@router.get("/v1/secrets/bindings", response_model=list[SecretBindingResponse])
async def list_secret_bindings(
    include_deleted: bool = Query(False),
    principal: PrincipalInfo = Depends(tenant_scope),
    _auth: None = requires(Action.SECRET_ADMIN),
) -> list[SecretBindingResponse]:
    """List secret bindings."""
    async with store.connection() as conn:
        query = """
            SELECT binding_id::text, tenant_id::text, name, provider, provider_reference,
                   version, allowed_plugins, allowed_tools, description,
                   created_by::text, created_at, rotated_at, deleted_at
            FROM secret_bindings
            WHERE TRUE  -- tenant isolation via RLS
        """
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        query += " ORDER BY created_at DESC"
        
        rows = await conn.fetch(query)
    
    return [SecretBindingResponse(**dict(row)) for row in rows]


@router.get("/v1/secrets/bindings/{binding_id}", response_model=SecretBindingResponse)
async def get_secret_binding(binding_id: str, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.SECRET_ADMIN)) -> SecretBindingResponse:
    """Get a secret binding."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT binding_id::text, tenant_id::text, name, provider, provider_reference,
                   version, allowed_plugins, allowed_tools, description,
                   created_by::text, created_at, rotated_at, deleted_at
            FROM secret_bindings
            WHERE binding_id = $1
            """,
            uuid.UUID(binding_id),
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Secret binding not found")
    
    return SecretBindingResponse(**dict(row))


@router.patch("/v1/secrets/bindings/{binding_id}", response_model=SecretBindingResponse)
async def update_secret_binding(binding_id: str, request: SecretBindingUpdateRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.SECRET_ADMIN)) -> SecretBindingResponse:
    """Update a secret binding."""
    async with store.connection() as conn:
        # Get current binding
        current = await conn.fetchrow(
            "SELECT * FROM secret_bindings WHERE binding_id = $1",
            uuid.UUID(binding_id),
        )
        if not current:
            raise HTTPException(status_code=404, detail="Secret binding not found")
        
        # Validate provider shape
        provider = request.provider or current["provider"]
        provider_reference = request.provider_reference if request.provider_reference is not None else current["provider_reference"]
        encrypted_value = request.encrypted_value if request.encrypted_value is not None else current["encrypted_value"]
        
        if provider == "inline":
            if not encrypted_value:
                raise HTTPException(status_code=400, detail="encrypted_value required for inline provider")
            if provider_reference:
                raise HTTPException(status_code=400, detail="provider_reference not allowed for inline provider")
        elif provider == "env":
            if not provider_reference:
                raise HTTPException(status_code=400, detail="provider_reference required for env provider")
            if encrypted_value:
                raise HTTPException(status_code=400, detail="encrypted_value not allowed for env provider")
        
        row = await conn.fetchrow(
            """
            UPDATE secret_bindings SET
                provider = $1,
                provider_reference = $2,
                encrypted_value = $3,
                version = version + 1,
                allowed_plugins = COALESCE($4, allowed_plugins),
                allowed_tools = COALESCE($5, allowed_tools),
                description = COALESCE($6, description),
                rotated_at = now()
            WHERE binding_id = $7
            RETURNING binding_id::text, tenant_id::text, name, provider, provider_reference,
                      version, allowed_plugins, allowed_tools, description,
                      created_by::text, created_at, rotated_at, deleted_at
            """,
            provider,
            provider_reference,
            encrypted_value,
            request.allowed_plugins,
            request.allowed_tools,
            request.description,
            uuid.UUID(binding_id),
        )
    
    return SecretBindingResponse(**dict(row))


@router.delete("/v1/secrets/bindings/{binding_id}")
async def delete_secret_binding(binding_id: str, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.SECRET_ADMIN)) -> dict[str, str]:
    """Soft delete a secret binding."""
    async with store.connection() as conn:
        result = await conn.execute(
            "UPDATE secret_bindings SET deleted_at = now() WHERE binding_id = $1",
            uuid.UUID(binding_id),
        )
    
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Secret binding not found")
    
    return {"status": "deleted"}


# Credential Grant endpoints
@router.post("/v1/credentials/grants", response_model=CredentialGrantResponse)
async def create_credential_grant(request: CredentialGrantRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.SECRET_ADMIN)) -> CredentialGrantResponse:
    """Create a credential grant for a tool invocation."""
    async with store.connection() as conn:
        # Validate binding if provided
        if request.source == "binding" and request.binding_id:
            binding = await conn.fetchrow(
                "SELECT * FROM secret_bindings WHERE binding_id = $1 AND deleted_at IS NULL",
                uuid.UUID(request.binding_id),
            )
            if not binding:
                raise HTTPException(status_code=404, detail="Secret binding not found")
            if request.binding_version and binding["version"] != request.binding_version:
                raise HTTPException(status_code=409, detail="Binding version mismatch")
        
        # Create grant with 5-minute TTL
        expires_at = datetime.now() + timedelta(minutes=5)
        
        row = await conn.fetchrow(
            """
            INSERT INTO credential_grants (
                grant_id, tenant_id, run_id, invocation_id, secret_name,
                source, binding_id, binding_version, audience, plugin_digest,
                created_at, expires_at
            ) VALUES (
                gen_random_uuid(),
                principal.tenant_id,
                $1, $2, $3, $4, $5, $6, $7, $8, now(), $9
            )
            RETURNING grant_id::text, tenant_id::text, run_id, invocation_id, secret_name,
                      source, binding_id::text, binding_version, audience, plugin_digest,
                      created_at, expires_at, redeemed_at, denied_reason
            """,
            request.run_id,
            request.invocation_id,
            request.secret_name,
            request.source,
            uuid.UUID(request.binding_id) if request.binding_id else None,
            request.binding_version,
            request.audience,
            request.plugin_digest,
            expires_at,
        )
    
    return CredentialGrantResponse(**dict(row))


@router.get("/v1/credentials/grants/{grant_id}", response_model=CredentialGrantResponse)
async def get_credential_grant(grant_id: str, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.SECRET_ADMIN)) -> CredentialGrantResponse:
    """Get a credential grant (for tool worker to redeem)."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT grant_id::text, tenant_id::text, run_id, invocation_id, secret_name,
                   source, binding_id::text, binding_version, audience, plugin_digest,
                   created_at, expires_at, redeemed_at, denied_reason
            FROM credential_grants
            WHERE grant_id = $1
            """,
            uuid.UUID(grant_id),
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Credential grant not found")
    
    return CredentialGrantResponse(**dict(row))


@router.post("/v1/credentials/grants/{grant_id}/redeem")
async def redeem_credential_grant(grant_id: str, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.SECRET_ADMIN)) -> dict[str, Any]:
    """Redeem a credential grant - returns the secret value (once)."""
    async with store.connection() as conn:
        async with conn.transaction():
            # Get grant with FOR UPDATE to prevent concurrent redemption
            grant = await conn.fetchrow(
                """
                SELECT * FROM credential_grants
                WHERE grant_id = $1
                FOR UPDATE
                """,
                uuid.UUID(grant_id),
            )
            
            if not grant:
                raise HTTPException(status_code=404, detail="Credential grant not found")
            
            if grant["redeemed_at"]:
                raise HTTPException(status_code=409, detail="Grant already redeemed")
            
            if grant["expires_at"] < datetime.now():
                await conn.execute(
                    "UPDATE credential_grants SET denied_reason = 'expired' WHERE grant_id = $1",
                    uuid.UUID(grant_id),
                )
                raise HTTPException(status_code=410, detail="Grant expired")
            
            # Get the secret value
            secret_value = None
            if grant["source"] == "binding" and grant["binding_id"]:
                binding = await conn.fetchrow(
                    "SELECT encrypted_value, provider, provider_reference FROM secret_bindings WHERE binding_id = $1",
                    grant["binding_id"],
                )
                if binding:
                    if binding["provider"] == "inline":
                        # In production, decrypt the value
                        secret_value = binding["encrypted_value"]  # Placeholder - would decrypt
                    elif binding["provider"] == "env":
                        secret_value = os.getenv(binding["provider_reference"])
            
            # Mark as redeemed
            await conn.execute(
                "UPDATE credential_grants SET redeemed_at = now() WHERE grant_id = $1",
                uuid.UUID(grant_id),
            )
            
            return {
                "secret_value": secret_value,
                "audience": grant["audience"],
                "plugin_digest": grant["plugin_digest"],
            }


# Egress Policy endpoints
@router.get("/v1/egress/policy", response_model=EgressPolicyResponse)
async def get_egress_policy(principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.SECRET_ADMIN)) -> EgressPolicyResponse:
    """Get the tenant's egress policy."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT tenant_id::text, allow_hosts, allow_cidrs, max_bytes_per_run,
                   version, updated_by::text, updated_at
            FROM egress_policies
            WHERE TRUE  -- tenant isolation via RLS
            """,
        )
    
    if not row:
        # Return default policy (deny all)
        return EgressPolicyResponse(
            tenant_id="default",
            allow_hosts=[],
            allow_cidrs=[],
            max_bytes_per_run=None,
            version=1,
            updated_by=None,
            updated_at=datetime.now(),
        )
    
    return EgressPolicyResponse(**dict(row))


@router.put("/v1/egress/policy", response_model=EgressPolicyResponse)
async def set_egress_policy(request: EgressPolicyRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.SECRET_ADMIN)) -> EgressPolicyResponse:
    """Set or update the tenant's egress policy."""
    async with store.connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO egress_policies (
                tenant_id, allow_hosts, allow_cidrs, max_bytes_per_run,
                version, updated_by, updated_at
            ) VALUES (
                principal.tenant_id,
                $1, $2, $3, 1, $4, now()
            )
            ON CONFLICT (tenant_id) DO UPDATE SET
                allow_hosts = EXCLUDED.allow_hosts,
                allow_cidrs = EXCLUDED.allow_cidrs,
                max_bytes_per_run = EXCLUDED.max_bytes_per_run,
                version = egress_policies.version + 1,
                updated_by = EXCLUDED.updated_by,
                updated_at = now()
            RETURNING tenant_id::text, allow_hosts, allow_cidrs, max_bytes_per_run,
                      version, updated_by::text, updated_at
            """,
            request.allow_hosts,
            request.allow_cidrs,
            request.max_bytes_per_run,
            principal.principal_id,  # Would come from auth context
        )
    
    return EgressPolicyResponse(**dict(row))


@router.post("/v1/egress/check")
async def check_egress_allowed(url: str, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.SECRET_ADMIN)) -> dict[str, Any]:
    """Check if a URL is allowed by the egress policy."""
    from urllib.parse import urlparse
    import ipaddress
    
    async with store.connection() as conn:
        policy = await conn.fetchrow(
            "SELECT allow_hosts, allow_cidrs FROM egress_policies WHERE TRUE  -- tenant isolation via RLS"
        )
    
    if not policy:
        return {"allowed": False, "reason": "No egress policy configured (deny by default)"}
    
    parsed = urlparse(url)
    host = parsed.hostname or ""
    
    # Check host allowlist
    host_allowed = False
    for allowed_host in policy["allow_hosts"]:
        if allowed_host == host or (allowed_host.startswith("*.") and host.endswith(allowed_host[1:])):
            host_allowed = True
            break
    
    # Check CIDR allowlist
    cidr_allowed = False
    try:
        host_ip = ipaddress.ip_address(host)
        for cidr in policy["allow_cidrs"]:
            if host_ip in ipaddress.ip_network(cidr):
                cidr_allowed = True
                break
    except ValueError:
        # Host is not an IP address
        pass
    
    allowed = host_allowed or cidr_allowed
    
    return {
        "allowed": allowed,
        "host": host,
        "host_allowed": host_allowed,
        "cidr_allowed": cidr_allowed,
        "reason": "Allowed by policy" if allowed else "Denied by policy (host not in allowlist)",
    }


# Redaction endpoints
@router.post("/v1/redaction/rules", response_model=RedactionRuleResponse)
async def create_redaction_rule(request: RedactionRuleRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.SECRET_ADMIN)) -> RedactionRuleResponse:
    """Create a redaction rule."""
    # In a full implementation, this would store rules in a database table
    # For now, return a mock response
    return RedactionRuleResponse(
        rule_id=str(uuid.uuid4()),
        tenant_id="default",
        name=request.name,
        pattern=request.pattern,
        replacement=request.replacement,
        applies_to=request.applies_to,
        enabled=request.enabled,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )


@router.get("/v1/redaction/rules", response_model=list[RedactionRuleResponse])
async def list_redaction_rules(principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.SECRET_ADMIN)) -> list[RedactionRuleResponse]:
    """List redaction rules."""
    # In a full implementation, this would query a database table
    # For now, return default rules
    return [
        RedactionRuleResponse(
            rule_id="default-api-key",
            tenant_id="default",
            name="API Key Pattern",
            pattern=r"(?i)(api[_-]?key|secret[_-]?key|access[_-]?token)[\"']?\s*[:=]\s*[\"']?([a-zA-Z0-9_\-]{20,})",
            replacement=r"\1=[REDACTED]",
            applies_to=["model", "events", "logs", "callbacks"],
            enabled=True,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        ),
        RedactionRuleResponse(
            rule_id="default-bearer-token",
            tenant_id="default",
            name="Bearer Token Pattern",
            pattern=r"(?i)bearer\s+([a-zA-Z0-9_\-\.]{20,})",
            replacement="Bearer [REDACTED]",
            applies_to=["model", "events", "logs", "callbacks"],
            enabled=True,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        ),
    ]


@router.post("/v1/redaction/test", response_model=RedactionTestResponse)
async def test_redaction(request: RedactionTestRequest, principal: PrincipalInfo = Depends(tenant_scope), _auth: None = requires(Action.SECRET_ADMIN)) -> RedactionTestResponse:
    """Test redaction rules against text."""
    import re
    
    # Get rules to test
    rules = await list_redaction_rules()
    if request.rules:
        rules = [r for r in rules if r.rule_id in request.rules]
    
    redacted = request.text
    matches = []
    
    for rule in rules:
        if not rule.enabled:
            continue
        try:
            pattern = re.compile(rule.pattern)
            for match in pattern.finditer(request.text):
                matches.append({
                    "rule_id": rule.rule_id,
                    "rule_name": rule.name,
                    "match": match.group(0),
                    "start": match.start(),
                    "end": match.end(),
                })
            redacted = pattern.sub(rule.replacement, redacted)
        except re.error:
            continue
    
    return RedactionTestResponse(
        original=request.text,
        redacted=redacted,
        matches=matches,
    )
