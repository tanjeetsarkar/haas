from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import redis.asyncio as aioredis

from app.core.config import settings
from app.core.secret_vault import decrypt_secrets, encrypt_secrets


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


class DistributedStore:
    def __init__(self) -> None:
        self.pg_pool: asyncpg.Pool | None = None
        self.redis: aioredis.Redis | None = None

    async def init(self, *, pool_max: int = 15) -> None:
        self.pg_pool = await asyncpg.create_pool(
            dsn=settings.postgres_dsn, min_size=1, max_size=pool_max
        )
        self.redis = aioredis.from_url(settings.redis_url, decode_responses=True)

    async def close(self) -> None:
        if self.pg_pool:
            await self.pg_pool.close()
        if self.redis:
            await self.redis.aclose()

    async def resolve_oidc_membership(
        self, issuer: str, subject: str, tenant_selector: str
    ) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        if updated is None:
            raise RuntimeError("Tenant transition did not update the locked tenant.")
        return dict(updated)

    async def get_active_tenant_policy(
        self, tenant_selector: str
    ) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        tenant_id: UUID,
        actor_principal_id: UUID,
        action: str,
        target_type: str,
        target_id: str,
        request_id: UUID,
        details: dict[str, Any],
    ) -> None:
        await conn.execute(
            """
            INSERT INTO audit_events (
                event_id, tenant_id, actor_principal_id, action, target_type,
                target_id, request_id, details
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
            """,
            uuid4(),
            tenant_id,
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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

    async def clear_ephemeral_secrets(self, run_id: str) -> None:
        await self.rds.delete(f"haas:secrets:{run_id}")

    async def acquire_state_lock(self, key: str, ttl_sec: int = 15) -> bool:
        return bool(await self.rds.set(f"haas:lock:{key}", "1", nx=True, ex=ttl_sec))

    async def release_state_lock(self, key: str) -> None:
        await self.rds.delete(f"haas:lock:{key}")

    # --- Redis Streams Real-Time Event Bus (XADD / XREAD) ---

    async def publish_event(
        self, run_id: str, event_type: str, payload: dict[str, Any]
    ) -> str:
        stream_key = f"haas:stream:{run_id}"
        entry_id = await self.rds.xadd(
            stream_key,
            {"event_type": event_type, "payload_json": json.dumps(payload)},
            maxlen=settings.redis_stream_max_len,
            approximate=True,
        )
        await self.rds.expire(stream_key, 86400)
        return str(entry_id)

    # --- PostgreSQL Thread & Run Persistence ---

    async def get_or_create_thread_messages(
        self, thread_id: str, tenant_id: str
    ) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO threads (thread_id, tenant_id, messages_json, updated_at) "
                "VALUES ($1, $2, '[]'::jsonb, $3) ON CONFLICT (thread_id) DO NOTHING",
                thread_id,
                tenant_id,
                time.time(),
            )
            row = await conn.fetchrow(
                "SELECT tenant_id, messages_json FROM threads WHERE thread_id = $1",
                thread_id,
            )
            assert row is not None
            if row["tenant_id"] != tenant_id:
                raise ThreadOwnershipError(thread_id)
            return _loads(row["messages_json"])

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
    ) -> None:
        now = time.time()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO runs (
                    run_id, thread_id, tenant_id, traceparent, status,
                    primary_model, planner_model, active_skills_json,
                    messages_checkpoint_json, max_steps, max_cost_usd,
                    webhook_url, created_at, updated_at,
                    skill_allowlist_json, registry_fingerprint,
                    submitted_by_principal_id, policy_revision_id
                ) VALUES (
                    $1, $2, $3, $4, 'running',
                    $5, $6, $7::jsonb,
                    $8::jsonb, $9, $10,
                    $11, $12, $13,
                    $14::jsonb, $15, $16, $17
                )
                """,
                run_id,
                thread_id,
                tenant_id,
                traceparent,
                primary_model,
                planner_model,
                json.dumps(sorted(initial_skills)),
                json.dumps(initial_messages),
                max_steps,
                max_cost_usd,
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
                UUID(policy_revision_id) if policy_revision_id is not None else None,
            )

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM runs WHERE run_id = $1", run_id)
        if not row:
            return None
        d = dict(row)
        d["active_skills"] = _loads(d["active_skills_json"])
        d["messages"] = _loads(d["messages_checkpoint_json"])
        d["fingerprints"] = _loads(d["fingerprints_json"])
        # NULL = unrestricted (also the value for runs created before allowlists existed).
        d["skill_allowlist"] = (
            _loads(d["skill_allowlist_json"])
            if d.get("skill_allowlist_json") is not None
            else None
        )
        d["pending_tool_calls"] = (
            _loads(d["pending_tool_calls_json"])
            if d["pending_tool_calls_json"]
            else None
        )
        return d

    async def list_runs(
        self,
        tenant_id: str,
        *,
        limit: int,
        before: tuple[float, str] | None = None,
    ) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT run_id, thread_id, status, step_count, total_cost_usd,
                       created_at, updated_at
                FROM runs
                WHERE tenant_id = $1
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
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE runs
                SET status = 'cancelled', result_text = 'Run cancelled.',
                    pending_tool_calls_json = NULL, updated_at = $3
                WHERE run_id = $1 AND tenant_id = $2
                  AND status IN ('running', 'input_required')
                RETURNING run_id
                """,
                run_id,
                tenant_id,
                time.time(),
            )
        if row is None:
            return False
        await self.rds.setex(f"haas:cancelled:{run_id}", 604_800, "1")
        await self.clear_ephemeral_secrets(run_id)
        return True

    async def reap_orphaned_runs(self, stale_before: float) -> list[dict[str, str]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE runs
                SET status = 'failed', result_text = 'Run expired by orphan reaper.',
                    pending_tool_calls_json = NULL, updated_at = $2
                WHERE status = 'running' AND updated_at < $1
                RETURNING run_id, thread_id
                """,
                stale_before,
                time.time(),
            )
        return [
            {"run_id": row["run_id"], "thread_id": row["thread_id"]} for row in rows
        ]

    async def claim_resume(
        self,
        run_id: str,
        tenant_id: str,
        *,
        actor_principal_id: str | None,
        request_id: str,
        approved: bool,
        pending_tool_count: int,
    ) -> bool:
        """Claim a pending decision and audit authenticated actors atomically."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                audit_tenant_id: UUID | None = None
                if actor_principal_id is not None:
                    audit_tenant_id = await conn.fetchval(
                        """
                        SELECT tenant_id FROM tenants
                        WHERE (slug = $1 OR tenant_id::text = $1) AND status = 'active'
                        FOR SHARE
                        """,
                        tenant_id,
                    )
                    if audit_tenant_id is None:
                        return False
                row = await conn.fetchrow(
                    "UPDATE runs SET status = 'running', updated_at = $3 "
                    "WHERE run_id = $1 AND tenant_id = $2 AND status = 'input_required' "
                    "RETURNING run_id",
                    run_id,
                    tenant_id,
                    time.time(),
                )
                if row is None:
                    return False
                if audit_tenant_id is not None and actor_principal_id is not None:
                    await self._insert_audit_event(
                        conn,
                        tenant_id=audit_tenant_id,
                        actor_principal_id=UUID(actor_principal_id),
                        action="run.approved" if approved else "run.rejected",
                        target_type="run",
                        target_id=run_id,
                        request_id=UUID(request_id),
                        details={"pending_tool_count": pending_tool_count},
                    )
        return True

    async def save_checkpoint(
        self,
        run_id: str,
        *,
        step_count: int,
        messages: list[dict[str, Any]],
        active_skills: set[str],
        fingerprints: list[str],
        cost_delta_usd: float = 0.0,
        last_step_had_error: bool = False,
        status: str = "running",
        pending_tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE runs
                SET step_count = $2,
                    messages_checkpoint_json = $3::jsonb,
                    active_skills_json = $4::jsonb,
                    fingerprints_json = $5::jsonb,
                    total_cost_usd = total_cost_usd + $6,
                    last_step_had_error = $7,
                    status = $8,
                    pending_tool_calls_json = $9::jsonb,
                    updated_at = $10
                WHERE run_id = $1
                  AND status NOT IN ('completed', 'failed', 'cancelled')
                """,
                run_id,
                step_count,
                json.dumps(messages),
                json.dumps(sorted(active_skills)),
                json.dumps(fingerprints[-20:]),
                cost_delta_usd,
                last_step_had_error,
                status,
                json.dumps(pending_tool_calls)
                if pending_tool_calls is not None
                else None,
                time.time(),
            )

    async def finalize_run(
        self,
        run_id: str,
        thread_id: str,
        status: str,
        result_text: str,
        final_messages: list[dict[str, Any]],
        cost_delta_usd: float = 0.0,
    ) -> None:
        now = time.time()
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE runs
                SET status = $2,
                    result_text = $3,
                    messages_checkpoint_json = $4::jsonb,
                    total_cost_usd = total_cost_usd + $5,
                    pending_tool_calls_json = NULL,
                    updated_at = $6
                WHERE run_id = $1
                  AND status NOT IN ('completed', 'failed', 'cancelled')
                """,
                run_id,
                status,
                result_text,
                json.dumps(final_messages),
                cost_delta_usd,
                now,
            )
            if status == "completed" and result.endswith(" 1"):
                await conn.execute(
                    "UPDATE threads SET messages_json = $2::jsonb, updated_at = $3 WHERE thread_id = $1",
                    thread_id,
                    json.dumps(final_messages),
                    now,
                )
        await self.clear_ephemeral_secrets(run_id)
