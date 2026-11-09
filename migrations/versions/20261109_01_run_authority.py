"""Immutable nullable run authority; legacy NULL retains its existing meaning.

Revision ID: 20261109_01
Revises: 20261024_01
"""

from alembic import op

revision = "20261109_01"
down_revision = "20261024_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing runs tenant_id, forced RLS and grants are unchanged.
    op.execute("""
        ALTER TABLE runs ADD COLUMN authority_json JSONB
            CHECK (authority_json IS NULL OR jsonb_typeof(authority_json) = 'object')
    """)
    op.execute("""
        CREATE FUNCTION protect_run_authority() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.authority_json IS DISTINCT FROM OLD.authority_json OR
                (OLD.authority_json IS NOT NULL AND
                 ROW(NEW.run_id, NEW.tenant_id, NEW.tenant_uuid,
                     NEW.submitted_by_principal_id, NEW.policy_revision_id,
                     NEW.agent_revision_id, NEW.registry_fingerprint)
                 IS DISTINCT FROM
                 ROW(OLD.run_id, OLD.tenant_id, OLD.tenant_uuid,
                     OLD.submitted_by_principal_id, OLD.policy_revision_id,
                     OLD.agent_revision_id, OLD.registry_fingerprint)) THEN
                RAISE EXCEPTION 'Run authority is immutable' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER runs_immutable_authority BEFORE UPDATE ON runs
            FOR EACH ROW EXECUTE FUNCTION protect_run_authority();
    """)


def downgrade() -> None:
    # Serialize the emptiness check with INSERTs; retain this lock through DDL.
    op.execute("LOCK TABLE runs IN ACCESS EXCLUSIVE MODE")
    # Never turn an existing restricted run into an unrestricted legacy run.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM runs WHERE authority_json IS NOT NULL) THEN
                RAISE EXCEPTION 'Cannot remove persisted run authority';
            END IF;
        END $$;
    """)
    op.execute("DROP TRIGGER runs_immutable_authority ON runs")
    op.execute("DROP FUNCTION protect_run_authority()")
    op.execute("ALTER TABLE runs DROP COLUMN authority_json")
