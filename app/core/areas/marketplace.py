"""Track M marketplace registry, trust and installs (owner: MKT)."""

from __future__ import annotations

from typing import Any

# Tenant-owned tables added by this area; appended to the production RLS startup check.
RLS_TABLES: tuple[str, ...] = (
    "marketplace_installations",
    "marketplace_notifications",
)


class MarketplaceStore:
    """Mixed into ``DistributedStore``; uses ``self.connection()`` (tenant-scoped transaction)."""

    connection: Any
    _insert_audit_event: Any

    async def maintenance_marketplace(self) -> dict[str, int]:
        """Periodic work run by the maintenance service; return counters for logging."""
        counters = {"notifications_fanned_out": 0, "revoked_installations": 0}
        
        async with self.connection() as conn:
            # Process pending notifications (in a real implementation, this would send emails/webhooks)
            # For now, just count unacknowledged notifications
            result = await conn.fetchval(
                """
                SELECT count(*) FROM marketplace_notifications
                WHERE acknowledged_at IS NULL
                """
            )
            counters["notifications_fanned_out"] = result or 0
        
        return counters
