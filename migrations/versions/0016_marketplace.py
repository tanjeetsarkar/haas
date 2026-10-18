"""Marketplace publishers, listings, revisions, reviews, installs (Track M, owner MKT).

Catalog tables (publishers, keys, listings, revisions, artifacts, transparency log,
abuse reports) are global: they are shared across tenants and private-listing visibility
is enforced by the store. Tenant-owned rows (installations, notifications) are under the
standard ``tenant_boundary`` row-level security policy. The transparency log is
append-only and hash-chained by trigger.

Revision ID: 0016_marketplace
Revises: 0015_service_operations
"""

from alembic import op

revision = "0016_marketplace"
down_revision = "0015_service_operations"
branch_labels = None
depends_on = None

SHA256 = "'^[0-9a-f]{64}$'"
NAME = "'^[a-z0-9]+(-[a-z0-9]+)*$'"
TIERS = "('platform', 'verified', 'community', 'private', 'unreviewed')"
TENANT_TABLES = ("marketplace_installations", "marketplace_notifications")
GLOBAL_TABLES = (
    "marketplace_abuse_reports",
    "marketplace_revisions",
    "marketplace_transparency_log",
    "marketplace_artifacts",
    "marketplace_listings",
    "marketplace_publisher_keys",
    "marketplace_publishers",
)


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_boundary ON {table} TO haas_tenant_app
            USING (tenant_id = resolve_haas_tenant_uuid(current_setting('haas.tenant', true)))
            WITH CHECK (tenant_id = resolve_haas_tenant_uuid(current_setting('haas.tenant', true)));
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
    op.execute(f"""
        CREATE TABLE marketplace_publishers (
            publisher_id UUID PRIMARY KEY,
            namespace TEXT NOT NULL UNIQUE CHECK (namespace ~ {NAME} AND length(namespace) <= 64),
            display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 128),
            owner_tenant_id UUID NOT NULL REFERENCES tenants(tenant_id),
            created_by UUID,
            verification TEXT NOT NULL DEFAULT 'unverified'
                CHECK (verification IN ('unverified', 'verified', 'official')),
            domain TEXT CHECK (domain IS NULL OR domain ~ '^[a-z0-9.-]+[.][a-z]{{2,}}$'),
            verification_token TEXT,
            verified_at TIMESTAMPTZ,
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
            status_reason TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE marketplace_publisher_keys (
            key_id TEXT PRIMARY KEY CHECK (key_id ~ '^[0-9a-f]{32}$'),
            publisher_id UUID NOT NULL REFERENCES marketplace_publishers(publisher_id),
            public_key BYTEA NOT NULL UNIQUE CHECK (length(public_key) = 32),
            label TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            revoked_at TIMESTAMPTZ,
            revoked_reason TEXT
        )
    """)
    op.execute(f"""
        CREATE TABLE marketplace_listings (
            listing_id UUID PRIMARY KEY,
            plugin_id TEXT NOT NULL UNIQUE,
            publisher_id UUID NOT NULL REFERENCES marketplace_publishers(publisher_id),
            name TEXT NOT NULL CHECK (name ~ {NAME}),
            display_name TEXT NOT NULL,
            summary TEXT NOT NULL,
            categories TEXT[] NOT NULL DEFAULT '{{}}',
            keywords TEXT[] NOT NULL DEFAULT '{{}}',
            visibility TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public', 'private')),
            owner_tenant_id UUID NOT NULL REFERENCES tenants(tenant_id),
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'taken_down')),
            status_reason TEXT,
            install_count BIGINT NOT NULL DEFAULT 0,
            search_document TSVECTOR,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (publisher_id, name)
        )
    """)
    op.execute(
        "CREATE INDEX marketplace_listings_search ON marketplace_listings USING GIN (search_document)"
    )
    op.execute(
        "CREATE INDEX marketplace_listings_categories ON marketplace_listings USING GIN (categories)"
    )
    op.execute(f"""
        CREATE TABLE marketplace_artifacts (
            digest TEXT PRIMARY KEY CHECK (digest ~ {SHA256}),
            size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
            content BYTEA NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK (length(content) = size_bytes)
        )
    """)
    op.execute("""
        CREATE TABLE marketplace_transparency_log (
            seq BIGINT PRIMARY KEY,
            entry TEXT NOT NULL,
            leaf_hash BYTEA NOT NULL CHECK (length(leaf_hash) = 32),
            chain_hash BYTEA NOT NULL CHECK (length(chain_hash) = 32),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE FUNCTION marketplace_tlog_append() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        DECLARE
            previous BYTEA;
            last_seq BIGINT;
        BEGIN
            PERFORM pg_advisory_xact_lock(hashtext('marketplace_transparency_log'));
            SELECT seq, chain_hash INTO last_seq, previous
              FROM marketplace_transparency_log ORDER BY seq DESC LIMIT 1;
            NEW.seq := COALESCE(last_seq, 0) + 1;
            IF NEW.leaf_hash <> sha256(decode('00', 'hex') || convert_to(NEW.entry, 'UTF8')) THEN
                RAISE EXCEPTION 'transparency log leaf hash does not match entry';
            END IF;
            NEW.chain_hash := sha256(
                COALESCE(previous, decode(repeat('00', 32), 'hex')) || NEW.leaf_hash
            );
            NEW.created_at := now();
            RETURN NEW;
        END;
        $$;
    """)
    op.execute("""
        CREATE TRIGGER marketplace_tlog_append BEFORE INSERT ON marketplace_transparency_log
        FOR EACH ROW EXECUTE FUNCTION marketplace_tlog_append()
    """)
    op.execute("""
        CREATE FUNCTION marketplace_tlog_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'marketplace transparency log is append-only';
        END;
        $$;
    """)
    op.execute("""
        CREATE TRIGGER marketplace_tlog_immutable
        BEFORE UPDATE OR DELETE ON marketplace_transparency_log
        FOR EACH ROW EXECUTE FUNCTION marketplace_tlog_immutable()
    """)
    op.execute(f"""
        CREATE TABLE marketplace_revisions (
            revision_id UUID PRIMARY KEY,
            listing_id UUID NOT NULL REFERENCES marketplace_listings(listing_id),
            version TEXT NOT NULL,
            major INTEGER NOT NULL,
            minor INTEGER NOT NULL,
            patch INTEGER NOT NULL,
            prerelease TEXT,
            digest TEXT NOT NULL CHECK (digest ~ {SHA256}),
            manifest JSONB NOT NULL,
            manifest_raw JSONB NOT NULL,
            manifest_digest TEXT NOT NULL CHECK (manifest_digest ~ {SHA256}),
            source_manifest_version INTEGER NOT NULL CHECK (source_manifest_version IN (1, 2)),
            permissions JSONB NOT NULL,
            signature JSONB NOT NULL,
            signing_key_id TEXT NOT NULL REFERENCES marketplace_publisher_keys(key_id),
            provenance JSONB,
            tier TEXT NOT NULL CHECK (tier IN {TIERS}),
            review_state TEXT NOT NULL DEFAULT 'submitted'
                CHECK (review_state IN ('submitted', 'approved', 'rejected')),
            review_report JSONB NOT NULL,
            reviewed_by UUID,
            reviewed_at TIMESTAMPTZ,
            review_notes TEXT,
            tlog_seq BIGINT NOT NULL REFERENCES marketplace_transparency_log(seq),
            yanked_at TIMESTAMPTZ,
            yank_reason TEXT,
            published_by UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (listing_id, version)
        )
    """)
    op.execute(
        "CREATE INDEX marketplace_revisions_review ON marketplace_revisions (review_state, created_at)"
    )
    op.execute("CREATE INDEX marketplace_revisions_digest ON marketplace_revisions (digest)")
    op.execute(f"""
        CREATE TABLE marketplace_installations (
            installation_id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants(tenant_id),
            listing_id UUID NOT NULL REFERENCES marketplace_listings(listing_id),
            revision_id UUID NOT NULL REFERENCES marketplace_revisions(revision_id),
            plugin_id TEXT NOT NULL,
            version TEXT NOT NULL,
            digest TEXT NOT NULL CHECK (digest ~ {SHA256}),
            consent JSONB NOT NULL,
            consent_digest TEXT NOT NULL CHECK (consent_digest ~ {SHA256}),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'uninstalled', 'revoked')),
            status_reason TEXT,
            installed_by UUID,
            installed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX marketplace_installations_active
        ON marketplace_installations (tenant_id, listing_id) WHERE status = 'active'
    """)
    op.execute("""
        CREATE TABLE marketplace_notifications (
            notification_id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants(tenant_id),
            kind TEXT NOT NULL CHECK (kind IN ('yanked', 'taken_down', 'publisher_suspended',
                                               'key_revoked')),
            plugin_id TEXT NOT NULL,
            version TEXT,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            acknowledged_at TIMESTAMPTZ
        )
    """)
    op.execute(
        "CREATE INDEX marketplace_notifications_open ON marketplace_notifications "
        "(tenant_id, created_at) WHERE acknowledged_at IS NULL"
    )
    op.execute("""
        CREATE TABLE marketplace_abuse_reports (
            report_id UUID PRIMARY KEY,
            listing_id UUID NOT NULL REFERENCES marketplace_listings(listing_id),
            version TEXT,
            reporter_tenant_id UUID REFERENCES tenants(tenant_id),
            reporter_principal_id UUID,
            reason TEXT NOT NULL CHECK (reason IN ('malware', 'data_exfiltration', 'impersonation',
                                                   'prompt_injection', 'policy', 'other')),
            details TEXT NOT NULL CHECK (length(details) <= 4000),
            status TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'actioned', 'dismissed')),
            resolution TEXT,
            resolved_by UUID,
            resolved_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX marketplace_abuse_reports_open ON marketplace_abuse_reports (status, created_at)"
    )
    # Cross-tenant fan-out for yank/takedown/suspension: notify every tenant that has an
    # active install of the affected revisions and optionally revoke those installs.
    op.execute("""
        CREATE FUNCTION haas_marketplace_fanout(
            revision_ids UUID[], notification_kind TEXT, details JSONB, revoke BOOLEAN
        ) RETURNS INTEGER
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE
            affected INTEGER;
        BEGIN
            INSERT INTO marketplace_notifications (
                notification_id, tenant_id, kind, plugin_id, version, details
            )
            SELECT gen_random_uuid(), i.tenant_id, notification_kind, i.plugin_id, i.version,
                   details || jsonb_build_object('installation_id', i.installation_id,
                                                 'digest', i.digest,
                                                 'revoked', revoke)
            FROM marketplace_installations i
            WHERE i.revision_id = ANY(revision_ids) AND i.status = 'active';
            GET DIAGNOSTICS affected = ROW_COUNT;
            IF revoke THEN
                UPDATE marketplace_installations
                   SET status = 'revoked', status_reason = notification_kind, updated_at = now()
                 WHERE revision_id = ANY(revision_ids) AND status = 'active';
            END IF;
            RETURN affected;
        END;
        $$;
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION haas_marketplace_fanout(UUID[], TEXT, JSONB, BOOLEAN) FROM PUBLIC"
    )

    for table in TENANT_TABLES:
        _rls(table)

    for statement in (
        "GRANT EXECUTE ON FUNCTION haas_marketplace_fanout(UUID[], TEXT, JSONB, BOOLEAN) "
        "TO haas_tenant_app",
        "GRANT SELECT, INSERT, UPDATE ON marketplace_publishers, marketplace_publisher_keys, "
        "marketplace_listings, marketplace_revisions, marketplace_abuse_reports TO haas_tenant_app",
        "GRANT SELECT, INSERT ON marketplace_artifacts, marketplace_transparency_log "
        "TO haas_tenant_app",
        "GRANT SELECT, INSERT, UPDATE ON marketplace_installations, marketplace_notifications "
        "TO haas_tenant_app",
        "GRANT SELECT ON marketplace_publishers, marketplace_publisher_keys, marketplace_listings, "
        "marketplace_revisions, marketplace_artifacts, marketplace_transparency_log, "
        "marketplace_installations TO haas_execution_service",
    ):
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS haas_marketplace_fanout(UUID[], TEXT, JSONB, BOOLEAN)")
    for table in (*TENANT_TABLES, *GLOBAL_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DROP FUNCTION IF EXISTS marketplace_tlog_append()")
    op.execute("DROP FUNCTION IF EXISTS marketplace_tlog_immutable()")
