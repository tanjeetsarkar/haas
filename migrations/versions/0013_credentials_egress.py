"""Secret bindings, credential grants and tenant egress policy (P4, owner SEC).

Secret values are stored only as AES-GCM ciphertext (``inline``) or as a reference to a
deployment-provided environment variable (``env``). Credential grants record the
single-use, audience-bound hand-off of one secret to one tool invocation; the
sealed value itself lives in Redis for the grant TTL and never in PostgreSQL.

Revision ID: 0013_credentials_egress
Revises: 0012_execution_isolation
"""

from alembic import op

revision = "0013_credentials_egress"
down_revision = "0012_execution_isolation"
branch_labels = None
depends_on = None

NEW_TABLES = ("secret_bindings", "credential_grants", "egress_policies")


def _rls(table: str, key: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_boundary ON {table} TO haas_tenant_app
            USING ({key} = resolve_haas_tenant_uuid(
                current_setting('haas.tenant', true)))
            WITH CHECK ({key} = resolve_haas_tenant_uuid(
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


def upgrade() -> None:
    op.execute("""
        CREATE TABLE secret_bindings (
            binding_id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants (tenant_id),
            name TEXT NOT NULL CHECK (name ~ '^[A-Za-z_][A-Za-z0-9_]{0,127}$'),
            provider TEXT NOT NULL CHECK (provider IN ('inline', 'env')),
            provider_reference TEXT,
            encrypted_value TEXT,
            version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
            allowed_plugins TEXT[] NOT NULL DEFAULT '{}',
            allowed_tools TEXT[] NOT NULL DEFAULT '{}',
            description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 500),
            created_by UUID REFERENCES principals (principal_id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            rotated_at TIMESTAMPTZ,
            deleted_at TIMESTAMPTZ,
            CONSTRAINT secret_bindings_provider_shape CHECK (
                (provider = 'inline' AND encrypted_value IS NOT NULL
                    AND provider_reference IS NULL)
                OR (provider = 'env' AND provider_reference IS NOT NULL
                    AND encrypted_value IS NULL)
                OR (deleted_at IS NOT NULL AND encrypted_value IS NULL)
            )
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX secret_bindings_active_name
            ON secret_bindings (tenant_id, name) WHERE deleted_at IS NULL
    """)
    op.execute("""
        CREATE TABLE credential_grants (
            grant_id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants (tenant_id),
            run_id TEXT NOT NULL,
            invocation_id TEXT NOT NULL,
            secret_name TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('binding', 'run')),
            binding_id UUID REFERENCES secret_bindings (binding_id),
            binding_version INTEGER,
            audience TEXT NOT NULL,
            plugin_digest TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            redeemed_at TIMESTAMPTZ,
            denied_reason TEXT,
            CHECK (expires_at > created_at)
        )
    """)
    op.execute(
        "CREATE INDEX credential_grants_run ON credential_grants "
        "(tenant_id, run_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX credential_grants_recent ON credential_grants "
        "(tenant_id, created_at DESC, grant_id DESC)"
    )
    op.execute("""
        CREATE TABLE egress_policies (
            tenant_id UUID PRIMARY KEY REFERENCES tenants (tenant_id),
            allow_hosts TEXT[] NOT NULL DEFAULT '{}',
            allow_cidrs TEXT[] NOT NULL DEFAULT '{}',
            max_bytes_per_run BIGINT CHECK (max_bytes_per_run IS NULL OR max_bytes_per_run > 0),
            version INTEGER NOT NULL DEFAULT 1,
            updated_by UUID REFERENCES principals (principal_id),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    for table in NEW_TABLES:
        _rls(table, "tenant_id")

    for statement in (
        "GRANT SELECT, INSERT ON secret_bindings, egress_policies TO haas_tenant_app",
        "GRANT UPDATE (provider, provider_reference, encrypted_value, version, "
        "allowed_plugins, allowed_tools, description, rotated_at, deleted_at) "
        "ON secret_bindings TO haas_tenant_app",
        "GRANT UPDATE (allow_hosts, allow_cidrs, max_bytes_per_run, version, "
        "updated_by, updated_at) ON egress_policies TO haas_tenant_app",
        "GRANT SELECT ON secret_bindings, egress_policies TO haas_execution_service",
        "GRANT SELECT, INSERT ON credential_grants "
        "TO haas_tenant_app, haas_execution_service",
        "GRANT UPDATE (redeemed_at, denied_reason) ON credential_grants "
        "TO haas_execution_service",
        "GRANT DELETE ON credential_grants TO haas_execution_service",
    ):
        op.execute(statement)


def downgrade() -> None:
    for table in ("credential_grants", "egress_policies", "secret_bindings"):
        op.execute(f"DROP TABLE {table}")
