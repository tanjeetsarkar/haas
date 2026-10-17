"""Baseline the existing HaaS PostgreSQL schema.

Revision ID: 0001_initial_schema
Revises:
"""

from alembic import op
from sqlalchemy import text

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    has_existing_schema = connection.execute(
        text(
            "SELECT to_regclass('threads') IS NOT NULL "
            "OR to_regclass('runs') IS NOT NULL"
        )
    ).scalar_one()

    if not has_existing_schema:
        op.execute(
            """
            CREATE TABLE threads (
                thread_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                messages_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                updated_at DOUBLE PRECISION NOT NULL
            )
            """
        )
        op.execute(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                traceparent TEXT NOT NULL,
                status TEXT NOT NULL,
                primary_model TEXT NOT NULL,
                planner_model TEXT NOT NULL,
                active_skills_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                pending_tool_calls_json JSONB,
                messages_checkpoint_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                last_step_had_error BOOLEAN NOT NULL DEFAULT FALSE,
                result_text TEXT,
                total_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                max_steps INTEGER NOT NULL DEFAULT 15,
                max_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0.30,
                step_count INTEGER NOT NULL DEFAULT 0,
                fingerprints_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                webhook_url TEXT,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                skill_allowlist_json JSONB,
                registry_fingerprint TEXT
            )
            """
        )
        op.execute(
            "CREATE TABLE haas_baseline_ownership (id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id))"
        )
        op.execute("INSERT INTO haas_baseline_ownership (id) VALUES (TRUE)")
    else:
        existing_tables = (
            connection.execute(
                text(
                    "SELECT to_regclass('threads') IS NOT NULL AS threads, "
                    "to_regclass('runs') IS NOT NULL AS runs"
                )
            )
            .mappings()
            .one()
        )
        if not existing_tables["threads"] or not existing_tables["runs"]:
            raise RuntimeError(
                "Cannot baseline a partial HaaS schema; both threads and runs must exist"
            )

    op.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS skill_allowlist_json JSONB")
    op.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS registry_fingerprint TEXT")
    op.execute("CREATE INDEX IF NOT EXISTS runs_thread_idx ON runs (thread_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS runs_tenant_created_idx "
        "ON runs (tenant_id, created_at DESC, run_id DESC)"
    )


def downgrade() -> None:
    connection = op.get_bind()
    owns_schema = connection.execute(
        text("SELECT to_regclass('haas_baseline_ownership') IS NOT NULL")
    ).scalar_one()
    if owns_schema:
        op.execute("DROP TABLE runs")
        op.execute("DROP TABLE threads")
        op.execute("DROP TABLE haas_baseline_ownership")
