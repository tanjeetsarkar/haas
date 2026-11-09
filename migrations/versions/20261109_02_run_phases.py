"""Add nullable durable phase configuration and state to runs.

Revision ID: 20261109_02
Revises: 20261109_01
"""

from alembic import op

revision = "20261109_02"
down_revision = "20261109_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE runs ADD COLUMN phase_config_json JSONB
    """)
    op.execute("""
        ALTER TABLE runs ADD COLUMN phase_state_json JSONB
    """)
    op.execute("""
        ALTER TABLE runs ADD CONSTRAINT runs_phase_config_shape_ck
            CHECK (phase_config_json IS NULL OR (
                phase_config_json = jsonb_build_object(
                    'version', 1,
                    'mode', 'bounded_acquisition_finalization',
                    'reserve_basis_points', 2000
                )
                AND jsonb_typeof(phase_config_json->'version') = 'number'
                AND phase_config_json->>'version' = '1'
                AND jsonb_typeof(phase_config_json->'reserve_basis_points') = 'number'
                AND phase_config_json->>'reserve_basis_points' = '2000'
            ))
    """)
    op.execute("""
        ALTER TABLE runs ADD CONSTRAINT runs_phase_state_shape_ck
            CHECK (phase_state_json IS NULL OR
                   jsonb_typeof(phase_state_json) = 'object')
    """)
    op.execute("""
        ALTER TABLE runs ADD CONSTRAINT runs_phase_config_state_pair_ck
            CHECK ((phase_state_json IS NULL) = (phase_config_json IS NULL))
    """)
    op.execute("""
        CREATE FUNCTION protect_run_phases() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            old_phase TEXT;
            new_phase TEXT;
            old_attempt BOOLEAN;
            new_attempt BOOLEAN;
            old_attempt_key TEXT;
            new_attempt_key TEXT;
            old_state_version BIGINT;
            new_state_version BIGINT;
            state_fields INTEGER;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF NEW.phase_config_json IS DISTINCT FROM OLD.phase_config_json THEN
                    RAISE EXCEPTION 'Run phase configuration is immutable'
                        USING ERRCODE = '23514';
                END IF;
                IF OLD.phase_config_json IS NOT NULL AND
                    ROW(NEW.run_id, NEW.tenant_id, NEW.tenant_uuid,
                        NEW.submitted_by_principal_id, NEW.policy_revision_id,
                        NEW.agent_revision_id, NEW.registry_fingerprint)
                    IS DISTINCT FROM
                    ROW(OLD.run_id, OLD.tenant_id, OLD.tenant_uuid,
                        OLD.submitted_by_principal_id, OLD.policy_revision_id,
                        OLD.agent_revision_id, OLD.registry_fingerprint) THEN
                    RAISE EXCEPTION 'Run phase ownership is immutable'
                        USING ERRCODE = '23514';
                END IF;
            END IF;

            IF NEW.phase_config_json IS NULL THEN
                IF NEW.phase_state_json IS NOT NULL THEN
                    RAISE EXCEPTION 'Run phase state requires phase configuration'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;

            IF jsonb_typeof(NEW.phase_config_json) IS DISTINCT FROM 'object'
                OR NEW.phase_config_json IS DISTINCT FROM jsonb_build_object(
                    'version', 1,
                    'mode', 'bounded_acquisition_finalization',
                    'reserve_basis_points', 2000
                ) THEN
                RAISE EXCEPTION 'Invalid run phase configuration'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.phase_state_json IS NULL THEN
                RAISE EXCEPTION 'Enabled run requires initial phase state'
                    USING ERRCODE = '23514';
            END IF;

            IF jsonb_typeof(NEW.phase_state_json) IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION 'Invalid run phase state'
                    USING ERRCODE = '23514';
            END IF;
            SELECT COUNT(*) INTO state_fields
                FROM jsonb_object_keys(NEW.phase_state_json);
            IF state_fields <> 7
                OR NOT (NEW.phase_state_json ? 'version')
                OR NOT (NEW.phase_state_json ? 'state_version')
                OR NOT (NEW.phase_state_json ? 'phase')
                OR NOT (NEW.phase_state_json ? 'successful_tool_outcomes')
                OR NOT (NEW.phase_state_json ? 'finalizer_attempted')
                OR NOT (NEW.phase_state_json ? 'finalizer_attempt_key')
                OR NOT (NEW.phase_state_json ? 'failure_code')
                OR jsonb_typeof(NEW.phase_state_json->'version')
                    IS DISTINCT FROM 'number'
                OR NEW.phase_state_json->>'version' IS DISTINCT FROM '1'
                OR jsonb_typeof(NEW.phase_state_json->'state_version')
                    IS DISTINCT FROM 'number'
                OR COALESCE(NEW.phase_state_json->>'state_version' !~ '^[0-9]+$', TRUE)
                OR jsonb_typeof(NEW.phase_state_json->'successful_tool_outcomes')
                    IS DISTINCT FROM 'number'
                OR COALESCE(
                    NEW.phase_state_json->>'successful_tool_outcomes' !~ '^[0-9]+$',
                    TRUE
                )
                OR jsonb_typeof(NEW.phase_state_json->'finalizer_attempted')
                    IS DISTINCT FROM 'boolean'
                OR NEW.phase_state_json->>'phase' IS NULL
                OR NEW.phase_state_json->>'phase' NOT IN
                    ('acquisition','finalization','completed','failed','cancelled')
                OR (NEW.phase_state_json->>'phase' = 'acquisition'
                    AND NEW.phase_state_json->>'finalizer_attempted' <> 'false')
                OR (NEW.phase_state_json->>'phase' = 'cancelled'
                    AND NEW.phase_state_json->>'finalizer_attempted' <> 'false')
                OR (NEW.phase_state_json->>'phase' IN ('finalization','completed')
                    AND NEW.phase_state_json->>'finalizer_attempted' <> 'true')
                OR (NEW.phase_state_json->>'phase' IN ('finalization','completed')
                    AND (NEW.phase_state_json->>'successful_tool_outcomes')::BIGINT < 1)
                OR (NEW.phase_state_json->>'finalizer_attempted' = 'false'
                    AND (NEW.phase_state_json->>'finalizer_attempt_key' IS NOT NULL
                         OR jsonb_typeof(NEW.phase_state_json->'finalizer_attempt_key')
                            IS DISTINCT FROM 'null'))
                OR (NEW.phase_state_json->>'finalizer_attempted' = 'true'
                    AND (COALESCE(
                            NEW.phase_state_json->>'finalizer_attempt_key', ''
                        ) = ''
                         OR jsonb_typeof(NEW.phase_state_json->'finalizer_attempt_key')
                            IS DISTINCT FROM 'string'))
                OR (NEW.phase_state_json->>'phase' = 'failed'
                    AND NEW.phase_state_json->>'failure_code' IS NULL)
                OR (NEW.phase_state_json->>'phase' = 'failed'
                    AND NEW.phase_state_json->>'failure_code' NOT IN
                        ('no_successful_acquisition_result','insufficient_budget',
                         'finalizer_failed','finalizer_outcome_unknown'))
                OR (NEW.phase_state_json->>'phase' <> 'failed'
                    AND NEW.phase_state_json->>'failure_code' IS NOT NULL)
                OR (NEW.phase_state_json->>'failure_code' IS NOT NULL
                    AND jsonb_typeof(NEW.phase_state_json->'failure_code')
                        IS DISTINCT FROM 'string') THEN
                RAISE EXCEPTION 'Invalid run phase state'
                    USING ERRCODE = '23514';
            END IF;

            IF TG_OP = 'INSERT' THEN
                IF NEW.phase_state_json->>'state_version' <> '0'
                    OR NEW.phase_state_json->>'phase' <> 'acquisition'
                    OR NEW.phase_state_json->>'successful_tool_outcomes' <> '0'
                    OR NEW.phase_state_json->>'finalizer_attempted' <> 'false' THEN
                    RAISE EXCEPTION 'Initial phase state is invalid'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;

            IF NEW.phase_state_json IS NOT DISTINCT FROM OLD.phase_state_json THEN
                RETURN NEW;
            END IF;
            IF OLD.phase_state_json IS NULL THEN
                RAISE EXCEPTION 'Run phase state cannot be initialized after insert'
                    USING ERRCODE = '23514';
            END IF;

            old_phase := OLD.phase_state_json->>'phase';
            new_phase := NEW.phase_state_json->>'phase';
            old_attempt := OLD.phase_state_json->>'finalizer_attempted' = 'true';
            new_attempt := NEW.phase_state_json->>'finalizer_attempted' = 'true';
            old_attempt_key := OLD.phase_state_json->>'finalizer_attempt_key';
            new_attempt_key := NEW.phase_state_json->>'finalizer_attempt_key';
            old_state_version := (OLD.phase_state_json->>'state_version')::BIGINT;
            new_state_version := (NEW.phase_state_json->>'state_version')::BIGINT;

            IF new_state_version <> old_state_version + 1
                OR (NEW.phase_state_json->>'successful_tool_outcomes')::BIGINT
                    < (OLD.phase_state_json->>'successful_tool_outcomes')::BIGINT
                OR (old_phase <> 'acquisition'
                    AND NEW.phase_state_json->>'successful_tool_outcomes'
                        IS DISTINCT FROM
                        OLD.phase_state_json->>'successful_tool_outcomes')
                OR (old_phase = 'acquisition' AND new_phase NOT IN
                    ('acquisition','finalization','failed','cancelled'))
                OR (old_phase = 'finalization' AND new_phase NOT IN
                    ('finalization','completed','failed'))
                OR (old_phase IN ('completed','failed','cancelled')) THEN
                RAISE EXCEPTION 'Run phase state transition is not monotonic'
                    USING ERRCODE = '23514';
            END IF;
            IF (old_attempt AND NOT new_attempt)
                OR (old_attempt AND new_attempt_key IS DISTINCT FROM old_attempt_key)
                OR (NOT old_attempt AND new_attempt AND new_phase <> 'finalization') THEN
                RAISE EXCEPTION 'Finalizer attempt cannot be reset or replaced'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER runs_immutable_phase_config_and_monotonic_state
            BEFORE INSERT OR UPDATE ON runs
            FOR EACH ROW EXECUTE FUNCTION protect_run_phases()
    """)


def downgrade() -> None:
    # Block writes before checking; the transaction retains this lock through DDL.
    op.execute("LOCK TABLE runs IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM runs
                WHERE phase_config_json IS NOT NULL OR phase_state_json IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'Cannot remove persisted run phase data';
            END IF;
        END $$;
    """)
    op.execute("DROP TRIGGER runs_immutable_phase_config_and_monotonic_state ON runs")
    op.execute("DROP FUNCTION protect_run_phases()")
    op.execute("ALTER TABLE runs DROP COLUMN phase_state_json")
    op.execute("ALTER TABLE runs DROP COLUMN phase_config_json")
