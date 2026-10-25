"""add atlas_entity_cache and atlas_layer_nodes tables with forced RLS

Revision ID: 20261023_01
Revises: 0017_multi_agent_runtime
Create Date: 2026-10-23 08:15:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261023_01"
down_revision = "0017_multi_agent_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Shared Entity Cache (Tenant-Isolated with TTL expiration)
    # Note: tenants.tenant_id is UUID, threads/runs use tenant_id (text) + tenant_uuid (uuid)
    # We use UUID for tenant_id to match tenants table
    op.create_table(
        "atlas_entity_cache",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canonical_key", sa.String(length=128), nullable=False),
        sa.Column("dimension_key", sa.String(length=48), nullable=False),
        sa.Column("domain_category", sa.String(length=32), nullable=False),
        sa.Column("attributes_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provenance_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trust_tier", sa.String(length=32), nullable=False),
        sa.Column("content_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "canonical_key", "dimension_key"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name="fk_atlas_cache_tenant",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "trust_tier IN ('verified_official', 'extracted_document', 'aggregated_directory', 'estimated_range', 'unverified')",
            name="ck_atlas_cache_trust_tier",
        ),
    )
    op.create_index(
        "ix_atlas_entity_cache_lookup",
        "atlas_entity_cache",
        ["tenant_id", "canonical_key", "dimension_key", "expires_at"],
    )

    # 2. Materialized Layer Nodes per Exploration Thread/Run
    # Use UUID for tenant_id to match tenants table, and reference threads/runs via tenant_uuid + thread_key/run_key
    op.create_table(
        "atlas_layer_nodes",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("thread_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("run_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_node_id", sa.String(length=64), nullable=True),
        sa.Column("layer_depth", sa.SmallInteger(), nullable=False),
        sa.Column("canonical_key", sa.String(length=128), nullable=False),
        sa.Column("dimension_key", sa.String(length=48), nullable=False),
        sa.Column("component_type", sa.String(length=32), nullable=False),
        sa.Column("node_payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("envelope_digest", sa.String(length=71), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", "node_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "thread_key"],
            ["threads.tenant_uuid", "threads.thread_key"],
            name="fk_atlas_nodes_thread",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_key"],
            ["runs.tenant_uuid", "runs.run_key"],
            name="fk_atlas_nodes_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "parent_node_id"],
            ["atlas_layer_nodes.tenant_id", "atlas_layer_nodes.node_id"],
            name="fk_atlas_nodes_parent",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "layer_depth >= 1 AND layer_depth <= 10",
            name="ck_atlas_nodes_depth",
        ),
    )
    op.create_index(
        "ix_atlas_layer_nodes_thread_parent",
        "atlas_layer_nodes",
        ["tenant_id", "thread_id", "parent_node_id"],
    )

    # 3. Enforce PostgreSQL 16 Row-Level Security (RLS) matching HaaS Phase 2
    # Use app.current_tenant_id which is set to the UUID
    for table in ("atlas_entity_cache", "atlas_layer_nodes"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);
            """
        )

    # 4. Grant permissions to HaaS runtime roles
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON atlas_entity_cache TO haas_tenant_app, haas_execution_service;")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON atlas_layer_nodes TO haas_tenant_app, haas_execution_service;")


def downgrade() -> None:
    op.drop_table("atlas_layer_nodes")
    op.drop_table("atlas_entity_cache")