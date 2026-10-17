from __future__ import annotations

import base64
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.config import Settings, validate_profile_settings
from app.core.identity import (
    IdentityAuthenticator,
    IdentityError,
    IdentityMiddleware,
    IdentityUnavailable,
    OIDCVerifier,
)
from app.core.store import validate_tenant_transition

ISSUER = "https://identity.example.test/"
AUDIENCE = "haas-api"
TENANT = {
    "tenant_id": "f0a96551-898d-4e0d-985d-0175ce44bcfd",
    "tenant_slug": "acme",
    "principal_id": "75850cfb-1874-480b-b35e-75a3cb999139",
    "issuer": ISSUER,
    "subject": "user-123",
    "roles": ["tenant_admin"],
    "credential_valid_after": None,
    "tenant_credential_valid_after": None,
}


class MemoryIdentityStore:
    def __init__(self) -> None:
        self.membership = dict(TENANT)
        self.api_key: str | None = None

    async def resolve_oidc_membership(
        self, issuer: str, subject: str, tenant_selector: str
    ) -> dict[str, Any] | None:
        if (
            issuer != self.membership["issuer"]
            or subject != self.membership["subject"]
            or tenant_selector
            not in (self.membership["tenant_slug"], self.membership["tenant_id"])
        ):
            return None
        return dict(self.membership)

    async def resolve_api_key(self, credential: str) -> dict[str, Any] | None:
        if credential != self.api_key:
            return None
        return dict(self.membership)


def _b64url(value: int) -> str:
    size = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(size, "big")).decode().rstrip("=")


def _key_pair(kid: str) -> tuple[Any, dict[str, str]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64url(numbers.n),
        "e": _b64url(numbers.e),
    }
    return private_key, jwk


def _token(
    private_key: Any,
    kid: str,
    *,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    issued_at: int | None = None,
    expires_at: int | None = None,
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": issuer,
            "sub": "user-123",
            "aud": audience,
            "iat": issued_at if issued_at is not None else now,
            "exp": expires_at if expires_at is not None else now + 300,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": kid},
    )


def _settings(**values: Any) -> Settings:
    return Settings(
        _env_file=None,
        haas_profile="prod",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_jwks_url="https://identity.example.test/jwks",
        **values,
    )


def test_identity_settings_load_documented_environment_variables(monkeypatch):
    expected = {
        "HAAS_OIDC_ISSUER": ISSUER,
        "HAAS_OIDC_AUDIENCE": AUDIENCE,
        "HAAS_OIDC_JWKS_URL": "https://identity.example.test/jwks",
        "HAAS_BOOTSTRAP_TENANT_SLUG": "acme",
        "HAAS_BOOTSTRAP_ADMIN_ISSUER": ISSUER,
        "HAAS_BOOTSTRAP_ADMIN_SUBJECT": "user-123",
    }
    for name, value in expected.items():
        monkeypatch.setenv(name, value)

    config = Settings(_env_file=None)

    assert config.oidc_issuer == ISSUER
    assert config.oidc_audience == AUDIENCE
    assert config.oidc_jwks_url == expected["HAAS_OIDC_JWKS_URL"]
    assert config.bootstrap_tenant_slug == "acme"
    assert config.bootstrap_admin_issuer == ISSUER
    assert config.bootstrap_admin_subject == "user-123"


def test_tenant_lifecycle_enforces_qualification_reauthorization_and_deletion_gates():
    with pytest.raises(ValueError, match="completed qualification"):
        validate_tenant_transition("provisioning", "active")
    validate_tenant_transition("provisioning", "active", qualification_passed=True)

    with pytest.raises(ValueError, match="explicit reauthorization"):
        validate_tenant_transition("suspended", "active")
    validate_tenant_transition("suspended", "active", reauthorized=True)

    with pytest.raises(ValueError, match="fenced execution"):
        validate_tenant_transition("offboarding", "deleted", execution_fenced=True)
    validate_tenant_transition(
        "offboarding",
        "deleted",
        execution_fenced=True,
        retention_resolved=True,
    )
    with pytest.raises(ValueError, match="cannot transition"):
        validate_tenant_transition("deleted", "active")


async def _probe(request: Request) -> JSONResponse:
    identity = getattr(request.state, "identity", None)
    return JSONResponse(
        {
            "tenant": identity.tenant_id if identity else None,
            "principal": identity.subject if identity else None,
            "tenant_header": request.headers.get("x-tenant-id"),
            "has_internal_header": "x-haas-principal-id" in request.headers,
        }
    )


def _middleware_app(authenticator: Any, *, dev_headers: bool = False) -> Starlette:
    return Starlette(
        routes=[Route("/v1/probe", _probe), Route("/healthz", _probe)],
        middleware=[
            Middleware(
                IdentityMiddleware,
                authenticator=authenticator,
                allow_legacy_header_tenancy=dev_headers,
            )
        ],
    )


async def _request(app: Starlette, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get("/v1/probe", **kwargs)


async def test_oidc_verifier_accepts_valid_token_and_rejects_claim_mismatch():
    private_key, jwk = _key_pair("key-1")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": [jwk]})

    verifier = OIDCVerifier(
        ISSUER,
        AUDIENCE,
        "https://identity.example.test/jwks",
        transport=httpx.MockTransport(handler),
    )
    try:
        claims = await verifier.verify(_token(private_key, "key-1"))
        assert claims["sub"] == "user-123"
        with pytest.raises(IdentityError, match="Invalid bearer credential"):
            await verifier.verify(_token(private_key, "key-1", audience="another-api"))
        with pytest.raises(IdentityError, match="Invalid bearer credential"):
            await verifier.verify(
                _token(private_key, "key-1", expires_at=int(time.time()) - 60)
            )
    finally:
        await verifier.aclose()


async def test_oidc_verifier_refreshes_jwks_for_rotated_key_id():
    _old_private, old_jwk = _key_pair("old-key")
    new_private, new_jwk = _key_pair("new-key")
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        keys = [old_jwk] if requests == 1 else [old_jwk, new_jwk]
        return httpx.Response(200, json={"keys": keys})

    verifier = OIDCVerifier(
        ISSUER,
        AUDIENCE,
        "https://identity.example.test/jwks",
        transport=httpx.MockTransport(handler),
    )
    try:
        claims = await verifier.verify(_token(new_private, "new-key"))
        assert claims["sub"] == "user-123"
        assert requests == 2
    finally:
        await verifier.aclose()


async def test_oidc_jwks_outage_is_not_reported_as_invalid_user_token():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    verifier = OIDCVerifier(
        ISSUER,
        AUDIENCE,
        "https://identity.example.test/jwks",
        transport=httpx.MockTransport(handler),
    )
    try:
        private_key, _jwk = _key_pair("key-1")
        with pytest.raises(IdentityUnavailable, match="signing keys are unavailable"):
            await verifier.verify(_token(private_key, "key-1"))
    finally:
        await verifier.aclose()


async def test_oidc_membership_revocation_and_tenant_selection():
    private_key, jwk = _key_pair("key-1")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": [jwk]})

    store = MemoryIdentityStore()
    auth = IdentityAuthenticator(
        store,
        _settings(),
        jwks_transport=httpx.MockTransport(handler),
    )
    try:
        identity = await auth.authenticate(_token(private_key, "key-1"), "acme")
        assert identity.tenant_id == "acme"
        assert identity.roles == frozenset({"tenant_admin"})
        uuid_selected_identity = await auth.authenticate(
            _token(private_key, "key-1"), TENANT["tenant_id"]
        )
        assert uuid_selected_identity.tenant_id == "acme"

        with pytest.raises(IdentityError, match="No active membership"):
            await auth.authenticate(_token(private_key, "key-1"), "other")

        store.membership["credential_valid_after"] = datetime.fromtimestamp(
            time.time() + 30, UTC
        )
        with pytest.raises(IdentityError, match="has been revoked"):
            await auth.authenticate(_token(private_key, "key-1"), "acme")
    finally:
        await auth.aclose()


async def test_oidc_tenant_reactivation_rejects_pre_transition_credentials():
    private_key, jwk = _key_pair("key-1")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": [jwk]})

    store = MemoryIdentityStore()
    store.membership["tenant_credential_valid_after"] = datetime.fromtimestamp(
        time.time() + 30, UTC
    )
    auth = IdentityAuthenticator(
        store,
        _settings(),
        jwks_transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(IdentityError, match="has been revoked"):
            await auth.authenticate(_token(private_key, "key-1"), "acme")
    finally:
        await auth.aclose()


async def test_tenant_scoped_api_key_and_production_test_key_rejection():
    store = MemoryIdentityStore()
    api_key = "hk_live_" + "x" * 40
    store.api_key = api_key
    auth = IdentityAuthenticator(store, _settings())
    try:
        identity = await auth.authenticate(api_key, "acme")
        assert identity.credential_type == "api_key"
        with pytest.raises(IdentityError, match="not authorized for this tenant"):
            await auth.authenticate(api_key, "other")
        with pytest.raises(IdentityError, match="Test API keys"):
            await auth.authenticate("hk_test_" + "x" * 40, "acme")
    finally:
        await auth.aclose()


async def test_development_profile_can_use_test_api_keys_instead_of_header_fallback():
    store = MemoryIdentityStore()
    store.api_key = "hk_test_" + "t" * 40
    auth = IdentityAuthenticator(store, Settings(_env_file=None, haas_profile="dev"))
    app = _middleware_app(auth, dev_headers=True)
    response = await _request(
        app,
        headers={
            "Authorization": f"Bearer {store.api_key}",
            "X-Tenant-ID": "acme",
        },
    )
    assert response.status_code == 200
    assert response.json()["tenant"] == "acme"
    await auth.aclose()


async def test_identity_middleware_rejects_missing_auth_and_strips_spoofed_headers():
    store = MemoryIdentityStore()
    private_key, jwk = _key_pair("key-1")
    auth = IdentityAuthenticator(
        store,
        _settings(),
        jwks_transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"keys": [jwk]})
        ),
    )
    app = _middleware_app(auth)
    try:
        missing = await _request(app, headers={"X-Tenant-ID": "acme"})
        assert missing.status_code == 401
        assert missing.headers["content-type"].startswith("application/problem+json")
        missing_tenant = await _request(
            app, headers={"Authorization": "Bearer " + _token(private_key, "key-1")}
        )
        assert missing_tenant.status_code == 400
        duplicate_tenant = await _request(
            app,
            headers=[
                ("Authorization", "Bearer " + _token(private_key, "key-1")),
                ("X-Tenant-ID", "acme"),
                ("X-Tenant-ID", "other"),
            ],
        )
        assert duplicate_tenant.status_code == 400
        valid = await _request(
            app,
            headers={
                "Authorization": "Bearer " + _token(private_key, "key-1"),
                "X-Tenant-ID": "acme",
                "X-Haas-Principal-ID": "attacker",
            },
        )
        assert valid.status_code == 200
        assert valid.json() == {
            "tenant": "acme",
            "principal": "user-123",
            "tenant_header": "acme",
            "has_internal_header": False,
        }
        uuid_selected = await _request(
            app,
            headers={
                "Authorization": "Bearer " + _token(private_key, "key-1"),
                "X-Tenant-ID": TENANT["tenant_id"],
            },
        )
        assert uuid_selected.status_code == 200
        assert uuid_selected.json()["tenant_header"] == "acme"
    finally:
        await auth.aclose()


async def test_development_header_mode_still_strips_internal_identity_headers():
    class NoAuth:
        async def authenticate(self, _credential: str, _tenant: str) -> None:
            raise AssertionError("development header mode must not authenticate")

    app = _middleware_app(NoAuth(), dev_headers=True)
    response = await _request(
        app,
        headers={
            "X-Tenant-ID": "local-tenant",
            "X-Haas-Principal-ID": "forged",
            "X-Authenticated-User": "forged",
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "tenant": None,
        "principal": None,
        "tenant_header": "local-tenant",
        "has_internal_header": False,
    }


def test_production_oidc_configuration_requires_https_and_complete_values():
    production = Settings(
        _env_file=None,
        haas_profile="prod",
        secret_encryption_key=base64.b64encode(b"x" * 32).decode(),
        openrouter_api_key="configured-provider-key",
        oidc_issuer="http://identity.example.test/",
        oidc_audience=AUDIENCE,
        oidc_jwks_url="http://identity.example.test/jwks",
    )
    with pytest.raises(ValueError, match="HAAS_OIDC_ISSUER must use HTTPS"):
        validate_profile_settings(production, role="api")
    incomplete = production.model_copy(update={"oidc_jwks_url": None})
    with pytest.raises(ValueError, match="must be configured together"):
        validate_profile_settings(incomplete, role="api")
