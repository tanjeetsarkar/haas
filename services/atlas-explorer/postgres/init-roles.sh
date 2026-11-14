#!/bin/sh
set -eu

export ATLAS_MIGRATION_PASSWORD="$(cat /run/secrets/atlas_migration_password)"
export ATLAS_RUNTIME_PASSWORD="$(cat /run/secrets/atlas_runtime_password)"

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 <<'SQL'
\getenv migration_password ATLAS_MIGRATION_PASSWORD
\getenv runtime_password ATLAS_RUNTIME_PASSWORD
SELECT format(
    'CREATE ROLE atlas_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'migration_password'
) \gexec
SELECT format(
    'CREATE ROLE atlas_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'runtime_password'
) \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database()) \gexec
SELECT format('GRANT CONNECT, CREATE ON DATABASE %I TO atlas_migrator', current_database()) \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO atlas_runtime', current_database()) \gexec
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM atlas_migrator, atlas_runtime;
CREATE SCHEMA atlas_explorer AUTHORIZATION atlas_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE atlas_migrator IN SCHEMA atlas_explorer
    REVOKE ALL ON TABLES FROM PUBLIC, atlas_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE atlas_migrator IN SCHEMA atlas_explorer
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC, atlas_runtime;
SQL

unset ATLAS_MIGRATION_PASSWORD ATLAS_RUNTIME_PASSWORD
