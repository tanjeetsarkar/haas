"""Add append-only, tenant-scoped audit events.

Revision ID: 0005_audit_v1
Revises: 0004_tenant_policy
"""

from alembic import op

revision = "0005_audit_v1"
down_revision = "0004_tenant_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE audit_events (
            event_id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants (tenant_id),
            actor_principal_id UUID NOT NULL REFERENCES principals (principal_id),
            action TEXT NOT NULL CHECK (length(action) BETWEEN 1 AND 128),
            target_type TEXT NOT NULL CHECK (length(target_type) BETWEEN 1 AND 64),
            target_id TEXT NOT NULL CHECK (length(target_id) BETWEEN 1 AND 256),
            request_id UUID NOT NULL,
            details JSONB NOT NULL DEFAULT '{}'::jsonb
                CHECK (jsonb_typeof(details) = 'object'),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_audit_event_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit events are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION reject_audit_event_mutation()
        """
    )
    op.execute(
        "CREATE INDEX audit_events_tenant_timeline_idx "
        "ON audit_events (tenant_id, created_at DESC, event_id DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX audit_events_tenant_timeline_idx")
    op.execute("DROP TRIGGER audit_events_append_only ON audit_events")
    op.execute("DROP TABLE audit_events")
    op.execute("DROP FUNCTION reject_audit_event_mutation()")
