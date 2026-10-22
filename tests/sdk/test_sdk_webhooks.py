"""Webhook signature verification (shared vector with the TypeScript SDK tests)."""

from __future__ import annotations

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


def test_rotation_accepts_any_candidate_secret_or_v1() -> None:
    old_header = webhooks.sign(BODY, SECRET, timestamp=TIMESTAMP)
    new_secret = "whsec_new"
    # Header carrying both an invalid and a valid v1 (rotation window).
    mixed_header = (
        f"t={TIMESTAMP},v1=deadbeef,"
        f"v1={webhooks.compute_signature(BODY, new_secret, TIMESTAMP)}"
    )
    # Any configured secret may match any v1 candidate.
    assert webhooks.verify(BODY, mixed_header, [new_secret, SECRET], now=TIMESTAMP)
    assert webhooks.verify(BODY, old_header, [new_secret, SECRET], now=TIMESTAMP)
    # A header signed by a secret that is not configured must fail.
    assert not webhooks.verify(BODY, mixed_header, SECRET, now=TIMESTAMP)


def test_construct_event_reads_type_or_event() -> None:
    header = webhooks.sign(BODY, SECRET, timestamp=TIMESTAMP)
    event = webhooks.construct_event(
        BODY,
        {"HaaS-Signature": header, "HaaS-Webhook-Id": "d1", "HaaS-Key-Id": "k1"},
        SECRET,
        now=TIMESTAMP,
    )
    assert event.type == "run.completed"
    assert event.delivery_id == "d1" and event.key_id == "k1"
    assert event.data["data"]["run_id"] == "run-1"

    alt = json.dumps({"event": "run.failed", "run_id": "r2"}).encode()
    alt_header = webhooks.sign(alt, SECRET, timestamp=TIMESTAMP)
    event2 = webhooks.construct_event(
        alt, {"HaaS-Signature": alt_header}, SECRET, now=TIMESTAMP
    )
    assert event2.type == "run.failed"


def test_construct_event_rejects_bad_json() -> None:
    header = webhooks.sign(b"not json", SECRET, timestamp=TIMESTAMP)
    with pytest.raises(WebhookVerificationError, match="JSON"):
        webhooks.construct_event(
            b"not json", {"HaaS-Signature": header}, SECRET, now=TIMESTAMP
        )


def test_verify_signature_raises_with_timestamp() -> None:
    header = webhooks.sign(BODY, SECRET, timestamp=TIMESTAMP)
    assert webhooks.verify_signature(BODY, header, SECRET, now=TIMESTAMP) == TIMESTAMP
    with pytest.raises(WebhookVerificationError):
        webhooks.verify_signature(BODY, header, SECRET, now=TIMESTAMP + 9999)
