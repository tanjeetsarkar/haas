"""add metadata column to threads table

Revision ID: 20261024_01
Revises: 20261023_01
Create Date: 2026-10-24 11:45:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261024_01"
down_revision = "20261023_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("threads", sa.Column("metadata", postgresql.JSONB(), nullable=True, server_default="{}"))


def downgrade() -> None:
    op.drop_column("threads", "metadata")