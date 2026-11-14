DO $$
BEGIN
    ALTER TABLE atlas_explorer.workspace_mutations NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE atlas_explorer.workspace_mutations DISABLE ROW LEVEL SECURITY;
    ALTER TABLE atlas_explorer.workspaces NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE atlas_explorer.workspaces DISABLE ROW LEVEL SECURITY;
    IF EXISTS (SELECT 1 FROM atlas_explorer.workspace_mutations)
       OR EXISTS (SELECT 1 FROM atlas_explorer.workspaces) THEN
        RAISE EXCEPTION 'refusing to downgrade populated Atlas workspace storage';
    END IF;
END
$$;

DROP TRIGGER mutation_tenant_usage_trigger ON atlas_explorer.workspace_mutations;
DROP TRIGGER workspace_tenant_usage_trigger ON atlas_explorer.workspaces;
DROP FUNCTION atlas_explorer.adjust_tenant_usage();
DROP TABLE atlas_explorer.workspace_tenant_usage;
DROP TABLE atlas_explorer.workspace_mutations;
DROP TABLE atlas_explorer.workspaces;
