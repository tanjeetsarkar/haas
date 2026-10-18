"""Phase 4 credential broker, egress policy and redaction (owner: SEC)."""

from __future__ import annotations

from typing import Any

# Tenant-owned tables added by this area; appended to the production RLS startup check.
RLS_TABLES: tuple[str, ...] = (
    "secret_bindings",
    "credential_grants",
    "egress_policies",
    "redaction_rules",
)


class AccessStore:
    """Mixed into ``DistributedStore``; uses ``self.connection()`` (tenant-scoped transaction)."""

    connection: Any
    _insert_audit_event: Any

    async def maintenance_access(self) -> dict[str, int]:
        """Periodic work run by the maintenance service; return counters for logging."""
        counters = {"expired_grants_cleaned": 0, "retired_keys_marked": 0}
        
        async with self.connection() as conn:
            # Clean up expired credential grants
            result = await conn.execute(
                """
                DELETE FROM credential_grants
                WHERE expires_at < now()
                  AND (redeemed_at IS NOT NULL OR denied_reason IS NOT NULL)
                """
            )
            counters["expired_grants_cleaned"] = int(result.split()[-1]) if result else 0
            
            # Mark retired webhook signing keys as revoked after grace period
            result = await conn.execute(
                """
                UPDATE webhook_signing_keys
                SET status = 'revoked', revoked_at = now()
                WHERE status = 'retiring' AND retire_at < now()
                """
            )
            counters["retired_keys_marked"] = int(result.split()[-1]) if result else 0
        
        return counters
