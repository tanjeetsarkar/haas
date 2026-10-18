"""Record provider completion tokens alongside prompt tokens.

Revision ID: 0010_completion_usage
Revises: 0009_durable_workflow
"""

from alembic import op

revision = "0010_completion_usage"
down_revision = "0009_durable_workflow"
branch_labels = None
depends_on = None

_UNITS = (
    "'model_usd', 'prompt_tokens', 'tool_invocations', 'artifact_bytes', 'egress_bytes'"
)


def upgrade() -> None:
    op.execute("ALTER TABLE usage_ledger DROP CONSTRAINT usage_ledger_unit_check")
    op.execute(f"""
        ALTER TABLE usage_ledger ADD CONSTRAINT usage_ledger_unit_check
            CHECK (unit IN ({_UNITS}, 'completion_tokens'))
    """)
    op.execute("""
        ALTER TABLE run_steps ADD COLUMN completion_tokens INTEGER
            CHECK (completion_tokens >= 0)
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM usage_ledger WHERE unit = 'completion_tokens') THEN
                RAISE EXCEPTION
                    'usage_ledger holds completion_tokens rows; archive them before downgrade';
            END IF;
        END $$;
    """)
    op.execute("ALTER TABLE run_steps DROP COLUMN completion_tokens")
    op.execute("ALTER TABLE usage_ledger DROP CONSTRAINT usage_ledger_unit_check")
    op.execute(
        f"ALTER TABLE usage_ledger ADD CONSTRAINT usage_ledger_unit_check CHECK (unit IN ({_UNITS}))"
    )
