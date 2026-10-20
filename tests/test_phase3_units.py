"""Phase 3 units that need no database: envelopes, outbox routing, model calls, digests."""

from __future__ import annotations

import base64
from decimal import Decimal
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from app.core.config import Settings, validate_profile_settings
from app.core.envelope import (
    DEV_SIGNING_SEED_B64,
    EnvelopeError,
    arguments_digest,
    dev_verify_key_b64,
    generate_keypair,
    sign_envelope,
    verify_envelope,
)
from app.core.llm import CompletionResult
from app.core.workflow import batch_digest, effect_key
from app.worker import tasks


def _keypair() -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(private).decode(), base64.b64encode(public).decode()


def _body(**overrides: Any) -> dict[str, Any]:
    call = {"id": "c1", "name": "echo", "arguments": '{"x": "1"}'}
    body = {
        "invocation_id": "inv-1",
        "tool_call": call,
        "arguments_digest": arguments_digest(call["name"], call["arguments"]),
        "expires_at": 4_000_000_000.0,
    }
    body.update(overrides)
    return body


def test_envelope_round_trip_with_configured_keys():
    signing, verify = _keypair()
    config = Settings(
        haas_profile="prod",
        envelope_signing_key=signing,
        envelope_verify_key=verify,
    )
    envelope = sign_envelope(_body(), config)
    assert verify_envelope(envelope, config)["invocation_id"] == "inv-1"


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda e: e["body"].update(invocation_id="inv-2"), "signature is invalid"),
        (lambda e: e.update(signature="!!"), "Malformed"),
        (lambda e: e.pop("body"), "Malformed"),
        (lambda e: e["body"].update(v=99), "version"),
    ],
)
def test_envelope_tampering_is_rejected(mutate, message):
    config = Settings(haas_profile="dev")
    envelope = sign_envelope(_body(), config)
    mutate(envelope)
    with pytest.raises(EnvelopeError, match=message):
        verify_envelope(envelope, config)


def test_envelope_expiry_digest_and_foreign_key():
    config = Settings(haas_profile="dev")
    expired = sign_envelope(_body(expires_at=10.0), config)
    with pytest.raises(EnvelopeError, match="expired"):
        verify_envelope(expired, config)
    mismatched = sign_envelope(_body(arguments_digest="0" * 64), config)
    with pytest.raises(EnvelopeError, match="digest"):
        verify_envelope(mismatched, config)
    _, other_public = _keypair()
    foreign = Settings(haas_profile="dev", envelope_verify_key=other_public)
    with pytest.raises(EnvelopeError, match="signature is invalid"):
        verify_envelope(sign_envelope(_body(), config), foreign)


def test_envelope_keys_are_required_outside_dev():
    config = Settings(haas_profile="prod")
    with pytest.raises(EnvelopeError, match="SIGNING_KEY is not configured"):
        sign_envelope(_body(), config)
    with pytest.raises(EnvelopeError, match="VERIFY_KEY is not configured"):
        verify_envelope({"body": {"v": 1}, "signature": ""}, config)
    with pytest.raises(EnvelopeError, match="32 bytes"):
        sign_envelope(
            _body(), Settings(haas_profile="prod", envelope_signing_key="YQ==")
        )


def test_production_profile_rejects_missing_or_dev_envelope_keys():
    signing, verify = _keypair()
    base = {
        "haas_profile": "prod",
        "openrouter_api_key": "sk-or-v1-real",
        "flower_enabled": False,
    }
    for role, overrides, message in (
        ("orchestrator", {}, "HAAS_ENVELOPE_SIGNING_KEY is required"),
        (
            "orchestrator",
            {"envelope_signing_key": DEV_SIGNING_SEED_B64},
            "must not use the development key",
        ),
        ("tools", {}, "HAAS_ENVELOPE_VERIFY_KEY is required"),
        (
            "tools",
            {"envelope_verify_key": dev_verify_key_b64()},
            "must not use the development key",
        ),
    ):
        with pytest.raises(ValueError, match=message):
            validate_profile_settings(Settings(**base, **overrides), role=role)
    for role, overrides in (
        ("orchestrator", {"envelope_signing_key": signing}),
        ("tools", {"envelope_verify_key": verify}),
    ):
        try:
            validate_profile_settings(Settings(**base, **overrides), role=role)
        except ValueError as exc:
            assert "ENVELOPE" not in str(exc)


def test_batch_digest_and_effect_key_are_stable_and_binding():
    calls = [{"id": "a", "function": {"name": "t", "arguments": '{"x":1}'}}]
    assert batch_digest("r", 1, calls) == batch_digest("r", 1, [dict(calls[0])])
    assert batch_digest("r", 1, calls) != batch_digest("r", 2, calls)
    changed = [{"id": "a", "function": {"name": "t", "arguments": '{"x":2}'}}]
    assert batch_digest("r", 1, calls) != batch_digest("r", 1, changed)
    key = effect_key("tenant", "r", 1, 0, "a")
    assert key == effect_key("tenant", "r", 1, 0, "a")
    assert key != effect_key("tenant", "r", 1, 1, "a")
    assert key != effect_key("other", "r", 1, 0, "a")


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def apply_async(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


async def test_outbox_publisher_routes_each_destination(monkeypatch):
    orchestrate, execute, webhook = _Recorder(), _Recorder(), _Recorder()
    monkeypatch.setattr(tasks, "orchestrate_task", orchestrate)
    monkeypatch.setattr(tasks, "execute_tool_task", execute)
    monkeypatch.setattr(tasks, "deliver_webhook_task", webhook)

    async def _noop(*args, **kwargs):
        return None

    class Store:
        rds = type("FakeRedis", (), {"set": _noop, "expire": _noop, "hset": _noop})()

        async def get_scoped_secrets(self, run_id: str, keys: set[str]):
            return {k: f"{run_id}:{k}" for k in keys}

        async def resolve_secret_bindings(self, tenant_id, run_id, names, *, plugin_id=None, tool_name=None):
            return {}

        async def record_credential_grants(self, tenant_id, run_id, rows):
            pass

        async def egress_policy_for_run(self, tenant_id, run_id):
            from app.core.egress import EgressPolicy
            return EgressPolicy()

    publish = tasks.make_outbox_publisher(Store())  # type: ignore[arg-type]
    await publish(
        {
            "message_id": "m1",
            "run_id": "r1",
            "tenant_id": "tenant-1",
            "destination": "orchestrator",
            "payload": {"run_id": "r1", "action": "step", "expected_state_version": 3},
        }
    )
    await publish(
        {
            "message_id": "m2",
            "run_id": "r1",
            "tenant_id": "tenant-1",
            "destination": "tool",
            "payload": {
                "envelope": {"body": {"invocation_id": "inv-1", "tool_call": {"name": "bash"}}},
                "queue": "q_tools_sandbox",
                "secret_keys": ["GH"],
            },
        }
    )
    await publish(
        {
            "message_id": "m3",
            "run_id": "r1",
            "tenant_id": "tenant-1",
            "destination": "webhook",
            "payload": {"url": "https://hook.example", "body": {"ok": True}},
        }
    )
    assert orchestrate.calls == [
        {
            "kwargs": {
                "message_id": "m1",
                "run_id": "r1",
                "action": "step",
                "expected_state_version": 3,
            },
            "queue": "q_orchestrator",
            "task_id": "m1",
        }
    ]
    assert execute.calls[0]["kwargs"]["envelope"] == {
        "body": {"invocation_id": "inv-1", "tool_call": {"name": "bash"}}
    }
    assert isinstance(execute.calls[0]["kwargs"]["credential_grants"], list)
    assert execute.calls[0]["kwargs"]["egress_policy"] is not None
    assert execute.calls[0]["queue"] == "q_tools_sandbox"
    assert execute.calls[0]["task_id"] == "m2"
    assert webhook.calls[0]["args"] == ["https://hook.example", {"ok": True}]
    with pytest.raises(ValueError, match="Unknown outbox destination"):
        await publish(
            {"message_id": "m4", "run_id": "r1", "destination": "x", "payload": {}}
        )


def test_step_cost_estimate_uses_pricing_or_default(monkeypatch):
    monkeypatch.setattr(tasks.settings, "default_step_reservation_usd", 0.001)
    monkeypatch.setattr(tasks.settings, "model_pricing_json", "")
    assert tasks.estimate_step_cost("m", [], []) == Decimal("0.001")
    monkeypatch.setattr(
        tasks.settings,
        "model_pricing_json",
        '{"m": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}}',
    )
    estimate = tasks.estimate_step_cost(
        "m", [{"role": "user", "content": "x" * 300}], []
    )
    assert estimate > Decimal(0)
    assert estimate >= Decimal(str(tasks.settings.reservation_output_tokens * 2 / 1e6))


async def test_model_call_falls_back_and_retries(monkeypatch):
    calls: list[str] = []
    result = CompletionResult({"role": "assistant", "content": "ok"}, "p", 0.0, 1, 0)

    def status_error(code: int) -> httpx.HTTPStatusError:
        request = httpx.Request("POST", "https://llm.invalid")
        return httpx.HTTPStatusError(
            "x", request=request, response=httpx.Response(code, request=request)
        )

    outcomes: list[Any] = [status_error(400), status_error(503), result]

    async def chat_completion(_client, *, model: str, **_kwargs):
        calls.append(model)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def no_sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(tasks.gateway, "chat_completion", chat_completion)
    monkeypatch.setattr(tasks.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(tasks.settings, "model_call_attempts", 3)
    run = {"primary_model": "primary", "traceparent": "tp"}
    assert await tasks._call_model(run, "planner", [], []) is result
    assert calls == ["planner", "primary", "primary"]

    outcomes[:] = [status_error(401)]
    with pytest.raises(httpx.HTTPStatusError):
        await tasks._call_model(run, "primary", [], [])


def test_generated_keypair_round_trips_and_rejects_foreign_signer() -> None:
    signing, verify = generate_keypair()
    config = Settings(envelope_signing_key=signing, envelope_verify_key=verify)
    body = {
        "tool_call": {"name": "t", "arguments": "{}"},
        "arguments_digest": arguments_digest("t", "{}"),
        "expires_at": 9_999_999_999,
    }
    assert verify_envelope(sign_envelope(body, config), config)["v"] == 1
    other_signing, _ = generate_keypair()
    forged = sign_envelope(body, Settings(envelope_signing_key=other_signing))
    with pytest.raises(EnvelopeError):
        verify_envelope(forged, config)
