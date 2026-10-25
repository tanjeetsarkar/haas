from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from app.core.config import settings


class ToolRemediationError(Exception):
    """Expected domain error with actionable self-correction instructions for the LLM."""


class SecurityViolationError(Exception):
    """Raised on unauthorized secret access, path traversal, or SSRF attempts."""


@dataclass
class RunContext:
    """
    Passed into tool handlers declaring `ctx: RunContext`.
    Constructed inside the isolated tool worker with ONLY the secrets allowed by active skills.
    """

    run_id: str
    thread_id: str
    tenant_id: str
    traceparent: str
    active_skills: set[str] = field(default_factory=set)
    skill_allowlist: set[str] | None = None  # None = unrestricted
    allowed_secrets: set[str] = field(default_factory=set)
    _scoped_secrets: dict[str, str] = field(default_factory=dict, repr=False)
    http_client: httpx.AsyncClient | None = field(default=None, repr=False)
    # Optional database store for tools that need direct DB access
    store: Any = field(default=None, repr=False)
    # Stable per-invocation key; remote side effects should pass it as their idempotency key.
    effect_key: str = ""

    def get_secret(self, key: str) -> str:
        if key not in self.allowed_secrets:
            raise SecurityViolationError(
                f"Access denied: Secret '{key}' is not declared in any active skill's `allowed_secrets`."
            )
        val = self._scoped_secrets.get(key)
        if not val:
            raise ToolRemediationError(
                f"Secret '{key}' was not supplied in the run's secret vault by the caller."
            )
        return val


_BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "redis", "postgres"}


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_safe_outbound_url(url: str, *, resolve_dns: bool = False) -> str:
    """
    SSRF guard. Always checks scheme, blocked hostnames and literal IPs.
    With `resolve_dns=True` (used at request time) every address the hostname resolves
    to must also be public, which defeats hostnames that point at internal services.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SecurityViolationError(
            f"Invalid scheme '{parsed.scheme}'. Only http/https permitted."
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise SecurityViolationError("URL has no hostname.")
    if host in settings.ssrf_allowlist:
        return url
    if host in _BLOCKED_HOSTS or host.endswith((".internal", ".local", ".localhost")):
        raise SecurityViolationError(
            f"SSRF Guard: Outbound calls to internal host '{host}' are blocked."
        )

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_blocked_ip(literal):
            raise SecurityViolationError(
                f"SSRF Guard: Outbound calls to private IP '{literal}' are blocked."
            )
        return url

    if resolve_dns:
        try:
            infos = socket.getaddrinfo(
                host, parsed.port or (443 if parsed.scheme == "https" else 80)
            )
        except socket.gaierror as exc:
            raise ToolRemediationError(f"Cannot resolve host '{host}': {exc}") from exc
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if _is_blocked_ip(ip):
                raise SecurityViolationError(
                    f"SSRF Guard: '{host}' resolves to private address '{ip}' and is blocked."
                )
    return url
