"""Run-tree primitives, waiting states, plans, topology and interop (Track A, owner AGENTS).

Revision ID: 0017_multi_agent_runtime
Revises: 0016_marketplace
"""

from alembic import op

revision = "0017_multi_agent_runtime"
down_revision = "0016_marketplace"
branch_labels = None
depends_on = None

NEW_TABLES = ("agent_topologies", "hacp_mailbox")


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_boundary ON {table} TO haas_tenant_app
            USING (tenant_id = resolve_haas_tenant_uuid(current_setting('haas.tenant', true)))
            WITH CHECK (tenant_id = resolve_haas_tenant_uuid(current_setting('haas.tenant', true)));
    """)
    op.execute(f"""
        CREATE POLICY execution_service ON {table} TO haas_execution_service
            USING (true) WITH CHECK (true);
    """)
    op.execute(f"""
        DO $$ BEGIN
            EXECUTE format(
                'CREATE POLICY schema_administrator ON {table} TO %I USING (true) WITH CHECK (true)',
                current_user
            );
        END $$;
    """)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE agent_topologies (
            topology_id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants (tenant_id),
            name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 128),
            type TEXT NOT NULL CHECK (type IN ('supervisor', 'hierarchical', 'swarm', 'graph', 'map_reduce', 'debate')),
            config JSONB NOT NULL CHECK (jsonb_typeof(config) = 'object'),
            agent_revisions UUID[] NOT NULL,
            validation_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (validation_status IN ('pending', 'valid', 'invalid')),
            validation_report JSONB,
            created_by UUID REFERENCES principals (principal_id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX agent_topologies_tenant_idx ON agent_topologies (tenant_id, created_at DESC)")

    op.execute("""
        CREATE TABLE hacp_mailbox (
            message_id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants (tenant_id),
            mailbox_id TEXT NOT NULL CHECK (length(mailbox_id) BETWEEN 1 AND 128),
            sender TEXT NOT NULL CHECK (length(sender) BETWEEN 1 AND 128),
            recipient TEXT NOT NULL CHECK (length(recipient) BETWEEN 1 AND 128),
            message_type TEXT NOT NULL CHECK (length(message_type) BETWEEN 1 AND 64),
            payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
            correlation_id TEXT,
            delivered_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX hacp_mailbox_recipient_idx ON hacp_mailbox (tenant_id, recipient, created_at DESC) WHERE delivered_at IS NULL")
    op.execute("CREATE INDEX hacp_mailbox_correlation_idx ON hacp_mailbox (tenant_id, correlation_id) WHERE correlation_id IS NOT NULL")

    for table in NEW_TABLES:
        _rls(table)

    op.execute("GRANT SELECT, INSERT ON agent_topologies TO haas_tenant_app")
    op.execute("GRANT UPDATE (validation_status, validation_report, updated_at) ON agent_topologies TO haas_tenant_app")
    op.execute("GRANT SELECT ON agent_topologies TO haas_execution_service")
    op.execute("GRANT SELECT, INSERT ON hacp_mailbox TO haas_tenant_app, haas_execution_service")
    op.execute("GRANT UPDATE (delivered_at) ON hacp_mailbox TO haas_tenant_app, haas_execution_service")


def downgrade() -> None:
    for table in reversed(NEW_TABLES):
        op.execute(f"DROP TABLE {table}")
