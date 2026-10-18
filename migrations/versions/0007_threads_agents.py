"""Add fenced threads and immutable agent revisions.

Revision ID: 0007_threads_agents
Revises: 0006_phase2_schema_expand
"""

from alembic import op

revision = "0007_threads_agents"
down_revision = "0006_phase2_schema_expand"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM runs WHERE status IN ('running', 'input_required')) THEN
                RAISE EXCEPTION 'drain legacy runs to terminal before Phase 2 thread cutover';
            END IF;
        END $$;
    """)
    op.execute("""
        ALTER TABLE threads
            ADD COLUMN active_run_id TEXT,
            ADD COLUMN parent_thread_id TEXT REFERENCES threads (thread_id),
            ADD COLUMN parent_history_version BIGINT CHECK (parent_history_version >= 0),
            ADD CONSTRAINT threads_parent_version_check CHECK (
                (parent_thread_id IS NULL) = (parent_history_version IS NULL)
            ),
            ADD CONSTRAINT threads_tenant_alias_uq UNIQUE (tenant_id, thread_id),
            ADD CONSTRAINT threads_parent_tenant_fk
                FOREIGN KEY (tenant_id, parent_thread_id)
                REFERENCES threads (tenant_id, thread_id);
    """)
    op.execute("""
        ALTER TABLE runs
            ADD COLUMN thread_history_version BIGINT CHECK (thread_history_version >= 0),
            ADD CONSTRAINT runs_tenant_alias_uq UNIQUE (tenant_id, run_id),
            ADD CONSTRAINT runs_thread_alias_fk FOREIGN KEY (tenant_id, thread_id)
                REFERENCES threads (tenant_id, thread_id) NOT VALID;
    """)
    op.execute("""
        ALTER TABLE threads ADD CONSTRAINT threads_active_run_fk
            FOREIGN KEY (tenant_id, active_run_id)
            REFERENCES runs (tenant_id, run_id) DEFERRABLE INITIALLY DEFERRED;
    """)
    op.execute("""
        CREATE UNIQUE INDEX runs_one_active_thread_idx ON runs (tenant_id, thread_id)
            WHERE status IN ('running', 'input_required');
    """)
    op.execute("""
        CREATE TABLE agent_revisions (
            tenant_id UUID NOT NULL REFERENCES tenants (tenant_id),
            revision_id UUID NOT NULL,
            agent_name TEXT NOT NULL CHECK (length(agent_name) BETWEEN 1 AND 128),
            version BIGINT NOT NULL CHECK (version > 0),
            state TEXT NOT NULL DEFAULT 'draft'
                CHECK (state IN ('draft', 'validated', 'published')),
            configuration JSONB NOT NULL CHECK (jsonb_typeof(configuration) = 'object'),
            digest TEXT NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
            policy_revision_id UUID NOT NULL,
            registry_fingerprint TEXT NOT NULL,
            created_by UUID NOT NULL REFERENCES principals (principal_id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            published_at TIMESTAMPTZ,
            PRIMARY KEY (tenant_id, revision_id),
            UNIQUE (tenant_id, agent_name, version),
            FOREIGN KEY (tenant_id, policy_revision_id)
                REFERENCES tenant_policy_revisions (tenant_id, policy_revision_id),
            CHECK ((state = 'published') = (published_at IS NOT NULL))
        );
    """)
    op.execute("""
        ALTER TABLE runs ADD COLUMN agent_revision_id UUID,
            ADD CONSTRAINT runs_policy_tenant_fk
                FOREIGN KEY (tenant_uuid, policy_revision_id)
                REFERENCES tenant_policy_revisions (tenant_id, policy_revision_id) NOT VALID,
            ADD CONSTRAINT runs_agent_revision_fk
                FOREIGN KEY (tenant_uuid, agent_revision_id)
                REFERENCES agent_revisions (tenant_id, revision_id);
    """)
    op.execute("""
        CREATE INDEX agent_revisions_timeline_idx
            ON agent_revisions (tenant_id, created_at DESC, revision_id DESC);
    """)
    op.execute("""
        CREATE INDEX threads_tenant_history_idx
            ON threads (tenant_id, updated_at DESC, thread_id DESC);
    """)
    op.execute("""
        CREATE TABLE phase2_quarantine (
            source_table TEXT NOT NULL CHECK (source_table IN ('runs','threads')),
            source_id TEXT NOT NULL,
            payload JSONB NOT NULL,
            reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 1024),
            operator_principal_id UUID NOT NULL REFERENCES principals (principal_id),
            quarantined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (source_table, source_id)
        );
    """)
    op.execute("""
        CREATE FUNCTION guard_agent_revision() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'agent revisions cannot be deleted';
            END IF;
            IF OLD.state = 'published'
               OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
               OR NEW.revision_id IS DISTINCT FROM OLD.revision_id
               OR NEW.agent_name IS DISTINCT FROM OLD.agent_name
               OR NEW.version IS DISTINCT FROM OLD.version
               OR NEW.configuration IS DISTINCT FROM OLD.configuration
               OR NEW.digest IS DISTINCT FROM OLD.digest
               OR NEW.policy_revision_id IS DISTINCT FROM OLD.policy_revision_id
               OR NEW.registry_fingerprint IS DISTINCT FROM OLD.registry_fingerprint
               OR NEW.created_by IS DISTINCT FROM OLD.created_by
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR NOT ((OLD.state = 'draft' AND NEW.state = 'validated')
                   OR (OLD.state = 'validated' AND NEW.state = 'published')) THEN
                RAISE EXCEPTION 'invalid or immutable agent revision transition';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER agent_revision_immutable BEFORE UPDATE OR DELETE
            ON agent_revisions FOR EACH ROW EXECUTE FUNCTION guard_agent_revision();
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM agent_revisions)
               OR EXISTS (SELECT 1 FROM phase2_quarantine) THEN
                RAISE EXCEPTION 'agent revisions or quarantine exist; use forward recovery';
            END IF;
        END $$;
    """)
    op.execute("DROP INDEX threads_tenant_history_idx")
    op.execute("DROP INDEX runs_one_active_thread_idx")
    op.execute("ALTER TABLE threads DROP CONSTRAINT threads_active_run_fk")
    op.execute("""
        ALTER TABLE runs DROP CONSTRAINT runs_agent_revision_fk,
            DROP CONSTRAINT runs_policy_tenant_fk,
            DROP COLUMN agent_revision_id, DROP COLUMN thread_history_version,
            DROP CONSTRAINT runs_thread_alias_fk, DROP CONSTRAINT runs_tenant_alias_uq;
    """)
    op.execute("DROP TABLE agent_revisions")
    op.execute("DROP TABLE phase2_quarantine")
    op.execute("DROP FUNCTION guard_agent_revision()")
    op.execute("""
        ALTER TABLE threads DROP CONSTRAINT threads_parent_version_check,
            DROP CONSTRAINT threads_parent_tenant_fk,
            DROP CONSTRAINT threads_tenant_alias_uq,
            DROP COLUMN active_run_id, DROP COLUMN parent_thread_id,
            DROP COLUMN parent_history_version;
    """)
