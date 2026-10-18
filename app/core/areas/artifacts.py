"""Phase 4 artifact service and Phase 5 retention (owner: DATA)."""

from __future__ import annotations

from typing import Any

# Tenant-owned tables added by this area; appended to the production RLS startup check.
RLS_TABLES: tuple[str, ...] = (
    "artifacts",
    "retention_policies",
    "legal_holds",
    "erasure_requests",
    "retention_tombstones",
)


class ArtifactStore:
    """Mixed into ``DistributedStore``; uses ``self.connection()`` (tenant-scoped transaction)."""

    connection: Any
    _insert_audit_event: Any

    async def maintenance_artifacts(self) -> dict[str, int]:
        """Periodic work run by the maintenance service; return counters for logging."""
        counters = {"expired_artifacts_marked": 0, "erasure_requests_processed": 0, "tombstones_created": 0}
        
        async with self.connection() as conn:
            # Mark expired artifacts for deletion
            result = await conn.execute(
                """
                UPDATE artifacts
                SET deleted_at = now()
                WHERE expires_at IS NOT NULL
                  AND expires_at < now()
                  AND deleted_at IS NULL
                """
            )
            counters["expired_artifacts_marked"] = int(result.split()[-1]) if result else 0
            
            # Count pending erasure requests awaiting the retention workflow
            # (completion requires a verified manifest + manifest_sha256, which is
            # produced by the retention pipeline, not this maintenance sweep).
            result = await conn.fetchval(
                """
                SELECT count(*) FROM erasure_requests
                WHERE status = 'pending' AND kind = 'subject'
                """
            )
            counters["erasure_requests_processed"] = int(result or 0)
        
        return counters
