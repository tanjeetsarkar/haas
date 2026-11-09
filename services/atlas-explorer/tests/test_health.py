"""Contract tests for the standalone liveness endpoint."""

import asyncio
from pathlib import Path

import httpx
import pytest

import app as service_package
from app.main import app


def get(path: str) -> httpx.Response:
    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://service.test",
        ) as client:
            return await client.get(path)

    return asyncio.run(request())


def test_app_is_imported_from_service_directory() -> None:
    service_dir = Path(__file__).resolve().parents[1]
    assert Path(service_package.__file__).resolve() == (
        service_dir / "app" / "__init__.py"
    )


def test_health_is_exact_and_repeated_without_core_or_database_environment(
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    for name in (
        "DATABASE_URL",
        "HAAS_DATABASE_URL",
        "CORE_API_URL",
        "HAAS_API_URL",
        "HAAS_CORE_NETWORK",
    ):
        monkeypatch.delenv(name, raising=False)

    for _ in range(3):
        response = get("/healthz")
        assert response.status_code == 200
        assert response.text == '{"status":"ok"}'
        assert response.json() == {"status": "ok"}


def test_business_and_documentation_routes_are_absent() -> None:
    for path in ("/api/v1/entities", "/openapi.json", "/docs", "/redoc"):
        assert get(path).status_code == 404
