"""Signed webhooks, quotas/fairness, restore epochs, cells/placement (P5/P6, owner OPS).

Tenant-owned tables follow the 0011 RLS pattern. Platform-wide state (cells, restore
epoch) has no tenant key and is only changed through the SECURITY DEFINER functions
below or by PLATFORM_OPERATE API routes; cross-tenant reads (metrics, placement lists)
also go through definer functions so the tenant runtime role never bypasses RLS.

Revision ID: 0015_service_operations
Revises: 0014_artifacts_retention
"""

from alembic import op

revision = "0015_service_operations"
down_revision = "0014_artifacts_retention"
branch_labels = None
depends_on = None

TENANT_TABLES = (
    "webhook_endpoints",
    "webhook_signing_keys",
    "webhook_deliveries",
    "webhook_delivery_attempts",
    "tenant_quotas",
    "tenant_placements",
)
PLATFORM_TABLES = ("platform_state", "restore_events", "cells")
TERMINAL = "('completed', 'failed', 'cancelled')"
DEFINER = "SECURITY DEFINER SET search_path = pg_catalog, public"
FUNCTIONS = (
    "haas_ops_run_admission()",
    "haas_ops_stamp_restore_epoch()",
    "haas_ops_outbox_fairness()",
    "haas_ops_fence_runs(UUID)",
    "haas_ops_bump_restore_epoch(TEXT, TEXT)",
    "haas_ops_metrics()",
    "haas_ops_set_quota(UUID, INTEGER, INTEGER, INTEGER, INTEGER, INTEGER, INTEGER)",
    "haas_ops_list_placements(TEXT, BOOLEAN)",
    "haas_ops_begin_move(UUID, TEXT, TEXT, BIGINT, BOOLEAN)",
    "haas_ops_finish_move(UUID, BIGINT, BOOLEAN)",
)


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_boundary ON {table} TO haas_tenant_app
            USING (tenant_id = resolve_haas_tenant_uuid(
                current_setting('haas.tenant', true)))
            WITH CHECK (tenant_id = resolve_haas_tenant_uuid(
                current_setting('haas.tenant', true)));
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


def _tables() -> None:
    op.execute("""
        CREATE TABLE platform_state (
            singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
            restore_epoch BIGINT NOT NULL DEFAULT 1 CHECK (restore_epoch >= 1),
            default_max_concurrent_runs INTEGER NOT NULL DEFAULT 200
                CHECK (default_max_concurrent_runs > 0),
            default_max_inflight_runs INTEGER NOT NULL DEFAULT 32
                CHECK (default_max_inflight_runs > 0),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("INSERT INTO platform_state DEFAULT VALUES")
    op.execute("""
        CREATE TABLE restore_events (
            event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            previous_epoch BIGINT NOT NULL,
            restore_epoch BIGINT NOT NULL CHECK (restore_epoch > previous_epoch),
            reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
            actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 200),
            fenced_runs INTEGER NOT NULL DEFAULT 0,
            reset_deliveries INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE cells (
            cell_id TEXT PRIMARY KEY CHECK (cell_id ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
            region TEXT NOT NULL CHECK (length(region) BETWEEN 1 AND 64),
            base_url TEXT NOT NULL CHECK (base_url ~ '^https?://[^\\s]+$'),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'draining', 'drained')),
            dedicated BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE tenant_placements (
            tenant_id UUID PRIMARY KEY REFERENCES tenants (tenant_id),
            cell_id TEXT NOT NULL REFERENCES cells (cell_id),
            placement_epoch BIGINT NOT NULL DEFAULT 1 CHECK (placement_epoch >= 1),
            state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'moving')),
            target_cell_id TEXT REFERENCES cells (cell_id),
            moved_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK ((state = 'moving') = (target_cell_id IS NOT NULL)),
            CHECK (target_cell_id IS NULL OR target_cell_id <> cell_id)
        )
    """)
    op.execute("CREATE INDEX tenant_placements_cell_idx ON tenant_placements (cell_id)")
    op.execute("""
        CREATE TABLE tenant_quotas (
            tenant_id UUID PRIMARY KEY REFERENCES tenants (tenant_id),
            max_concurrent_runs INTEGER CHECK (max_concurrent_runs > 0),
            max_inflight_runs INTEGER CHECK (max_inflight_runs > 0),
            requests_per_minute INTEGER CHECK (requests_per_minute > 0),
            principal_requests_per_minute INTEGER
                CHECK (principal_requests_per_minute > 0),
            sse_connections INTEGER CHECK (sse_connections > 0),
            weight INTEGER NOT NULL DEFAULT 1 CHECK (weight BETWEEN 1 AND 100),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE webhook_endpoints (
            endpoint_id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants (tenant_id),
            url TEXT NOT NULL CHECK (url ~ '^https?://[^\\s]+$' AND length(url) <= 2048),
            events TEXT[] NOT NULL DEFAULT ARRAY['run.terminal']
                CHECK (cardinality(events) BETWEEN 1 AND 16),
            enabled BOOLEAN NOT NULL DEFAULT true,
            description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 500),
            created_by UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX webhook_endpoints_tenant_idx ON webhook_endpoints (tenant_id) "
        "WHERE enabled"
    )
    op.execute("""
        CREATE TABLE webhook_signing_keys (
            key_id TEXT PRIMARY KEY CHECK (key_id ~ '^whk_[0-9a-f]{24}$'),
            tenant_id UUID NOT NULL REFERENCES tenants (tenant_id),
            secret_envelope TEXT NOT NULL CHECK (length(secret_envelope) BETWEEN 8 AND 4096),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'retiring', 'revoked')),
            retire_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK (status <> 'retiring' OR retire_at IS NOT NULL),
            CHECK ((status = 'revoked') = (revoked_at IS NOT NULL))
        )
    """)
    op.execute(
        "CREATE INDEX webhook_signing_keys_tenant_idx ON webhook_signing_keys "
        "(tenant_id, created_at DESC) WHERE status <> 'revoked'"
    )
    op.execute("""
        CREATE TABLE webhook_deliveries (
            delivery_id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants (tenant_id),
            run_id TEXT,
            endpoint_id UUID,
            event_type TEXT NOT NULL CHECK (length(event_type) BETWEEN 1 AND 64),
            url TEXT NOT NULL CHECK (length(url) <= 2048),
            body JSONB NOT NULL CHECK (jsonb_typeof(body) = 'object'),
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                'pending', 'in_flight', 'retrying', 'succeeded', 'dead', 'blocked'
            )),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 50),
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            claim_token UUID,
            claim_expires_at TIMESTAMPTZ,
            restore_epoch BIGINT NOT NULL DEFAULT 1,
            last_status_code INTEGER,
            last_error TEXT,
            last_latency_ms INTEGER,
            replay_count INTEGER NOT NULL DEFAULT 0 CHECK (replay_count >= 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            CHECK ((status = 'in_flight') = (claim_token IS NOT NULL))
        )
    """)
    op.execute("""
        CREATE INDEX webhook_deliveries_due_idx ON webhook_deliveries (next_attempt_at)
            WHERE status IN ('pending', 'retrying', 'in_flight')
    """)
    op.execute(
        "CREATE INDEX webhook_deliveries_tenant_idx ON webhook_deliveries "
        "(tenant_id, created_at DESC)"
    )
    op.execute("""
        CREATE TABLE webhook_delivery_attempts (
            delivery_id UUID NOT NULL
                REFERENCES webhook_deliveries (delivery_id) ON DELETE CASCADE,
            attempt INTEGER NOT NULL CHECK (attempt >= 1),
            tenant_id UUID NOT NULL REFERENCES tenants (tenant_id),
            outcome TEXT NOT NULL CHECK (outcome IN ('succeeded', 'failed', 'blocked')),
            status_code INTEGER,
            latency_ms INTEGER CHECK (latency_ms >= 0),
            error TEXT,
            key_ids TEXT[] NOT NULL DEFAULT '{}',
            restore_epoch BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (delivery_id, attempt)
        )
    """)
    op.execute("ALTER TABLE runs ADD COLUMN restore_epoch BIGINT NOT NULL DEFAULT 1")


def _functions() -> None:
    lock = "pg_advisory_xact_lock(hashtextextended('haas.run_admission:' || {}::text, 0))"
    op.execute(f"""
        CREATE FUNCTION haas_ops_run_admission() RETURNS trigger
        LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE
            v_state TEXT;
            v_cap INTEGER;
            v_active INTEGER;
        BEGIN
            NEW.restore_epoch := (SELECT restore_epoch FROM platform_state WHERE singleton);
            IF NEW.tenant_uuid IS NULL THEN
                RETURN NEW;
            END IF;
            PERFORM {lock.format("NEW.tenant_uuid")};
            SELECT state INTO v_state FROM tenant_placements
                WHERE tenant_id = NEW.tenant_uuid;
            IF v_state = 'moving' THEN
                RAISE EXCEPTION 'Tenant is moving between cells; retry shortly.'
                    USING ERRCODE = 'HA503';
            END IF;
            SELECT COALESCE(q.max_concurrent_runs, p.default_max_concurrent_runs)
                INTO v_cap
                FROM platform_state p
                LEFT JOIN tenant_quotas q ON q.tenant_id = NEW.tenant_uuid
                WHERE p.singleton;
            SELECT count(*) INTO v_active FROM runs
                WHERE tenant_uuid = NEW.tenant_uuid AND status NOT IN {TERMINAL};
            IF v_active >= v_cap THEN
                RAISE EXCEPTION 'Tenant concurrent run limit (%) reached.', v_cap
                    USING ERRCODE = 'HA429';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER runs_ops_admission BEFORE INSERT ON runs
        FOR EACH ROW EXECUTE FUNCTION haas_ops_run_admission()
    """)
    op.execute(f"""
        CREATE FUNCTION haas_ops_stamp_restore_epoch() RETURNS trigger
        LANGUAGE plpgsql {DEFINER} AS $$
        BEGIN
            NEW.restore_epoch := (SELECT restore_epoch FROM platform_state WHERE singleton);
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER runs_ops_lease_epoch BEFORE UPDATE OF lease_epoch ON runs
        FOR EACH ROW WHEN (NEW.lease_epoch IS DISTINCT FROM OLD.lease_epoch)
        EXECUTE FUNCTION haas_ops_stamp_restore_epoch()
    """)
    op.execute(f"""
        CREATE FUNCTION haas_ops_outbox_fairness() RETURNS trigger
        LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE
            v_cap INTEGER;
            v_weight INTEGER;
            v_running INTEGER;
        BEGIN
            IF NEW.tenant_uuid IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT COALESCE(q.max_inflight_runs, p.default_max_inflight_runs),
                   COALESCE(q.weight, 1)
                INTO v_cap, v_weight
                FROM platform_state p
                LEFT JOIN tenant_quotas q ON q.tenant_id = NEW.tenant_uuid
                WHERE p.singleton;
            SELECT count(*) INTO v_running FROM runs
                WHERE tenant_uuid = NEW.tenant_uuid AND status = 'running'
                  AND run_id <> NEW.run_id;
            IF v_running >= v_cap THEN
                NEW.next_attempt_at := greatest(
                    NEW.next_attempt_at,
                    now() + make_interval(secs => least(
                        30.0, 0.5 * (v_running - v_cap + 1)::float8 / v_weight
                    ))
                );
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER outbox_ops_fairness BEFORE INSERT ON outbox_messages
        FOR EACH ROW WHEN (NEW.destination = 'orchestrator')
        EXECUTE FUNCTION haas_ops_outbox_fairness()
    """)
    op.execute(f"""
        CREATE FUNCTION haas_ops_fence_runs(p_tenant UUID) RETURNS INTEGER
        LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE
            v_count INTEGER;
        BEGIN
            UPDATE runs SET lease_epoch = lease_epoch + 1,
                state_version = state_version + 1,
                lease_expires_at = now() - interval '1 second',
                updated_at = extract(epoch FROM clock_timestamp())
            WHERE status = 'running' AND lease_owner IS NOT NULL
              AND (p_tenant IS NULL OR tenant_uuid = p_tenant);
            GET DIAGNOSTICS v_count = ROW_COUNT;
            RETURN v_count;
        END;
        $$
    """)
    op.execute(f"""
        CREATE FUNCTION haas_ops_bump_restore_epoch(p_reason TEXT, p_actor TEXT)
        RETURNS TABLE (new_epoch BIGINT, fenced_runs INTEGER, reset_deliveries INTEGER)
        LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE
            v_old BIGINT;
            v_new BIGINT;
            v_runs INTEGER;
            v_deliveries INTEGER;
        BEGIN
            SELECT p.restore_epoch INTO v_old FROM platform_state p
                WHERE p.singleton FOR UPDATE;
            v_new := v_old + 1;
            UPDATE platform_state p SET restore_epoch = v_new, updated_at = now()
                WHERE p.singleton;
            v_runs := haas_ops_fence_runs(NULL);
            UPDATE webhook_deliveries d SET status = 'retrying', claim_token = NULL,
                claim_expires_at = NULL, next_attempt_at = now(),
                restore_epoch = v_new, updated_at = now()
            WHERE d.status = 'in_flight';
            GET DIAGNOSTICS v_deliveries = ROW_COUNT;
            INSERT INTO restore_events (previous_epoch, restore_epoch, reason, actor,
                                        fenced_runs, reset_deliveries)
            VALUES (v_old, v_new, p_reason, p_actor, v_runs, v_deliveries);
            RETURN QUERY SELECT v_new, v_runs, v_deliveries;
        END;
        $$
    """)
    op.execute(f"""
        CREATE FUNCTION haas_ops_metrics()
        RETURNS TABLE (metric TEXT, labels JSONB, value DOUBLE PRECISION)
        LANGUAGE sql STABLE {DEFINER} AS $$
            SELECT 'haas_runs', jsonb_build_object('state', r.status), count(*)::float8
                FROM runs r GROUP BY r.status
            UNION ALL
            SELECT 'haas_outbox_messages',
                   jsonb_build_object('destination', o.destination, 'state', o.state),
                   count(*)::float8
                FROM outbox_messages o WHERE o.state <> 'dispatched'
                GROUP BY o.destination, o.state
            UNION ALL
            SELECT 'haas_outbox_oldest_pending_age_seconds',
                   jsonb_build_object('destination', d.destination),
                   COALESCE((SELECT extract(epoch FROM now() - min(o.created_at))
                             FROM outbox_messages o
                             WHERE o.state = 'pending'
                               AND o.destination = d.destination), 0)::float8
                FROM (VALUES ('orchestrator'), ('tool'), ('webhook')) AS d (destination)
            UNION ALL
            SELECT 'haas_webhook_deliveries', jsonb_build_object('status', w.status),
                   count(*)::float8
                FROM webhook_deliveries w GROUP BY w.status
            UNION ALL
            SELECT 'haas_webhook_attempts_total', jsonb_build_object('outcome', a.outcome),
                   count(*)::float8
                FROM webhook_delivery_attempts a GROUP BY a.outcome
            UNION ALL
            SELECT 'haas_restore_epoch', '{{}}'::jsonb, p.restore_epoch::float8
                FROM platform_state p WHERE p.singleton
            UNION ALL
            SELECT 'haas_tenant_placements',
                   jsonb_build_object('cell', t.cell_id, 'state', t.state),
                   count(*)::float8
                FROM tenant_placements t GROUP BY t.cell_id, t.state
        $$
    """)
    op.execute(f"""
        CREATE FUNCTION haas_ops_set_quota(
            p_tenant UUID, p_max_concurrent INTEGER, p_max_inflight INTEGER,
            p_rpm INTEGER, p_principal_rpm INTEGER, p_sse INTEGER, p_weight INTEGER
        ) RETURNS tenant_quotas
        LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE
            v tenant_quotas;
        BEGIN
            INSERT INTO tenant_quotas AS q (
                tenant_id, max_concurrent_runs, max_inflight_runs, requests_per_minute,
                principal_requests_per_minute, sse_connections, weight
            ) VALUES (p_tenant, p_max_concurrent, p_max_inflight, p_rpm,
                      p_principal_rpm, p_sse, COALESCE(p_weight, 1))
            ON CONFLICT (tenant_id) DO UPDATE SET
                max_concurrent_runs = EXCLUDED.max_concurrent_runs,
                max_inflight_runs = EXCLUDED.max_inflight_runs,
                requests_per_minute = EXCLUDED.requests_per_minute,
                principal_requests_per_minute = EXCLUDED.principal_requests_per_minute,
                sse_connections = EXCLUDED.sse_connections,
                weight = EXCLUDED.weight,
                updated_at = now()
            RETURNING * INTO v;
            RETURN v;
        END;
        $$
    """)
    op.execute(f"""
        CREATE FUNCTION haas_ops_list_placements(p_cell TEXT, p_is_default BOOLEAN)
        RETURNS TABLE (tenant_id UUID, slug TEXT, cell_id TEXT, placement_epoch BIGINT,
                       state TEXT, target_cell_id TEXT, active_runs BIGINT)
        LANGUAGE sql STABLE {DEFINER} AS $$
            SELECT t.tenant_id, t.slug, COALESCE(p.cell_id, p_cell),
                   COALESCE(p.placement_epoch, 1), COALESCE(p.state, 'active'),
                   p.target_cell_id,
                   (SELECT count(*) FROM runs r WHERE r.tenant_uuid = t.tenant_id
                      AND r.status NOT IN {TERMINAL})
            FROM tenants t
            LEFT JOIN tenant_placements p ON p.tenant_id = t.tenant_id
            WHERE t.status <> 'deleted'
              AND (p.cell_id = p_cell OR p.target_cell_id = p_cell
                   OR (p.tenant_id IS NULL AND p_is_default))
            ORDER BY t.slug
        $$
    """)
    op.execute(f"""
        CREATE FUNCTION haas_ops_begin_move(
            p_tenant UUID, p_current_cell TEXT, p_target TEXT,
            p_expected_epoch BIGINT, p_force BOOLEAN
        ) RETURNS tenant_placements
        LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE
            v tenant_placements;
            v_target_status TEXT;
            v_active INTEGER;
        BEGIN
            SELECT c.status INTO v_target_status FROM cells c WHERE c.cell_id = p_target;
            IF v_target_status IS NULL THEN
                RAISE EXCEPTION 'Target cell % is not registered.', p_target
                    USING ERRCODE = 'HA404';
            END IF;
            IF v_target_status <> 'active' THEN
                RAISE EXCEPTION 'Target cell % is not accepting placements.', p_target
                    USING ERRCODE = 'HA409';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM tenants t WHERE t.tenant_id = p_tenant) THEN
                RAISE EXCEPTION 'Tenant not found.' USING ERRCODE = 'HA404';
            END IF;
            PERFORM {lock.format("p_tenant")};
            INSERT INTO tenant_placements (tenant_id, cell_id)
                VALUES (p_tenant, p_current_cell) ON CONFLICT DO NOTHING;
            SELECT * INTO v FROM tenant_placements tp
                WHERE tp.tenant_id = p_tenant FOR UPDATE;
            IF v.placement_epoch <> p_expected_epoch THEN
                RAISE EXCEPTION 'Placement epoch mismatch (current %).', v.placement_epoch
                    USING ERRCODE = 'HA409';
            END IF;
            IF v.state <> 'active' THEN
                RAISE EXCEPTION 'Tenant is already moving.' USING ERRCODE = 'HA409';
            END IF;
            IF v.cell_id = p_target THEN
                RAISE EXCEPTION 'Tenant is already homed in cell %.', p_target
                    USING ERRCODE = 'HA409';
            END IF;
            SELECT count(*) INTO v_active FROM runs r
                WHERE r.tenant_uuid = p_tenant AND r.status NOT IN {TERMINAL};
            IF v_active > 0 AND NOT p_force THEN
                RAISE EXCEPTION 'Tenant has % active runs; drain them or force the move.',
                    v_active USING ERRCODE = 'HA409';
            END IF;
            UPDATE tenant_placements tp SET state = 'moving', target_cell_id = p_target,
                placement_epoch = tp.placement_epoch + 1, updated_at = now()
            WHERE tp.tenant_id = p_tenant RETURNING * INTO v;
            IF p_force THEN
                PERFORM haas_ops_fence_runs(p_tenant);
            END IF;
            RETURN v;
        END;
        $$
    """)
    op.execute(f"""
        CREATE FUNCTION haas_ops_finish_move(
            p_tenant UUID, p_expected_epoch BIGINT, p_commit BOOLEAN
        ) RETURNS tenant_placements
        LANGUAGE plpgsql {DEFINER} AS $$
        DECLARE
            v tenant_placements;
        BEGIN
            SELECT * INTO v FROM tenant_placements tp
                WHERE tp.tenant_id = p_tenant FOR UPDATE;
            IF NOT FOUND OR v.state <> 'moving' THEN
                RAISE EXCEPTION 'Tenant is not moving.' USING ERRCODE = 'HA409';
            END IF;
            IF v.placement_epoch <> p_expected_epoch THEN
                RAISE EXCEPTION 'Placement epoch mismatch (current %).', v.placement_epoch
                    USING ERRCODE = 'HA409';
            END IF;
            IF p_commit AND EXISTS (
                SELECT 1 FROM runs r WHERE r.tenant_uuid = p_tenant
                  AND r.status = 'running' AND r.lease_expires_at > now()
            ) THEN
                RAISE EXCEPTION 'In-flight leases remain in the source cell.'
                    USING ERRCODE = 'HA409';
            END IF;
            UPDATE tenant_placements tp SET
                cell_id = CASE WHEN p_commit THEN tp.target_cell_id ELSE tp.cell_id END,
                moved_at = CASE WHEN p_commit THEN now() ELSE tp.moved_at END,
                state = 'active', target_cell_id = NULL,
                placement_epoch = tp.placement_epoch + 1, updated_at = now()
            WHERE tp.tenant_id = p_tenant RETURNING * INTO v;
            RETURN v;
        END;
        $$
    """)


def upgrade() -> None:
    _tables()
    _functions()
    for table in TENANT_TABLES:
        _rls(table)
    for statement in (
        "GRANT SELECT ON platform_state, restore_events TO haas_tenant_app, "
        "haas_execution_service",
        "GRANT SELECT, INSERT, UPDATE ON cells TO haas_tenant_app",
        "GRANT SELECT ON cells TO haas_execution_service",
        "GRANT SELECT ON tenant_quotas, tenant_placements "
        "TO haas_tenant_app, haas_execution_service",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON webhook_endpoints TO haas_tenant_app",
        "GRANT SELECT ON webhook_endpoints TO haas_execution_service",
        "GRANT SELECT, INSERT, UPDATE ON webhook_signing_keys, webhook_deliveries "
        "TO haas_tenant_app, haas_execution_service",
        "GRANT SELECT, INSERT ON webhook_delivery_attempts "
        "TO haas_tenant_app, haas_execution_service",
    ):
        op.execute(statement)
    for function in FUNCTIONS[3:]:
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
        op.execute(
            f"GRANT EXECUTE ON FUNCTION {function} "
            "TO haas_tenant_app, haas_execution_service"
        )


def downgrade() -> None:
    op.execute("DROP TRIGGER outbox_ops_fairness ON outbox_messages")
    op.execute("DROP TRIGGER runs_ops_lease_epoch ON runs")
    op.execute("DROP TRIGGER runs_ops_admission ON runs")
    for function in reversed(FUNCTIONS):
        op.execute(f"DROP FUNCTION {function}")
    op.execute("ALTER TABLE runs DROP COLUMN restore_epoch")
    for table in reversed((*PLATFORM_TABLES, *TENANT_TABLES)):
        op.execute(f"DROP TABLE {table}")
