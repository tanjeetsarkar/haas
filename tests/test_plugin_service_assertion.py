import base64
import json
from typing import cast

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.service_assertion import sign_service_assertion, verify_service_assertion
from haas_plugin_sdk import ServiceInvocationClaims

# PyJWT 2.15.1 vector; seed 00..1f; public key 03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8.
KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
KID = "test_key_1"
HEADER = b'{"alg":"EdDSA","kid":"test_key_1","typ":"haas-service-invocation+jws"}'
PAYLOAD = (
    b'{"aud":"https://service.example:443/v1","catalog_revision":"catalog-a","content_type":"application/j'
    b'son","destination":"https://service.example:443/v1","effect_key":"effect-a","exp":1700000060,"iat":1'
    b'700000000,"invocation_id":"invoke-a","iss":"core","jti":"AAECAwQFBgcICQoLDA0ODw","method":"POST","op'
    b'eration":"read","plugin_digest":"sha256:revision","plugin_id":"fake-plugin","policy_revision":"polic'
    b'y-a","request_sha256":"AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8","run_id":"run-a","tenant_id":"te'
    b'nant-a","tool_name":"tool-a","v":1}'
)
CLAIMS = cast(ServiceInvocationClaims, json.loads(PAYLOAD))
TOKEN = (
    "eyJhbGciOiJFZERTQSIsImtpZCI6InRlc3Rfa2V5XzEiLCJ0eXAiOiJoYWFzLXNlcnZpY2UtaW52b2NhdGlvbitqd3MifQ."
    "eyJhdWQiOiJodHRwczovL3NlcnZpY2UuZXhhbXBsZTo0NDMvdjEiLCJjYXRhbG9nX3JldmlzaW9uIjoiY2F0YWxvZy1hIiwiY29u"
    "dGVudF90eXBlIjoiYXBwbGljYXRpb24vanNvbiIsImRlc3RpbmF0aW9uIjoiaHR0cHM6Ly9zZXJ2aWNlLmV4YW1wbGU6NDQzL3Yx"
    "IiwiZWZmZWN0X2tleSI6ImVmZmVjdC1hIiwiZXhwIjoxNzAwMDAwMDYwLCJpYXQiOjE3MDAwMDAwMDAsImludm9jYXRpb25faWQi"
    "OiJpbnZva2UtYSIsImlzcyI6ImNvcmUiLCJqdGkiOiJBQUVDQXdRRkJnY0lDUW9MREEwT0R3IiwibWV0aG9kIjoiUE9TVCIsIm9w"
    "ZXJhdGlvbiI6InJlYWQiLCJwbHVnaW5fZGlnZXN0Ijoic2hhMjU2OnJldmlzaW9uIiwicGx1Z2luX2lkIjoiZmFrZS1wbHVnaW4i"
    "LCJwb2xpY3lfcmV2aXNpb24iOiJwb2xpY3ktYSIsInJlcXVlc3Rfc2hhMjU2IjoiQUFFQ0F3UUZCZ2NJQ1FvTERBME9EeEFSRWhN"
    "VUZSWVhHQmthR3h3ZEhoOCIsInJ1bl9pZCI6InJ1bi1hIiwidGVuYW50X2lkIjoidGVuYW50LWEiLCJ0b29sX25hbWUiOiJ0b29s"
    "LWEiLCJ2IjoxfQ."
    "pHqtCK1ctjGwiLqqp53-UAIxy0H2Z5aqRrGAG-yJHICmmvnaltmQPeyKvuv0W-sMOP2Bx-bg1hRLFT6ScHyvCg"
)

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

def _signed(header: bytes, payload: bytes) -> str:
    body = f"{_b64(header)}.{_b64(payload)}"
    return f"{body}.{_b64(KEY.sign(body.encode()))}"

def _verify(token: str = TOKEN, **kwargs: object) -> ServiceInvocationClaims:
    options: dict[str, object] = {
        "trusted_keys": {KID: KEY.public_key()}, "expected_issuer": "core",
        "expected_audience": "https://service.example:443/v1", "now": 1700000000,
        "clock_skew": 5, **kwargs,
    }
    return verify_service_assertion(token, **options)

def test_frozen_pyjwt_vector_signs_verifies_and_interoperates() -> None:
    claims = CLAIMS
    before = dict(claims)
    assert sign_service_assertion(claims, kid=KID, private_key=KEY) == TOKEN
    result = _verify()
    assert result == claims and _verify() == claims
    keys = {KID: KEY.public_key()}
    assert verify_service_assertion(
        TOKEN, trusted_keys=keys, expected_issuer="core",
        expected_audience=claims["aud"], now=1700000000,
    ) == claims
    assert keys == {KID: KEY.public_key()}
    assert result is not claims and claims == before
    assert sign_service_assertion({**claims, "operation": "write"}, kid=KID, private_key=KEY) != TOKEN
    assert base64.urlsafe_b64decode(TOKEN.split(".")[0] + "==") == HEADER
    assert base64.urlsafe_b64decode(TOKEN.split(".")[1] + "==") == PAYLOAD
    assert f"{TOKEN.split('.')[0]}.{TOKEN.split('.')[1]}".encode() == (
        f"{_b64(HEADER)}.{_b64(PAYLOAD)}".encode()
    )
    assert jwt.decode(
        TOKEN, KEY.public_key(), algorithms=["EdDSA"], issuer="core",
        audience="https://service.example:443/v1",
        options={"verify_exp": False, "verify_iat": False},
    ) == claims
    second = Ed25519PrivateKey.from_private_bytes(bytes([7]) * 32)
    token = sign_service_assertion(claims, kid="second", private_key=second)
    keys = {KID: KEY.public_key(), "second": second.public_key()}
    assert verify_service_assertion(
        token, trusted_keys=keys, expected_issuer="core",
        expected_audience="https://service.example:443/v1", now=1700000000,
    ) == claims

@pytest.mark.parametrize(
    "bad", ["", "a.b", TOKEN + ".", TOKEN + "=", TOKEN[:-1], TOKEN[:-1] + "A",
            TOKEN.replace("eyJhbGci", "fyJhbGci", 1), TOKEN.replace("eyJhdW", "fyJhdW", 1),
            TOKEN.replace(".", ".!", 1), f"{TOKEN.rsplit('.', 1)[0]}.{TOKEN.rsplit('.', 1)[1][:-2]}",
            "é", "x" * 16385],
)
def test_compact_encoding_and_size_rejections(bad: str) -> None:
    with pytest.raises(ValueError) as error:
        _verify(bad)
    assert TOKEN not in str(error.value) and error.value.__cause__ is None

@pytest.mark.parametrize(
    ("header", "payload"),
    [
        *((HEADER.replace(b"EdDSA", alg.encode()), PAYLOAD) for alg in ("none", "HS256", "RS256")),
        (b'{"alg":"EdDSA","kid":"test_key_1","typ":"wrong"}', PAYLOAD),
        (b'{"alg":"EdDSA","kid":"test_key_1","typ":"haas-service-invocation+jws","crit":[],"jku":"https://x"}', PAYLOAD),
        (b'{"alg":"EdDSA","typ":"haas-service-invocation+jws"}', PAYLOAD),
        (HEADER.replace(b"test_key_1", b"x" * 129), PAYLOAD),
        (b'{"alg":"EdDSA","kid":"test_key_1","kid":"test_key_1","typ":"haas-service-invocation+jws"}', PAYLOAD),
        (HEADER.replace(b",", b", "), PAYLOAD),
        (HEADER, PAYLOAD.replace(b',"iss"', b', "iss"')),
        (HEADER, b'{"v":1,"v":1}'),
        (HEADER, b"\xff"),
    ],
)
def test_signed_nonprofile_or_noncanonical_json_is_rejected(header: bytes, payload: bytes) -> None:
    with pytest.raises(ValueError):
        _verify(_signed(header, payload))

@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("v", True), ("v", 2), ("iat", True), *(("exp", x) for x in (1700000061, "1700000060")),
        ("aud", ["https://service.example:443/v1"]), ("iss", ""),
        ("plugin_id", "x" * 2049), ("iat", -1), ("exp", 1700000000),
        ("jti", "bad"), ("request_sha256", "bad"), ("method", "post"), ("destination", "https://service.example/v1"),
    ],
)
def test_signed_invalid_claims_and_lifetimes_fail(name: str, value: object) -> None:
    claims = {**CLAIMS, name: value}
    with pytest.raises(ValueError):
        _verify(_signed(HEADER, json.dumps(claims, separators=(",", ":")).encode()))
    for invalid in ({**CLAIMS, "extra": "x"}, {k: v for k, v in CLAIMS.items() if k != "tenant_id"}):
        with pytest.raises(ValueError):
            _verify(_signed(HEADER, json.dumps(invalid, separators=(",", ":")).encode()))

def test_identity_time_key_and_signature_fail_closed() -> None:
    claims = CLAIMS
    wrong = Ed25519PrivateKey.from_private_bytes(bytes([1]) * 32)
    for options in [
        {"trusted_keys": {}}, {"trusted_keys": {"TEST_KEY_1": KEY.public_key()}},
        {"trusted_keys": {"bad kid": KEY.public_key()}},
        {"trusted_keys": {KID: Ed25519PrivateKey.from_private_bytes(bytes(32))}},
        {"trusted_keys": {KID: wrong.public_key()}}, {"expected_issuer": "other"},
        {"expected_audience": "other"}, {"now": 1700000065, "clock_skew": 0},
        {"now": 1700000065, "clock_skew": 5}, {"now": 1699999994, "clock_skew": 5},
    ]:
        with pytest.raises(ValueError):
            _verify(**options)
    assert _verify(now=1700000064, clock_skew=5) == claims
    assert _verify(now=1699999995, clock_skew=5) == claims
    for options in ({"now": True}, {"now": -1}, {"clock_skew": True},
                    {"clock_skew": 6}, {"expected_issuer": ""}, {"expected_audience": ""}):
        with pytest.raises(ValueError):
            _verify(**options)
    for kid in ("", "bad kid", "x" * 129):
        with pytest.raises(ValueError):
            sign_service_assertion(claims, kid=kid, private_key=KEY)
    with pytest.raises(ValueError):
        sign_service_assertion(claims, kid=KID, private_key=KEY.public_key())  # type: ignore[arg-type]
