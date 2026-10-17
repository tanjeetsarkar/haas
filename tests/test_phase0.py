from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from fastapi import HTTPException
import httpx
import yaml

from app.core.config import Settings, settings, validate_profile_settings
from app.core.http import RequestSizeLimitMiddleware, problem_response
from app.core.maintenance import garbage_collect_scratchpad
from app.core.observability import JsonRedactingFormatter
from app.core.secret_vault import decrypt_secrets, encrypt_secrets
from app.main import CreateRunRequest, _decode_cursor, _encode_cursor, app
import app.main as main_module
from app.core.store import DistributedStore


def test_secret_envelope_is_authenticated_and_run_scoped(monkeypatch):
    monkeypatch.setattr(
        settings, "secret_encryption_key", base64.b64encode(b"k" * 32).decode()
    )
    secrets = {"BILLING_TOKEN": "private-value"}
    envelope = encrypt_secrets("run-1", secrets)
    assert "private-value" not in envelope
    assert decrypt_secrets("run-1", envelope) == secrets
    with pytest.raises(InvalidTag):
        decrypt_secrets("run-2", envelope)


def test_secret_encryption_key_reads_compose_environment(monkeypatch):
    key = base64.b64encode(b"k" * 32).decode()
    monkeypatch.setenv("HAAS_SECRET_ENCRYPTION_KEY", key)
    assert Settings(_env_file=None).secret_encryption_key == key


def test_secret_envelope_rejects_missing_or_invalid_key(monkeypatch):
    monkeypatch.setattr(settings, "secret_encryption_key", None)
    with pytest.raises(ValueError, match="must be configured"):
        encrypt_secrets("run-1", {"TOKEN": "value"})
    monkeypatch.setattr(settings, "secret_encryption_key", "not-base64")
    with pytest.raises(ValueError, match="base64-encoded"):
        encrypt_secrets("run-1", {"TOKEN": "value"})


def test_production_profile_requires_identity_and_encryption():
    production = Settings(
        _env_file=None,
        haas_profile="prod",
        secret_encryption_key=base64.b64encode(b"x" * 32).decode(),
        openrouter_api_key="configured-provider-key",
        oidc_issuer="https://identity.example.test/",
        oidc_audience="haas-api",
        oidc_jwks_url="https://identity.example.test/jwks",
    )
    validate_profile_settings(production, role="api")

    no_oidc = production.model_copy(
        update={"oidc_issuer": None, "oidc_audience": None, "oidc_jwks_url": None}
    )
    with pytest.raises(ValueError, match="required for production API identity"):
        validate_profile_settings(no_oidc, role="api")

    missing_key = production.model_copy(update={"secret_encryption_key": None})
    with pytest.raises(ValueError, match="HAAS_SECRET_ENCRYPTION_KEY"):
        validate_profile_settings(missing_key, role="orchestrator")

    development_key = production.model_copy(
        update={
            "secret_encryption_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            "openrouter_api_key": "sk-or-v1-replace-me",
        }
    )
    with pytest.raises(ValueError, match="development key"):
        validate_profile_settings(development_key, role="orchestrator")

    flower_open = production.model_copy(
        update={"flower_enabled": True, "flower_basic_auth": ""}
    )
    with pytest.raises(ValueError, match="Flower must be disabled"):
        validate_profile_settings(flower_open, role="tools")


def test_production_api_can_pass_phase_one_configuration_gate():
    production = Settings(
        _env_file=None,
        haas_profile="prod",
        secret_encryption_key=base64.b64encode(b"x" * 32).decode(),
        oidc_issuer="https://identity.example.test/",
        oidc_audience="haas-api",
        oidc_jwks_url="https://identity.example.test/jwks",
    )
    validate_profile_settings(production, role="api")


def test_dev_profile_retains_local_defaults():
    validate_profile_settings(Settings(_env_file=None))


def test_production_compose_does_not_publish_stores_or_flower():
    root = Path(__file__).resolve().parents[1]
    base = yaml.safe_load((root / "docker-compose.yml").read_text())["services"]
    production = yaml.safe_load((root / "docker-compose.prod.yml").read_text())[
        "services"
    ]
    development = yaml.safe_load((root / "docker-compose.dev.yml").read_text())[
        "services"
    ]
    assert "ports" not in base["redis"]
    assert "ports" not in base["postgres"]
    assert "flower" not in base
    assert "flower" not in production
    assert all(
        port.startswith("127.0.0.1:")
        for service in ("redis", "postgres", "flower")
        for port in development[service].get("ports", [])
    )


def test_problem_response_uses_rfc9457_media_type():
    response = problem_response(422, "Invalid request.", instance="/v1/runs")
    assert response.media_type == "application/problem+json"
    assert json.loads(response.body)["status"] == 422


def test_prompt_limit_is_enforced_by_request_model():
    with pytest.raises(ValueError):
        CreateRunRequest(prompt="p" * (settings.max_prompt_chars + 1))


def test_http_errors_are_problem_json(monkeypatch):
    async def no_run(run_id: str):
        return None

    monkeypatch.setattr(main_module.store, "get_run", no_run)

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/v1/runs/not-found", headers={"X-Tenant-ID": "t"})

    response = asyncio.run(request())
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["detail"] == "Run not found."


def test_request_size_limit_rejects_actual_body_size():
    sent: list[dict] = []
    called = False

    async def downstream(scope, receive, send):
        nonlocal called
        called = True

    async def receive():
        return {"type": "http.request", "body": b"123456", "more_body": False}

    async def send(message):
        sent.append(message)

    middleware = RequestSizeLimitMiddleware(downstream, max_bytes=5)
    asyncio.run(
        middleware({"type": "http", "path": "/v1/runs", "headers": []}, receive, send)
    )
    assert not called
    assert sent[0]["status"] == 413
    assert (b"content-type", b"application/problem+json") in sent[0]["headers"]


def test_request_size_limit_allows_and_replays_body():
    received = bytearray()

    async def downstream(scope, receive, send):
        message = await receive()
        received.extend(message["body"])

    async def receive():
        return {"type": "http.request", "body": b"12345", "more_body": False}

    async def send(message):
        return None

    asyncio.run(
        RequestSizeLimitMiddleware(downstream, max_bytes=5)(
            {"type": "http", "path": "/", "headers": []}, receive, send
        )
    )
    assert received == b"12345"


def test_run_list_cursor_round_trip_and_rejects_invalid():
    cursor = _encode_cursor(1_725_000_000.5, "run_abc")
    assert _decode_cursor(cursor) == (1_725_000_000.5, "run_abc")
    with pytest.raises(HTTPException, match="Invalid run-list cursor"):
        _decode_cursor("invalid")


def test_scratchpad_gc_removes_only_expired_regular_text_files(tmp_path):
    stale = tmp_path / "old.txt"
    fresh = tmp_path / "fresh.txt"
    unrelated = tmp_path / "old.json"
    stale.write_text("expired")
    fresh.write_text("keep")
    unrelated.write_text("keep")
    os.utime(stale, (1, 1))
    os.utime(unrelated, (1, 1))

    assert garbage_collect_scratchpad(tmp_path, ttl_sec=100, now=1_000) == 1
    assert not stale.exists()
    assert fresh.exists() and unrelated.exists()


def test_orphan_reaper_updates_only_rows_returned_by_atomic_update():
    class Connection:
        query = ""
        params: tuple = ()

        async def fetch(self, query, *params):
            self.query = query
            self.params = params
            return [{"run_id": "run_stale", "thread_id": "thread_stale"}]

    class Pool:
        def __init__(self):
            self.connection = Connection()

        def acquire(self):
            return self

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, *args):
            return None

    async def reap():
        store = DistributedStore()
        store.pg_pool = Pool()
        return await store.reap_orphaned_runs(123.0), store.pg_pool

    reaped, pool = asyncio.run(reap())
    assert reaped == [{"run_id": "run_stale", "thread_id": "thread_stale"}]
    assert "WHERE status = 'running' AND updated_at < $1" in pool.connection.query
    assert pool.connection.params[0] == 123.0


def test_json_logs_redact_credentials():
    import logging

    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "Authorization: Bearer abcdefghijklmnop",
        (),
        None,
    )
    rendered = JsonRedactingFormatter().format(record)
    assert "[REDACTED]" in rendered
    assert "abcdefghijklmnop" not in rendered


def test_openapi_v1_snapshot_matches():
    schema = app.openapi()
    schema["paths"] = {
        path: value
        for path, value in schema["paths"].items()
        if path.startswith("/v1/")
    }
    snapshot = Path(__file__).resolve().parents[1] / "docs" / "openapi-v1.json"
    assert json.loads(snapshot.read_text()) == schema
