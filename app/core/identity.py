from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import asyncpg
import httpx
import jwt
from jwt.exceptions import InvalidTokenError, PyJWKError
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import Settings
from app.core.http import problem_response
from app.core.store import DistributedStore

_INTERNAL_HEADER_PREFIXES = (
    b"x-haas-",
    b"x-internal-",
    b"x-authenticated-",
    b"x-principal-",
)
_INTERNAL_HEADERS = {
    b"x-haas-roles",
    b"x-tenant-owner",
    b"x-user-id",
    b"x-user-email",
    b"x-user-roles",
    b"x-forwarded-user",
    b"x-forwarded-groups",
    b"x-auth-request-user",
    b"x-auth-request-email",
}
_API_KEY_PREFIXES = ("hk_live_", "hk_test_")


class IdentityError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status


class IdentityUnavailable(Exception):
    """A required identity dependency is unavailable."""


@dataclass(frozen=True)
class AuthenticatedIdentity:
    tenant_id: str
    principal_id: str
    issuer: str
    subject: str
    roles: frozenset[str]
    credential_type: str


class OIDCVerifier:
    """Verify OIDC JWTs against a cached, rotatable JWKS document."""

    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        cache_seconds: int = 300,
    ) -> None:
        self.issuer = issuer
        self.audience = audience
        self.jwks_url = jwks_url
        self.cache_seconds = cache_seconds
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(5.0),
            follow_redirects=False,
        )
        self._keys: dict[str, dict[str, Any]] = {}
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _load_keys(self, *, force: bool = False) -> dict[str, dict[str, Any]]:
        async with self._lock:
            if (
                not force
                and self._keys
                and time.monotonic() - self._loaded_at < self.cache_seconds
            ):
                return self._keys
            try:
                response = await self._http.get(self.jwks_url)
                response.raise_for_status()
                document = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise IdentityUnavailable("OIDC signing keys are unavailable.") from exc
            keys = document.get("keys") if isinstance(document, dict) else None
            if not isinstance(keys, list):
                raise IdentityUnavailable("OIDC signing keys response is invalid.")
            parsed = {
                key["kid"]: key
                for key in keys
                if isinstance(key, dict)
                and isinstance(key.get("kid"), str)
                and key.get("use", "sig") == "sig"
            }
            if not parsed:
                raise IdentityUnavailable(
                    "OIDC signing keys response has no usable keys."
                )
            self._keys = parsed
            self._loaded_at = time.monotonic()
            return parsed

    async def verify(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except InvalidTokenError as exc:
            raise IdentityError(401, "Invalid bearer credential.") from exc
        kid = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(kid, str):
            raise IdentityError(401, "Unsupported bearer credential.")
        keys = await self._load_keys()
        if kid not in keys:
            keys = await self._load_keys(force=True)
        key_data = keys.get(kid)
        if key_data is None:
            raise IdentityError(401, "Unknown bearer signing key.")
        try:
            key = jwt.PyJWK.from_dict(key_data, algorithm="RS256").key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "sub", "aud"]},
            )
        except (InvalidTokenError, PyJWKError, TypeError, ValueError) as exc:
            raise IdentityError(401, "Invalid bearer credential.") from exc
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise IdentityError(401, "Bearer credential has no subject.")
        return claims


class IdentityAuthenticator:
    def __init__(
        self,
        store: DistributedStore,
        config: Settings,
        *,
        jwks_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.verifier = (
            OIDCVerifier(
                config.oidc_issuer,
                config.oidc_audience,
                config.oidc_jwks_url,
                transport=jwks_transport,
            )
            if config.oidc_issuer and config.oidc_audience and config.oidc_jwks_url
            else None
        )

    async def aclose(self) -> None:
        if self.verifier:
            await self.verifier.aclose()

    async def authenticate(
        self, credential: str, tenant_selector: str
    ) -> AuthenticatedIdentity:
        if credential.startswith(_API_KEY_PREFIXES):
            if self.config.haas_profile == "prod" and credential.startswith("hk_test_"):
                raise IdentityError(
                    401, "Test API keys are not accepted in production."
                )
            identity = await self.store.resolve_api_key(credential)
            if identity is None:
                raise IdentityError(401, "Invalid or expired API key.")
            if (
                identity.get("tenant_slug") != tenant_selector
                and identity.get("tenant_id") != tenant_selector
            ):
                raise IdentityError(403, "API key is not authorized for this tenant.")
            if (
                identity.get("principal_status", "active") != "active"
                or identity.get("tenant_status", "active") != "active"
                or not identity.get("roles")
            ):
                raise IdentityError(
                    403, "No active membership for the selected tenant."
                )
            identity.pop("principal_status", None)
            identity.pop("tenant_status", None)
            credential_type = "api_key"
        else:
            if self.verifier is None:
                raise IdentityError(
                    401, "OIDC bearer authentication is not configured."
                )
            claims = await self.verifier.verify(credential)
            identity = await self.store.resolve_oidc_membership(
                claims["iss"],
                claims["sub"],
                tenant_selector,
            )
            if identity is not None:
                valid_after = max(
                    (
                        timestamp
                        for timestamp in (
                            identity.pop("credential_valid_after", None),
                            identity.pop("tenant_credential_valid_after", None),
                        )
                        if timestamp is not None
                    ),
                    default=None,
                )
                if (
                    valid_after is not None
                    and int(claims["iat"]) < valid_after.timestamp()
                ):
                    raise IdentityError(401, "Bearer credential has been revoked.")
            credential_type = "oidc"
        if identity is None:
            raise IdentityError(403, "No active membership for the selected tenant.")
        return AuthenticatedIdentity(
            tenant_id=identity["tenant_slug"],
            principal_id=str(identity["principal_id"]),
            issuer=identity["issuer"],
            subject=identity["subject"],
            roles=frozenset(identity["roles"]),
            credential_type=credential_type,
        )


class IdentityMiddleware:
    """Authenticate API requests and remove client-supplied identity headers."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        authenticator: IdentityAuthenticator,
        allow_legacy_header_tenancy: bool,
    ) -> None:
        self.app = app
        self.authenticator = authenticator
        self.allow_legacy_header_tenancy = allow_legacy_header_tenancy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        scope["headers"] = [
            (key, value)
            for key, value in scope.get("headers", [])
            if not key.lower().startswith(_INTERNAL_HEADER_PREFIXES)
            and key.lower() not in _INTERNAL_HEADERS
        ]
        path = scope.get("path", "")
        if path != "/v1" and not path.startswith("/v1/"):
            await self.app(scope, receive, send)
            return
        has_authorization = any(
            key.lower() == b"authorization" for key, _ in scope["headers"]
        )
        if self.allow_legacy_header_tenancy and not has_authorization:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        try:
            tenant_values = [
                value.decode("latin-1")
                for key, value in scope["headers"]
                if key.lower() == b"x-tenant-id"
            ]
            if len(tenant_values) != 1 or not tenant_values[0].strip():
                raise IdentityError(400, "Select exactly one tenant with X-Tenant-ID.")
            authorization = [
                value.decode("latin-1")
                for key, value in scope["headers"]
                if key.lower() == b"authorization"
            ]
            if len(authorization) != 1:
                raise IdentityError(401, "A single bearer credential is required.")
            scheme, separator, credential = authorization[0].partition(" ")
            if scheme.lower() != "bearer" or not separator or not credential.strip():
                raise IdentityError(401, "A single bearer credential is required.")
            identity = await self.authenticator.authenticate(
                credential.strip(), tenant_values[0].strip()
            )
            request.state.identity = identity
            scope["headers"] = [
                (
                    key,
                    identity.tenant_id.encode("utf-8")
                    if key.lower() == b"x-tenant-id"
                    else value,
                )
                for key, value in scope["headers"]
            ]
        except IdentityError as exc:
            response = problem_response(exc.status, str(exc), instance=request.url.path)
            await response(scope, receive, send)
            return
        except (
            IdentityUnavailable,
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
        ):
            response = problem_response(
                503,
                "Identity verification is temporarily unavailable.",
                instance=request.url.path,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
