"""Brokered outbound HTTP for tool code (owner: SEC).

``egress_request`` is the only sanctioned path from tool code to the network:

1. the URL scheme/host is checked against the tenant egress policy host allowlist;
2. DNS is resolved exactly once and *every* returned address must be public (private,
   loopback, link-local, ULA, multicast, reserved, IPv4-mapped/NAT64 forms of those
   and cloud metadata endpoints are denied) unless an explicit policy CIDR allows it;
3. the connection goes to the pinned IP while the ``Host`` header and TLS SNI keep the
   original hostname (no second resolution => no DNS rebinding window);
4. redirects are followed manually and every hop is re-validated from step 1;
5. response bytes are metered against the per-run cap and recorded as ``egress_bytes``.
"""

from __future__ import annotations

import asyncio
import contextvars
import fnmatch
import ipaddress
import json
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4

import httpx

from app.core.config import settings
from app.core.context import SecurityViolationError

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

USAGE_QUEUE_KEY = "haas:egress-usage"
_COUNTER_TTL_SEC = 7 * 86_400
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata",
    "metadata.google.internal",
    "instance-data",
    "redis",
    "postgres",
}
_BLOCKED_SUFFIXES = (".internal", ".local", ".localhost", ".localdomain")
_METADATA_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "169.254.169.254/32",
        "169.254.170.2/32",
        "100.100.100.200/32",
        "fd00:ec2::254/128",
    )
)
_EXTRA_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "100.64.0.0/10",
        "192.0.0.0/24",
        "198.18.0.0/15",
        "fc00::/7",
        "fe80::/10",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "2002::/16",
    )
)


class EgressDenied(SecurityViolationError):
    """The egress broker refused a request; ``reason`` is a stable machine code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class EgressPolicy:
    allow_hosts: tuple[str, ...] = ()
    allow_cidrs: tuple[str, ...] = ()
    max_bytes_per_run: int | None = None
    version: int = 0
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = field(
        default=(), compare=False, repr=False
    )

    @classmethod
    def from_value(cls, value: EgressPolicy | Mapping[str, Any] | None) -> EgressPolicy:
        if isinstance(value, EgressPolicy):
            return value
        data = dict(value or {})
        cidrs = tuple(str(c) for c in data.get("allow_cidrs") or ())
        cap = data.get("max_bytes_per_run")
        return cls(
            allow_hosts=tuple(normalize_host_pattern(h) for h in data.get("allow_hosts") or ()),
            allow_cidrs=cidrs,
            max_bytes_per_run=int(cap) if cap is not None else None,
            version=int(data.get("version") or 0),
            networks=tuple(ipaddress.ip_network(c, strict=False) for c in cidrs),
        )

    @property
    def byte_cap(self) -> int:
        return self.max_bytes_per_run or settings.egress_default_max_bytes_per_run

    def as_dict(self) -> dict[str, Any]:
        return {
            "allow_hosts": list(self.allow_hosts),
            "allow_cidrs": list(self.allow_cidrs),
            "max_bytes_per_run": self.max_bytes_per_run,
            "version": self.version,
        }


def normalize_host_pattern(pattern: str) -> str:
    value = pattern.strip().lower().rstrip(".")
    body = value[2:] if value.startswith("*.") else value
    if not body or "*" in body or "/" in body or ":" in body or " " in body:
        raise ValueError(f"Invalid egress host pattern {pattern!r}.")
    return value


def host_allowed(host: str, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return True
    host = host.lower().rstrip(".")
    for pattern in patterns:
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]) and fnmatch.fnmatchcase(host, pattern):
                return True
        elif host == pattern:
            return True
    return False


def _unwrap(ip: IPAddress) -> IPAddress:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip.sixtofour is not None:
            return ip.sixtofour
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def classify_address(ip: IPAddress) -> str | None:
    """Return a denial reason for a non-public address, or ``None`` when it is public."""
    for addr in dict.fromkeys((_unwrap(ip), ip)):
        if any(addr in n for n in _METADATA_NETWORKS):
            return "metadata"
        if addr.is_loopback:
            return "loopback"
        if addr.is_link_local:
            return "link_local"
        if addr.is_multicast:
            return "multicast"
        if addr.is_unspecified:
            return "unspecified"
        if isinstance(addr, ipaddress.IPv6Address) and addr in ipaddress.ip_network(
            "fc00::/7"
        ):
            return "unique_local"
        if addr.is_private or any(addr in n for n in _EXTRA_BLOCKED_NETWORKS):
            return "private"
        if addr.is_reserved:
            return "reserved"
    return None


def _cidr_allows(ip: IPAddress, policy: EgressPolicy) -> bool:
    return any(ip in n or _unwrap(ip) in n for n in policy.networks)


async def resolve_host(host: str, port: int) -> list[IPAddress]:
    infos = await asyncio.to_thread(
        socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM
    )
    seen: list[IPAddress] = []
    for info in infos:
        ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        if ip not in seen:
            seen.append(ip)
    return seen


@dataclass(frozen=True)
class PinnedTarget:
    url: str
    host: str
    port: int
    ip: IPAddress
    scheme: str

    @property
    def connect_url(self) -> str:
        parts = urlsplit(self.url)
        ip_text = f"[{self.ip}]" if self.ip.version == 6 else str(self.ip)
        default = 443 if self.scheme == "https" else 80
        netloc = ip_text if self.port == default else f"{ip_text}:{self.port}"
        return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))

    @property
    def host_header(self) -> str:
        default = 443 if self.scheme == "https" else 80
        shown = f"[{self.host}]" if ":" in self.host else self.host
        return shown if self.port == default else f"{shown}:{self.port}"


async def check_destination(url: str, policy: EgressPolicy) -> PinnedTarget:
    """Validate ``url`` under ``policy`` and pin it to a single vetted IP address."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise EgressDenied("scheme", f"Egress denied: scheme {scheme!r} is not allowed.")
    if parts.username or parts.password:
        raise EgressDenied("userinfo", "Egress denied: credentials in URLs are not allowed.")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise EgressDenied("host", "Egress denied: URL has no hostname.")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise EgressDenied("port", "Egress denied: invalid port.") from exc
    if not host_allowed(host, policy.allow_hosts):
        raise EgressDenied(
            "host_not_allowed",
            f"Egress denied: host '{host}' is not in the tenant egress allowlist.",
        )
    dev_exempt = host in settings.ssrf_allowlist
    try:
        literal: IPAddress | None = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is None and not dev_exempt and (
        host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES)
    ):
        raise EgressDenied(
            "internal_host", f"Egress denied: internal host '{host}' is blocked."
        )
    if literal is not None:
        addresses = [literal]
    else:
        try:
            addresses = await resolve_host(host, port)
        except (socket.gaierror, UnicodeError) as exc:
            raise EgressDenied("dns", f"Egress denied: cannot resolve '{host}'.") from exc
    if not addresses:
        raise EgressDenied("dns", f"Egress denied: '{host}' has no addresses.")
    if not dev_exempt:
        for ip in addresses:
            reason = classify_address(ip)
            if reason is not None and not _cidr_allows(ip, policy):
                raise EgressDenied(
                    reason,
                    f"Egress denied: '{host}' resolves to non-public address {ip} ({reason}).",
                )
    return PinnedTarget(url=url, host=host, port=port, ip=addresses[0], scheme=scheme)


# ---------------------------------------------------------------- metering


class EgressMeter(Protocol):
    async def used(self, run_id: str) -> int: ...

    async def charge(self, run_id: str, nbytes: int) -> int: ...

    async def record(self, tenant_id: str, run_id: str, nbytes: int) -> None: ...


class RedisEgressMeter:
    """Per-run byte counter in Redis plus a usage queue drained into ``usage_ledger``."""

    def __init__(self, redis: Any) -> None:
        self.redis = redis

    @staticmethod
    def counter_key(run_id: str) -> str:
        return f"haas:egress:{run_id}:bytes"

    async def used(self, run_id: str) -> int:
        return int(await self.redis.get(self.counter_key(run_id)) or 0)

    async def charge(self, run_id: str, nbytes: int) -> int:
        key = self.counter_key(run_id)
        total = int(await self.redis.incrby(key, nbytes))
        await self.redis.expire(key, _COUNTER_TTL_SEC)
        return total

    async def record(self, tenant_id: str, run_id: str, nbytes: int) -> None:
        if nbytes <= 0:
            return
        await self.redis.lpush(
            USAGE_QUEUE_KEY,
            json.dumps(
                {
                    "entry_id": str(uuid4()),
                    "tenant_id": tenant_id,
                    "run_id": run_id,
                    "bytes": nbytes,
                    "at": time.time(),
                }
            ),
        )


class InMemoryEgressMeter:
    def __init__(self) -> None:
        self.totals: dict[str, int] = {}
        self.records: list[tuple[str, str, int]] = []

    async def used(self, run_id: str) -> int:
        return self.totals.get(run_id, 0)

    async def charge(self, run_id: str, nbytes: int) -> int:
        self.totals[run_id] = self.totals.get(run_id, 0) + nbytes
        return self.totals[run_id]

    async def record(self, tenant_id: str, run_id: str, nbytes: int) -> None:
        if nbytes > 0:
            self.records.append((tenant_id, run_id, nbytes))


# Set by the tool worker for the duration of one invocation (see ``execute_tool_task``).
active_policy: contextvars.ContextVar[EgressPolicy | None] = contextvars.ContextVar(
    "haas_egress_policy", default=None
)
active_meter: contextvars.ContextVar[EgressMeter | None] = contextvars.ContextVar(
    "haas_egress_meter", default=None
)


def _request_size(request: httpx.Request) -> int:
    try:
        return len(request.content)
    except httpx.RequestNotRead:
        return 0


async def egress_request(
    method: str,
    url: str,
    *,
    tenant_id: str,
    run_id: str,
    policy: EgressPolicy | Mapping[str, Any] | None,
    meter: EgressMeter | None = None,
    client: httpx.AsyncClient | None = None,
    **httpx_kwargs: Any,
) -> httpx.Response:
    """Send one brokered request; raises ``EgressDenied`` instead of touching the network."""
    rules = EgressPolicy.from_value(policy)
    httpx_kwargs.pop("follow_redirects", None)
    extensions = dict(httpx_kwargs.pop("extensions", None) or {})
    timeout = httpx_kwargs.pop("timeout", settings.egress_request_timeout_sec)
    headers = httpx.Headers(httpx_kwargs.pop("headers", None))
    headers.pop("host", None)
    owned = client is None
    http = client or httpx.AsyncClient(timeout=timeout)
    cap = rules.byte_cap
    spent = 0
    try:
        if meter is not None and await meter.used(run_id) >= cap:
            raise EgressDenied("byte_cap", "Egress denied: run egress byte budget exhausted.")
        current_url, current_method = url, method.upper()
        body_kwargs = httpx_kwargs
        for hop in range(settings.egress_max_redirects + 1):
            target = await check_destination(current_url, rules)
            hop_headers = httpx.Headers(headers)
            hop_headers["Host"] = target.host_header
            hop_ext = dict(extensions)
            if target.scheme == "https":
                hop_ext["sni_hostname"] = target.host
            request = http.build_request(
                current_method,
                target.connect_url,
                headers=hop_headers,
                extensions=hop_ext,
                timeout=timeout,
                **body_kwargs,
            )
            sent = _request_size(request)
            spent += sent
            response = await http.send(request, stream=True, follow_redirects=False)
            chunks: list[bytes] = []
            try:
                async for chunk in response.aiter_raw():
                    chunks.append(chunk)
                    spent += len(chunk)
                    total = (
                        await meter.charge(run_id, len(chunk))
                        if meter is not None
                        else spent
                    )
                    if total > cap:
                        raise EgressDenied(
                            "byte_cap",
                            f"Egress denied: run exceeded its egress budget of {cap} bytes.",
                        )
            finally:
                await response.aclose()
            if meter is not None and sent:
                await meter.charge(run_id, sent)
            location = response.headers.get("location")
            if response.status_code in _REDIRECT_CODES and location:
                if hop >= settings.egress_max_redirects:
                    raise EgressDenied("redirects", "Egress denied: too many redirects.")
                next_url = urljoin(current_url, location)
                if urlsplit(next_url).netloc.lower() != urlsplit(current_url).netloc.lower():
                    for name in ("authorization", "cookie", "proxy-authorization"):
                        headers.pop(name, None)
                if response.status_code == 303 or (
                    response.status_code in (301, 302) and current_method == "POST"
                ):
                    current_method = "GET" if current_method != "HEAD" else "HEAD"
                    body_kwargs = {
                        k: v
                        for k, v in body_kwargs.items()
                        if k not in ("content", "data", "files", "json")
                    }
                current_url = next_url
                continue
            original = httpx.Request(current_method, current_url, headers=hop_headers)
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=b"".join(chunks),
                request=original,
                extensions={"http_version": response.extensions.get("http_version", b"HTTP/1.1")},
            )
        raise EgressDenied("redirects", "Egress denied: too many redirects.")
    finally:
        if meter is not None:
            await meter.record(tenant_id, run_id, spent)
        if owned:
            await http.aclose()
