"""Verify caller identity through the public HaaS ``/v1/me`` contract."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx
from fastapi import HTTPException, Request

MAX_IDENTITY_BYTES = 64 * 1024
IDENTITY_TIMEOUT = 2.0


@dataclass(frozen=True)
class VerifiedIdentity:
    tenant_id: str
    principal_id: str


def _trusted_me_url() -> str:
    origin = os.getenv("ATLAS_HAAS_API_ORIGIN", "").rstrip("/")
    if not origin:
        raise HTTPException(503, "Identity verifier is not configured.")
    parsed = urlsplit(origin)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise HTTPException(503, "Identity verifier is not configured.")
    if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise HTTPException(503, "Identity verifier is not configured.")
    if parsed.scheme == "http":
        if os.getenv("ATLAS_ALLOW_LOCAL_HAAS_HTTP") != "true" or not _private_local_host(parsed.hostname):
            raise HTTPException(503, "Identity verifier is not configured.")
    return f"{origin}/v1/me"


def _private_local_host(host: str) -> bool:
    private_networks = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("fc00::/7"),
    )

    def is_local(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return address.is_loopback or any(
            address.version == network.version and address in network for network in private_networks
        )

    try:
        return is_local(ipaddress.ip_address(host))
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(result[4][0])
                for result in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            }
        except OSError:
            return False
        return bool(addresses) and all(is_local(address) for address in addresses)


async def verify_identity(
    authorization: str | None,
    tenant_selector: str | None,
    *,
    origin: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> VerifiedIdentity:
    """Return only canonical IDs proved by HaaS; all verifier errors fail closed."""
    if (
        not authorization
        or not authorization.lower().startswith("bearer ")
        or not authorization.partition(" ")[2].strip()
    ):
        raise HTTPException(401, "Authenticated identity required.")
    if "\r" in authorization or "\n" in authorization:
        raise HTTPException(401, "Authenticated identity required.")
    url = _trusted_me_url() if origin is None else origin
    headers = {"Authorization": authorization}
    if tenant_selector:
        headers["X-Tenant-ID"] = tenant_selector
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(IDENTITY_TIMEOUT),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream("GET", url, headers=headers) as response:
                status = response.status_code
                if status in {401, 403}:
                    raise HTTPException(status, "HaaS identity verification denied.")
                if status != 200:
                    raise HTTPException(503, "HaaS identity verification unavailable.")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_IDENTITY_BYTES:
                        raise HTTPException(503, "HaaS identity response is invalid.")
                data = json.loads(body)
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "HaaS identity verification unavailable.") from None

    tenant_id = data.get("tenant_id") if isinstance(data, dict) else None
    principal_id = data.get("principal_id") if isinstance(data, dict) else None
    capabilities = data.get("capabilities") if isinstance(data, dict) else None
    actions = capabilities.get("actions") if isinstance(capabilities, dict) else None
    if (
        not isinstance(tenant_id, str)
        or not tenant_id.strip()
        or "\x00" in tenant_id
        or len(tenant_id) > 512
        or not isinstance(principal_id, str)
        or not principal_id.strip()
        or "\x00" in principal_id
        or len(principal_id) > 512
        or not isinstance(actions, list)
        or "run.submit" not in actions
    ):
        raise HTTPException(503, "HaaS identity response is invalid.")
    return VerifiedIdentity(tenant_id=tenant_id, principal_id=principal_id)


async def require_identity(request: Request) -> VerifiedIdentity:
    """FastAPI dependency; identity and tenant always come from public HaaS."""
    return await verify_identity(
        request.headers.get("authorization"),
        request.headers.get("x-tenant-id"),
        transport=getattr(request.app.state, "identity_transport", None),
    )


async def verify_public_run(
    authorization: str | None,
    tenant_selector: str | None,
    run_id: str,
    *,
    terminal_status: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, str | None]:
    """Confirm run correlation and optional terminal status through public API."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Authenticated identity required.")
    me_url = _trusted_me_url()
    origin = me_url.removesuffix("/v1/me")
    headers = {"Authorization": authorization}
    if tenant_selector:
        headers["X-Tenant-ID"] = tenant_selector
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(IDENTITY_TIMEOUT),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                "GET",
                f"{origin}/v1/runs/{quote(run_id, safe='')}",
                headers=headers,
            ) as response:
                if response.status_code in {401, 403}:
                    raise HTTPException(response.status_code, "Run status verification denied.")
                if response.status_code == 404:
                    raise HTTPException(404, "Run not found.")
                if response.status_code != 200:
                    raise HTTPException(503, "Run status verification unavailable.")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_IDENTITY_BYTES:
                        raise HTTPException(503, "Run status response is invalid.")
                result = json.loads(body)
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "Run status verification unavailable.") from None
    status = result.get("status") if isinstance(result, dict) else None
    returned_run_id = result.get("run_id") if isinstance(result, dict) else None
    if returned_run_id != run_id:
        raise HTTPException(503, "Run status response is invalid.")
    thread_id = result.get("thread_id") if isinstance(result, dict) else None
    if thread_id is not None and not isinstance(thread_id, str):
        raise HTTPException(503, "Run status response is invalid.")
    if terminal_status is not None:
        if status not in {"completed", "failed", "cancelled"}:
            raise HTTPException(409, {"code": "run_not_terminal"})
        if status != terminal_status:
            raise HTTPException(409, {"code": "run_status_mismatch"})
    return {"status": status if isinstance(status, str) else None, "thread_id": thread_id}
