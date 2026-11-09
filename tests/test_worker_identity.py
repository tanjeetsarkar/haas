import asyncio
import json
import time
from unittest.mock import AsyncMock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, PublicFormat,
)

from app.core.config import Settings
from app.core.worker_identity import (
    AUDIENCE, ISSUER, METHOD, PATH, SUBJECT, WorkerIdentityError,
    authenticate_worker_request, sign_worker_request,
)


@pytest.fixture
def auth(tmp_path):
    private = Ed25519PrivateKey.generate()
    keyfile, trustfile = tmp_path / "key.pem", tmp_path / "trust.json"
    keyfile.write_bytes(private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    keyfile.chmod(0o600)
    entry = {"subject": SUBJECT, "public_key_pem": private.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()}
    trustfile.write_text(json.dumps({"keys": {"key-one": entry}}))
    config = Settings(_env_file=None, worker_auth_private_key_file=keyfile,
                      worker_auth_trust_keys_file=trustfile, worker_auth_key_id="key-one")
    body = {"request_id": "request-0123456789", "operation": "pre_grant"}
    token = sign_worker_request(config, body)
    claims = jwt.decode(token, options={"verify_signature": False})
    replay = AsyncMock()
    replay.set.return_value = True
    return config, body, token, claims, private, replay, entry


async def test_roundtrip_canonical_binding_and_replay(auth):
    config, body, token, _, _, replay, _ = auth
    assert await authenticate_worker_request(config, replay, token, dict(reversed(list(body.items())))) == SUBJECT
    assert replay.set.call_args.kwargs == {"nx": True, "ex": 60}
    replay.set.return_value = False
    with pytest.raises(WorkerIdentityError):
        await authenticate_worker_request(config, replay, token, body)
    replay.set.side_effect = ConnectionError("private-infrastructure")
    with pytest.raises(WorkerIdentityError, match="^worker_auth.denied$"):
        await authenticate_worker_request(config, replay, token, body)


@pytest.mark.parametrize("changes", [
    {"iss": "tenant"}, {"sub": "other-service"}, {"aud": "public-api"},
    {"aud": [AUDIENCE]}, {"jti": "different"}, {"method": "GET"}, {"path": "/v1/runs"},
    {"body_sha256": "0" * 64}, {"iat": True}, {"exp": False}, {"iat": "1"},
    {"exp": 1.5}, {"iat": None}, {"exp": None},
    {"iat": 0, "exp": 20}, {"iat": 0, "exp": 0},
    {"iat_offset": 7, "exp_offset": 27},
    {"iat_offset": 0, "exp_offset": 21},
])
async def test_signed_claim_mutations_deny_before_replay(auth, changes):
    config, body, _, claims, private, replay, _ = auth
    changes = {key.removesuffix("_offset"): int(time.time()) + value
               if key.endswith("_offset") else value for key, value in changes.items()}
    token = jwt.encode({**claims, **changes}, private, algorithm="EdDSA", headers={"kid": "key-one"})
    with pytest.raises(WorkerIdentityError, match="^worker_auth.denied$"):
        await authenticate_worker_request(config, replay, token, body)
    replay.set.assert_not_awaited()


@pytest.mark.parametrize("case", ["missing", "unknown", "none", "hmac", "tamper", "expiry",
                                  "wrong_body", "wrong_path", "wrong_method", "bad_request_id"])
async def test_invalid_requests_never_touch_replay(auth, case):
    config, body, token, claims, private, replay, _ = auth
    kwargs = {}
    if case == "missing":
        claims.pop("exp")
    elif case == "unknown":
        token = jwt.encode(claims, private, algorithm="EdDSA", headers={"kid": "unknown"})
    elif case in ("none", "hmac"):
        token = jwt.encode(claims, "" if case == "none" else "synthetic-hmac-secret-long-enough",
                           algorithm="none" if case == "none" else "HS256", headers={"kid": "key-one"})
    elif case == "tamper":
        token += "invalid"
    elif case == "expiry":
        claims.update(iat=int(time.time()) - 30, exp=int(time.time()) - 10)
    elif case == "wrong_body":
        body = {**body, "operation": "final_entry"}
    elif case == "bad_request_id":
        body = {**body, "request_id": ""}
    else:
        kwargs["method" if case == "wrong_method" else "path"] = "wrong"
    if case in ("missing", "expiry"):
        token = jwt.encode(claims, private, algorithm="EdDSA", headers={"kid": "key-one"})
    with pytest.raises(WorkerIdentityError):
        await authenticate_worker_request(config, replay, token, body, **kwargs)
    replay.set.assert_not_awaited()


async def test_rotation_revocation_and_no_user_or_dev_fallback(auth):
    config, body, token, _, _, replay, entry = auth
    config.worker_auth_trust_keys_file.write_text(json.dumps({"keys": {"new-key": entry}}))
    with pytest.raises(WorkerIdentityError):
        await authenticate_worker_request(config, replay, token, body)
    config.worker_auth_key_id = "new-key"
    new = sign_worker_request(config, body)
    assert await authenticate_worker_request(config, replay, new, body) == SUBJECT
    for trust in ('{"keys":{}}', '{"keys":{},"keys":{}}', "not-json"):
        config.worker_auth_trust_keys_file.write_text(trust)
        with pytest.raises(WorkerIdentityError):
            await authenticate_worker_request(config, replay, new, body)
    with pytest.raises(WorkerIdentityError):
        await authenticate_worker_request(Settings(_env_file=None), replay, token, body)
    with pytest.raises(WorkerIdentityError):
        sign_worker_request(Settings(_env_file=None), body)


def test_secret_file_handling_and_settings_names(auth, monkeypatch):
    config, body, *_ = auth
    config.worker_auth_private_key_file.chmod(0o644)
    with pytest.raises(WorkerIdentityError, match="^worker_auth.unavailable$"):
        sign_worker_request(config, body)
    for name in ("HAAS_INTERNAL_AUTHORIZATION_URL", "HAAS_WORKER_AUTH_PRIVATE_KEY_FILE",
                 "HAAS_WORKER_AUTH_KEY_ID", "HAAS_WORKER_AUTH_TRUST_KEYS_FILE",
                 "HAAS_INTERNAL_AUTHORIZATION_CA_FILE"):
        monkeypatch.setenv(name, "synthetic")
    settings = Settings(_env_file=None)
    assert settings.worker_auth_key_id == "synthetic"
    assert settings.internal_authorization_url == "synthetic"
    assert "private_key_file" not in repr(settings)
    assert settings.worker_auth_private_key_file == settings.internal_authorization_ca_file
    assert (ISSUER, METHOD, PATH) == ("haas:tool-worker", "POST", "/internal/v1/run-authorizations/check")


async def test_two_replicas_one_request_id_and_rotation_share_replay_domain(auth):
    config, body, token, _, _, _, entry = auth
    seen = set()

    async def atomic_set(name, value, **kwargs):
        if name in seen:
            return False
        seen.add(name)
        return True

    replay = AsyncMock()
    replay.set.side_effect = atomic_set
    results = await asyncio.gather(*[
        authenticate_worker_request(config, replay, token, body) for _ in range(2)
    ], return_exceptions=True)
    assert results.count(SUBJECT) == 1
    assert sum(isinstance(result, WorkerIdentityError) for result in results) == 1
    config.worker_auth_trust_keys_file.write_text(json.dumps({"keys": {"rotated": entry}}))
    config.worker_auth_key_id = "rotated"
    with pytest.raises(WorkerIdentityError):
        await authenticate_worker_request(config, replay, sign_worker_request(config, body), body)


@pytest.mark.parametrize("kind", ["wrong_subject", "wrong_key", "writable", "missing", "directory"])
async def test_untrusted_or_unavailable_key_files_deny_without_replay(auth, kind):
    config, body, token, _, _, replay, entry = auth
    path = config.worker_auth_trust_keys_file
    if kind == "wrong_subject":
        entry["subject"] = "other-service"
    elif kind == "wrong_key":
        entry["public_key_pem"] = "malformed"
    path.write_text(json.dumps({"keys": {"key-one": entry}}))
    if kind == "writable":
        path.chmod(0o666)
    elif kind == "missing":
        path.unlink()
    elif kind == "directory":
        config.worker_auth_trust_keys_file = path.parent
    with pytest.raises(WorkerIdentityError, match="^worker_auth.denied$"):
        await authenticate_worker_request(config, replay, token, body)
    replay.set.assert_not_awaited()
