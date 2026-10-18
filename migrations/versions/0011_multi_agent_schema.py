"""Run-tree lineage, delegations, mailbox, versioned workspace and agent cards.

Implements the tenant-owned half of data-model §4a. Marketplace catalog tables
(publisher, listing, plugin revision, scan report, advisory, installation) belong
to Track M and are intentionally not created here.

Revision ID: 0011_multi_agent_schema
Revises: 0010_completion_usage
"""

from alembic import op

revision = "0011_multi_agent_schema"
down_revision = "0010_completion_usage"
branch_labels = None
depends_on = None

MAX_TREE_DEPTH = 32
TREE_TABLES = (
    "delegations",
    "agent_messages",
    "workspaces",
    "workspace_entry_versions",
)
NEW_TABLES = (*TREE_TABLES, "agent_cards")
SHA256 = "'^[0-9a-f]{64}$'"
LEGACY = (
    " OR (tenant_uuid IS NULL "
    "AND current_setting('haas.legacy_dev', true) = 'on' "
    "AND tenant_id = current_setting('haas.tenant', true))"
)


def _rls(table: str, key: str, legacy: str) -> None:
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


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE runs
            ADD COLUMN root_run_id TEXT,
            ADD COLUMN depth INTEGER NOT NULL DEFAULT 0
                CHECK (depth BETWEEN 0 AND {MAX_TREE_DEPTH}),
            ADD COLUMN delegation_id UUID,
            ADD CONSTRAINT runs_root_tenant_fk FOREIGN KEY (tenant_id, root_run_id)
                REFERENCES runs (tenant_id, run_id),
            ADD CONSTRAINT runs_lineage_shape_check CHECK (
                (parent_run_id IS NULL) = (root_run_id IS NULL)
                AND (parent_run_id IS NULL) = (depth = 0)
                AND (delegation_id IS NULL OR parent_run_id IS NOT NULL)
            )
    """)
    op.execute("""
        CREATE INDEX runs_root_idx ON runs (tenant_id, root_run_id)
            WHERE root_run_id IS NOT NULL
    """)
    op.execute("""
        CREATE FUNCTION haas_run_root(p_tenant_id TEXT, p_run_id TEXT)
        RETURNS TEXT LANGUAGE sql STABLE AS $$
            SELECT COALESCE(root_run_id, run_id) FROM runs
            WHERE tenant_id = p_tenant_id AND run_id = p_run_id
        $$;
    """)
    # Lineage is derived from the parent, never trusted from the caller, and frozen afterwards.
    op.execute("""
        CREATE FUNCTION sync_haas_run_lineage()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            parent RECORD;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.parent_run_id IS NULL THEN
                    NEW.root_run_id := NULL;
                    NEW.depth := 0;
                    RETURN NEW;
                END IF;
                SELECT run_id, root_run_id, depth, tenant_uuid INTO parent FROM runs
                WHERE tenant_id = NEW.tenant_id AND run_id = NEW.parent_run_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'parent run % is not visible in this tenant',
                        NEW.parent_run_id USING ERRCODE = 'foreign_key_violation';
                END IF;
                IF parent.tenant_uuid IS DISTINCT FROM NEW.tenant_uuid THEN
                    RAISE EXCEPTION 'child run tenant must equal its root tenant'
                        USING ERRCODE = 'check_violation';
                END IF;
                NEW.root_run_id := COALESCE(parent.root_run_id, parent.run_id);
                NEW.depth := parent.depth + 1;
                RETURN NEW;
            END IF;
            IF NEW.parent_run_id IS DISTINCT FROM OLD.parent_run_id
                OR NEW.root_run_id IS DISTINCT FROM OLD.root_run_id
                OR NEW.depth IS DISTINCT FROM OLD.depth THEN
                RAISE EXCEPTION 'run lineage is immutable';
            END IF;
            IF NEW.delegation_id IS DISTINCT FROM OLD.delegation_id THEN
                IF OLD.delegation_id IS NOT NULL THEN
                    RAISE EXCEPTION 'run delegation is immutable once bound';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM delegations d
                    WHERE d.tenant_id = NEW.tenant_id
                      AND d.delegation_id = NEW.delegation_id
                      AND d.child_run_id = NEW.run_id) THEN
                    RAISE EXCEPTION 'delegation % does not delegate to run %',
                        NEW.delegation_id, NEW.run_id USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
    """)
    # Named to sort after runs_schema_expand_sync so tenant_uuid is already resolved.
    op.execute("""
        CREATE TRIGGER runs_tree_lineage
            BEFORE INSERT OR UPDATE OF parent_run_id, root_run_id, depth, delegation_id
            ON runs FOR EACH ROW EXECUTE FUNCTION sync_haas_run_lineage()
    """)

    op.execute(f"""
        CREATE TABLE delegations (
            tenant_id TEXT NOT NULL,
            tenant_uuid UUID REFERENCES tenants (tenant_id),
            delegation_id UUID NOT NULL,
            root_run_id TEXT NOT NULL,
            parent_run_id TEXT NOT NULL,
            child_run_id TEXT NOT NULL,
            depth INTEGER NOT NULL CHECK (depth BETWEEN 1 AND {MAX_TREE_DEPTH}),
            mode TEXT NOT NULL CHECK (mode IN ('await', 'async')),
            failure_policy TEXT NOT NULL DEFAULT 'fail_fast'
                CHECK (failure_policy IN ('fail_fast', 'best_effort', 'retry', 'escalate')),
            max_retries INTEGER NOT NULL DEFAULT 0 CHECK (max_retries BETWEEN 0 AND 10),
            caveats JSONB NOT NULL CHECK (jsonb_typeof(caveats) = 'object'),
            caveats_digest TEXT NOT NULL CHECK (caveats_digest ~ {SHA256}),
            budget_slice_usd NUMERIC NOT NULL CHECK (budget_slice_usd >= 0),
            budget_returned_usd NUMERIC NOT NULL DEFAULT 0
                CHECK (budget_returned_usd >= 0 AND budget_returned_usd <= budget_slice_usd),
            deadline_at TIMESTAMPTZ,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'settled', 'cancelled', 'failed')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            settled_at TIMESTAMPTZ,
            PRIMARY KEY (tenant_id, delegation_id),
            UNIQUE (tenant_id, child_run_id),
            CHECK (parent_run_id <> child_run_id),
            CHECK ((status = 'active') = (settled_at IS NULL)),
            FOREIGN KEY (tenant_id, root_run_id) REFERENCES runs (tenant_id, run_id),
            FOREIGN KEY (tenant_id, parent_run_id) REFERENCES runs (tenant_id, run_id),
            FOREIGN KEY (tenant_id, child_run_id) REFERENCES runs (tenant_id, run_id)
        )
    """)
    op.execute(
        "CREATE INDEX delegations_parent_idx ON delegations (tenant_id, parent_run_id)"
    )
    op.execute("""
        ALTER TABLE runs ADD CONSTRAINT runs_delegation_tenant_fk
            FOREIGN KEY (tenant_id, delegation_id)
            REFERENCES delegations (tenant_id, delegation_id)
            DEFERRABLE INITIALLY DEFERRED
    """)

    op.execute("""
        CREATE TABLE agent_messages (
            tenant_id TEXT NOT NULL,
            tenant_uuid UUID REFERENCES tenants (tenant_id),
            message_id UUID NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'hacp/1' CHECK (schema_version = 'hacp/1'),
            root_run_id TEXT NOT NULL,
            from_run_id TEXT NOT NULL,
            to_run_id TEXT NOT NULL,
            delegation_id UUID,
            kind TEXT NOT NULL CHECK (kind IN ('message', 'result', 'handoff', 'cancel')),
            body_reference JSONB NOT NULL CHECK (
                jsonb_typeof(body_reference) = 'object'
                AND octet_length(body_reference::text) <= 65536
            ),
            sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            consumed_at TIMESTAMPTZ,
            PRIMARY KEY (tenant_id, message_id),
            CHECK (from_run_id <> to_run_id),
            CHECK (consumed_at IS NULL OR consumed_at >= sent_at),
            FOREIGN KEY (tenant_id, root_run_id) REFERENCES runs (tenant_id, run_id),
            FOREIGN KEY (tenant_id, from_run_id) REFERENCES runs (tenant_id, run_id),
            FOREIGN KEY (tenant_id, to_run_id) REFERENCES runs (tenant_id, run_id),
            FOREIGN KEY (tenant_id, delegation_id)
                REFERENCES delegations (tenant_id, delegation_id)
        )
    """)
    op.execute("""
        CREATE INDEX agent_messages_inbox_idx ON agent_messages (tenant_id, to_run_id, sent_at)
            WHERE consumed_at IS NULL
    """)

    op.execute("""
        CREATE TABLE workspaces (
            tenant_id TEXT NOT NULL,
            tenant_uuid UUID REFERENCES tenants (tenant_id),
            workspace_id UUID NOT NULL,
            root_run_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, workspace_id),
            UNIQUE (tenant_id, root_run_id),
            FOREIGN KEY (tenant_id, root_run_id) REFERENCES runs (tenant_id, run_id)
        )
    """)
    op.execute(f"""
        CREATE TABLE workspace_entry_versions (
            tenant_id TEXT NOT NULL,
            tenant_uuid UUID REFERENCES tenants (tenant_id),
            workspace_id UUID NOT NULL,
            path TEXT NOT NULL CHECK (
                length(path) <= 1024
                AND path ~ '^/[A-Za-z0-9._/-]*$'
                AND path !~ '(^|/)\\.\\.?(/|$)'
                AND path !~ '//'
            ),
            version BIGINT NOT NULL CHECK (version >= 1),
            written_by_run_id TEXT NOT NULL,
            content_digest TEXT NOT NULL CHECK (content_digest ~ {SHA256}),
            inline_content TEXT CHECK (octet_length(inline_content) <= 65536),
            artifact_reference TEXT CHECK (length(artifact_reference) BETWEEN 1 AND 512),
            tombstone BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, workspace_id, path, version),
            CHECK (
                (tombstone AND inline_content IS NULL AND artifact_reference IS NULL)
                OR (NOT tombstone AND num_nonnulls(inline_content, artifact_reference) = 1)
            ),
            FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces (tenant_id, workspace_id),
            FOREIGN KEY (tenant_id, written_by_run_id) REFERENCES runs (tenant_id, run_id)
        )
    """)

    # Every tree row copies tenant_uuid from the root run and proves each referenced run
    # belongs to that root, so a sibling tree in the same tenant cannot be addressed either.
    op.execute("""
        CREATE FUNCTION sync_haas_run_tree_row()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            root_id TEXT;
            root_uuid UUID;
            root_depth INTEGER;
            current_version BIGINT;
        BEGIN
            IF TG_OP = 'UPDATE' AND (
                NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
                OR NEW.tenant_uuid IS DISTINCT FROM OLD.tenant_uuid
            ) THEN
                RAISE EXCEPTION '% rows cannot change tenant', TG_TABLE_NAME;
            END IF;
            IF TG_TABLE_NAME = 'workspace_entry_versions' THEN
                SELECT w.root_run_id INTO root_id FROM workspaces w
                WHERE w.tenant_id = NEW.tenant_id AND w.workspace_id = NEW.workspace_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'workspace % is not visible in this tenant',
                        NEW.workspace_id USING ERRCODE = 'foreign_key_violation';
                END IF;
            ELSE
                root_id := NEW.root_run_id;
            END IF;
            SELECT tenant_uuid, depth INTO root_uuid, root_depth FROM runs
            WHERE tenant_id = NEW.tenant_id AND run_id = root_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'root run % is not visible in this tenant', root_id
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            IF root_depth <> 0 THEN
                RAISE EXCEPTION 'run % is not a root run', root_id
                    USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'INSERT' THEN
                NEW.tenant_uuid := root_uuid;
            END IF;

            IF TG_TABLE_NAME = 'delegations' THEN
                IF haas_run_root(NEW.tenant_id, NEW.parent_run_id) IS DISTINCT FROM root_id
                    OR NOT EXISTS (SELECT 1 FROM runs r
                        WHERE r.tenant_id = NEW.tenant_id AND r.run_id = NEW.child_run_id
                          AND r.parent_run_id = NEW.parent_run_id
                          AND r.root_run_id = root_id AND r.depth = NEW.depth) THEN
                    RAISE EXCEPTION 'delegation must link a parent to its direct child in one tree'
                        USING ERRCODE = 'check_violation';
                END IF;
                IF TG_OP = 'UPDATE' AND (
                    NEW.delegation_id, NEW.root_run_id, NEW.parent_run_id, NEW.child_run_id,
                    NEW.depth, NEW.mode, NEW.failure_policy, NEW.max_retries, NEW.caveats,
                    NEW.caveats_digest, NEW.budget_slice_usd, NEW.deadline_at, NEW.created_at
                ) IS DISTINCT FROM (
                    OLD.delegation_id, OLD.root_run_id, OLD.parent_run_id, OLD.child_run_id,
                    OLD.depth, OLD.mode, OLD.failure_policy, OLD.max_retries, OLD.caveats,
                    OLD.caveats_digest, OLD.budget_slice_usd, OLD.deadline_at, OLD.created_at
                ) THEN
                    RAISE EXCEPTION 'delegation terms are immutable';
                END IF;
                IF TG_OP = 'UPDATE' AND OLD.status <> 'active' THEN
                    RAISE EXCEPTION 'settled delegations are immutable';
                END IF;
            ELSIF TG_TABLE_NAME = 'agent_messages' THEN
                IF haas_run_root(NEW.tenant_id, NEW.from_run_id) IS DISTINCT FROM root_id
                    OR haas_run_root(NEW.tenant_id, NEW.to_run_id) IS DISTINCT FROM root_id THEN
                    RAISE EXCEPTION 'messages may only flow within one run tree'
                        USING ERRCODE = 'check_violation';
                END IF;
                IF TG_OP = 'UPDATE' AND (
                    (NEW.message_id, NEW.schema_version, NEW.root_run_id, NEW.from_run_id,
                     NEW.to_run_id, NEW.delegation_id, NEW.kind, NEW.body_reference, NEW.sent_at)
                    IS DISTINCT FROM
                    (OLD.message_id, OLD.schema_version, OLD.root_run_id, OLD.from_run_id,
                     OLD.to_run_id, OLD.delegation_id, OLD.kind, OLD.body_reference, OLD.sent_at)
                    OR OLD.consumed_at IS NOT NULL
                ) THEN
                    RAISE EXCEPTION 'agent messages are immutable once sent or consumed';
                END IF;
            ELSIF TG_TABLE_NAME = 'workspace_entry_versions' THEN
                IF haas_run_root(NEW.tenant_id, NEW.written_by_run_id) IS DISTINCT FROM root_id THEN
                    RAISE EXCEPTION 'only runs in the owning tree may write this workspace'
                        USING ERRCODE = 'check_violation';
                END IF;
                SELECT max(version) INTO current_version FROM workspace_entry_versions
                WHERE tenant_id = NEW.tenant_id AND workspace_id = NEW.workspace_id
                  AND path = NEW.path;
                IF NEW.version <> COALESCE(current_version, 0) + 1 THEN
                    RAISE EXCEPTION 'workspace CAS conflict on %: expected version %, got %',
                        NEW.path, COALESCE(current_version, 0) + 1, NEW.version
                        USING ERRCODE = 'serialization_failure';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
    """)
    for table in TREE_TABLES:
        events = (
            "INSERT"
            if table in {"workspaces", "workspace_entry_versions"}
            else "INSERT OR UPDATE"
        )
        op.execute(f"""
            CREATE TRIGGER {table}_tree_sync BEFORE {events} ON {table}
            FOR EACH ROW EXECUTE FUNCTION sync_haas_run_tree_row()
        """)
    for table in ("workspaces", "workspace_entry_versions"):
        op.execute(f"""
            CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_haas_immutable_change()
        """)

    op.execute("""
        CREATE TABLE agent_cards (
            tenant_id UUID NOT NULL REFERENCES tenants (tenant_id),
            agent_revision_id UUID NOT NULL,
            card JSONB NOT NULL CHECK (
                jsonb_typeof(card) = 'object' AND octet_length(card::text) <= 65536
            ),
            exposure TEXT NOT NULL DEFAULT 'tenant'
                CHECK (exposure IN ('disabled', 'tenant', 'public')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, agent_revision_id),
            FOREIGN KEY (tenant_id, agent_revision_id)
                REFERENCES agent_revisions (tenant_id, revision_id)
        )
    """)
    op.execute("""
        CREATE FUNCTION check_haas_agent_card()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND (
                NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
                OR NEW.agent_revision_id IS DISTINCT FROM OLD.agent_revision_id
            ) THEN
                RAISE EXCEPTION 'agent cards cannot move between revisions';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM agent_revisions a
                WHERE a.tenant_id = NEW.tenant_id AND a.revision_id = NEW.agent_revision_id
                  AND a.state = 'published') THEN
                RAISE EXCEPTION 'agent cards require a published agent revision'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)
    op.execute("""
        CREATE TRIGGER agent_cards_published BEFORE INSERT OR UPDATE ON agent_cards
        FOR EACH ROW EXECUTE FUNCTION check_haas_agent_card()
    """)

    for table in TREE_TABLES:
        _rls(table, "tenant_uuid", LEGACY)
    _rls("agent_cards", "tenant_id", "")

    for statement in (
        "GRANT EXECUTE ON FUNCTION haas_run_root(TEXT, TEXT) "
        "TO haas_tenant_app, haas_execution_service",
        "GRANT SELECT, INSERT ON delegations, agent_messages, workspaces, "
        "workspace_entry_versions TO haas_tenant_app, haas_execution_service",
        "GRANT UPDATE (status, settled_at, budget_returned_usd) ON delegations "
        "TO haas_tenant_app, haas_execution_service",
        "GRANT UPDATE (consumed_at) ON agent_messages "
        "TO haas_tenant_app, haas_execution_service",
        "GRANT SELECT, INSERT ON agent_cards TO haas_tenant_app",
        "GRANT UPDATE (card, exposure, updated_at) ON agent_cards TO haas_tenant_app",
        "GRANT SELECT ON agent_cards TO haas_execution_service",
    ):
        op.execute(statement)


def downgrade() -> None:
    checks = "\n".join(
        f"""
            IF EXISTS (SELECT 1 FROM {table}) THEN
                RAISE EXCEPTION 'drain {table} before downgrading the multi-agent schema';
            END IF;"""
        for table in NEW_TABLES
    )
    op.execute(f"""
        DO $$ BEGIN{checks}
            IF EXISTS (SELECT 1 FROM runs WHERE parent_run_id IS NOT NULL) THEN
                RAISE EXCEPTION 'drain child runs before downgrading the multi-agent schema';
            END IF;
        END $$;
    """)
    op.execute("DROP TABLE agent_cards")
    op.execute("DROP FUNCTION check_haas_agent_card()")
    op.execute("ALTER TABLE runs DROP CONSTRAINT runs_delegation_tenant_fk")
    for table in reversed(TREE_TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION sync_haas_run_tree_row()")
    op.execute("DROP TRIGGER runs_tree_lineage ON runs")
    op.execute("DROP FUNCTION sync_haas_run_lineage()")
    op.execute("DROP FUNCTION haas_run_root(TEXT, TEXT)")
    op.execute("DROP INDEX runs_root_idx")
    op.execute("""
        ALTER TABLE runs
            DROP CONSTRAINT runs_lineage_shape_check,
            DROP CONSTRAINT runs_root_tenant_fk,
            DROP COLUMN delegation_id,
            DROP COLUMN depth,
            DROP COLUMN root_run_id
    """)
