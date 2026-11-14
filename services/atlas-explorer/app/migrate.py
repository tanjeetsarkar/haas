"""Explicit service-owned SQL migration runner; never applies on app startup."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import asyncpg

MIGRATION_ID = "001_workspace"
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


async def migrate(direction: str, dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT pg_advisory_lock(hashtextextended('atlas_explorer_migrations', 0))")
        try:
            await conn.execute("CREATE SCHEMA IF NOT EXISTS atlas_explorer")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS atlas_explorer.schema_migrations "
                "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            applied = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM atlas_explorer.schema_migrations WHERE version=$1)",
                MIGRATION_ID,
            )
            if direction == "up" and not applied:
                async with conn.transaction():
                    await conn.execute((MIGRATIONS / f"{MIGRATION_ID}.up.sql").read_text())
                    await conn.execute(
                        "INSERT INTO atlas_explorer.schema_migrations(version) VALUES($1)",
                        MIGRATION_ID,
                    )
            elif direction == "down" and applied:
                async with conn.transaction():
                    await conn.execute((MIGRATIONS / f"{MIGRATION_ID}.down.sql").read_text())
                    await conn.execute(
                        "DELETE FROM atlas_explorer.schema_migrations WHERE version=$1",
                        MIGRATION_ID,
                    )
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtextextended('atlas_explorer_migrations', 0))")
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("direction", choices=("up", "down"))
    args = parser.parse_args()
    dsn = os.getenv("ATLAS_MIGRATION_DATABASE_URL")
    if not dsn:
        parser.error("ATLAS_MIGRATION_DATABASE_URL must target the dedicated Atlas database")
    asyncio.run(migrate(args.direction, dsn))


if __name__ == "__main__":
    main()
