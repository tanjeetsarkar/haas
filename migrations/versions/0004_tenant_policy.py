"""Add immutable tenant policy revisions and pin them to runs.

Revision ID: 0004_tenant_policy
Revises: 0003_run_submitter
"""

from alembic import op

revision = "0004_tenant_policy"
down_revision = "0003_run_submitter"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE tenant_policy_revisions (
            policy_revision_id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants (tenant_id),
            version BIGINT NOT NULL CHECK (version > 0),
            digest TEXT NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
            rules JSONB NOT NULL CHECK (jsonb_typeof(rules) = 'object'),
            created_by_principal_id UUID REFERENCES principals (principal_id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, version),
            UNIQUE (tenant_id, policy_revision_id)
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_tenant_policy_revision_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'tenant policy revisions are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER tenant_policy_revisions_immutable
        BEFORE UPDATE OR DELETE ON tenant_policy_revisions
        FOR EACH ROW EXECUTE FUNCTION reject_tenant_policy_revision_mutation()
        """
    )
    op.execute(
        "CREATE INDEX tenant_policy_revisions_tenant_idx "
        "ON tenant_policy_revisions (tenant_id, version DESC)"
    )
    op.execute("ALTER TABLE tenants ADD COLUMN active_policy_revision_id UUID")
    op.execute(
        """
        ALTER TABLE tenants
        ADD CONSTRAINT tenants_active_policy_revision_fk
        FOREIGN KEY (tenant_id, active_policy_revision_id)
        REFERENCES tenant_policy_revisions (tenant_id, policy_revision_id)
        """
    )
    op.execute(
        """
        ALTER TABLE runs
        ADD COLUMN policy_revision_id UUID
        REFERENCES tenant_policy_revisions (policy_revision_id)
        """
    )
    op.execute("CREATE INDEX runs_policy_revision_idx ON runs (policy_revision_id)")


def downgrade() -> None:
    op.execute("DROP INDEX runs_policy_revision_idx")
    op.execute("ALTER TABLE runs DROP COLUMN policy_revision_id")
    op.execute("ALTER TABLE tenants DROP CONSTRAINT tenants_active_policy_revision_fk")
    op.execute("ALTER TABLE tenants DROP COLUMN active_policy_revision_id")
    op.execute("DROP INDEX tenant_policy_revisions_tenant_idx")
    op.execute(
        "DROP TRIGGER tenant_policy_revisions_immutable ON tenant_policy_revisions"
    )
    op.execute("DROP TABLE tenant_policy_revisions")
    op.execute("DROP FUNCTION reject_tenant_policy_revision_mutation()")
