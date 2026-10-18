"""Phase 4 execution isolation: static catalog, plugin artifacts, sandbox (owner: EXEC)."""

from __future__ import annotations

from typing import Any

# Tenant-owned tables added by this area; appended to the production RLS startup check.
RLS_TABLES: tuple[str, ...] = (
    "sandbox_sessions",
    "plugin_artifact_pins",
)


class IsolationStore:
    """Mixed into ``DistributedStore``; uses ``self.connection()`` (tenant-scoped transaction)."""

    connection: Any
    _insert_audit_event: Any

    async def maintenance_isolation(self) -> dict[str, int]:
        """Periodic work run by the maintenance service; return counters for logging."""
        counters = {"expired_sandboxes_cleaned": 0, "orphan_pins_removed": 0}
        
        async with self.connection() as conn:
            # Clean up expired sandbox sessions
            result = await conn.execute(
                """
                DELETE FROM sandbox_sessions
                WHERE expires_at < now()
                """
            )
            counters["expired_sandboxes_cleaned"] = int(result.split()[-1]) if result else 0
            
            # Remove orphaned plugin artifact pins (pins for runs that no longer exist)
            result = await conn.execute(
                """
                DELETE FROM plugin_artifact_pins pap
                WHERE NOT EXISTS (
                    SELECT 1 FROM runs r WHERE r.run_id = pap.run_id
                )
                """
            )
            counters["orphan_pins_removed"] = int(result.split()[-1]) if result else 0
        
        return counters
