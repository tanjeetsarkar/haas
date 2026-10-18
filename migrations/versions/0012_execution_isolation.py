"""Plugin artifacts, sandbox sessions and static catalog pins (P4, owner EXEC).

Sandbox sessions track the per-run execution sandbox lifecycle so the
maintenance service can reap expired sessions. Plugin artifact pins bind a run
to the content-addressed artifact digest it was qualified against, so a later
artifact replacement cannot silently change what an in-flight run executes.

Both are run-child tables: keyed by the tenant alias and run id, with
``tenant_uuid`` copied from the parent run by ``sync_haas_run_child_tenant``
and forced tenant RLS.

Revision ID: 0012_execution_isolation
Revises: 0011_multi_agent_schema
"""

from alembic import op

revision = "0012_execution_isolation"
down_revision = "0011_multi_agent_schema"
branch_labels = None
depends_on = None

SHA256 = "'^[0-9a-f]{64}$'"

NEW_TABLES = ("sandbox_sessions", "plugin_artifact_pins")

LEGACY = (
    " OR (tenant_uuid IS NULL "
    "AND current_setting('haas.legacy_dev', true) = 'on' "
    "AND tenant_id = current_setting('haas.tenant', true))"
)


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_boundary ON {table} TO haas_tenant_app
            USING (tenant_uuid = resolve_haas_tenant_uuid(
                current_setting('haas.tenant', true)){LEGACY})
            WITH CHECK (tenant_uuid = resolve_haas_tenant_uuid(
                current_setting('haas.tenant', true)){LEGACY});
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


def upgrade() -> None:
    op.execute("""
        CREATE TABLE sandbox_sessions (
            session_id UUID PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            tenant_uuid UUID REFERENCES tenants (tenant_id),
            run_id TEXT NOT NULL,
            cell TEXT NOT NULL CHECK (length(cell) BETWEEN 1 AND 128),
            runtime TEXT NOT NULL CHECK (length(runtime) BETWEEN 1 AND 64),
            status TEXT NOT NULL CHECK (status IN (
                'pending', 'running', 'stopped', 'terminated', 'failed'
            )),
            pid INTEGER CHECK (pid IS NULL OR pid > 0),
            cpu_quota INTEGER CHECK (cpu_quota IS NULL OR cpu_quota > 0),
            memory_limit_mb INTEGER CHECK (memory_limit_mb IS NULL OR memory_limit_mb > 0),
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            stopped_at TIMESTAMPTZ,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb
                CHECK (jsonb_typeof(metadata) = 'object'),
            FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, run_id),
            CHECK (expires_at > started_at)
        )
    """)
    op.execute(
        "CREATE INDEX sandbox_sessions_run_idx "
        "ON sandbox_sessions (tenant_id, run_id, started_at DESC)"
    )
    op.execute(
        "CREATE INDEX sandbox_sessions_expiry_idx ON sandbox_sessions (expires_at) "
        "WHERE expires_at IS NOT NULL"
    )

    op.execute(f"""
        CREATE TABLE plugin_artifact_pins (
            pin_id UUID PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            tenant_uuid UUID REFERENCES tenants (tenant_id),
            run_id TEXT NOT NULL,
            plugin_id TEXT NOT NULL CHECK (length(plugin_id) BETWEEN 1 AND 255),
            version TEXT NOT NULL CHECK (length(version) BETWEEN 1 AND 128),
            digest TEXT NOT NULL CHECK (digest ~ {SHA256}),
            pinned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, run_id),
            UNIQUE (tenant_id, run_id, plugin_id)
        )
    """)
    op.execute(
        "CREATE INDEX plugin_artifact_pins_run_idx "
        "ON plugin_artifact_pins (tenant_id, run_id)"
    )
    op.execute(
        "CREATE INDEX plugin_artifact_pins_digest_idx "
        "ON plugin_artifact_pins (digest)"
    )

    for table in NEW_TABLES:
        op.execute(f"""
            CREATE TRIGGER {table}_tenant_sync BEFORE INSERT OR UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION sync_haas_run_child_tenant()
        """)
        _rls(table)

    for statement in (
        "GRANT SELECT, INSERT ON sandbox_sessions, plugin_artifact_pins "
        "TO haas_tenant_app",
        "GRANT UPDATE (status, pid, stopped_at, metadata) ON sandbox_sessions "
        "TO haas_tenant_app",
        "GRANT SELECT ON sandbox_sessions, plugin_artifact_pins "
        "TO haas_execution_service",
        "GRANT UPDATE (status, stopped_at) ON sandbox_sessions "
        "TO haas_execution_service",
        "GRANT DELETE ON sandbox_sessions, plugin_artifact_pins "
        "TO haas_execution_service",
    ):
        op.execute(statement)


def downgrade() -> None:
    for table in NEW_TABLES:
        op.execute(f"DROP POLICY schema_administrator ON {table}")
        op.execute(f"DROP POLICY execution_service ON {table}")
        op.execute(f"DROP POLICY tenant_boundary ON {table}")
        op.execute(f"DROP TRIGGER {table}_tenant_sync ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    for table in reversed(NEW_TABLES):
        op.execute(f"DROP TABLE {table}")
