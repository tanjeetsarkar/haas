"""Capability negotiation (§5.1).

`require_compat()` is never called implicitly; `Haas(..., verify_server=True)`
calls it on the first request.
"""

from __future__ import annotations

from haas_sdk.errors import IncompatibleServerError
from haas_sdk.models import Capabilities

SUPPORTED_CAPABILITIES_VERSION = "1"
REQUIRED_EVENT_SCHEMA = "run-events.v1"


def check_compat(capabilities: Capabilities) -> None:
    """SDK-COMPAT-01: raise unless the server speaks `/v1` with durable replay."""
    problems: list[str] = []
    if capabilities.version != SUPPORTED_CAPABILITIES_VERSION:
        problems.append(f"capabilities.version is {capabilities.version!r}, want '1'")
    if REQUIRED_EVENT_SCHEMA not in capabilities.event_schemas:
        problems.append(f"{REQUIRED_EVENT_SCHEMA!r} missing from event_schemas")
    if not capabilities.durable_replay:
        problems.append("durable_replay is not true")
    if problems:
        raise IncompatibleServerError("incompatible server: " + "; ".join(problems))
