"""Record authenticated run submitters for approval separation of duties.

Revision ID: 0003_run_submitter
Revises: 0002_identity_registry
"""

from alembic import op

revision = "0003_run_submitter"
down_revision = "0002_identity_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE runs
        ADD COLUMN submitted_by_principal_id UUID
        REFERENCES principals (principal_id)
        """
    )
    op.execute("CREATE INDEX runs_submitted_by_idx ON runs (submitted_by_principal_id)")


def downgrade() -> None:
    op.execute("DROP INDEX runs_submitted_by_idx")
    op.execute("ALTER TABLE runs DROP COLUMN submitted_by_principal_id")
