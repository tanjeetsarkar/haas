"""Add tenant, principal, membership and API-key registry tables.

Revision ID: 0002_identity_registry
Revises: 0001_initial_schema
"""

from alembic import op

revision = "0002_identity_registry"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE tenants (
            tenant_id UUID PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE
                CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
            status TEXT NOT NULL DEFAULT 'provisioning'
                CHECK (status IN ('provisioning', 'active', 'suspended',
                                  'offboarding', 'deleted')),
            region TEXT NOT NULL DEFAULT 'default',
            revocation_epoch BIGINT NOT NULL DEFAULT 0 CHECK (revocation_epoch >= 0),
            credential_valid_after TIMESTAMPTZ,
            retention_class TEXT NOT NULL DEFAULT 'standard',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE principals (
            principal_id UUID PRIMARY KEY,
            issuer TEXT NOT NULL,
            subject TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('human', 'service', 'platform')),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'suspended', 'revoked')),
            credential_valid_after TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (issuer, subject)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE memberships (
            tenant_id UUID NOT NULL REFERENCES tenants (tenant_id),
            principal_id UUID NOT NULL REFERENCES principals (principal_id),
            role TEXT NOT NULL CHECK (
                role IN ('tenant_admin', 'submitter', 'approver', 'auditor',
                         'operator', 'break_glass')
            ),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'suspended', 'revoked')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, principal_id, role)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE tenant_api_keys (
            key_id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants (tenant_id),
            principal_id UUID NOT NULL REFERENCES principals (principal_id),
            key_prefix TEXT NOT NULL UNIQUE,
            key_hash BYTEA NOT NULL UNIQUE,
            name TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            last_used_at TIMESTAMPTZ,
            CHECK (expires_at IS NULL OR expires_at > created_at)
        )
        """
    )
    op.execute(
        "CREATE INDEX memberships_principal_status_idx "
        "ON memberships (principal_id, status)"
    )
    op.execute(
        "CREATE INDEX memberships_tenant_status_idx ON memberships (tenant_id, status)"
    )
    op.execute(
        "CREATE INDEX tenant_api_keys_principal_idx "
        "ON tenant_api_keys (principal_id, tenant_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE tenant_api_keys")
    op.execute("DROP TABLE memberships")
    op.execute("DROP TABLE principals")
    op.execute("DROP TABLE tenants")
