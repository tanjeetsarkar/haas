# HaaS Atlas — Data Model, ERD & Data Lifecycle

## 1. Storage Strategy & Strict HaaS RLS Alignment

In `tanjeetsarkar/haas`, all persistent state resides in **PostgreSQL 16** under strict multi-tenant isolation:
* Every tenant-scoped table includes `tenant_id` as part of **tenant-composite foreign keys**.
* Every table enforces `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY` scoped to the transaction session variable set by the HaaS API and execution database roles (`app.current_tenant_id`).
* Runtime services (`haas-api`, `worker-orchestrator`, `worker-tools`) connect using non-owner PostgreSQL logins; DDL is strictly executed via the advisory-locked Alembic migration job (`migrations/`).

To keep HaaS Atlas lean, we add **only two tables** (`atlas_entity_cache` and `atlas_layer_nodes`) to PostgreSQL 16 that link directly to HaaS's existing `tenants`, `threads`, and `runs` tables.

---

## 2. Entity-Relationship Diagram (ERD)

```
+-------------------------------------------------------------------------+
|                           tenants (Existing HaaS)                       |
|-------------------------------------------------------------------------|
| PK  tenant_id          VARCHAR(64)                                      |
|     status             VARCHAR(32)                                      |
|     active_policy_rev  UUID                                             |
+-------------------+---------------------------------+-------------------+
                    | 1                               | 1
                    |                                 |
                    | *                               | *
+-------------------+-------------------+   +---------+-------------------------------+
|       threads (Existing HaaS)         |   |      atlas_entity_cache (New Atlas)     |
|---------------------------------------|   |-----------------------------------------|
| PK,FK1 tenant_id       VARCHAR(64)    |   | PK,FK1 tenant_id        VARCHAR(64)     |
| PK     thread_id       VARCHAR(64)    |   | PK     canonical_key    VARCHAR(128)    |
|        parent_thread_id VARCHAR(64)   |   | PK     dimension_key    VARCHAR(48)     |
|        branched_at_ver INTEGER        |   |        domain_category  VARCHAR(32)     |
|        current_version INTEGER        |   |        attributes_json  JSONB           |
|        claimed_by_run  VARCHAR(64)    |   |        provenance_json  JSONB           |
+-------------------+-------------------+   |        trust_tier       VARCHAR(32)     |
                    | 1                     |        content_sha256   CHAR(64)        |
                    |                       |        fetched_at       TIMESTAMPTZ     |
                    | *                     |        expires_at       TIMESTAMPTZ     |
+-------------------+-------------------+   +-------------------+---------------------+
|         runs (Existing HaaS)          |                       | 0..1
|---------------------------------------|                       |
| PK,FK1 tenant_id       VARCHAR(64)    |                       |
| PK     run_id          VARCHAR(64)    |                       |
| FK2    thread_id       VARCHAR(64)    |                       |
|        parent_run_id   VARCHAR(64)    |                       |
|        status          VARCHAR(32)    |                       |
|        state_version   INTEGER        |                       |
|        cost_usd        NUMERIC(12,6)  |                       |
+-------------------+-------------------+                       |
                    | 1                                         |
                    |                                           |
                    | *                                         | *
+-------------------+-------------------------------------------+---------+
|                        atlas_layer_nodes (New Atlas)                    |
|-------------------------------------------------------------------------|
| PK,FK1 tenant_id          VARCHAR(64)                                   |
| PK     node_id            VARCHAR(64)                                   |
| FK2    thread_id          VARCHAR(64)                                   |
| FK3    run_id             VARCHAR(64)                                   |
|        parent_node_id     VARCHAR(64)  NULL                             |
|        layer_depth        SMALLINT     CHECK (layer_depth BETWEEN 1..10)|
|        canonical_key      VARCHAR(128)                                  |
|        dimension_key      VARCHAR(48)                                   |
|        component_type     VARCHAR(32)                                   |
|        node_payload_json  JSONB                                         |
|        envelope_digest    VARCHAR(71)                                   |
|        created_at         TIMESTAMPTZ                                   |
+-------------------------------------------------------------------------+
```

---

## 3. PostgreSQL 16 DDL & RLS Policies (Alembic Migration)

The following Alembic migration (`migrations/versions/20261023_01_add_atlas_tables.py`) follows HaaS's Phase 2 database conventions (`ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY`, composite tenant foreign keys, and explicit grants to non-owner runtime roles `haas_api_role` and `haas_worker_role`):

```python
"""add atlas_entity_cache and atlas_layer_nodes tables with forced RLS

Revision ID: 20261023_01
Revises: phase2_cutover
Create Date: 2026-10-23 08:15:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261023_01"
down_revision = "phase2_cutover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Shared Entity Cache (Tenant-Isolated with TTL expiration)
    op.create_table(
        "atlas_entity_cache",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
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
    op.create_table(
        "atlas_layer_nodes",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
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
            ["tenant_id", "thread_id"],
            ["threads.tenant_id", "threads.thread_id"],
            name="fk_atlas_nodes_thread",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.run_id"],
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
    for table in ("atlas_entity_cache", "atlas_layer_nodes"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.current_tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true));
            """
        )


def downgrade() -> None:
    op.drop_table("atlas_layer_nodes")
    op.drop_table("atlas_entity_cache")
```

---

## 4. Data Lifecycle, TTLs & Garbage Collection

### 4.1 Attribute-Specific Cache TTL Policy
Not all layers age at the same rate. `atlas_cache_upsert` computes `expires_at` dynamically based on `(domain_category, dimension_key, trust_tier)`:

| Dimension Key | Example Layer | Default TTL (`verified_official` / `extracted_document`) | Default TTL (`estimated_range` / `unverified`) |
| :--- | :--- | :--- | :--- |
| `root` | Layer 1: Cancer Hospitals in Mumbai | $30\text{ days}$ | $3\text{ days}$ |
| `facilities` | Layer 2: LINAC, PET-CT, BMT Units | $14\text{ days}$ | $48\text{ hours}$ |
| `pricing` | Layer 3: Ward Tariffs & Scheme Coverage | $7\text{ days}$ | $24\text{ hours}$ |
| `specialists` | Layer 4: Oncologist Roster | $7\text{ days}$ | $24\text{ hours}$ |
| `credentials` | Layer 5: Qualifications & OPD Schedule | $3\text{ days}$ | $12\text{ hours}$ |

### 4.2 Scratchpad Volume & Retention Cleanup
As noted in HaaS's known caveats:
> *"data-analyst datasets live on the shared scratchpad volume (per tenant + thread) and are not garbage-collected."*

When HaaS Atlas uses `data-analyst` (`dataset_load`, `dataset_query`) to parse hospital tariff CSVs/tables on the per-tenant/thread scratchpad volume:
1. Once validated summary rows are promoted into `atlas_entity_cache`, a periodic Celery beat task (`reap_expired_atlas_artifacts_task`) deletes scratchpad files older than the tenant policy's `retention_class` window (default $24\text{ hours}$).
2. Expired rows in `atlas_entity_cache` (`expires_at < now()`) are purged in bounded batches of $1{,}000$ rows per run.