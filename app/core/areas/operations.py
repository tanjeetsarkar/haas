"""Phase 5 webhooks, quotas, telemetry, DR and Phase 6 cells (owner: OPS)."""

from __future__ import annotations

from typing import Any

# Tenant-owned tables added by this area; appended to the production RLS startup check.
RLS_TABLES: tuple[str, ...] = (
    "webhook_endpoints",
    "webhook_signing_keys",
    "webhook_deliveries",
    "webhook_delivery_attempts",
    "tenant_quotas",
    "tenant_placements",
)


class OperationsStore:
    """Mixed into ``DistributedStore``; uses ``self.connection()`` (tenant-scoped transaction)."""

    connection: Any
    _insert_audit_event: Any

    async def maintenance_operations(self) -> dict[str, int]:
        """Periodic work run by the maintenance service; return counters for logging."""
        counters = {"webhook_deliveries_retried": 0, "dead_deliveries_marked": 0, "stale_claims_released": 0}
        
        async with self.connection() as conn:
            # Retry pending webhook deliveries
            result = await conn.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'pending', claim_token = NULL, claim_expires_at = NULL,
                    next_attempt_at = now()
                WHERE status = 'in_flight'
                  AND claim_expires_at < now()
                """
            )
            counters["stale_claims_released"] = int(result.split()[-1]) if result else 0
            
            # Mark dead deliveries (exceeded max attempts)
            result = await conn.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'dead', completed_at = now()
                WHERE status IN ('pending', 'retrying')
                  AND attempts >= max_attempts
                """
            )
            counters["dead_deliveries_marked"] = int(result.split()[-1]) if result else 0
            
            # Reset deliveries stuck in retrying for too long
            result = await conn.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'pending', next_attempt_at = now()
                WHERE status = 'retrying'
                  AND next_attempt_at < now() - interval '1 hour'
                """
            )
            counters["webhook_deliveries_retried"] = int(result.split()[-1]) if result else 0
        
        return counters
