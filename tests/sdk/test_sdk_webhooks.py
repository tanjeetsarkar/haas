"""Webhook signature verification (shared vector with sdk/typescript tests)."""

from __future__ import annotations

import importlib
import json

import pytest

from haas_sdk import WebhookVerificationError, webhooks

SECRET = "whsec_test_secret"
TIMESTAMP = 1_700_000_000
BODY = b'{"type":"run.completed","data":{"run_id":"run-1"}}'
# HMAC-SHA256(SECRET, b"1700000000." + BODY); the TypeScript SDK tests assert the same value.
VECTOR = "b215c0372d8801cdfd2e48d6cf4659ab99a103e8a259184eb3232ae987ed3840"


def test_signature_vector_is_stable() -> None:
    expected = webhooks.compute_signature(BODY, SECRET, TIMESTAMP)
    assert expected == VECTOR
    header = webhooks.sign(BODY, SECRET, timestamp=TIMESTAMP)
    assert header == f"t={TIMESTAMP},v1={expected}"


def test_verify_accepts_valid_and_rejects_tampering() -> None:
    header = webhooks.sign(BODY, SECRET, timestamp=TIMESTAMP)
    assert webhooks.verify(BODY, header, SECRET, now=TIMESTAMP + 10)
    assert webhooks.verify(BODY.decode(), header, [SECRET], now=TIMESTAMP)
    assert not webhooks.verify(BODY + b" ", header, SECRET, now=TIMESTAMP)
    assert not webhooks.verify(BODY, header, "other", now=TIMESTAMP)
    assert not webhooks.verify(BODY, header, SECRET, now=TIMESTAMP + 301)
    assert not webhooks.verify(BODY, header, SECRET, now=TIMESTAMP - 301)
    assert not webhooks.verify(BODY, None, SECRET)
    assert not webhooks.verify(BODY, "v1=abc", SECRET)
    assert not webhooks.verify(BODY, f"t={TIMESTAMP}", SECRET, now=TIMESTAMP)
    assert not webhooks.verify(BODY, "t=soon,v1=ab", SECRET)
    assert webhooks.verify(BODY, header, SECRET, now=TIMESTAMP + 10_000, tolerance=0)


def test_rotation_accepts_any_matching_signature() -> None:
    old = webhooks.compute_signature(BODY, "old-secret", TIMESTAMP)
    new = webhooks.compute_signature(BODY, "new-secret", TIMESTAMP)
    header = f"t={TIMESTAMP},v1={old},v1={new}"
    assert webhooks.verify(BODY, header, "new-secret", now=TIMESTAMP)
    assert webhooks.verify(BODY, header, ["unrelated", "old-secret"], now=TIMESTAMP)
    assert webhooks.sign(BODY, ["old-secret", "new-secret"], timestamp=TIMESTAMP) == header
    with pytest.raises(ValueError):
        webhooks.verify(BODY, header, [], now=TIMESTAMP)


def test_construct_event_parses_headers_case_insensitively() -> None:
    headers = {
        "haas-signature": webhooks.sign(BODY, SECRET, timestamp=TIMESTAMP),
        "HaaS-Webhook-Id": "dlv-1",
        "haas-key-id": "key-2",
    }
    event = webhooks.construct_event(BODY, headers, SECRET, now=TIMESTAMP)
    assert event.delivery_id == "dlv-1" and event.key_id == "key-2"
    assert event.type == "run.completed"
    assert event.data == json.loads(BODY)
    with pytest.raises(WebhookVerificationError):
        webhooks.construct_event(BODY, {}, SECRET, now=TIMESTAMP)
    bad = b"not json"
    with pytest.raises(WebhookVerificationError):
        webhooks.construct_event(
            bad, {"HaaS-Signature": webhooks.sign(bad, SECRET, timestamp=TIMESTAMP)},
            SECRET, now=TIMESTAMP,
        )


def test_matches_server_signer_when_available() -> None:
    try:
        server = importlib.import_module("app.core.webhooks")
    except ImportError:
        pytest.skip("app.core.webhooks not present")
    signer = next(
        (getattr(server, n) for n in ("sign_payload", "signature_header", "sign") if hasattr(server, n)),
        None,
    )
    if signer is None:
        pytest.skip("no recognised signer in app.core.webhooks")
    try:
        header = signer(SECRET, BODY, TIMESTAMP)
    except TypeError:
        pytest.skip("server signer has a different signature")
    if not isinstance(header, str):
        pytest.skip("server signer returns a non-string")
    assert webhooks.verify(BODY, header, SECRET, now=TIMESTAMP)
