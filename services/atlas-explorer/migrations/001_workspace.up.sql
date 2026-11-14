CREATE SCHEMA IF NOT EXISTS atlas_explorer;

CREATE TABLE atlas_explorer.workspaces (
    tenant_id text NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 512),
    workspace_id uuid NOT NULL,
    owner_principal_id text NOT NULL CHECK (length(owner_principal_id) BETWEEN 1 AND 512),
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    schema_version smallint NOT NULL CHECK (schema_version = 1),
    state jsonb NOT NULL CONSTRAINT workspace_state_size CHECK (octet_length(state::text) <= 2097152),
    revision bigint NOT NULL CHECK (revision >= 1),
    create_mutation_id text NOT NULL CHECK (length(create_mutation_id) BETWEEN 1 AND 128),
    create_digest text NOT NULL CHECK (length(create_digest) = 64),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, workspace_id),
    UNIQUE (tenant_id, workspace_id, owner_principal_id),
    UNIQUE (tenant_id, owner_principal_id, create_mutation_id)
);

CREATE TABLE atlas_explorer.workspace_mutations (
    tenant_id text NOT NULL,
    workspace_id uuid NOT NULL,
    owner_principal_id text NOT NULL,
    mutation_id text NOT NULL CHECK (length(mutation_id) BETWEEN 1 AND 128),
    request_digest text NOT NULL CHECK (length(request_digest) = 64),
    kind text NOT NULL CHECK (length(kind) BETWEEN 1 AND 64),
    payload jsonb NOT NULL CONSTRAINT workspace_mutation_payload_size CHECK (octet_length(payload::text) <= 262144),
    revision bigint NOT NULL CHECK (revision >= 1),
    receipt jsonb NOT NULL,
    saved_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, workspace_id, mutation_id),
    UNIQUE (tenant_id, workspace_id, revision),
    FOREIGN KEY (tenant_id, workspace_id, owner_principal_id)
        REFERENCES atlas_explorer.workspaces (tenant_id, workspace_id, owner_principal_id)
        ON DELETE RESTRICT
);

CREATE TABLE atlas_explorer.workspace_tenant_usage (
    tenant_id text PRIMARY KEY CHECK (length(tenant_id) BETWEEN 1 AND 512),
    owner_principal_id text NOT NULL DEFAULT '__tenant_usage__',
    persisted_bytes bigint NOT NULL DEFAULT 0 CHECK (persisted_bytes >= 0)
);

CREATE INDEX workspace_catalog_page
    ON atlas_explorer.workspaces (tenant_id, owner_principal_id, updated_at DESC, workspace_id DESC);
CREATE INDEX workspace_journal_page
    ON atlas_explorer.workspace_mutations (tenant_id, workspace_id, revision);

ALTER TABLE atlas_explorer.workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE atlas_explorer.workspaces FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_owner_isolation ON atlas_explorer.workspaces
    USING (
        tenant_id = nullif(current_setting('atlas.tenant_id', true), '')
        AND owner_principal_id = nullif(current_setting('atlas.principal_id', true), '')
    )
    WITH CHECK (
        tenant_id = nullif(current_setting('atlas.tenant_id', true), '')
        AND owner_principal_id = nullif(current_setting('atlas.principal_id', true), '')
    );

ALTER TABLE atlas_explorer.workspace_mutations ENABLE ROW LEVEL SECURITY;
ALTER TABLE atlas_explorer.workspace_mutations FORCE ROW LEVEL SECURITY;
CREATE POLICY mutation_owner_isolation ON atlas_explorer.workspace_mutations
    USING (
        tenant_id = nullif(current_setting('atlas.tenant_id', true), '')
        AND owner_principal_id = nullif(current_setting('atlas.principal_id', true), '')
    )
    WITH CHECK (
        tenant_id = nullif(current_setting('atlas.tenant_id', true), '')
        AND owner_principal_id = nullif(current_setting('atlas.principal_id', true), '')
    );

ALTER TABLE atlas_explorer.workspace_tenant_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE atlas_explorer.workspace_tenant_usage FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_usage_isolation ON atlas_explorer.workspace_tenant_usage
    USING (tenant_id = nullif(current_setting('atlas.tenant_id', true), ''))
    WITH CHECK (tenant_id = nullif(current_setting('atlas.tenant_id', true), ''));

CREATE FUNCTION atlas_explorer.adjust_tenant_usage() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    tenant text;
    delta bigint;
BEGIN
    tenant := COALESCE(NEW.tenant_id, OLD.tenant_id);
    IF TG_TABLE_NAME = 'workspaces' THEN
        delta := CASE WHEN TG_OP = 'INSERT' THEN octet_length(NEW.state::text)
                      WHEN TG_OP = 'DELETE' THEN -octet_length(OLD.state::text)
                      ELSE octet_length(NEW.state::text)-octet_length(OLD.state::text) END;
    ELSE
        delta := CASE WHEN TG_OP = 'INSERT' THEN octet_length(NEW.payload::text)+octet_length(NEW.receipt::text)
                      WHEN TG_OP = 'DELETE' THEN -octet_length(OLD.payload::text)-octet_length(OLD.receipt::text)
                      ELSE octet_length(NEW.payload::text)+octet_length(NEW.receipt::text)
                           -octet_length(OLD.payload::text)-octet_length(OLD.receipt::text) END;
    END IF;
    UPDATE atlas_explorer.workspace_tenant_usage
        SET persisted_bytes = persisted_bytes + delta
        WHERE tenant_id = tenant;
    IF NOT FOUND AND delta >= 0 THEN
        INSERT INTO atlas_explorer.workspace_tenant_usage(tenant_id, owner_principal_id, persisted_bytes)
            VALUES (tenant, '__tenant_usage__', delta)
        ON CONFLICT (tenant_id) DO UPDATE
            SET persisted_bytes = atlas_explorer.workspace_tenant_usage.persisted_bytes + EXCLUDED.persisted_bytes;
    END IF;
    RETURN NULL;
END
$$;

CREATE TRIGGER workspace_tenant_usage_trigger
AFTER INSERT OR UPDATE OR DELETE ON atlas_explorer.workspaces
FOR EACH ROW EXECUTE FUNCTION atlas_explorer.adjust_tenant_usage();
CREATE TRIGGER mutation_tenant_usage_trigger
AFTER INSERT OR UPDATE OR DELETE ON atlas_explorer.workspace_mutations
FOR EACH ROW EXECUTE FUNCTION atlas_explorer.adjust_tenant_usage();

-- PostgreSQL's public/default ACLs are explicit; the service role gets no DDL.
REVOKE ALL ON ALL TABLES IN SCHEMA atlas_explorer FROM PUBLIC, atlas_runtime;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA atlas_explorer FROM PUBLIC, atlas_runtime;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA atlas_explorer FROM PUBLIC, atlas_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE atlas_migrator IN SCHEMA atlas_explorer
    REVOKE ALL ON TABLES FROM PUBLIC, atlas_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE atlas_migrator IN SCHEMA atlas_explorer
    REVOKE ALL ON SEQUENCES FROM PUBLIC, atlas_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE atlas_migrator IN SCHEMA atlas_explorer
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC, atlas_runtime;

GRANT USAGE ON SCHEMA atlas_explorer TO atlas_runtime;
GRANT SELECT, INSERT, UPDATE ON atlas_explorer.workspaces TO atlas_runtime;
GRANT SELECT, INSERT ON atlas_explorer.workspace_mutations TO atlas_runtime;
GRANT SELECT, INSERT, UPDATE ON atlas_explorer.workspace_tenant_usage TO atlas_runtime;
GRANT SELECT ON atlas_explorer.schema_migrations TO atlas_runtime;
