"""Artifact service, retention classes, tombstones, legal holds (P4/P5, owner DATA).

Purges run through one privileged path: only ``haas_execution_service`` holds
DELETE on ``run_events``/``artifacts`` and every delete must run inside a
transaction that set ``haas.retention_purge = 'on'``. Ordinary sessions still
hit the immutability trigger, and tombstones themselves are append-only.

Revision ID: 0014_artifacts_retention
Revises: 0013_credentials_egress
"""

from alembic import op

revision = "0014_artifacts_retention"
down_revision = "0013_credentials_egress"
branch_labels = None
depends_on = None

SHA256 = "'^[0-9a-f]{64}$'"
NEW_TABLES = (
    "artifacts",
    "retention_policies",
    "legal_holds",
    "erasure_requests",
    "retention_tombstones",
)
LEGACY = (
    " OR (tenant_uuid IS NULL "
    "AND current_setting('haas.legacy_dev', true) = 'on' "
    "AND tenant_id = current_setting('haas.tenant', true))"
)


def _rls(table: str, legacy: str = "") -> None:
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


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION haas_retention_purge_or_reject()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE'
               AND current_setting('haas.retention_purge', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME;
        END;
        $$;
    """)
    op.execute("DROP TRIGGER run_events_immutable ON run_events")
    op.execute("""
        CREATE TRIGGER run_events_immutable BEFORE UPDATE OR DELETE ON run_events
        FOR EACH ROW EXECUTE FUNCTION haas_retention_purge_or_reject()
    """)
    op.execute("GRANT DELETE ON run_events TO haas_execution_service")

    op.execute(f"""
        CREATE TABLE artifacts (
            artifact_id UUID PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            tenant_uuid UUID REFERENCES tenants (tenant_id),
            run_id TEXT NOT NULL,
            name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 255),
            content_type TEXT NOT NULL CHECK (length(content_type) BETWEEN 1 AND 255),
            size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
            sha256 TEXT NOT NULL CHECK (sha256 ~ {SHA256}),
            object_key TEXT NOT NULL CHECK (length(object_key) BETWEEN 1 AND 512),
            source TEXT NOT NULL CHECK (source IN ('api', 'tool')),
            created_by TEXT CHECK (created_by IS NULL OR length(created_by) <= 256),
            metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb
                CHECK (jsonb_typeof(metadata) = 'object'),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ,
            deleted_at TIMESTAMPTZ,
            FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, run_id)
        )
    """)
    op.execute(
        "CREATE INDEX artifacts_run_idx ON artifacts (tenant_id, run_id, created_at)"
    )
    op.execute("CREATE INDEX artifacts_object_idx ON artifacts (object_key)")
    op.execute(
        "CREATE INDEX artifacts_expiry_idx ON artifacts (expires_at) "
        "WHERE expires_at IS NOT NULL"
    )
    op.execute("""
        CREATE TRIGGER artifacts_tenant_sync BEFORE INSERT OR UPDATE ON artifacts
        FOR EACH ROW EXECUTE FUNCTION sync_haas_run_child_tenant()
    """)
    op.execute("""
        CREATE FUNCTION guard_haas_artifact_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF current_setting('haas.retention_purge', true) = 'on' THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'artifacts are deleted only by retention purge';
            END IF;
            IF (NEW.artifact_id, NEW.name, NEW.content_type, NEW.size_bytes,
                NEW.sha256, NEW.object_key, NEW.source, NEW.created_by,
                NEW.metadata, NEW.created_at, NEW.expires_at)
               IS DISTINCT FROM
               (OLD.artifact_id, OLD.name, OLD.content_type, OLD.size_bytes,
                OLD.sha256, OLD.object_key, OLD.source, OLD.created_by,
                OLD.metadata, OLD.created_at, OLD.expires_at)
               OR (OLD.deleted_at IS NOT NULL
                   AND NEW.deleted_at IS DISTINCT FROM OLD.deleted_at) THEN
                RAISE EXCEPTION 'artifact content is immutable';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)
    op.execute("""
        CREATE TRIGGER artifacts_guard BEFORE UPDATE OR DELETE ON artifacts
        FOR EACH ROW EXECUTE FUNCTION guard_haas_artifact_change()
    """)

    op.execute("""
        CREATE TABLE retention_policies (
            tenant_uuid UUID PRIMARY KEY REFERENCES tenants (tenant_id),
            run_events_ttl_days INTEGER CHECK (run_events_ttl_days >= 0),
            artifacts_ttl_days INTEGER CHECK (artifacts_ttl_days >= 0),
            messages_ttl_days INTEGER CHECK (messages_ttl_days >= 0),
            artifact_quota_bytes BIGINT CHECK (artifact_quota_bytes >= 0),
            updated_by UUID REFERENCES principals (principal_id),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE legal_holds (
            hold_id UUID PRIMARY KEY,
            tenant_uuid UUID NOT NULL REFERENCES tenants (tenant_id),
            scope TEXT NOT NULL CHECK (scope IN ('tenant', 'run', 'thread')),
            subject_id TEXT CHECK (subject_id IS NULL OR length(subject_id) BETWEEN 1 AND 255),
            reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 1000),
            placed_by UUID REFERENCES principals (principal_id),
            placed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            released_by UUID REFERENCES principals (principal_id),
            released_at TIMESTAMPTZ,
            release_reason TEXT CHECK (release_reason IS NULL OR length(release_reason) <= 1000),
            CHECK ((scope = 'tenant') = (subject_id IS NULL))
        )
    """)
    op.execute(
        "CREATE INDEX legal_holds_active_idx ON legal_holds (tenant_uuid, scope, subject_id) "
        "WHERE released_at IS NULL"
    )
    op.execute("""
        CREATE FUNCTION guard_haas_legal_hold_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' OR OLD.released_at IS NOT NULL
               OR NEW.released_at IS NULL
               OR (NEW.hold_id, NEW.tenant_uuid, NEW.scope, NEW.subject_id,
                   NEW.reason, NEW.placed_by, NEW.placed_at)
                  IS DISTINCT FROM
                  (OLD.hold_id, OLD.tenant_uuid, OLD.scope, OLD.subject_id,
                   OLD.reason, OLD.placed_by, OLD.placed_at) THEN
                RAISE EXCEPTION 'legal holds can only be released once';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)
    op.execute("""
        CREATE TRIGGER legal_holds_guard BEFORE UPDATE OR DELETE ON legal_holds
        FOR EACH ROW EXECUTE FUNCTION guard_haas_legal_hold_change()
    """)

    op.execute(f"""
        CREATE TABLE erasure_requests (
            request_id UUID PRIMARY KEY,
            tenant_uuid UUID NOT NULL REFERENCES tenants (tenant_id),
            kind TEXT NOT NULL CHECK (kind IN ('subject', 'offboarding')),
            subject_type TEXT CHECK (subject_type IN ('run', 'thread')),
            subject_ids TEXT[] NOT NULL DEFAULT '{{}}',
            reason TEXT CHECK (reason IS NULL OR length(reason) <= 1000),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'blocked', 'completed')),
            requested_by UUID REFERENCES principals (principal_id),
            requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            last_error TEXT,
            completed_at TIMESTAMPTZ,
            manifest JSONB,
            manifest_sha256 TEXT CHECK (manifest_sha256 IS NULL OR manifest_sha256 ~ {SHA256}),
            receipt_hmac TEXT CHECK (receipt_hmac IS NULL OR receipt_hmac ~ {SHA256}),
            receipt_key_id TEXT,
            CHECK ((kind = 'subject') = (subject_type IS NOT NULL)),
            CHECK (kind = 'offboarding' OR cardinality(subject_ids) BETWEEN 1 AND 100),
            CHECK ((status = 'completed') = (completed_at IS NOT NULL
                                             AND manifest_sha256 IS NOT NULL))
        )
    """)
    op.execute(
        "CREATE UNIQUE INDEX erasure_requests_one_offboarding ON erasure_requests (tenant_uuid) "
        "WHERE kind = 'offboarding' AND status <> 'completed'"
    )
    op.execute(
        "CREATE INDEX erasure_requests_open_idx ON erasure_requests (requested_at) "
        "WHERE status <> 'completed'"
    )
    op.execute("""
        CREATE FUNCTION guard_haas_erasure_request_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' OR OLD.status = 'completed'
               OR (NEW.request_id, NEW.tenant_uuid, NEW.kind, NEW.subject_type,
                   NEW.subject_ids, NEW.reason, NEW.requested_by, NEW.requested_at)
                  IS DISTINCT FROM
                  (OLD.request_id, OLD.tenant_uuid, OLD.kind, OLD.subject_type,
                   OLD.subject_ids, OLD.reason, OLD.requested_by, OLD.requested_at) THEN
                RAISE EXCEPTION 'erasure requests are immutable once completed';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)
    op.execute("""
        CREATE TRIGGER erasure_requests_guard BEFORE UPDATE OR DELETE ON erasure_requests
        FOR EACH ROW EXECUTE FUNCTION guard_haas_erasure_request_change()
    """)

    op.execute(f"""
        CREATE TABLE retention_tombstones (
            tombstone_id UUID PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            tenant_uuid UUID REFERENCES tenants (tenant_id),
            data_class TEXT NOT NULL
                CHECK (data_class IN ('run_events', 'artifacts', 'messages', 'tenant')),
            subject_type TEXT NOT NULL CHECK (subject_type IN ('run', 'thread', 'tenant')),
            subject_id TEXT NOT NULL CHECK (length(subject_id) BETWEEN 1 AND 255),
            reason TEXT NOT NULL
                CHECK (reason IN ('retention', 'erasure_request', 'offboarding')),
            policy JSONB NOT NULL CHECK (jsonb_typeof(policy) = 'object'),
            records_erased INTEGER NOT NULL CHECK (records_erased >= 0),
            bytes_erased BIGINT NOT NULL DEFAULT 0 CHECK (bytes_erased >= 0),
            manifest_sha256 TEXT NOT NULL CHECK (manifest_sha256 ~ {SHA256}),
            request_id UUID REFERENCES erasure_requests (request_id),
            erased_by TEXT NOT NULL CHECK (length(erased_by) BETWEEN 1 AND 256),
            erased_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX retention_tombstones_subject_idx ON retention_tombstones "
        "(tenant_id, subject_type, subject_id, data_class)"
    )
    op.execute(
        "CREATE INDEX retention_tombstones_request_idx ON retention_tombstones (request_id) "
        "WHERE request_id IS NOT NULL"
    )
    op.execute("""
        CREATE TRIGGER retention_tombstones_immutable
        BEFORE UPDATE OR DELETE ON retention_tombstones
        FOR EACH ROW EXECUTE FUNCTION reject_haas_immutable_change()
    """)

    _rls("artifacts", LEGACY)
    _rls("retention_tombstones", LEGACY)
    for table in ("retention_policies", "legal_holds", "erasure_requests"):
        _rls(table)

    op.execute("GRANT SELECT, INSERT, UPDATE (deleted_at) ON artifacts TO haas_tenant_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE (deleted_at), DELETE ON artifacts "
        "TO haas_execution_service"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON retention_policies TO haas_tenant_app"
    )
    op.execute("GRANT SELECT ON retention_policies TO haas_execution_service")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE (released_by, released_at, release_reason) "
        "ON legal_holds TO haas_tenant_app"
    )
    op.execute("GRANT SELECT ON legal_holds TO haas_execution_service")
    op.execute("GRANT SELECT, INSERT ON erasure_requests TO haas_tenant_app")
    op.execute("GRANT SELECT, UPDATE ON erasure_requests TO haas_execution_service")
    op.execute("GRANT SELECT ON retention_tombstones TO haas_tenant_app")
    op.execute(
        "GRANT SELECT, INSERT ON retention_tombstones TO haas_execution_service"
    )
    # Offboarding transitions the tenant to `offboarding` and finally `deleted`.
    op.execute("GRANT UPDATE (status, updated_at) ON tenants TO haas_execution_service")


def downgrade() -> None:
    checks = "\n".join(
        f"""
            IF EXISTS (SELECT 1 FROM {table}) THEN
                RAISE EXCEPTION 'drain {table} before downgrading the artifact schema';
            END IF;"""
        for table in NEW_TABLES
    )
    op.execute(f"DO $$ BEGIN{checks}\n        END $$;")
    op.execute("REVOKE UPDATE (status, updated_at) ON tenants FROM haas_execution_service")
    for table in reversed(NEW_TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION guard_haas_erasure_request_change()")
    op.execute("DROP FUNCTION guard_haas_legal_hold_change()")
    op.execute("DROP FUNCTION guard_haas_artifact_change()")
    op.execute("REVOKE DELETE ON run_events FROM haas_execution_service")
    op.execute("DROP TRIGGER run_events_immutable ON run_events")
    op.execute("""
        CREATE TRIGGER run_events_immutable BEFORE UPDATE OR DELETE ON run_events
        FOR EACH ROW EXECUTE FUNCTION reject_haas_immutable_change()
    """)
    op.execute("DROP FUNCTION haas_retention_purge_or_reject()")
