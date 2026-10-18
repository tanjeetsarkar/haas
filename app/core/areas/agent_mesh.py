"""Track A multi-agent primitives, topologies and interop (owner: AGENTS)."""

from __future__ import annotations

from typing import Any

# Tenant-owned tables added by this area; appended to the production RLS startup check.
RLS_TABLES: tuple[str, ...] = (
    "delegations",
    "agent_messages",
    "workspaces",
    "workspace_entry_versions",
    "agent_cards",
    "agent_topologies",
    "hacp_mailbox",
)


class AgentMeshStore:
    """Mixed into ``DistributedStore``; uses ``self.connection()`` (tenant-scoped transaction)."""

    connection: Any
    _insert_audit_event: Any

    async def maintenance_agent_mesh(self) -> dict[str, int]:
        """Periodic work run by the maintenance service; return counters for logging."""
        counters = {"stale_delegations_cleaned": 0, "old_messages_archived": 0}
        
        async with self.connection() as conn:
            # Clean up stale delegations (pending for too long)
            result = await conn.execute(
                """
                UPDATE delegations
                SET status = 'failed', settled_at = now()
                WHERE status = 'active'
                  AND created_at < now() - interval '1 hour'
                """
            )
            counters["stale_delegations_cleaned"] = int(result.split()[-1]) if result else 0
            
            # Archive old agent messages (could move to cold storage)
            # For now, just count them
            result = await conn.fetchval(
                """
                SELECT count(*) FROM agent_messages
                WHERE sent_at < now() - interval '30 days'
                """
            )
            counters["old_messages_archived"] = result or 0
        
        return counters
