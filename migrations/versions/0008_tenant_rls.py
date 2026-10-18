"""Force tenant policies using separate application and execution service roles.

Revision ID: 0008_tenant_rls
Revises: 0007_threads_agents
"""

from alembic import op

revision = "0008_tenant_rls"
down_revision = "0007_threads_agents"
branch_labels = None
depends_on = None

TENANT_TABLES = {
    "tenants": "tenant_id",
    "memberships": "tenant_id",
    "tenant_api_keys": "tenant_id",
    "tenant_policy_revisions": "tenant_id",
    "audit_events": "tenant_id",
    "agent_revisions": "tenant_id",
    "threads": "tenant_uuid",
    "runs": "tenant_uuid",
}


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'haas_tenant_app') THEN
                CREATE ROLE haas_tenant_app NOLOGIN NOSUPERUSER NOBYPASSRLS;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'haas_execution_service') THEN
                CREATE ROLE haas_execution_service NOLOGIN NOSUPERUSER NOBYPASSRLS;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles
                WHERE rolname IN ('haas_tenant_app','haas_execution_service')
                  AND (rolsuper OR rolbypassrls OR rolcanlogin)) THEN
                RAISE EXCEPTION 'runtime group roles must be NOLOGIN NOSUPERUSER NOBYPASSRLS';
            END IF;
        END $$;
    """)
    for statement in (
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
        "GRANT USAGE ON SCHEMA public TO haas_tenant_app, haas_execution_service",
        "GRANT SELECT ON tenants, principals TO haas_tenant_app",
        "GRANT INSERT ON principals TO haas_tenant_app",
        "GRANT UPDATE (subject) ON principals TO haas_tenant_app",
        "GRANT UPDATE (active_policy_revision_id, retention_class, updated_at) ON tenants TO haas_tenant_app",
        "GRANT SELECT, INSERT, UPDATE ON memberships, tenant_api_keys, tenant_policy_revisions, agent_revisions, runs, threads TO haas_tenant_app",
        "GRANT SELECT, INSERT ON audit_events TO haas_tenant_app",
        "GRANT SELECT, UPDATE ON runs, threads TO haas_execution_service",
        "GRANT SELECT ON tenants, principals, memberships, tenant_policy_revisions, agent_revisions TO haas_execution_service",
        "GRANT SELECT, INSERT ON audit_events TO haas_execution_service",
    ):
        op.execute(statement)
    op.execute("""
        CREATE OR REPLACE FUNCTION resolve_haas_tenant_uuid(legacy_id TEXT)
        RETURNS UUID LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
            SELECT COALESCE(
                (SELECT tenant_uuid FROM public.legacy_tenant_mappings
                    WHERE legacy_tenant_id = legacy_id),
                (SELECT tenant_id FROM public.tenants WHERE tenant_id::text = legacy_id),
                (SELECT tenant_id FROM public.tenants WHERE slug = legacy_id)
            )
        $$;
    """)
    op.execute("REVOKE ALL ON FUNCTION resolve_haas_tenant_uuid(TEXT) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION resolve_haas_tenant_uuid(TEXT) TO haas_tenant_app, haas_execution_service"
    )
    for table, key in TENANT_TABLES.items():
        legacy = (
            " OR (tenant_uuid IS NULL "
            "AND current_setting('haas.legacy_dev', true) = 'on' "
            "AND tenant_id = current_setting('haas.tenant', true))"
            if table in {"runs", "threads"}
            else ""
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_boundary ON {table} TO haas_tenant_app
                USING ({key} = resolve_haas_tenant_uuid(
                    current_setting('haas.tenant', true)){legacy})
                WITH CHECK ({key} = resolve_haas_tenant_uuid(
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


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP POLICY schema_administrator ON {table}")
        op.execute(f"DROP POLICY execution_service ON {table}")
        op.execute(f"DROP POLICY tenant_boundary ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER FUNCTION resolve_haas_tenant_uuid(TEXT) SECURITY INVOKER")
    op.execute("GRANT EXECUTE ON FUNCTION resolve_haas_tenant_uuid(TEXT) TO PUBLIC")
    op.execute(
        "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM haas_tenant_app, haas_execution_service"
    )
