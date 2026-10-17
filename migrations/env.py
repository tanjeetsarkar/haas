from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings

config = context.config
config.set_main_option(
    "sqlalchemy.url",
    settings.postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1).replace(
        "%", "%%"
    ),
)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

LOCK_ID = 7_300_001


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": LOCK_ID}
        )
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await connectable.dispose()


if context.is_offline_mode():
    raise RuntimeError(
        "Offline migrations are not supported; use the locked migration job"
    )
else:
    asyncio.run(run_async_migrations())
