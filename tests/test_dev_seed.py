from __future__ import annotations

import pytest

from app import seed_dev
from app.core.config import settings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile", "dsn", "message"),
    [
        ("prod", "postgresql://haas@localhost:5432/haas_db", "HAAS_PROFILE=dev"),
        (
            "dev",
            "postgresql://haas@database.example.test:5432/haas_db",
            "local PostgreSQL host",
        ),
    ],
)
async def test_seed_refuses_non_dev_or_remote_database(
    profile, dsn, message, monkeypatch
):
    monkeypatch.setattr(
        seed_dev,
        "settings",
        settings.model_copy(update={"haas_profile": profile, "postgres_dsn": dsn}),
    )

    with pytest.raises(RuntimeError, match=message):
        await seed_dev.seed_dev()
