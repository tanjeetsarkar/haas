"""Durable workflow: leases, admission, steps, effects, approvals, events, outbox, ledger.

Revision ID: 0009_durable_workflow
Revises: 0008_tenant_rls
"""

from alembic import op

revision = "0009_durable_workflow"
down_revision = "0008_tenant_rls"
branch_labels = None
depends_on = None

OLD_STATUSES = "'running', 'input_required', 'completed', 'failed', 'cancelled'"
NEW_STATUSES = (
    "'queued', 'running', 'input_required', 'cancelling', "
    "'completed', 'failed', 'cancelled'"
)

# Child tables keyed by the tenant alias and run id; tenant_uuid is copied from the run.
RUN_CHILD_TABLES = (
    "run_steps",
    "approval_batches",
    "approval_decisions",
    "tool_invocations",
    "run_events",
    "outbox_messages",
    "consumer_inbox",
    "usage_ledger",
    "admission_requests",
)
IMMUTABLE_TABLES = ("run_events", "approval_decisions")


def _backfill_view(statuses: str) -> str:
    return f"""
        CREATE OR REPLACE VIEW phase2_schema_backfill_status AS
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
                   WHERE status NOT IN ({statuses})
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


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM runs WHERE status IN ('running', 'input_required')) THEN
                RAISE EXCEPTION
                    'drain active and paused runs before the durable workflow cutover';
            END IF;
        END $$;
    """)
    op.execute("ALTER TABLE runs DROP CONSTRAINT runs_status_phase2_check")
    op.execute(f"""
        ALTER TABLE runs
            ADD CONSTRAINT runs_status_phase2_check
                CHECK (status IN ({NEW_STATUSES})),
            ADD COLUMN state_version BIGINT NOT NULL DEFAULT 0
                CHECK (state_version >= 0),
            ADD COLUMN lease_epoch BIGINT NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
            ADD COLUMN lease_owner TEXT,
            ADD COLUMN lease_expires_at TIMESTAMPTZ,
            ADD COLUMN deadline_at TIMESTAMPTZ,
            ADD COLUMN event_sequence BIGINT NOT NULL DEFAULT 0
                CHECK (event_sequence >= 0),
            ADD COLUMN budget_reserved_usd NUMERIC NOT NULL DEFAULT 0
                CHECK (budget_reserved_usd >= 0),
            ADD COLUMN max_tool_invocations INTEGER NOT NULL DEFAULT 100
                CHECK (max_tool_invocations > 0),
            ADD COLUMN tool_invocations_used INTEGER NOT NULL DEFAULT 0
                CHECK (tool_invocations_used >= 0),
            ADD COLUMN recovery_attempts INTEGER NOT NULL DEFAULT 0
                CHECK (recovery_attempts >= 0),
            ADD COLUMN next_action TEXT NOT NULL DEFAULT 'none'
                CHECK (next_action IN ('step', 'aggregate', 'decision', 'none')),
            ADD COLUMN parent_run_id TEXT,
            ADD CONSTRAINT runs_lease_owner_check
                CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL)),
            ADD CONSTRAINT runs_parent_tenant_fk FOREIGN KEY (tenant_id, parent_run_id)
                REFERENCES runs (tenant_id, run_id)
    """)
    op.execute("DROP INDEX runs_one_active_thread_idx")
    op.execute("""
        CREATE UNIQUE INDEX runs_one_active_thread_idx ON runs (tenant_id, thread_id)
            WHERE status IN ('queued', 'running', 'input_required', 'cancelling')
    """)
    op.execute("""
        CREATE INDEX runs_reconcile_idx ON runs (status, lease_expires_at, updated_at_ts)
            WHERE status IN ('queued', 'running', 'input_required', 'cancelling')
    """)
    op.execute("CREATE INDEX runs_parent_idx ON runs (tenant_id, parent_run_id)")
    op.execute(_backfill_view(NEW_STATUSES))

    op.execute("""
        CREATE TABLE admission_requests (
            tenant_id TEXT NOT NULL,
            tenant_uuid UUID REFERENCES tenants (tenant_id),
            principal_key TEXT NOT NULL CHECK (length(principal_key) BETWEEN 1 AND 300),
            operation TEXT NOT NULL CHECK (operation IN ('run.create')),
            idempotency_key TEXT NOT NULL
                CHECK (length(idempotency_key) BETWEEN 1 AND 255),
            request_digest TEXT NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
            run_id TEXT NOT NULL,
            response JSONB NOT NULL CHECK (jsonb_typeof(response) = 'object'),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (tenant_id, principal_key, operation, idempotency_key),
            FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, run_id)
        )
    """)
    op.execute(
        "CREATE INDEX admission_requests_expiry_idx ON admission_requests (expires_at)"
    )
    op.execute("""
        CREATE TABLE run_steps (
            tenant_id TEXT NOT NULL,
            tenant_uuid UUID REFERENCES tenants (tenant_id),
            run_id TEXT NOT NULL,
            step_number INTEGER NOT NULL CHECK (step_number > 0),
            attempt INTEGER NOT NULL CHECK (attempt > 0),
            lease_epoch BIGINT NOT NULL CHECK (lease_epoch > 0),
            model TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN (
                'model_running', 'completed', 'tools_dispatched',
                'waiting_approval', 'failed', 'abandoned'
            )),
            reserved_usd NUMERIC NOT NULL DEFAULT 0 CHECK (reserved_usd >= 0),
            cost_usd NUMERIC CHECK (cost_usd >= 0),
            cost_reported BOOLEAN,
            prompt_tokens INTEGER CHECK (prompt_tokens >= 0),
            cached_tokens INTEGER CHECK (cached_tokens >= 0),
            error TEXT,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            PRIMARY KEY (tenant_id, run_id, step_number, attempt),
            FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, run_id)
        )
    """)
    op.execute("""
        CREATE TABLE approval_batches (
            batch_id UUID PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            tenant_uuid UUID REFERENCES tenants (tenant_id),
            run_id TEXT NOT NULL,
            step_number INTEGER NOT NULL CHECK (step_number > 0),
            batch_digest TEXT NOT NULL CHECK (batch_digest ~ '^[0-9a-f]{64}$'),
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                'pending', 'approved', 'rejected', 'expired', 'cancelled'
            )),
            run_state_version BIGINT NOT NULL CHECK (run_state_version >= 0),
            tool_calls JSONB NOT NULL CHECK (jsonb_typeof(tool_calls) = 'array'),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            resolved_at TIMESTAMPTZ,
            CHECK (expires_at > created_at),
            CHECK ((status = 'pending') = (resolved_at IS NULL)),
            UNIQUE (tenant_id, batch_id),
            FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, run_id)
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX approval_batches_one_pending_idx
            ON approval_batches (tenant_id, run_id) WHERE status = 'pending'
    """)
    op.execute("""
        CREATE INDEX approval_batches_inbox_idx
            ON approval_batches (tenant_id, status, created_at DESC)
    """)
    op.execute("""
        CREATE TABLE approval_decisions (
            decision_id UUID PRIMARY KEY,
            batch_id UUID NOT NULL UNIQUE,
            tenant_id TEXT NOT NULL,
            tenant_uuid UUID REFERENCES tenants (tenant_id),
            run_id TEXT NOT NULL,
            decided_by UUID REFERENCES principals (principal_id),
            decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
            batch_digest TEXT NOT NULL CHECK (batch_digest ~ '^[0-9a-f]{64}$'),
            idempotency_key TEXT CHECK (length(idempotency_key) BETWEEN 1 AND 255),
            reason TEXT CHECK (length(reason) <= 2000),
            request_id UUID NOT NULL,
            decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (tenant_id, batch_id) REFERENCES approval_batches (tenant_id, batch_id),
            FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, run_id)
        )
    """)
    op.execute("""
        CREATE TABLE tool_invocations (
            invocation_id UUID PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            tenant_uuid UUID REFERENCES tenants (tenant_id),
            run_id TEXT NOT NULL,
            step_number INTEGER NOT NULL CHECK (step_number > 0),
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            tool_call_id TEXT NOT NULL CHECK (length(tool_call_id) BETWEEN 1 AND 255),
            tool_name TEXT NOT NULL CHECK (length(tool_name) BETWEEN 1 AND 255),
            queue TEXT NOT NULL,
            arguments TEXT NOT NULL,
            arguments_digest TEXT NOT NULL CHECK (arguments_digest ~ '^[0-9a-f]{64}$'),
            effect_key TEXT NOT NULL UNIQUE CHECK (effect_key ~ '^[0-9a-f]{64}$'),
            status TEXT NOT NULL CHECK (status IN (
                'awaiting_approval', 'approved', 'denied', 'dispatched',
                'succeeded', 'failed', 'unknown', 'cancelled'
            )),
            attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
            lease_epoch BIGINT NOT NULL CHECK (lease_epoch > 0),
            approval_batch_id UUID,
            outcome JSONB CHECK (outcome IS NULL OR jsonb_typeof(outcome) = 'object'),
            preview TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            dispatched_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            UNIQUE (tenant_id, run_id, step_number, ordinal),
            FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, run_id),
            FOREIGN KEY (tenant_id, approval_batch_id)
                REFERENCES approval_batches (tenant_id, batch_id)
        )
    """)
    op.execute("""
        CREATE INDEX tool_invocations_outstanding_idx ON tool_invocations (dispatched_at)
            WHERE status = 'dispatched'
    """)
    op.execute("""
        CREATE TABLE run_events (
            tenant_id TEXT NOT NULL,
            tenant_uuid UUID REFERENCES tenants (tenant_id),
            run_id TEXT NOT NULL,
            sequence BIGINT NOT NULL CHECK (sequence > 0),
            event_id UUID NOT NULL UNIQUE,
            schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
            type TEXT NOT NULL CHECK (type ~ '^[a-z][a-z0-9_]{1,63}$'),
            payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
            occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, run_id, sequence),
            FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, run_id)
        )
    """)
    op.execute("""
        CREATE TABLE outbox_messages (
            message_id UUID PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            tenant_uuid UUID REFERENCES tenants (tenant_id),
            run_id TEXT NOT NULL,
            destination TEXT NOT NULL
                CHECK (destination IN ('orchestrator', 'tool', 'webhook')),
            payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
            dedup_key TEXT NOT NULL UNIQUE CHECK (length(dedup_key) BETWEEN 1 AND 300),
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'dispatched', 'dead')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            dispatched_at TIMESTAMPTZ,
            FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, run_id)
        )
    """)
    op.execute("""
        CREATE INDEX outbox_messages_pending_idx ON outbox_messages (next_attempt_at)
            WHERE state = 'pending'
    """)
    op.execute("""
        CREATE TABLE consumer_inbox (
            consumer TEXT NOT NULL CHECK (consumer IN ('orchestrator', 'tool_outcome')),
            tenant_id TEXT NOT NULL,
            tenant_uuid UUID REFERENCES tenants (tenant_id),
            message_id TEXT NOT NULL CHECK (length(message_id) BETWEEN 1 AND 300),
            run_id TEXT NOT NULL,
            processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (consumer, tenant_id, message_id),
            FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, run_id)
        )
    """)
    op.execute("""
        CREATE TABLE usage_ledger (
            entry_id UUID PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            tenant_uuid UUID REFERENCES tenants (tenant_id),
            run_id TEXT NOT NULL,
            source_attempt_key TEXT NOT NULL
                CHECK (length(source_attempt_key) BETWEEN 1 AND 300),
            unit TEXT NOT NULL CHECK (unit IN (
                'model_usd', 'prompt_tokens', 'tool_invocations',
                'artifact_bytes', 'egress_bytes'
            )),
            quantity NUMERIC NOT NULL CHECK (
                quantity >= 0 AND quantity NOT IN ('NaN'::numeric, 'Infinity'::numeric)
            ),
            cost_usd NUMERIC NOT NULL DEFAULT 0 CHECK (
                cost_usd >= 0 AND cost_usd NOT IN ('NaN'::numeric, 'Infinity'::numeric)
            ),
            settlement_status TEXT NOT NULL CHECK (settlement_status IN (
                'reserved', 'settled', 'released', 'uncertain'
            )),
            pricing_revision TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            settled_at TIMESTAMPTZ,
            UNIQUE (tenant_id, source_attempt_key, unit),
            FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, run_id)
        )
    """)
    op.execute("""
        CREATE INDEX usage_ledger_run_idx ON usage_ledger (tenant_id, run_id, created_at)
    """)

    # Child rows inherit the parent run's tenant key and can never move tenants.
    op.execute("""
        CREATE FUNCTION sync_haas_run_child_tenant()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND (
                NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
                OR NEW.run_id IS DISTINCT FROM OLD.run_id
                OR NEW.tenant_uuid IS DISTINCT FROM OLD.tenant_uuid
            ) THEN
                RAISE EXCEPTION 'workflow rows cannot change tenant or run';
            END IF;
            IF TG_OP = 'INSERT' THEN
                SELECT r.tenant_uuid INTO NEW.tenant_uuid FROM runs r
                WHERE r.tenant_id = NEW.tenant_id AND r.run_id = NEW.run_id;
            END IF;
            RETURN NEW;
        END;
        $$;
    """)
    op.execute("""
        CREATE FUNCTION reject_haas_immutable_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME;
        END;
        $$;
    """)
    for table in RUN_CHILD_TABLES:
        op.execute(f"""
            CREATE TRIGGER {table}_tenant_sync BEFORE INSERT OR UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION sync_haas_run_child_tenant()
        """)
    for table in IMMUTABLE_TABLES:
        op.execute(f"""
            CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_haas_immutable_change()
        """)

    for table in RUN_CHILD_TABLES:
        legacy = (
            " OR (tenant_uuid IS NULL "
            "AND current_setting('haas.legacy_dev', true) = 'on' "
            "AND tenant_id = current_setting('haas.tenant', true))"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_boundary ON {table} TO haas_tenant_app
                USING (tenant_uuid = resolve_haas_tenant_uuid(
                    current_setting('haas.tenant', true)){legacy})
                WITH CHECK (tenant_uuid = resolve_haas_tenant_uuid(
                    current_setting('haas.tenant', true)){legacy});
        """)
        op.execute(f"""
            CREATE POLICY execution_service ON {table} TO haas_execution_service
                USING (true) WITH CHECK (true);
        """)
        op.execute(f"""
            DO $$ BEGIN
                EXECUTE format(
                    'CREATE POLICY schema_administrator ON {table} TO %I USING (true) WITH CHECK (true)',
                    current_user
                );
            END $$;
        """)

    for statement in (
        "GRANT SELECT, INSERT, UPDATE ON run_steps, tool_invocations, approval_batches, "
        "outbox_messages, usage_ledger, admission_requests "
        "TO haas_tenant_app, haas_execution_service",
        "GRANT SELECT, INSERT ON run_events, approval_decisions "
        "TO haas_tenant_app, haas_execution_service",
        "GRANT SELECT, INSERT, DELETE ON consumer_inbox TO haas_execution_service",
        "GRANT DELETE ON admission_requests TO haas_execution_service",
    ):
        op.execute(statement)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM runs
                WHERE status IN ('queued', 'running', 'input_required', 'cancelling')) THEN
                RAISE EXCEPTION
                    'drain nonterminal runs before downgrading the durable workflow';
            END IF;
            IF EXISTS (SELECT 1 FROM outbox_messages WHERE state = 'pending') THEN
                RAISE EXCEPTION 'drain pending outbox messages before downgrade';
            END IF;
        END $$;
    """)
    for table in reversed(RUN_CHILD_TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION reject_haas_immutable_change()")
    op.execute("DROP FUNCTION sync_haas_run_child_tenant()")
    op.execute(_backfill_view(OLD_STATUSES))
    op.execute("DROP INDEX runs_parent_idx")
    op.execute("DROP INDEX runs_reconcile_idx")
    op.execute("DROP INDEX runs_one_active_thread_idx")
    op.execute("""
        CREATE UNIQUE INDEX runs_one_active_thread_idx ON runs (tenant_id, thread_id)
            WHERE status IN ('running', 'input_required')
    """)
    op.execute("ALTER TABLE runs DROP CONSTRAINT runs_status_phase2_check")
    op.execute("""
        ALTER TABLE runs
            DROP CONSTRAINT runs_parent_tenant_fk,
            DROP CONSTRAINT runs_lease_owner_check,
            DROP COLUMN parent_run_id,
            DROP COLUMN next_action,
            DROP COLUMN recovery_attempts,
            DROP COLUMN tool_invocations_used,
            DROP COLUMN max_tool_invocations,
            DROP COLUMN budget_reserved_usd,
            DROP COLUMN event_sequence,
            DROP COLUMN deadline_at,
            DROP COLUMN lease_expires_at,
            DROP COLUMN lease_owner,
            DROP COLUMN lease_epoch,
            DROP COLUMN state_version
    """)
    op.execute(f"""
        ALTER TABLE runs ADD CONSTRAINT runs_status_phase2_check
            CHECK (status IN ({OLD_STATUSES})) NOT VALID
    """)
