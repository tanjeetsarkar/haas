"""Add backward-compatible tenant-aware schema expansion fields.

Revision ID: 0006_phase2_schema_expand
Revises: 0005_audit_v1
"""

from alembic import op

revision = "0006_phase2_schema_expand"
down_revision = "0005_audit_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE legacy_tenant_mappings (
            legacy_tenant_id TEXT PRIMARY KEY
                CHECK (length(legacy_tenant_id) BETWEEN 1 AND 256),
            tenant_uuid UUID NOT NULL REFERENCES tenants (tenant_id),
            approved_by_principal_id UUID NOT NULL
                REFERENCES principals (principal_id),
            approved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 1024)
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION resolve_haas_tenant_uuid(legacy_id TEXT)
        RETURNS UUID
        LANGUAGE SQL
        STABLE
        AS $$
            SELECT COALESCE(
                (SELECT tenant_uuid FROM legacy_tenant_mappings
                 WHERE legacy_tenant_id = legacy_id),
                (SELECT tenant_id FROM tenants
                 WHERE tenant_id::text = legacy_id),
                (SELECT tenant_id FROM tenants WHERE slug = legacy_id)
            )
        $$
        """
    )

    op.execute(
        """
        ALTER TABLE threads
            ADD COLUMN tenant_uuid UUID,
            ADD COLUMN thread_key UUID,
            ADD COLUMN history_version BIGINT NOT NULL DEFAULT 0,
            ADD COLUMN updated_at_ts TIMESTAMPTZ
        """
    )
    op.execute(
        """
        ALTER TABLE runs
            ADD COLUMN tenant_uuid UUID,
            ADD COLUMN run_key UUID,
            ADD COLUMN thread_key UUID,
            ADD COLUMN created_at_ts TIMESTAMPTZ,
            ADD COLUMN updated_at_ts TIMESTAMPTZ,
            ADD COLUMN total_cost_usd_exact NUMERIC,
            ADD COLUMN max_cost_usd_exact NUMERIC
        """
    )

    op.execute(
        """
        ALTER TABLE threads
            ADD CONSTRAINT threads_tenant_uuid_fk
                FOREIGN KEY (tenant_uuid) REFERENCES tenants (tenant_id) NOT VALID,
            ADD CONSTRAINT threads_tenant_key_uq
                UNIQUE (tenant_uuid, thread_key),
            ADD CONSTRAINT threads_history_version_nonnegative
                CHECK (history_version >= 0) NOT VALID
        """
    )
    op.execute(
        """
        ALTER TABLE runs
            ADD CONSTRAINT runs_tenant_uuid_fk
                FOREIGN KEY (tenant_uuid) REFERENCES tenants (tenant_id) NOT VALID,
            ADD CONSTRAINT runs_tenant_key_uq
                UNIQUE (tenant_uuid, run_key),
            ADD CONSTRAINT runs_thread_key_tenant_fk
                FOREIGN KEY (tenant_uuid, thread_key)
                REFERENCES threads (tenant_uuid, thread_key) NOT VALID,
            ADD CONSTRAINT runs_status_phase2_check
                CHECK (status IN (
                    'running', 'input_required', 'completed', 'failed', 'cancelled'
                )) NOT VALID,
            ADD CONSTRAINT runs_cost_phase2_check
                CHECK (
                    total_cost_usd_exact >= 0
                    AND max_cost_usd_exact > 0
                    AND total_cost_usd_exact NOT IN ('NaN'::numeric, 'Infinity'::numeric)
                    AND max_cost_usd_exact NOT IN ('NaN'::numeric, 'Infinity'::numeric)
                ) NOT VALID
        """
    )
    op.execute(
        "CREATE INDEX legacy_tenant_mappings_tenant_idx "
        "ON legacy_tenant_mappings (tenant_uuid)"
    )
    op.execute(
        "CREATE INDEX runs_tenant_uuid_created_ts_idx "
        "ON runs (tenant_uuid, created_at_ts DESC, run_key DESC)"
    )

    op.execute(
        """
        CREATE FUNCTION sync_haas_thread_schema_expand()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE'
               AND OLD.thread_key IS NOT NULL
               AND NEW.thread_key IS DISTINCT FROM OLD.thread_key THEN
                RAISE EXCEPTION 'thread_key is immutable';
            END IF;
            IF NEW.thread_key IS NULL THEN
                NEW.thread_key := gen_random_uuid();
            END IF;
            IF TG_OP = 'INSERT'
               OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
               OR NEW.tenant_uuid IS DISTINCT FROM OLD.tenant_uuid
               OR NEW.tenant_uuid IS NULL THEN
                NEW.tenant_uuid := resolve_haas_tenant_uuid(NEW.tenant_id);
            END IF;
            NEW.updated_at_ts := to_timestamp(NEW.updated_at);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER threads_schema_expand_sync
        BEFORE INSERT OR UPDATE ON threads
        FOR EACH ROW EXECUTE FUNCTION sync_haas_thread_schema_expand()
        """
    )
    op.execute(
        """
        CREATE FUNCTION sync_haas_run_schema_expand()
        RETURNS trigger AS $$
        DECLARE
            resolved_thread_key UUID;
            identity_changed BOOLEAN;
        BEGIN
            IF TG_OP = 'UPDATE'
               AND OLD.run_key IS NOT NULL
               AND NEW.run_key IS DISTINCT FROM OLD.run_key THEN
                RAISE EXCEPTION 'run_key is immutable';
            END IF;
            IF NEW.run_key IS NULL THEN
                NEW.run_key := gen_random_uuid();
            END IF;
            identity_changed := TG_OP = 'INSERT'
                OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
                OR NEW.thread_id IS DISTINCT FROM OLD.thread_id
                OR NEW.tenant_uuid IS DISTINCT FROM OLD.tenant_uuid;
            IF identity_changed OR NEW.tenant_uuid IS NULL THEN
                NEW.tenant_uuid := resolve_haas_tenant_uuid(NEW.tenant_id);
            END IF;

            NEW.created_at_ts := to_timestamp(NEW.created_at);
            NEW.updated_at_ts := to_timestamp(NEW.updated_at);
            NEW.total_cost_usd_exact := NEW.total_cost_usd::numeric;
            NEW.max_cost_usd_exact := NEW.max_cost_usd::numeric;

            IF NEW.tenant_uuid IS NULL THEN
                NEW.thread_key := NULL;
                RETURN NEW;
            END IF;

            SELECT t.thread_key
            INTO resolved_thread_key
            FROM threads AS t
            WHERE t.thread_id = NEW.thread_id
              AND t.tenant_uuid = NEW.tenant_uuid;

            IF resolved_thread_key IS NULL THEN
                IF identity_changed THEN
                    RAISE EXCEPTION
                        'run thread is not owned by the resolved tenant';
                END IF;
                NEW.thread_key := NULL;
            ELSE
                NEW.thread_key := resolved_thread_key;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER runs_schema_expand_sync
        BEFORE INSERT OR UPDATE ON runs
        FOR EACH ROW EXECUTE FUNCTION sync_haas_run_schema_expand()
        """
    )
    op.execute(
        """
        CREATE VIEW phase2_schema_backfill_status AS
        SELECT 'threads'::text AS relation,
               count(*) AS total_rows,
               count(*) FILTER (WHERE tenant_uuid IS NULL) AS unresolved_tenant_rows,
               0::bigint AS unresolved_thread_rows,
               0::bigint AS unresolved_state_rows,
               count(*) FILTER (
                   WHERE thread_key IS NULL OR updated_at_ts IS NULL
               ) AS unresolved_schema_rows
        FROM threads
        UNION ALL
        SELECT 'runs'::text AS relation,
               count(*) AS total_rows,
               count(*) FILTER (WHERE tenant_uuid IS NULL) AS unresolved_tenant_rows,
               count(*) FILTER (
                   WHERE tenant_uuid IS NOT NULL AND thread_key IS NULL
               ) AS unresolved_thread_rows,
               count(*) FILTER (
                   WHERE status NOT IN (
                       'running', 'input_required', 'completed', 'failed', 'cancelled'
                   )
                      OR COALESCE(total_cost_usd_exact, total_cost_usd::numeric) < 0
                      OR COALESCE(max_cost_usd_exact, max_cost_usd::numeric) <= 0
                      OR COALESCE(total_cost_usd_exact, total_cost_usd::numeric)
                            IN ('NaN'::numeric, 'Infinity'::numeric)
                      OR COALESCE(max_cost_usd_exact, max_cost_usd::numeric)
                            IN ('NaN'::numeric, 'Infinity'::numeric)
               ) AS unresolved_state_rows,
               count(*) FILTER (
                   WHERE run_key IS NULL
                      OR created_at_ts IS NULL
                      OR updated_at_ts IS NULL
                      OR total_cost_usd_exact IS NULL
                      OR max_cost_usd_exact IS NULL
               ) AS unresolved_schema_rows
        FROM runs
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM legacy_tenant_mappings) THEN
                RAISE EXCEPTION
                    'export and explicitly clear legacy tenant mappings before downgrade';
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP VIEW phase2_schema_backfill_status")
    op.execute("DROP TRIGGER runs_schema_expand_sync ON runs")
    op.execute("DROP FUNCTION sync_haas_run_schema_expand()")
    op.execute("DROP TRIGGER threads_schema_expand_sync ON threads")
    op.execute("DROP FUNCTION sync_haas_thread_schema_expand()")
    op.execute("DROP INDEX runs_tenant_uuid_created_ts_idx")
    op.execute("DROP INDEX legacy_tenant_mappings_tenant_idx")
    op.execute("ALTER TABLE runs DROP CONSTRAINT runs_thread_key_tenant_fk")
    op.execute("ALTER TABLE runs DROP CONSTRAINT runs_cost_phase2_check")
    op.execute("ALTER TABLE runs DROP CONSTRAINT runs_status_phase2_check")
    op.execute("ALTER TABLE runs DROP CONSTRAINT runs_tenant_key_uq")
    op.execute("ALTER TABLE runs DROP CONSTRAINT runs_tenant_uuid_fk")
    op.execute(
        """
        ALTER TABLE runs
            DROP COLUMN max_cost_usd_exact,
            DROP COLUMN total_cost_usd_exact,
            DROP COLUMN updated_at_ts,
            DROP COLUMN created_at_ts,
            DROP COLUMN thread_key,
            DROP COLUMN run_key,
            DROP COLUMN tenant_uuid
        """
    )
    op.execute(
        "ALTER TABLE threads DROP CONSTRAINT threads_history_version_nonnegative"
    )
    op.execute("ALTER TABLE threads DROP CONSTRAINT threads_tenant_key_uq")
    op.execute("ALTER TABLE threads DROP CONSTRAINT threads_tenant_uuid_fk")
    op.execute(
        """
        ALTER TABLE threads
            DROP COLUMN updated_at_ts,
            DROP COLUMN history_version,
            DROP COLUMN thread_key,
            DROP COLUMN tenant_uuid
        """
    )
    op.execute("DROP FUNCTION resolve_haas_tenant_uuid(TEXT)")
    op.execute("DROP TABLE legacy_tenant_mappings")
