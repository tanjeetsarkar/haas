"""Published contracts (docs/contracts) stay valid and match what the producers emit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.output_stream import OUTPUT_DELTA_SCHEMA, DeltaPublisher, decode_delta
from app.core.workflow import RUN_ERROR_CODES, TERMINAL_EVENTS, WEBHOOK_SCHEMA

from contract_support import CONTRACTS, event_violations, load_contract
from test_output_deltas import RecordingRedis

jsonschema = pytest.importorskip("jsonschema")
ROOT = Path(__file__).resolve().parent.parent


def _validator(name: str):
    return jsonschema.Draft202012Validator(
        load_contract(name), format_checker=jsonschema.FormatChecker()
    )


def test_contract_documents_are_valid_json_schema():
    names = sorted(p.name for p in CONTRACTS.glob("*.schema.json"))
    assert names == [
        "run-events.v1.schema.json",
        "run-output-delta.v1.schema.json",
        "run-terminal-webhook.v1.schema.json",
    ]
    for name in names:
        jsonschema.Draft202012Validator.check_schema(
            json.loads((CONTRACTS / name).read_text())
        )


def test_error_codes_and_terminal_types_match_the_code():
    events = load_contract("run-events.v1")
    assert (
        set(events["$defs"]["run_error"]["properties"]["code"]["enum"])
        == RUN_ERROR_CODES
    )
    assert set(events["x-terminal-types"]) == set(TERMINAL_EVENTS)
    assert set(events["properties"]["type"]["enum"]) == set(events["$defs"])
    webhook = load_contract("run-terminal-webhook.v1")
    assert set(webhook["properties"]["code"]["enum"]) == RUN_ERROR_CODES
    assert webhook["properties"]["schema"]["const"] == WEBHOOK_SCHEMA
    delta = load_contract("run-output-delta.v1")
    assert delta["properties"]["schema"]["const"] == OUTPUT_DELTA_SCHEMA


def test_event_envelope_and_drift_detection():
    validator = _validator("run-events.v1")
    envelope = {
        "sequence": 1,
        "event_id": "7d0c8f2e-8a64-4b44-9a43-0f3b0f6d2b10",
        "schema_version": 1,
        "type": "run_error",
        "run_id": "run_1",
        "occurred_at": "2025-01-01T00:00:00+00:00",
        "data": {"error": "Run deadline exceeded.", "code": "budget.deadline"},
    }
    assert not list(validator.iter_errors(envelope))
    bad = {**envelope, "data": {"error": "x", "code": "made.up"}}
    assert list(validator.iter_errors(bad))
    assert event_violations("run_completed", {"output": "x", "secret_field": 1}) == [
        "run_completed: undeclared fields ['secret_field']"
    ]
    assert event_violations("brand_new", {}) == [
        "brand_new: event type is not declared in run-events.v1"
    ]


async def test_output_delta_frames_conform():
    redis = RecordingRedis()
    pub = DeltaPublisher(
        redis,
        "run_1",
        step=1,
        attempt=2,
        call=1,
        flush_chars=3,
        flush_ms=60_000,
        max_chars=8,
        buffer=50,
    )
    for text in ("hello ", "world", "!!!"):
        await pub.push(text)
    await pub.flush(final=True)
    frames = [decode_delta(fields) for _, fields, _ in redis.entries]
    validator = _validator("run-output-delta.v1")
    assert frames and all(f is not None for f in frames)
    for frame in frames:
        assert not list(validator.iter_errors(frame)), frame
    assert frames[-1]["final"] and frames[-1]["truncated"]


def test_asyncapi_and_spectral_reference_the_contracts():
    asyncapi = (ROOT / "docs" / "asyncapi.yaml").read_text()
    for name in ("run-events.v1", "run-output-delta.v1", "run-terminal-webhook.v1"):
        assert f"contracts/{name}.schema.json" in asyncapi
    assert (ROOT / ".spectral.yaml").exists()


async def test_deprecation_headers_and_openapi_flag():
    from datetime import UTC, datetime

    import httpx
    from fastapi import FastAPI

    from app.core.deprecation import RouteDeprecation, deprecated_route

    since = datetime(2026, 1, 1, tzinfo=UTC)
    dep = RouteDeprecation(
        since=since, sunset=datetime(2026, 7, 1, tzinfo=UTC), successor="/v1/new"
    )
    with pytest.raises(ValueError, match="180 days"):
        RouteDeprecation(since=since, sunset=datetime(2026, 2, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="timezone"):
        RouteDeprecation(since=datetime(2026, 1, 1))
    app = FastAPI()

    @app.get("/v1/old", deprecated=True, dependencies=[deprecated_route(dep)])
    async def old() -> dict[str, str]:
        return {"ok": "yes"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://qa"
    ) as client:
        resp = await client.get("/v1/old")
    assert resp.json() == {"ok": "yes"}
    assert resp.headers["deprecation"] == f"@{int(since.timestamp())}"
    assert resp.headers["sunset"] == "Wed, 01 Jul 2026 00:00:00 GMT"
    assert 'rel="deprecation"' in resp.headers["link"]
    assert '</v1/new>; rel="successor-version"' in resp.headers["link"]
    assert app.openapi()["paths"]["/v1/old"]["get"]["deprecated"] is True
