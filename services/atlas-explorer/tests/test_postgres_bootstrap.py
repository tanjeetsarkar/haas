"""Safe static checks for Atlas-owned PostgreSQL provisioning and Compose."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1]
POSTGRES = SERVICE / "postgres"


def test_bootstrap_and_connection_helpers_are_shell_valid_and_secret_based() -> None:
    scripts = (
        POSTGRES / "init-roles.sh",
        POSTGRES / "atlas-api-entrypoint.sh",
        POSTGRES / "atlas-migration-entrypoint.sh",
        POSTGRES / "create-secrets.sh",
    )
    for script in scripts:
        subprocess.run(["sh", "-n", str(script)], check=True)

    bootstrap = (POSTGRES / "init-roles.sh").read_text()
    assert "CREATE ROLE atlas_migrator LOGIN NOSUPERUSER" in bootstrap
    assert "CREATE ROLE atlas_runtime LOGIN NOSUPERUSER" in bootstrap
    assert "NOBYPASSRLS" in bootstrap
    assert "PASSWORD %L" in bootstrap
    assert "REVOKE ALL ON DATABASE %I FROM PUBLIC" in bootstrap
    assert "GRANT CONNECT, CREATE ON DATABASE %I TO atlas_migrator" in bootstrap
    assert "GRANT CONNECT ON DATABASE %I TO atlas_runtime" in bootstrap
    assert "REVOKE ALL ON SCHEMA public FROM PUBLIC" in bootstrap
    assert "CREATE SCHEMA atlas_explorer AUTHORIZATION atlas_migrator" in bootstrap
    assert "ALTER DEFAULT PRIVILEGES" in bootstrap
    secret_helper = (POSTGRES / "create-secrets.sh").read_text()
    assert "openssl rand -hex 32" in secret_helper
    assert "chmod 700 postgres/secrets" in secret_helper
    assert 'chmod 644 "$path"' in secret_helper


def test_migration_grants_only_runtime_dml_and_read_only_history() -> None:
    migration = (SERVICE / "migrations/001_workspace.up.sql").read_text()
    assert "GRANT CREATE ON SCHEMA public" not in migration
    assert "GRANT USAGE ON SCHEMA atlas_explorer TO atlas_runtime" in migration
    assert "GRANT SELECT, INSERT, UPDATE ON atlas_explorer.workspaces TO atlas_runtime" in migration
    assert "GRANT SELECT, INSERT ON atlas_explorer.workspace_mutations TO atlas_runtime" in migration
    assert "GRANT SELECT, INSERT, UPDATE ON atlas_explorer.workspace_tenant_usage TO atlas_runtime" in migration
    assert "GRANT SELECT ON atlas_explorer.schema_migrations TO atlas_runtime" in migration
    assert "GRANT INSERT ON atlas_explorer.schema_migrations" not in migration
    assert migration.count("ENABLE ROW LEVEL SECURITY") == 3
    assert migration.count("FORCE ROW LEVEL SECURITY") == 3


def _compose_config(*files: str, profile: str | None = None) -> dict:
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker Compose is required for topology contract tests.")
    command = [docker, "compose", "--env-file", "/dev/null"]
    if profile:
        command.extend(["--profile", profile])
    for compose_file in files:
        command.extend(["-f", str(SERVICE / compose_file)])
    command.extend(["config", "--format", "json"])
    clean_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
    }
    rendered = subprocess.run(
        command, check=True, capture_output=True, text=True, env=clean_env
    ).stdout
    assert "postgresql://" not in rendered
    assert "password=" not in rendered.lower()
    return json.loads(rendered)


def test_default_compose_uses_private_persistent_database_and_loopback_api() -> None:
    config = _compose_config("docker-compose.yml")
    services = config["services"]
    api = services["atlas-explorer"]
    database = services["atlas-postgres"]

    assert set(api["networks"]) == {"haas-core", "atlas-db"}
    assert set(database["networks"]) == {"atlas-db"}
    assert database.get("ports") is None
    assert api["ports"] == [
        {
            "host_ip": "127.0.0.1",
            "mode": "ingress",
            "protocol": "tcp",
            "published": "8081",
            "target": 8080,
        }
    ]
    assert config["networks"]["atlas-db"]["internal"] is True
    assert config["networks"]["haas-core"]["name"] == "harness_default"
    assert config["volumes"]["atlas_postgres_data"]["name"] == "atlas_explorer_postgres_data"
    assert api["environment"]["ATLAS_DATABASE_HOST"] == "atlas-postgres"
    assert "ATLAS_DATABASE_URL" not in api["environment"]


def test_external_compose_omits_local_database_and_private_network() -> None:
    files = ("docker-compose.yml", "docker-compose.external-db.yml")
    config = _compose_config(*files)
    assert set(config["services"]) == {"atlas-explorer"}
    assert set(config["services"]["atlas-explorer"]["networks"]) == {"haas-core"}
    assert config["services"]["atlas-explorer"]["environment"][
        "ATLAS_DATABASE_URL_FILE"
    ] == "/run/secrets/atlas_external_runtime_url"

    migration_config = _compose_config(*files, profile="migrate")
    assert set(migration_config["services"]) == {"atlas-explorer", "atlas-migrate"}
    assert set(migration_config["services"]["atlas-migrate"]["networks"]) == {"haas-core"}


@pytest.mark.parametrize(
    "files",
    [
        ("docker-compose.yml",),
        ("docker-compose.yml", "docker-compose.external-db.yml"),
    ],
    ids=["default", "external"],
)
def test_compose_api_has_explicit_startup_command(files: tuple[str, ...]) -> None:
    api = _compose_config(*files)["services"]["atlas-explorer"]
    assert api["entrypoint"] == ["/bin/sh", "/postgres/atlas-api-entrypoint.sh"]
    # A Compose entrypoint override suppresses image CMD: pass the API command
    # explicitly so the secret wrapper's exec "$@" cannot exit without serving.
    assert api["command"] == [
        "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"
    ]


def test_built_service_image_contains_explicit_migration_asset() -> None:
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker is required to verify packaged migration assets.")

    image = f"atlas-explorer-migration-asset-test:{uuid.uuid4().hex}"
    try:
        subprocess.run(
            [
                docker,
                "build",
                "--quiet",
                "--tag",
                image,
                "--file",
                str(SERVICE / "Dockerfile"),
                str(SERVICE),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                docker,
                "run",
                "--rm",
                image,
                "python",
                "-c",
                (
                    "from pathlib import Path; "
                    "root=Path('/service/migrations'); "
                    "up=(root/'001_workspace.up.sql').read_bytes(); "
                    "down=(root/'001_workspace.down.sql').read_bytes(); "
                    "assert up and down"
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        subprocess.run(
            [docker, "image", "rm", "--force", image],
            check=False,
            capture_output=True,
            text=True,
        )
