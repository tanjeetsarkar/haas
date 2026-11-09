from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import time
from decimal import Decimal
from typing import Any
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import asyncpg
import redis.asyncio as aioredis

from app.core.config import settings
from app.core.database_context import tenant_context
from app.core.egress import EgressPolicy
from app.core.object_store import build_object_store
from app.core.intent_preflight import IntentPreflight
from app.core.held_intent import (
    bind_held_intent, destination_binding, held_request_digest,
    validate_held_creation, validate_held_key,
)
from app.core.tenant_policy import TenantPolicy
from app.core.run_phases import PhaseConfig, PhaseState, validate_phase_transition
from app.core.run_restrictions import (
    RunAuthority, authority_request_digest, validate_intent_provenance,
)
from app.core.secret_vault import decrypt_secrets, encrypt_secrets
from app.core.areas import (
    access as _access,
    agent_mesh as _agent_mesh,
    artifacts as _artifacts,
    isolation as _isolation,
    marketplace as _marketplace,
    operations as _operations,
)
from app.core.workflow import (
    Effects,
    IdempotencyConflictError,
    IdempotentReplay,
    WorkflowMixin,
    decode_run,
    deadline_from_now,
)


class ThreadOwnershipError(Exception):
    """The thread exists but belongs to a different tenant."""


class StoreConflictError(Exception):
    """The requested registry operation conflicts with current tenant state."""


class StoreNotFoundError(Exception):
    """The requested tenant-scoped registry object does not exist."""


_TENANT_TRANSITIONS = {
    "provisioning": {"active", "suspended"},
    "active": {"suspended", "offboarding"},
    "suspended": {"active", "offboarding"},
    "offboarding": {"deleted"},
    "deleted": set(),
}


def validate_tenant_transition(
    current: str,
    target: str,
    *,
    qualification_passed: bool = False,
    reauthorized: bool = False,
    execution_fenced: bool = False,
    retention_resolved: bool = False,
) -> None:
    if target not in _TENANT_TRANSITIONS.get(current, set()):
        raise ValueError(f"Tenant cannot transition from {current} to {target}.")
    if current == "provisioning" and target == "active" and not qualification_passed:
        raise ValueError("Tenant activation requires completed qualification.")
    if current == "suspended" and target == "active" and not reauthorized:
        raise ValueError("Tenant reactivation requires explicit reauthorization.")
    if target == "deleted" and not (execution_fenced and retention_resolved):
        raise ValueError(
            "Tenant deletion requires fenced execution and resolved retention duties."
        )


def _loads(value: Any) -> Any:
    # asyncpg returns JSONB as `str` unless a codec is registered.
    return json.loads(value) if isinstance(value, str) else value


def decode_run_phases(row: Any) -> tuple[PhaseConfig | None, PhaseState | None]:
    """Decode only server-owned run columns, never checkpoint messages."""
    data = dict(row)
    config = data.get("phase_config_json")
    state = data.get("phase_state_json")
    if config is None and state is None:
        return None, None
    if config is None or state is None:
        raise ValueError("phase.incomplete_storage")
    config_data = _loads(config)
    state_data = _loads(state)
    if not isinstance(config_data, dict) or set(config_data) != set(PhaseConfig.model_fields):
        raise ValueError("phase.invalid_config_storage")
    if not isinstance(state_data, dict) or set(state_data) != set(PhaseState.model_fields):
        raise ValueError("phase.invalid_state_storage")
    if (
        type(config_data["version"]) is not int
        or type(config_data["reserve_basis_points"]) is not int
        or type(state_data["version"]) is not int
    ):
        raise ValueError("phase.invalid_storage_version")
    return PhaseConfig.model_validate(config_data), PhaseState.model_validate(state_data)


RLS_TABLES = (
    "tenants",
    "runs",
    "threads",
    "agent_revisions",
    "memberships",
    "tenant_api_keys",
    "tenant_policy_revisions",
    "audit_events",
    "admission_requests",
    "run_steps",
    "approval_batches",
    "approval_decisions",
    "tool_invocations",
    "run_events",
    "outbox_messages",
    "consumer_inbox",
    "usage_ledger",
    "delegations",
    "agent_messages",
    "workspaces",
    "workspace_entry_versions",
    "agent_cards",
    *_isolation.RLS_TABLES,
    *_access.RLS_TABLES,
    *_artifacts.RLS_TABLES,
    *_operations.RLS_TABLES,
    *_marketplace.RLS_TABLES,
    *_agent_mesh.RLS_TABLES,
)


class _AdmissionRace(Exception):
    pass


class DistributedStore(
    WorkflowMixin,
    _isolation.IsolationStore,
    _access.AccessStore,
    _artifacts.ArtifactStore,
    _operations.OperationsStore,
    _marketplace.MarketplaceStore,
    _agent_mesh.AgentMeshStore,
):
    def __init__(self) -> None:
        self.pg_pool: asyncpg.Pool | None = None
        self.redis: aioredis.Redis | None = None

    async def init(self, *, pool_max: int = 15, service: bool = False) -> None:
        self.pg_pool = await asyncpg.create_pool(
            dsn=settings.postgres_dsn, min_size=1, max_size=pool_max
        )
        self.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        self.object_store = build_object_store(settings)
        if settings.haas_profile == "prod":
            async with self.pool.acquire() as conn:
                safe = await conn.fetchval(
                    "SELECT NOT rolsuper AND NOT rolbypassrls "
                    "AND pg_has_role(current_user, $1, 'member') "
                    "AND ($2 OR NOT pg_has_role(current_user, 'haas_execution_service', 'member')) "
                    "AND NOT EXISTS (SELECT 1 FROM pg_class c "
                    "WHERE c.relname = ANY($3::text[]) "
                    "AND pg_has_role(current_user, c.relowner, 'member')) "
                    "AND (SELECT count(*) = cardinality($3::text[]) "
                    "AND bool_and(relrowsecurity AND relforcerowsecurity) "
                    "FROM pg_class WHERE relkind = 'r' AND relnamespace = 'public'::regnamespace "
                    "AND relname = ANY($3::text[])) "
                    "AND EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'runs'::regclass "
                    "AND attname = 'tenant_uuid' AND attnotnull) "
                    "FROM pg_roles WHERE rolname = current_user",
                    "haas_execution_service" if service else "haas_tenant_app",
                    service,
                    list(RLS_TABLES),
                )
            if not safe:
                await self.close()
                raise RuntimeError(
                    "Production requires a non-owner, non-BYPASSRLS runtime login, "
                    "forced tenant RLS, and completed legacy cutover."
                )

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[Any]:
        assert self.pg_pool is not None, "PostgreSQL pool not initialized"
        async with self.pg_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('haas.tenant', $1, true), "
                    "set_config('haas.legacy_dev', $2, true)",
                    tenant_context.get(),
                    "on" if settings.haas_profile == "dev" else "off",
                )
                yield conn

    async def close(self) -> None:
        if self.pg_pool:
            await self.pg_pool.close()
        if self.redis:
            await self.redis.aclose()

    async def resolve_oidc_membership(
        self, issuer: str, subject: str, tenant_selector: str
    ) -> dict[str, Any] | None:
        async with self.connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT p.principal_id::text AS principal_id, p.issuer, p.subject,
                       p.credential_valid_after, t.tenant_id::text AS tenant_id,
                       t.slug AS tenant_slug,
                       t.credential_valid_after AS tenant_credential_valid_after,
                       array_agg(DISTINCT m.role ORDER BY m.role) AS roles
                FROM principals AS p
                JOIN memberships AS m ON m.principal_id = p.principal_id
                JOIN tenants AS t ON t.tenant_id = m.tenant_id
                WHERE p.issuer = $1 AND p.subject = $2
                  AND p.status = 'active' AND m.status = 'active'
                  AND t.status = 'active'
                  AND (t.slug = $3 OR t.tenant_id::text = $3)
                GROUP BY p.principal_id, t.tenant_id
                """,
                issuer,
                subject,
                tenant_selector,
            )
        if row is None:
            return None
        return dict(row)

    async def transition_tenant(
        self,
        tenant_selector: str,
        target: str,
        *,
        qualification_passed: bool = False,
        reauthorized: bool = False,
        execution_fenced: bool = False,
        retention_resolved: bool = False,
    ) -> dict[str, Any]:
        fx = Effects()
        async with self.connection() as conn:
            async with conn.transaction():
                tenant = await conn.fetchrow(
                    """
                    SELECT tenant_id, slug, status
                    FROM tenants
                    WHERE slug = $1 OR tenant_id::text = $1
                    FOR UPDATE
                    """,
                    tenant_selector,
                )
                if tenant is None:
                    raise LookupError("Tenant not found.")
                current = tenant["status"]
                validate_tenant_transition(
                    current,
                    target,
                    qualification_passed=qualification_passed,
                    reauthorized=reauthorized,
                    execution_fenced=execution_fenced,
                    retention_resolved=retention_resolved,
                )

                revoke_credentials = target in {"suspended", "offboarding", "deleted"}
                if revoke_credentials:
                    await conn.execute(
                        """
                        UPDATE tenant_api_keys
                        SET revoked_at = COALESCE(revoked_at, now())
                        WHERE tenant_id = $1 AND revoked_at IS NULL
                        """,
                        tenant["tenant_id"],
                    )
                invalidates_identity = revoke_credentials or (
                    current == "suspended" and target == "active"
                )
                updated = await conn.fetchrow(
                    """
                    UPDATE tenants
                    SET status = $2,
                        revocation_epoch = revocation_epoch + $3,
                        credential_valid_after = CASE
                            WHEN $4 THEN now() ELSE credential_valid_after
                        END,
                        updated_at = now()
                    WHERE tenant_id = $1
                    RETURNING tenant_id::text AS tenant_id, slug, status,
                              revocation_epoch
                    """,
                    tenant["tenant_id"],
                    target,
                    int(invalidates_identity),
                    invalidates_identity,
                )
                cancelled_runs = 0
                if target in {"suspended", "offboarding"}:
                    cancelled_runs = await self._cancel_tenant_runs(
                        conn, fx, tenant["tenant_id"]
                    )
        if updated is None:
            raise RuntimeError("Tenant transition did not update the locked tenant.")
        await self._after_commit(fx)
        return {**dict(updated), "cancelled_runs": cancelled_runs}

    async def get_active_tenant_policy(
        self, tenant_selector: str
    ) -> dict[str, Any] | None:
        async with self.connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT p.policy_revision_id::text AS policy_revision_id,
                       p.version, p.digest, p.rules
                FROM tenants AS t
                JOIN tenant_policy_revisions AS p
                  ON p.tenant_id = t.tenant_id
                 AND p.policy_revision_id = t.active_policy_revision_id
                WHERE (t.slug = $1 OR t.tenant_id::text = $1)
                  AND t.status = 'active'
                """,
                tenant_selector,
            )
        return dict(row) if row is not None else None

    async def list_tenant_memberships(
        self, tenant_selector: str
    ) -> list[dict[str, Any]]:
        async with self.connection() as conn:
            rows = await conn.fetch(
                """
                SELECT p.principal_id::text AS principal_id, p.issuer, p.subject,
                       p.kind, p.status AS principal_status, m.role,
                       m.status AS membership_status, m.created_at
                FROM tenants AS t
                JOIN memberships AS m ON m.tenant_id = t.tenant_id
                JOIN principals AS p ON p.principal_id = m.principal_id
                WHERE t.slug = $1 OR t.tenant_id::text = $1
                ORDER BY p.subject, m.role
                """,
                tenant_selector,
            )
        return [dict(row) for row in rows]

    async def grant_tenant_membership(
        self,
        *,
        tenant_selector: str,
        issuer: str,
        subject: str,
        kind: str,
        role: str,
        actor_principal_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        async with self.connection() as conn:
            async with conn.transaction():
                tenant = await conn.fetchrow(
                    """
                    SELECT tenant_id, status FROM tenants
                    WHERE slug = $1 OR tenant_id::text = $1
                    FOR UPDATE
                    """,
                    tenant_selector,
                )
                if tenant is None:
                    raise StoreNotFoundError("Tenant not found.")
                if tenant["status"] != "active":
                    raise StoreConflictError("Tenant is not active.")

                principal = await conn.fetchrow(
                    """
                    SELECT principal_id, kind, status FROM principals
                    WHERE issuer = $1 AND subject = $2
                    FOR UPDATE
                    """,
                    issuer,
                    subject,
                )
                if principal is None:
                    await conn.execute(
                        """
                        INSERT INTO principals (principal_id, issuer, subject, kind)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (issuer, subject) DO NOTHING
                        """,
                        uuid4(),
                        issuer,
                        subject,
                        kind,
                    )
                    principal = await conn.fetchrow(
                        """
                        SELECT principal_id, kind, status FROM principals
                        WHERE issuer = $1 AND subject = $2
                        FOR UPDATE
                        """,
                        issuer,
                        subject,
                    )
                if (
                    principal is None
                    or principal["kind"] != kind
                    or principal["status"] != "active"
                ):
                    raise StoreConflictError(
                        "Principal already exists with an incompatible or inactive state."
                    )

                membership = await conn.fetchrow(
                    """
                    INSERT INTO memberships (tenant_id, principal_id, role, status)
                    VALUES ($1, $2, $3, 'active')
                    ON CONFLICT (tenant_id, principal_id, role)
                    DO UPDATE SET status = 'active', updated_at = now()
                    RETURNING tenant_id, principal_id::text AS principal_id,
                              role, status
                    """,
                    tenant["tenant_id"],
                    principal["principal_id"],
                    role,
                )
                await self._insert_audit_event(
                    conn,
                    tenant_id=tenant["tenant_id"],
                    actor_principal_id=UUID(actor_principal_id),
                    action="membership.granted",
                    target_type="principal",
                    target_id=str(principal["principal_id"]),
                    request_id=UUID(request_id),
                    details={"role": role},
                )
        if membership is None:
            raise RuntimeError("Membership grant did not return its stored row.")
        return dict(membership)

    async def revoke_tenant_membership(
        self,
        *,
        tenant_selector: str,
        principal_id: str,
        role: str,
        actor_principal_id: str,
        request_id: str,
    ) -> None:
        async with self.connection() as conn:
            async with conn.transaction():
                tenant = await conn.fetchrow(
                    """
                    SELECT tenant_id FROM tenants
                    WHERE slug = $1 OR tenant_id::text = $1
                    FOR UPDATE
                    """,
                    tenant_selector,
                )
                if tenant is None:
                    raise StoreNotFoundError("Tenant not found.")
                membership = await conn.fetchrow(
                    """
                    SELECT status FROM memberships
                    WHERE tenant_id = $1 AND principal_id = $2 AND role = $3
                    FOR UPDATE
                    """,
                    tenant["tenant_id"],
                    UUID(principal_id),
                    role,
                )
                if membership is None:
                    raise StoreNotFoundError("Membership not found.")
                if membership["status"] == "revoked":
                    return
                if role == "tenant_admin" and membership["status"] == "active":
                    admin_count = await conn.fetchval(
                        """
                        SELECT count(*) FROM memberships
                        WHERE tenant_id = $1 AND role = 'tenant_admin'
                          AND status = 'active' AND principal_id <> $2
                        """,
                        tenant["tenant_id"],
                        UUID(principal_id),
                    )
                    if admin_count == 0:
                        raise StoreConflictError(
                            "Cannot revoke the last active tenant administrator."
                        )
                await conn.execute(
                    """
                    UPDATE memberships SET status = 'revoked', updated_at = now()
                    WHERE tenant_id = $1 AND principal_id = $2 AND role = $3
                    """,
                    tenant["tenant_id"],
                    UUID(principal_id),
                    role,
                )
                await self._insert_audit_event(
                    conn,
                    tenant_id=tenant["tenant_id"],
                    actor_principal_id=UUID(actor_principal_id),
                    action="membership.revoked",
                    target_type="principal",
                    target_id=principal_id,
                    request_id=UUID(request_id),
                    details={"role": role},
                )

    async def list_tenant_api_keys(self, tenant_selector: str) -> list[dict[str, Any]]:
        async with self.connection() as conn:
            rows = await conn.fetch(
                """
                SELECT k.key_id::text AS key_id,
                       k.principal_id::text AS principal_id,
                       k.key_prefix, k.name, k.created_at, k.expires_at,
                       k.revoked_at, k.last_used_at
                FROM tenant_api_keys AS k
                JOIN tenants AS t ON t.tenant_id = k.tenant_id
                WHERE t.slug = $1 OR t.tenant_id::text = $1
                ORDER BY k.created_at DESC, k.key_id DESC
                """,
                tenant_selector,
            )
        return [dict(row) for row in rows]

    async def create_tenant_api_key(
        self,
        *,
        tenant_selector: str,
        principal_id: str,
        credential: str,
        name: str,
        expires_in_days: int,
        actor_principal_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        async with self.connection() as conn:
            async with conn.transaction():
                tenant = await conn.fetchrow(
                    """
                    SELECT tenant_id FROM tenants
                    WHERE (slug = $1 OR tenant_id::text = $1) AND status = 'active'
                    FOR UPDATE
                    """,
                    tenant_selector,
                )
                if tenant is None:
                    raise StoreNotFoundError("Active tenant not found.")
                membership = await conn.fetchrow(
                    """
                    SELECT p.status AS principal_status, m.status AS membership_status
                    FROM memberships AS m
                    JOIN principals AS p ON p.principal_id = m.principal_id
                    WHERE m.tenant_id = $1 AND m.principal_id = $2
                    ORDER BY CASE WHEN m.status = 'active' THEN 0 ELSE 1 END
                    LIMIT 1
                    """,
                    tenant["tenant_id"],
                    UUID(principal_id),
                )
                if (
                    membership is None
                    or membership["principal_status"] != "active"
                    or membership["membership_status"] != "active"
                ):
                    raise StoreConflictError(
                        "API keys can only be issued to an active tenant member."
                    )
                key_id = uuid4()
                key_prefix = credential[:16]
                key_hash = hashlib.sha256(credential.encode("utf-8")).digest()
                key = await conn.fetchrow(
                    """
                    INSERT INTO tenant_api_keys (
                        key_id, tenant_id, principal_id, key_prefix, key_hash,
                        name, expires_at
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6,
                        now() + ($7 * interval '1 day')
                    )
                    RETURNING key_id::text AS key_id,
                              principal_id::text AS principal_id, key_prefix,
                              name, created_at, expires_at
                    """,
                    key_id,
                    tenant["tenant_id"],
                    UUID(principal_id),
                    key_prefix,
                    key_hash,
                    name,
                    expires_in_days,
                )
                await self._insert_audit_event(
                    conn,
                    tenant_id=tenant["tenant_id"],
                    actor_principal_id=UUID(actor_principal_id),
                    action="api_key.created",
                    target_type="api_key",
                    target_id=str(key_id),
                    request_id=UUID(request_id),
                    details={"key_prefix": key_prefix},
                )
        if key is None:
            raise RuntimeError("API key creation did not return its stored row.")
        return dict(key)

    async def revoke_tenant_api_key(
        self,
        *,
        tenant_selector: str,
        key_id: str,
        actor_principal_id: str,
        request_id: str,
    ) -> None:
        async with self.connection() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT k.tenant_id, k.key_prefix, k.revoked_at
                    FROM tenant_api_keys AS k
                    JOIN tenants AS t ON t.tenant_id = k.tenant_id
                    WHERE (t.slug = $1 OR t.tenant_id::text = $1) AND k.key_id = $2
                    FOR UPDATE OF k
                    """,
                    tenant_selector,
                    UUID(key_id),
                )
                if row is None:
                    raise StoreNotFoundError("API key not found.")
                if row["revoked_at"] is not None:
                    return
                await conn.execute(
                    "UPDATE tenant_api_keys SET revoked_at = now() WHERE key_id = $1",
                    UUID(key_id),
                )
                await self._insert_audit_event(
                    conn,
                    tenant_id=row["tenant_id"],
                    actor_principal_id=UUID(actor_principal_id),
                    action="api_key.revoked",
                    target_type="api_key",
                    target_id=key_id,
                    request_id=UUID(request_id),
                    details={"key_prefix": row["key_prefix"]},
                )

    async def activate_tenant_policy_revision(
        self,
        *,
        tenant_selector: str,
        rules: dict[str, Any],
        digest: str,
        actor_principal_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        async with self.connection() as conn:
            async with conn.transaction():
                tenant = await conn.fetchrow(
                    """
                    SELECT tenant_id FROM tenants
                    WHERE (slug = $1 OR tenant_id::text = $1) AND status = 'active'
                    FOR UPDATE
                    """,
                    tenant_selector,
                )
                if tenant is None:
                    raise StoreNotFoundError("Active tenant not found.")
                version = await conn.fetchval(
                    """
                    SELECT COALESCE(max(version), 0) + 1
                    FROM tenant_policy_revisions WHERE tenant_id = $1
                    """,
                    tenant["tenant_id"],
                )
                policy_id = uuid4()
                revision = await conn.fetchrow(
                    """
                    INSERT INTO tenant_policy_revisions (
                        policy_revision_id, tenant_id, version, digest, rules,
                        created_by_principal_id
                    )
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                    RETURNING policy_revision_id::text AS policy_revision_id,
                              version, digest, rules, created_at
                    """,
                    policy_id,
                    tenant["tenant_id"],
                    version,
                    digest,
                    json.dumps(rules, sort_keys=True, separators=(",", ":")),
                    UUID(actor_principal_id),
                )
                await conn.execute(
                    """
                    UPDATE tenants
                    SET active_policy_revision_id = $2,
                        retention_class = $3, updated_at = now()
                    WHERE tenant_id = $1
                    """,
                    tenant["tenant_id"],
                    policy_id,
                    rules["retention_class"],
                )
                await self._insert_audit_event(
                    conn,
                    tenant_id=tenant["tenant_id"],
                    actor_principal_id=UUID(actor_principal_id),
                    action="policy.activated",
                    target_type="policy_revision",
                    target_id=str(policy_id),
                    request_id=UUID(request_id),
                    details={"version": version, "digest": digest},
                )
        if revision is None:
            raise RuntimeError("Policy revision creation did not return its row.")
        result = dict(revision)
        result["rules"] = _loads(result["rules"])
        result["created_by_principal_id"] = actor_principal_id
        return result

    async def list_audit_events(
        self,
        tenant_selector: str,
        *,
        limit: int,
        before: tuple[float, str] | None = None,
    ) -> list[dict[str, Any]]:
        async with self.connection() as conn:
            rows = await conn.fetch(
                """
                SELECT e.event_id::text AS event_id,
                       e.actor_principal_id::text AS actor_principal_id,
                       e.action, e.target_type, e.target_id, e.request_id::text AS request_id,
                       e.details, e.created_at,
                       extract(epoch FROM e.created_at)::double precision AS cursor_time
                FROM audit_events AS e
                JOIN tenants AS t ON t.tenant_id = e.tenant_id
                WHERE (t.slug = $1 OR t.tenant_id::text = $1)
                  AND ($2::double precision IS NULL OR
                       (extract(epoch FROM e.created_at), e.event_id::text) < ($2, $3))
                ORDER BY e.created_at DESC, e.event_id DESC
                LIMIT $4
                """,
                tenant_selector,
                before[0] if before else None,
                before[1] if before else None,
                limit,
            )
        events = [dict(row) for row in rows]
        for event in events:
            event["details"] = _loads(event["details"])
            event.pop("cursor_time")
        return events

    @staticmethod
    async def _insert_audit_event(
        conn: Any,
        *,
        tenant_id: UUID | str | None,
        actor_principal_id: UUID,
        action: str,
        target_type: str,
        target_id: str,
        request_id: UUID,
        details: dict[str, Any],
    ) -> None:
        """Append one audit event inside the caller's transaction.

        ``audit_events.tenant_id`` is the canonical tenant UUID. Callers holding a
        request-level tenant selector (slug, UUID text or mapped legacy id) are
        resolved with the same ``resolve_haas_tenant_uuid`` function the RLS
        policies use; an unresolvable selector fails closed and aborts the
        transaction instead of writing an unattributed or mis-attributed row.
        """
        tenant_uuid: UUID | None
        if isinstance(tenant_id, UUID) or tenant_id is None:
            tenant_uuid = tenant_id
        else:
            tenant_uuid = await conn.fetchval(
                "SELECT resolve_haas_tenant_uuid($1)", tenant_id
            )
        if tenant_uuid is None:
            raise StoreConflictError("Audit tenant could not be resolved.")
        await conn.execute(
            """
            INSERT INTO audit_events (
                event_id, tenant_id, actor_principal_id, action, target_type,
                target_id, request_id, details
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
            """,
            uuid4(),
            tenant_uuid,
            actor_principal_id,
            action,
            target_type,
            target_id,
            request_id,
            json.dumps(details, sort_keys=True, separators=(",", ":")),
        )

    async def resolve_api_key(self, credential: str) -> dict[str, Any] | None:
        digest = hashlib.sha256(credential.encode("utf-8")).digest()
        prefix = credential[:16]
        async with self.connection() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT k.key_hash, k.key_id,
                           p.principal_id::text AS principal_id, p.issuer, p.subject,
                           p.status AS principal_status,
                           t.tenant_id::text AS tenant_id, t.slug AS tenant_slug,
                           t.status AS tenant_status,
                           array_agg(DISTINCT m.role ORDER BY m.role)
                             FILTER (WHERE m.status = 'active') AS roles
                    FROM tenant_api_keys AS k
                    JOIN principals AS p ON p.principal_id = k.principal_id
                    JOIN tenants AS t ON t.tenant_id = k.tenant_id
                    LEFT JOIN memberships AS m
                      ON m.tenant_id = t.tenant_id
                     AND m.principal_id = p.principal_id
                    WHERE k.key_prefix = $1
                      AND (k.expires_at IS NULL OR k.expires_at > now())
                      AND k.revoked_at IS NULL
                    GROUP BY k.key_hash, k.key_id, p.principal_id, t.tenant_id
                    """,
                    prefix,
                )
                if row is None or not hmac.compare_digest(row["key_hash"], digest):
                    return None
                await conn.execute(
                    "UPDATE tenant_api_keys SET last_used_at = now() WHERE key_id = $1",
                    row["key_id"],
                )
        identity = dict(row)
        identity.pop("key_hash")
        identity.pop("key_id")
        return identity

    async def bootstrap_tenant(
        self, slug: str, issuer: str, subject: str
    ) -> dict[str, str]:
        tenant_id, principal_id = uuid4(), uuid4()
        async with self.connection() as conn:
            async with conn.transaction():
                created_tenant = await conn.fetchrow(
                    """
                    INSERT INTO tenants (tenant_id, slug, status)
                    VALUES ($1, $2, 'active')
                    ON CONFLICT (slug) DO NOTHING
                    RETURNING tenant_id
                    """,
                    tenant_id,
                    slug,
                )
                if created_tenant is None:
                    raise RuntimeError(
                        "Bootstrap tenant already exists; refusing to grant access again."
                    )
                tenant = created_tenant
                await conn.execute(
                    """
                    INSERT INTO principals (principal_id, issuer, subject, kind, status)
                    VALUES ($1, $2, $3, 'human', 'active')
                    ON CONFLICT (issuer, subject) DO NOTHING
                    """,
                    principal_id,
                    issuer,
                    subject,
                )
                principal = await conn.fetchrow(
                    """
                    SELECT principal_id, status FROM principals
                    WHERE issuer = $1 AND subject = $2
                    """,
                    issuer,
                    subject,
                )
                if principal is None or principal["status"] != "active":
                    raise RuntimeError(
                        "Bootstrap principal is not active; it was not changed."
                    )
                await conn.execute(
                    """
                    INSERT INTO memberships (tenant_id, principal_id, role, status)
                    VALUES ($1, $2, 'tenant_admin', 'active')
                    ON CONFLICT (tenant_id, principal_id, role) DO NOTHING
                    """,
                    tenant["tenant_id"],
                    principal["principal_id"],
                )
                await self._insert_audit_event(
                    conn,
                    tenant_id=tenant["tenant_id"],
                    actor_principal_id=principal["principal_id"],
                    action="tenant.bootstrapped",
                    target_type="tenant",
                    target_id=slug,
                    request_id=uuid4(),
                    details={"source": "bootstrap_cli"},
                )
        return {
            "tenant_id": str(tenant["tenant_id"]),
            "principal_id": str(principal["principal_id"]),
            "tenant_slug": slug,
        }

    @property
    def pool(self) -> asyncpg.Pool:
        assert self.pg_pool is not None, "PostgreSQL pool not initialized"
        return self.pg_pool

    @property
    def rds(self) -> aioredis.Redis:
        assert self.redis is not None, "Redis client not initialized"
        return self.redis

    # --- Redis Ephemeral Vault & Idempotency Locks ---

    async def store_ephemeral_secrets(
        self, run_id: str, secrets: dict[str, str], ttl_sec: int = 3600
    ) -> None:
        if secrets:
            await self.rds.setex(
                f"haas:secrets:{run_id}", ttl_sec, encrypt_secrets(run_id, secrets)
            )

    async def get_scoped_secrets(
        self, run_id: str, allowed_keys: set[str]
    ) -> dict[str, str]:
        raw = await self.rds.get(f"haas:secrets:{run_id}")
        if not raw:
            return {}
        full = decrypt_secrets(run_id, raw)
        return {k: v for k, v in full.items() if k in allowed_keys}

    async def resolve_secret_bindings(
        self,
        tenant_id: str,
        run_id: str,
        names: set[str],
        *,
        plugin_id: str | None = None,
        tool_name: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Resolve secret binding values for a tool invocation.

        Looks up active secret_bindings for the tenant and returns
        the plaintext value plus provenance info.
        """
        async with self.connection() as conn:
            resolved: dict[str, dict[str, Any]] = {}
            for name in names:
                row = await conn.fetchrow(
                    """
                    SELECT binding_id, encrypted_value, provider, provider_reference,
                    allowed_plugins, allowed_tools, version
                    FROM secret_bindings
                    WHERE tenant_id = resolve_haas_tenant_uuid($1) AND name = $2 AND deleted_at IS NULL
                    """,
                    tenant_id,
                    name,
                )
                if row is None:
                    resolved[name] = {"value": None, "source": "missing"}
                    continue

                if row["provider"] == "inline":
                    resolved[name] = {
                        "value": row["encrypted_value"],
                        "source": "binding",
                        "binding_id": str(row["binding_id"]) if row.get("binding_id") else None,
                        "version": row["version"],
                    }
                elif row["provider"] == "env":
                    ref = row.get("provider_reference")
                    resolved[name] = {
                        "value": f"<env:{ref}>",
                        "source": "binding",
                        "binding_id": str(row["binding_id"]) if row.get("binding_id") else None,
                        "version": row["version"],
                    }
                else:
                    resolved[name] = {"value": None, "source": "unknown"}

                # Filter by plugin/tool allowlists if specified
                if plugin_id and plugin_id not in (row.get("allowed_plugins") or []):
                    resolved[name] = {"value": None, "source": "plugin_disallowed"}
                if tool_name and tool_name not in (row.get("allowed_tools") or []):
                    resolved[name] = {"value": None, "source": "tool_disallowed"}

            return resolved

    async def record_credential_grants(
        self, tenant_id: str, run_id: str, rows: list[dict[str, Any]]
    ) -> None:
        """Record credential grant entries in PostgreSQL for audit.

        Each row should contain: grant_id, invocation_id, secret_name,
        source, binding_id, binding_version, audience, plugin_digest, expires_at
        """
        if not rows:
            return
        async with self.connection() as conn:
            for row in rows:
                await conn.execute(
                    """
                    INSERT INTO credential_grants
                        (grant_id, tenant_id, run_id, invocation_id, secret_name,
                         source, binding_id, binding_version, audience,
                         plugin_digest, created_at, expires_at)
                    VALUES
                        ($1::uuid, resolve_haas_tenant_uuid($2), $3::text, $4::text, $5::text,
                         $6::text, $7::uuid, $8::integer, $9::text,
                         $10::text, now(), $11::timestamptz)
                    ON CONFLICT (grant_id) DO NOTHING
                    """,
                    UUID(row["grant_id"]),
                    tenant_id,
                    run_id,
                    row.get("invocation_id", ""),
                    row["secret_name"],
                    row.get("source", "binding"),
                    UUID(row["binding_id"]) if row.get("binding_id") else None,
                    row.get("binding_version"),
                    row.get("audience", ""),
                    row.get("plugin_digest", ""),
                    datetime.fromtimestamp(row["expires_at"], tz=timezone.utc),
                )

    async def egress_policy_for_run(
        self, tenant_id: str, run_id: str
    ) -> EgressPolicy:
        """Return the tenant's egress policy for a given run.

        Reads from the egress_policies table (tenant-scoped) and merges
        with the global default from settings.
        """
        from app.core.egress import EgressPolicy, normalize_host_pattern

        async with self.connection() as conn:
            row = await conn.fetchrow(
                "SELECT allow_hosts, allow_cidrs, max_bytes_per_run, version "
                "FROM egress_policies WHERE tenant_id = resolve_haas_tenant_uuid($1)",
                tenant_id,
            )
            if row is None:
                return EgressPolicy()

            cap = int(row["max_bytes_per_run"]) if row["max_bytes_per_run"] else None
            return EgressPolicy(
                allow_hosts=tuple(
                    normalize_host_pattern(h) for h in row["allow_hosts"]
                ),
                allow_cidrs=tuple(
                    str(c) for c in row["allow_cidrs"]
                ),
                max_bytes_per_run=cap,
                version=int(row["version"] or 0),
            )

    async def clear_ephemeral_secrets(self, run_id: str) -> None:
        await self.rds.delete(f"haas:secrets:{run_id}")

    async def acquire_state_lock(self, key: str, ttl_sec: int = 15) -> bool:
        return bool(await self.rds.set(f"haas:lock:{key}", "1", nx=True, ex=ttl_sec))

    async def release_state_lock(self, key: str) -> None:
        await self.rds.delete(f"haas:lock:{key}")

    # --- PostgreSQL Thread & Run Persistence ---

    async def list_threads(
        self,
        tenant_id: str,
        *,
        limit: int,
        before: tuple[float, str] | None = None,
        search: str = "",
    ) -> list[dict[str, Any]]:
        async with self.connection() as conn:
            rows = await conn.fetch(
                """
                SELECT thread_id, history_version, active_run_id, updated_at,
                       parent_thread_id, parent_history_version
                FROM threads
                WHERE COALESCE(tenant_uuid::text, tenant_id)
                    = COALESCE(resolve_haas_tenant_uuid($1)::text, $1)
                  AND ($2::double precision IS NULL OR (updated_at, thread_id) < ($2, $3))
                  AND strpos(thread_id, $5) > 0
                ORDER BY updated_at DESC, thread_id DESC LIMIT $4
                """,
                tenant_id,
                before[0] if before else None,
                before[1] if before else None,
                limit,
                search,
            )
        return [dict(row) for row in rows]

    async def get_thread(self, thread_id: str, tenant_id: str) -> dict[str, Any]:
        async with self.connection() as conn:
            row = await conn.fetchrow(
                "SELECT thread_id, history_version, active_run_id, messages_json, "
                "updated_at, parent_thread_id, parent_history_version "
                "FROM threads WHERE thread_id = $1 "
                "AND COALESCE(tenant_uuid::text, tenant_id) "
                "= COALESCE(resolve_haas_tenant_uuid($2)::text, $2)",
                thread_id,
                tenant_id,
            )
        if row is None:
            raise StoreNotFoundError("Thread not found.")
        result = dict(row)
        result["messages"] = _loads(result.pop("messages_json"))
        return result

    async def create_thread(
        self,
        thread_id: str,
        tenant_id: str,
        metadata: dict[str, Any],
        actor_principal_id: str | None,
        request_id: str,
    ) -> None:
        async with self.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO threads (thread_id, tenant_id, messages_json, updated_at, metadata) "
                    "VALUES ($1, $2, '[]'::jsonb, $3, $4)",
                    thread_id,
                    tenant_id,
                    time.time(),
                    json.dumps(metadata),
                )
                if actor_principal_id is not None:
                    await self._insert_audit_event(
                        conn,
                        tenant_id=tenant_id,
                        actor_principal_id=UUID(actor_principal_id),
                        action="thread.created",
                        target_type="thread",
                        target_id=thread_id,
                        request_id=UUID(request_id),
                        details={"metadata": metadata},
                    )

    async def branch_thread(
        self,
        thread_id: str,
        tenant_id: str,
        *,
        expected_history_version: int,
        actor_principal_id: str | None,
        request_id: str,
    ) -> dict[str, Any]:
        branch_id = f"thd_{uuid4().hex[:12]}"
        async with self.connection() as conn:
            async with conn.transaction():
                await self._lock_derivation_threads(conn, thread_id, tenant_id)
                source = await conn.fetchrow(
                    "SELECT * FROM threads WHERE thread_id = $1 "
                    "AND COALESCE(tenant_uuid::text, tenant_id) "
                    "= COALESCE(resolve_haas_tenant_uuid($2)::text, $2) "
                    "FOR UPDATE",
                    thread_id,
                    tenant_id,
                )
                if source is None:
                    raise StoreNotFoundError("Thread not found.")
                await self._check_thread_derivation(conn, thread_id, tenant_id)
                if source["history_version"] != expected_history_version:
                    raise StoreConflictError(
                        "Thread history changed; refresh before branching."
                    )
                await conn.execute(
                    "INSERT INTO threads (thread_id, tenant_id, messages_json, updated_at, "
                    "parent_thread_id, parent_history_version) "
                    "VALUES ($1, $2, $3::jsonb, $4, $5, $6)",
                    branch_id,
                    source["tenant_id"],
                    source["messages_json"],
                    time.time(),
                    thread_id,
                    expected_history_version,
                )
                if actor_principal_id is not None:
                    await self._insert_audit_event(
                        conn,
                        tenant_id=source["tenant_uuid"],
                        actor_principal_id=UUID(actor_principal_id),
                        action="thread.branched",
                        target_type="thread",
                        target_id=branch_id,
                        request_id=UUID(request_id),
                        details={
                            "parent_thread_id": thread_id,
                            "parent_history_version": expected_history_version,
                        },
                    )
        return {
            "thread_id": branch_id,
            "history_version": 0,
            "parent_thread_id": thread_id,
            "parent_history_version": expected_history_version,
        }

    async def _check_thread_derivation(
        self, conn: Any, thread_id: str, tenant_id: str, *, delegation: bool = False,
    ) -> None:
        """Caller holds the source thread lock, also used by run admission."""
        guards = await conn.fetchrow(
            "WITH RECURSIVE lineage AS ("
            "SELECT thread_id, parent_thread_id FROM threads WHERE thread_id=$1 "
            "AND COALESCE(tenant_uuid::text,tenant_id)="
            "COALESCE(resolve_haas_tenant_uuid($2)::text,$2) UNION "
            "SELECT t.thread_id,t.parent_thread_id FROM threads t JOIN lineage l "
            "ON t.thread_id=l.parent_thread_id WHERE COALESCE(t.tenant_uuid::text,t.tenant_id)="
            "COALESCE(resolve_haas_tenant_uuid($2)::text,$2)) "
            "SELECT EXISTS(SELECT 1 FROM runs r JOIN lineage l USING(thread_id) "
            "WHERE r.authority_json IS NOT NULL AND COALESCE(r.tenant_uuid::text,r.tenant_id)="
            "COALESCE(resolve_haas_tenant_uuid($2)::text,$2)) AS restricted, "
            "EXISTS(SELECT 1 FROM runs r JOIN lineage l USING(thread_id) "
            "WHERE (r.phase_config_json IS NOT NULL OR r.phase_state_json IS NOT NULL) "
            "AND COALESCE(r.tenant_uuid::text,r.tenant_id)="
            "COALESCE(resolve_haas_tenant_uuid($2)::text,$2)) AS phased",
            thread_id, tenant_id,
        )
        if guards["restricted"]:
            raise StoreConflictError("restriction.derivation_unavailable")
        if delegation and guards["phased"]:
            raise StoreConflictError("phase.delegation_unavailable")

    async def _lock_derivation_threads(
        self, conn: Any, thread_id: str | None, tenant_id: str, parent_id: str | None = None,
    ) -> None:
        """Lock all source ancestry plus target in stable order through caller's writes."""
        await conn.fetch(
            "WITH RECURSIVE parents AS ("
            "SELECT run_id,parent_run_id,thread_id FROM runs WHERE run_id=$3 "
            "AND COALESCE(tenant_uuid::text,tenant_id)="
            "COALESCE(resolve_haas_tenant_uuid($2)::text,$2) UNION "
            "SELECT r.run_id,r.parent_run_id,r.thread_id FROM runs r JOIN parents p "
            "ON r.run_id=p.parent_run_id WHERE COALESCE(r.tenant_uuid::text,r.tenant_id)="
            "COALESCE(resolve_haas_tenant_uuid($2)::text,$2)), lineage AS ("
            "SELECT thread_id,parent_thread_id FROM threads WHERE "
            "(thread_id=$1 OR thread_id IN (SELECT thread_id FROM parents)) "
            "AND COALESCE(tenant_uuid::text,tenant_id)="
            "COALESCE(resolve_haas_tenant_uuid($2)::text,$2) UNION "
            "SELECT t.thread_id,t.parent_thread_id FROM threads t JOIN lineage l "
            "ON t.thread_id=l.parent_thread_id WHERE COALESCE(t.tenant_uuid::text,t.tenant_id)="
            "COALESCE(resolve_haas_tenant_uuid($2)::text,$2)) "
            "SELECT t.thread_id FROM threads t WHERE t.thread_id IN "
            "(SELECT thread_id FROM lineage) ORDER BY t.thread_id FOR UPDATE OF t",
            thread_id, tenant_id, parent_id,
        )

    async def _check_parent_authority(self, conn: Any, parent_id: str, tenant_id: str) -> None:
        """Immutable authority is checked on every ancestor, never child config."""
        await self._lock_derivation_threads(conn, None, tenant_id, parent_id)
        rows = await conn.fetch(
            "WITH RECURSIVE lineage AS ("
            "SELECT run_id,parent_run_id,thread_id,authority_json,phase_config_json,phase_state_json "
            "FROM runs WHERE run_id=$1 "
            "AND COALESCE(tenant_uuid::text,tenant_id)="
            "COALESCE(resolve_haas_tenant_uuid($2)::text,$2) UNION "
            "SELECT r.run_id,r.parent_run_id,r.thread_id,r.authority_json,"
            "r.phase_config_json,r.phase_state_json FROM runs r JOIN lineage l "
            "ON r.run_id=l.parent_run_id WHERE COALESCE(r.tenant_uuid::text,r.tenant_id)="
            "COALESCE(resolve_haas_tenant_uuid($2)::text,$2)) "
            "SELECT thread_id,authority_json,phase_config_json,phase_state_json FROM lineage",
            parent_id, tenant_id,
        )
        if not rows:
            raise StoreNotFoundError("Parent run not found.")
        if any(row["authority_json"] is not None for row in rows):
            raise StoreConflictError("restriction.derivation_unavailable")
        if any(row["phase_config_json"] is not None or row["phase_state_json"] is not None for row in rows):
            raise StoreConflictError("phase.delegation_unavailable")
        for thread_id in {row["thread_id"] for row in rows}:
            await self._check_thread_derivation(conn, thread_id, tenant_id, delegation=True)

    async def list_agent_revisions(
        self,
        tenant_id: str,
        *,
        limit: int,
        before: tuple[float, str] | None = None,
    ) -> list[dict[str, Any]]:
        async with self.connection() as conn:
            rows = await conn.fetch(
                "SELECT revision_id::text, agent_name, version, state, digest, "
                "extract(epoch FROM created_at)::double precision AS created_at "
                "FROM agent_revisions WHERE tenant_id = resolve_haas_tenant_uuid($1) "
                "AND ($2::double precision IS NULL OR "
                "(created_at, revision_id) < (to_timestamp($2), $3::uuid)) "
                "ORDER BY created_at DESC, revision_id DESC LIMIT $4",
                tenant_id,
                before[0] if before else None,
                before[1] if before else None,
                limit,
            )
        return [dict(row) for row in rows]

    async def list_published_agents(self, tenant_id: str) -> list[dict[str, Any]]:
        async with self.connection() as conn:
            rows = await conn.fetch(
                "SELECT revision_id::text, agent_name, version, "
                "extract(epoch FROM published_at)::bigint AS published_at "
                "FROM agent_revisions WHERE tenant_id = resolve_haas_tenant_uuid($1) "
                "AND state = 'published' ORDER BY agent_name, version DESC LIMIT 500",
                tenant_id,
            )
        return [dict(row) for row in rows]

    async def resolve_published_agent(
        self, tenant_id: str, agent_name: str, version: int | None
    ) -> str | None:
        """Revision id of `agent_name` at `version`, or its latest published version."""
        async with self.connection() as conn:
            value = await conn.fetchval(
                "SELECT revision_id::text FROM agent_revisions "
                "WHERE tenant_id = resolve_haas_tenant_uuid($1) AND agent_name = $2 "
                "AND state = 'published' AND ($3::bigint IS NULL OR version = $3) "
                "ORDER BY version DESC LIMIT 1",
                tenant_id,
                agent_name,
                version,
            )
        return value

    async def get_agent_revision(
        self, tenant_id: str, revision_id: str
    ) -> dict[str, Any]:
        async with self.connection() as conn:
            row = await conn.fetchrow(
                "SELECT a.*, p.rules AS policy_rules FROM agent_revisions a "
                "JOIN tenant_policy_revisions p ON p.tenant_id = a.tenant_id "
                "AND p.policy_revision_id = a.policy_revision_id "
                "WHERE a.tenant_id = resolve_haas_tenant_uuid($1) AND a.revision_id = $2",
                tenant_id,
                UUID(revision_id),
            )
        if row is None:
            raise StoreNotFoundError("Agent revision not found.")
        result = dict(row)
        result["configuration"] = _loads(result["configuration"])
        result["policy_rules"] = _loads(result["policy_rules"])
        return result

    async def create_agent_revision(
        self,
        tenant_id: str,
        *,
        agent_name: str,
        configuration: dict[str, Any],
        digest: str,
        policy_revision_id: str,
        registry_fingerprint: str,
        actor_principal_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        revision_id = uuid4()
        async with self.connection() as conn:
            async with conn.transaction():
                tenant = await conn.fetchrow(
                    "SELECT tenant_id, active_policy_revision_id FROM tenants "
                    "WHERE tenant_id = resolve_haas_tenant_uuid($1) AND status = 'active' "
                    "FOR UPDATE",
                    tenant_id,
                )
                if tenant is None:
                    raise StoreNotFoundError("Active tenant not found.")
                if str(tenant["active_policy_revision_id"]) != policy_revision_id:
                    raise StoreConflictError(
                        "Policy changed; refresh before creating a draft."
                    )
                version = await conn.fetchval(
                    "SELECT COALESCE(max(version), 0) + 1 FROM agent_revisions "
                    "WHERE tenant_id = $1 AND agent_name = $2",
                    tenant["tenant_id"],
                    agent_name,
                )
                await conn.execute(
                    "INSERT INTO agent_revisions (tenant_id, revision_id, agent_name, "
                    "version, configuration, digest, policy_revision_id, "
                    "registry_fingerprint, created_by) "
                    "VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9)",
                    tenant["tenant_id"],
                    revision_id,
                    agent_name,
                    version,
                    json.dumps(configuration),
                    digest,
                    UUID(policy_revision_id),
                    registry_fingerprint,
                    UUID(actor_principal_id),
                )
                await self._insert_audit_event(
                    conn,
                    tenant_id=tenant["tenant_id"],
                    actor_principal_id=UUID(actor_principal_id),
                    action="agent.drafted",
                    target_type="agent_revision",
                    target_id=str(revision_id),
                    request_id=UUID(request_id),
                    details={"version": version, "digest": digest},
                )
        return {"revision_id": str(revision_id), "version": version, "state": "draft"}

    async def transition_agent_revision(
        self,
        tenant_id: str,
        revision_id: str,
        *,
        target: str,
        actor_principal_id: str,
        request_id: str,
        policy_revision_id: str,
        registry_fingerprint: str,
    ) -> dict[str, Any]:
        previous = {"validated": "draft", "published": "validated"}[target]
        async with self.connection() as conn:
            async with conn.transaction():
                tenant = await conn.fetchrow(
                    "SELECT tenant_id, active_policy_revision_id FROM tenants "
                    "WHERE tenant_id = resolve_haas_tenant_uuid($1) AND status = 'active' "
                    "FOR SHARE",
                    tenant_id,
                )
                if (
                    tenant is None
                    or str(tenant["active_policy_revision_id"]) != policy_revision_id
                ):
                    raise StoreConflictError("Policy changed; create a new draft.")
                row = await conn.fetchrow(
                    "UPDATE agent_revisions SET state = $3, "
                    "published_at = CASE WHEN $3 = 'published' THEN now() ELSE NULL END "
                    "WHERE tenant_id = $1 AND revision_id = $2 AND state = $4 "
                    "AND policy_revision_id = $5 AND registry_fingerprint = $6 "
                    "RETURNING revision_id::text, version, state",
                    tenant["tenant_id"],
                    UUID(revision_id),
                    target,
                    previous,
                    UUID(policy_revision_id),
                    registry_fingerprint,
                )
                if row is None:
                    raise StoreConflictError(
                        "Revision state, policy or plugin inventory changed."
                    )
                await self._insert_audit_event(
                    conn,
                    tenant_id=tenant["tenant_id"],
                    actor_principal_id=UUID(actor_principal_id),
                    action=f"agent.{target}",
                    target_type="agent_revision",
                    target_id=revision_id,
                    request_id=UUID(request_id),
                    details={},
                )
        return dict(row)

    async def get_or_create_thread_messages(
        self, thread_id: str, tenant_id: str
    ) -> list[dict[str, Any]]:
        async with self.connection() as conn:
            await conn.execute(
                "INSERT INTO threads (thread_id, tenant_id, messages_json, updated_at) "
                "VALUES ($1, $2, '[]'::jsonb, $3) ON CONFLICT (thread_id) DO NOTHING",
                thread_id,
                tenant_id,
                time.time(),
            )
            row = await conn.fetchrow(
                "SELECT messages_json FROM threads WHERE thread_id = $1 "
                "AND COALESCE(tenant_uuid::text, tenant_id) "
                "= COALESCE(resolve_haas_tenant_uuid($2)::text, $2)",
                thread_id,
                tenant_id,
            )
            if row is None:
                raise ThreadOwnershipError(thread_id)
            return _loads(row["messages_json"])

    async def find_admission(
        self,
        tenant_id: str,
        principal_key: str,
        idempotency_key: str,
        *,
        include_created_at: bool = False,
    ) -> dict[str, Any] | None:
        # Other admission consumers retain their existing response shape.
        if not include_created_at:
            return await super().find_admission(tenant_id, principal_key, idempotency_key)
        async with self.connection() as conn:
            row = await conn.fetchrow(
                "SELECT a.request_digest, a.response, r.created_at "
                "FROM admission_requests a JOIN runs r "
                "ON r.tenant_id = a.tenant_id AND r.run_id = a.run_id "
                "WHERE a.principal_key = $1 "
                "AND COALESCE(a.tenant_uuid::text, a.tenant_id) "
                "= COALESCE(resolve_haas_tenant_uuid($2)::text, $2) "
                "AND a.operation = 'run.create' AND a.idempotency_key = $3 "
                "AND a.expires_at > now()",
                principal_key,
                tenant_id,
                idempotency_key,
            )
        if row is None:
            return None
        response = _loads(row["response"])
        if "created_at" not in response:
            # The composite foreign key supplies the authoritative legacy association.
            response["created_at"] = float(row["created_at"])
        return {"request_digest": row["request_digest"], "response": response}

    async def create_held_intent_run(
        self, snapshot: IntentPreflight, *, idempotency_key: str | None = None, **run_fields: Any,
    ) -> float:
        """Internal only; creation rechecks pins transactionally and retains every hold."""
        if {"admission", "authority", "intent_preflight"}.intersection(run_fields):
            raise ValueError("restriction.server_owned_admission")
        if idempotency_key is not None:
            validate_held_key(idempotency_key)
        policy_record = await self.get_active_tenant_policy(snapshot.tenant_id)
        if policy_record is None:
            raise ValueError("restriction.policy_unavailable")
        policy = TenantPolicy.from_record(policy_record)
        destination = destination_binding(snapshot, policy)
        authority = bind_held_intent(
            snapshot, run_id=run_fields["run_id"],
            destination=destination,
            idempotency_key=idempotency_key,
        )
        admission = None if idempotency_key is None else {
            "principal_key": snapshot.submitter_id, "idempotency_key": idempotency_key,
            "request_digest": held_request_digest(snapshot, run_fields, destination),
            "response": {"run_id": run_fields["run_id"], "status": "queued"},
        }
        return await self.create_run(
            **run_fields, authority=authority, intent_preflight=snapshot, admission=admission,
        )

    async def prepare_intent_approval(self, lease: Any) -> dict[str, Any]:
        from app.core.deterministic_intent import prepare_intent_approval
        return await prepare_intent_approval(self, lease)

    async def record_held_intent_decision(self, **decision: Any) -> dict[str, Any]:
        from app.core.held_intent_decision import record_held_intent_decision
        return await record_held_intent_decision(self, **decision)

    async def release_approved_intent(self, *, tenant_id: str, run_id: str) -> dict[str, Any]:
        from app.core.intent_release import release_approved_intent
        return await release_approved_intent(self, tenant_id=tenant_id, run_id=run_id)

    async def create_run(
        self,
        *,
        run_id: str,
        thread_id: str,
        tenant_id: str,
        traceparent: str,
        primary_model: str,
        planner_model: str,
        initial_skills: list[str],
        initial_messages: list[dict[str, Any]],
        max_steps: int,
        max_cost_usd: float,
        webhook_url: str | None,
        skill_allowlist: list[str] | None = None,
        registry_fingerprint: str | None = None,
        submitted_by_principal_id: str | None = None,
        policy_revision_id: str | None = None,
        agent_revision_id: str | None = None,
        expected_history_version: int | None = None,
        admission: dict[str, Any] | None = None,
        include_created_at: bool = False,
        deadline_sec: int | None = None,
        max_tool_invocations: int | None = None,
        parent_run_id: str | None = None,
        authority: RunAuthority | None = None,
        phase_config: PhaseConfig | None = None,
        intent_preflight: IntentPreflight | None = None,
    ) -> float:
        now = time.time()
        fx = Effects()
        if intent_preflight is not None and (
            authority is None or authority.intent is None
            or parent_run_id is not None or phase_config is not None
            or initial_skills != list(intent_preflight.preload_skills)
        ):
            raise ValueError("restriction.held_creation_only")
        if phase_config is not None:
            phase_config, _ = decode_run_phases({
                "phase_config_json": phase_config.model_dump_json(),
                "phase_state_json": PhaseState().model_dump_json(),
            })
            if admission is not None or parent_run_id is not None:
                raise ValueError("phase.runtime_not_enabled")
        if agent_revision_id is not None and policy_revision_id is None:
            raise ValueError("Agent revisions require a pinned tenant policy.")
        if authority is not None:
            authority = RunAuthority.model_validate_json(authority.model_dump_json())
            if not (policy_revision_id and submitted_by_principal_id and registry_fingerprint):
                raise ValueError("restriction.authenticated_provenance_required")
            if admission is not None and admission["principal_key"] != submitted_by_principal_id:
                raise ValueError("restriction.admission_principal_mismatch")
        if admission is not None:
            admission = {
                **admission,
                "request_digest": authority_request_digest(admission["request_digest"], authority),
            }
        try:
            async with self.connection() as conn:
                async with conn.transaction():
                    held_replay = None
                    if intent_preflight is not None and admission is not None:
                        canonical_tenant = await conn.fetchval(
                            "SELECT resolve_haas_tenant_uuid($1)", tenant_id,
                        )
                        if str(canonical_tenant) != intent_preflight.tenant_id:
                            raise ValueError("restriction.provenance_mismatch")
                        # Same held request serializes before thread checks, so
                        # concurrent replay never becomes a second action.
                        await conn.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                            json.dumps([str(canonical_tenant), admission["principal_key"],
                                        "run.create", admission["idempotency_key"]]),
                        )
                        prior = await conn.fetchrow(
                            "SELECT request_digest,response FROM admission_requests "
                            "WHERE tenant_uuid=$1 AND principal_key=$2 AND operation='run.create' "
                            "AND idempotency_key=$3 AND expires_at>now()",
                            canonical_tenant, admission["principal_key"], admission["idempotency_key"],
                        )
                        if prior is not None:
                            if prior["request_digest"] != admission["request_digest"]:
                                raise IdempotencyConflictError(admission["idempotency_key"])
                            held_replay = _loads(prior["response"])
                    if policy_revision_id is not None:
                        tenant = await conn.fetchrow(
                            "SELECT tenant_id, active_policy_revision_id FROM tenants "
                            "WHERE tenant_id = resolve_haas_tenant_uuid($1) "
                            "AND status = 'active' FOR SHARE",
                            tenant_id,
                        )
                        if (
                            tenant is None
                            or str(tenant["active_policy_revision_id"])
                            != policy_revision_id
                        ):
                            raise StoreConflictError(
                                "Tenant policy changed; retry admission."
                            )
                        if agent_revision_id is not None:
                            pinned = await conn.fetchval(
                                "SELECT EXISTS (SELECT 1 FROM agent_revisions "
                                "WHERE tenant_id = $1 AND revision_id = $2 "
                                "AND state = 'published' AND policy_revision_id = $3 "
                                "AND registry_fingerprint = $4)",
                                tenant["tenant_id"],
                                UUID(agent_revision_id),
                                UUID(policy_revision_id),
                                registry_fingerprint,
                            )
                            if not pinned:
                                raise StoreConflictError(
                                    "Agent revision is not eligible for admission."
                                )
                    if held_replay is not None:
                        assert intent_preflight is not None and authority is not None
                        validate_intent_provenance(
                            authority, tenant_id=intent_preflight.tenant_id, run_id=run_id,
                            submitter_id=str(submitted_by_principal_id),
                            policy_revision=str(policy_revision_id),
                            registry_fingerprint=str(registry_fingerprint),
                            idempotency_key=admission["idempotency_key"] if admission else None,
                        )
                        await validate_held_creation(
                            conn, intent_preflight, authority, skill_ceiling=skill_allowlist,
                            agent_revision_id=agent_revision_id,
                            agent_fields={
                                "primary_model": primary_model, "planner_model": planner_model,
                                "max_steps": max_steps, "max_cost_usd": max_cost_usd,
                            },
                        )
                        raise IdempotentReplay(held_replay)
                    await self._lock_derivation_threads(conn, thread_id, tenant_id, parent_run_id)
                    thread = await conn.fetchrow(
                        "SELECT tenant_id, messages_json, history_version, active_run_id "
                        "FROM threads WHERE thread_id = $1 "
                        "AND COALESCE(tenant_uuid::text, tenant_id) "
                        "= COALESCE(resolve_haas_tenant_uuid($2)::text, $2) FOR UPDATE",
                        thread_id,
                        tenant_id,
                    )
                    if thread is None:
                        raise ThreadOwnershipError(thread_id)
                    await self._check_thread_derivation(conn, thread_id, tenant_id)
                    if parent_run_id is not None:
                        try:
                            await self._check_parent_authority(conn, parent_run_id, tenant_id)
                        except StoreNotFoundError:
                            # Preserve the existing lineage trigger's public failure shape.
                            raise asyncpg.ForeignKeyViolationError("Parent run is not visible.") from None
                    if authority is not None:
                        canonical_tenant = await conn.fetchval(
                            "SELECT resolve_haas_tenant_uuid($1)", thread["tenant_id"]
                        )
                        if canonical_tenant is None:
                            raise ValueError("restriction.tenant_required")
                        validate_intent_provenance(
                            authority, tenant_id=str(canonical_tenant), run_id=run_id,
                            submitter_id=str(submitted_by_principal_id),
                            policy_revision=str(policy_revision_id),
                            registry_fingerprint=str(registry_fingerprint),
                            idempotency_key=admission["idempotency_key"] if admission else None,
                        )
                        if intent_preflight is not None:
                            await validate_held_creation(
                                conn, intent_preflight, authority, skill_ceiling=skill_allowlist,
                                agent_revision_id=agent_revision_id,
                                agent_fields={
                                    "primary_model": primary_model, "planner_model": planner_model,
                                    "max_steps": max_steps, "max_cost_usd": max_cost_usd,
                                },
                            )
                    if thread["active_run_id"] is not None:
                        raise StoreConflictError(
                            "Thread has an active run. Wait, cancel it, or explicitly branch."
                        )
                    if (
                        expected_history_version is not None
                        and thread["history_version"] != expected_history_version
                    ):
                        raise StoreConflictError(
                            "Thread history changed; refresh or branch."
                        )
                    committed = _loads(thread["messages_json"])
                    if committed and initial_messages[:-1] != committed:
                        raise StoreConflictError(
                            "Thread history changed; refresh or branch."
                        )
                    run_row = await conn.fetchrow(
                        """
                    INSERT INTO runs (
                        run_id, thread_id, tenant_id, traceparent, status,
                        primary_model, planner_model, active_skills_json,
                        messages_checkpoint_json, max_steps, max_cost_usd,
                        webhook_url, created_at, updated_at,
                        skill_allowlist_json, registry_fingerprint,
                        submitted_by_principal_id, policy_revision_id,
                        agent_revision_id, thread_history_version,
                        next_action, deadline_at, max_tool_invocations, parent_run_id,
                        authority_json, phase_config_json, phase_state_json
                    ) VALUES (
                        $1, $2, $3, $4, 'queued',
                        $5, $6, $7::jsonb,
                        $8::jsonb, $9, $10,
                        $11, $12, $13,
                        $14::jsonb, $15, $16, $17, $18, $19,
                        $23, $20, $21, $22, $24::jsonb, $25::jsonb, $26::jsonb
                    ) RETURNING *
                    """,
                        run_id,
                        thread_id,
                        thread["tenant_id"],
                        traceparent,
                        primary_model,
                        planner_model,
                        json.dumps(sorted(initial_skills)),
                        json.dumps(initial_messages),
                        max_steps,
                        Decimal(str(max_cost_usd)),
                        webhook_url,
                        now,
                        now,
                        json.dumps(sorted(skill_allowlist))
                        if skill_allowlist is not None
                        else None,
                        registry_fingerprint,
                        UUID(submitted_by_principal_id)
                        if submitted_by_principal_id is not None
                        else None,
                        UUID(policy_revision_id)
                        if policy_revision_id is not None
                        else None,
                        UUID(agent_revision_id)
                        if agent_revision_id is not None
                        else None,
                        thread["history_version"],
                        deadline_from_now(
                            deadline_sec or settings.default_run_deadline_sec
                        ),
                        max_tool_invocations or settings.default_max_tool_invocations,
                        parent_run_id,
                        "none" if authority is not None or phase_config is not None else "step",
                        authority.model_dump_json() if authority is not None else None,
                        phase_config.model_dump_json() if phase_config is not None else None,
                        PhaseState().model_dump_json() if phase_config is not None else None,
                    )
                    created_at = float(run_row["created_at"])
                    await self._append_events(
                        conn,
                        fx,
                        thread["tenant_id"],
                        run_id,
                        [("run_queued", {"run_id": run_id, "thread_id": thread_id})],
                    )
                    # Internal storage staging only: no restricted work until all
                    # dispatch paths enforce authority and this hold is reviewed.
                    if authority is None and phase_config is None:
                        await self._enqueue_orchestrate(conn, fx, run_row, "step", 0)
                    if admission is not None:
                        admission_response = dict(admission["response"])
                        if include_created_at:
                            admission_response["created_at"] = created_at
                        stored = await conn.fetchval(
                            "INSERT INTO admission_requests (tenant_id, principal_key, "
                            "operation, idempotency_key, request_digest, run_id, response, "
                            "expires_at) VALUES ($1, $2, 'run.create', $3, $4, $5, $6::jsonb, "
                            "now() + make_interval(secs => $7)) "
                            "ON CONFLICT DO NOTHING RETURNING 1",
                            thread["tenant_id"],
                            admission["principal_key"],
                            admission["idempotency_key"],
                            admission["request_digest"],
                            run_id,
                            json.dumps(admission_response),
                            float(settings.idempotency_ttl_sec),
                        )
                        if not stored:
                            raise _AdmissionRace()
                    await conn.execute(
                        "UPDATE threads SET active_run_id = $2 WHERE thread_id = $1",
                        thread_id,
                        run_id,
                    )
        except _AdmissionRace:
            assert admission is not None
            previous = await self.find_admission(
                tenant_id,
                admission["principal_key"],
                admission["idempotency_key"],
                include_created_at=include_created_at,
            )
            if (
                previous is None
                or previous["request_digest"] != admission["request_digest"]
            ):
                raise IdempotencyConflictError(admission["idempotency_key"]) from None
            raise IdempotentReplay(previous["response"]) from None
        await self._after_commit(fx)
        return created_at

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async with self.connection() as conn:
            row = await conn.fetchrow(
                "SELECT r.*, t.slug AS tenant_slug FROM runs r "
                "LEFT JOIN tenants t ON t.tenant_id = r.tenant_uuid WHERE r.run_id = $1",
                run_id,
            )
        if row is None:
            return None
        run = decode_run(row)
        run["phase_config"], run["phase_state"] = decode_run_phases(row)
        return run

    async def acquire_lease(
        self, *, message_id: str, run_id: str, action: str,
        expected_state_version: int, owner: str,
    ) -> Any:
        # "none" is a storage hold, never an executable orchestration action.
        # Other actions still pass the base class's locked next_action fence.
        if action == "none":
            return None
        return await super().acquire_lease(
            message_id=message_id, run_id=run_id, action=action,
            expected_state_version=expected_state_version, owner=owner,
        )

    async def transition_held_run_phase(
        self, *, tenant_id: str, run_id: str, expected_state_version: int,
        following: PhaseState,
    ) -> PhaseState:
        """CAS phase state only for a held run; never enqueue or lease it."""
        async with self.connection() as conn:
            async with conn.transaction():
                row = await self._lock_run(conn, run_id, tenant_id)
                if row is None:
                    raise StoreNotFoundError("phase.run_not_found")
                config, current = decode_run_phases(row)
                if config is None or current is None:
                    raise StoreConflictError("phase.not_enabled")
                if (
                    row["status"] != "queued" or row["next_action"] != "none"
                    or row["lease_owner"] is not None
                ):
                    raise StoreConflictError("phase.run_not_held")
                if current.state_version != expected_state_version:
                    raise StoreConflictError("phase.state_version_conflict")
                _, validated = decode_run_phases({
                    "phase_config_json": config.model_dump_json(),
                    "phase_state_json": following.model_dump_json(),
                })
                assert validated is not None
                validate_phase_transition(current, validated)
                if validated == current:
                    return current
                updated = await conn.fetchval(
                    "UPDATE runs SET phase_state_json=$3::jsonb WHERE run_id=$1 "
                    "AND (phase_state_json->>'state_version')::bigint=$2 RETURNING 1",
                    run_id, expected_state_version, validated.model_dump_json(),
                )
                if not updated:
                    raise StoreConflictError("phase.state_version_conflict")
                return validated

    async def list_runs(
        self,
        tenant_id: str,
        *,
        limit: int,
        before: tuple[float, str] | None = None,
    ) -> list[dict[str, Any]]:
        async with self.connection() as conn:
            rows = await conn.fetch(
                """
                SELECT run_id, thread_id, status, step_count, total_cost_usd,
                       created_at, updated_at
                FROM runs
                WHERE COALESCE(tenant_uuid::text, tenant_id)
                    = COALESCE(resolve_haas_tenant_uuid($1)::text, $1)
                  AND ($2::DOUBLE PRECISION IS NULL OR (created_at, run_id) < ($2, $3))
                ORDER BY created_at DESC, run_id DESC
                LIMIT $4
                """,
                tenant_id,
                before[0] if before else None,
                before[1] if before else None,
                limit,
            )
        return [dict(row) for row in rows]

    async def cancel_run(self, run_id: str, tenant_id: str) -> bool:
        return await self.request_cancel(run_id, tenant_id) in {
            "cancelled",
            "cancelling",
        }

    async def publish_event(
        self, run_id: str, event_type: str, payload: dict[str, Any]
    ) -> int:
        """Append one durable event outside an orchestration transition."""
        fx = Effects()
        async with self.connection() as conn:
            row = await conn.fetchrow(
                "SELECT tenant_id FROM runs WHERE run_id = $1 FOR UPDATE", run_id
            )
            if row is None:
                return 0
            sequence = await self._append_events(
                conn, fx, row["tenant_id"], run_id, [(event_type, payload)]
            )
        await self._after_commit(fx)
        return sequence

    async def finalize_run(
        self,
        run_id: str,
        thread_id: str,
        status: str,
        result_text: str,
        final_messages: list[dict[str, Any]],
        cost_delta_usd: float = 0.0,
    ) -> bool:
        """Administrative finalization that bypasses the lease (tests, operator repair)."""
        fx = Effects()
        async with self.connection() as conn:
            row = await self._lock_run(conn, run_id)
            if row is None or row["thread_id"] != thread_id:
                return False
            thread = await conn.fetchrow(
                "SELECT active_run_id, history_version FROM threads WHERE thread_id = $1",
                thread_id,
            )
            if (
                row["status"] in ("completed", "failed", "cancelled")
                or thread is None
                or thread["active_run_id"] != run_id
                or thread["history_version"] != row["thread_history_version"]
            ):
                return False
            event = (
                ("run_completed", {"output": result_text})
                if status == "completed"
                else ("run_cancelled", {"run_id": run_id})
                if status == "cancelled"
                else ("run_error", {"error": result_text})
            )
            await self._finalize(
                conn,
                fx,
                row,
                status,
                result_text,
                messages=final_messages,
                cost_delta=Decimal(str(cost_delta_usd)),
                events=[event],
            )
        await self._after_commit(fx)
        return True
