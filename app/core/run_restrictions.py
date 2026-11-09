"""Pure v1 run-authority primitives; not an admission or dispatch authorization API."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_ARGUMENT_BYTES = 65_536


def canonical_arguments(arguments: object) -> str:
    """Only JSON-native objects; never stringify, repair, normalize or coerce."""
    def check(value: object) -> None:
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("restriction.arguments")
                check(item)
        elif type(value) is list:
            for item in value:
                check(item)
        elif value is not None and type(value) not in (str, int, float, bool):
            raise ValueError("restriction.arguments")

    if type(arguments) is not dict:
        raise ValueError("restriction.arguments")
    try:
        check(arguments)
        encoded = json.dumps(
            arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > MAX_ARGUMENT_BYTES:
            raise ValueError("restriction.arguments_size")
        return encoded
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("restriction.arguments") from exc


def validate_exact_arguments(
    arguments: object, schema: type[BaseModel],
) -> str:
    """Validate the full effective payload, including aliases/defaults, without changes."""
    submitted = canonical_arguments(arguments)
    validated = schema.model_validate(arguments, strict=True)
    if canonical_arguments(validated.model_dump(mode="json", by_alias=True)) != submitted:
        raise ValueError("restriction.arguments_changed")
    return submitted


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class IntentContext(FrozenModel):
    """Server-owned target and provenance; never populated from a client digest."""

    tenant_id: str = Field(min_length=1)
    submitter_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    policy_revision: str = Field(min_length=1)
    registry_fingerprint: str = Field(min_length=1)
    plugin_id: str = Field(min_length=1)
    plugin_digest: str = Field(min_length=1, pattern=r"^[0-9a-f]{64}$")
    destination: str = Field(min_length=1)
    idempotency_key: str | None = None


class ExactIntent(FrozenModel):
    tool_name: str = Field(min_length=1)
    arguments_json: str
    context: IntentContext

    @model_validator(mode="after")
    def canonical(self) -> ExactIntent:
        if canonical_arguments(json.loads(self.arguments_json)) != self.arguments_json:
            raise ValueError("restriction.noncanonical_arguments")
        return self

    @property
    def binding(self) -> str:
        body = {"domain": "haas.run-intent.v1", **self.model_dump(mode="json")}
        return hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            .encode("utf-8")
        ).hexdigest()

    def matches(self, tool_name: str, arguments: object) -> bool:
        return tool_name == self.tool_name and canonical_arguments(arguments) == self.arguments_json


class RunAuthority(FrozenModel):
    version: Literal[1] = 1
    tools: tuple[str, ...] | None = Field(default=None, max_length=128)
    intent: ExactIntent | None = None

    @model_validator(mode="after")
    def valid(self) -> RunAuthority:
        if self.tools is not None and (
            any(not name for name in self.tools) or len(set(self.tools)) != len(self.tools)
        ):
            raise ValueError("restriction.tools")
        if self.intent is not None and self.tools != (self.intent.tool_name,):
            raise ValueError("restriction.intent_requires_singleton")
        return self

    def permits(self, name: str) -> bool:
        return self.tools is None or name in self.tools


def narrow_tools(
    requested: tuple[str, ...] | None, *, available: Collection[str],
    parent: tuple[str, ...] | None = None,
) -> tuple[str, ...] | None:
    """Reject explicit excess; omitted authority inherits, never widens, its parent."""
    RunAuthority(tools=requested)
    ceiling = set(available)
    if parent is not None:
        ceiling.intersection_update(parent)
    if requested is not None and not set(requested).issubset(ceiling):
        raise ValueError("restriction.authority_exceeded")
    return parent if requested is None else requested


def authority_request_digest(base_digest: str, authority: RunAuthority | None) -> str:
    """Include attenuation in replay identity, not server-assigned per-run binding."""
    if authority is None:
        return base_digest
    intent = authority.intent
    return hashlib.sha256(json.dumps({
        "domain": "haas.run-admission-authority.v1", "request": base_digest,
        "tools": authority.tools,
        "invocation_intent": (
            {"tool_name": intent.tool_name, "arguments": json.loads(intent.arguments_json)}
            if intent is not None else None
        ),
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def validate_intent_provenance(
    authority: RunAuthority, *, tenant_id: str, run_id: str, submitter_id: str,
    policy_revision: str, registry_fingerprint: str, idempotency_key: str | None,
) -> None:
    if authority.intent is None:
        return
    context = authority.intent.context
    expected = {
        "tenant_id": tenant_id, "run_id": run_id, "submitter_id": submitter_id,
        "policy_revision": policy_revision, "registry_fingerprint": registry_fingerprint,
        "idempotency_key": idempotency_key,
    }
    if any(getattr(context, key) != value for key, value in expected.items()):
        raise ValueError("restriction.provenance_mismatch")


class RestrictionError(ValueError):
    """Stable non-sensitive denial code; never includes arguments or credentials."""


def decode_authority(value: object) -> RunAuthority | None:
    if value is None:
        return None
    try:
        payload = json.loads(value) if isinstance(value, str) else value
        if not isinstance(payload, dict) or not {"version", "tools"} <= payload.keys():
            raise ValueError()
        return RunAuthority.model_validate_json(json.dumps(payload))
    except (ValueError, TypeError) as exc:
        raise RestrictionError("restriction.invalid_authority") from exc


def check_dispatch_authority(
    authority: RunAuthority | None, tool_name: str, *,
    policy_tools: Collection[str], unlocked_tools: Collection[str],
) -> None:
    if authority is None:
        return
    if not authority.permits(tool_name):
        raise RestrictionError("restriction.tool_denied")
    if tool_name not in policy_tools or tool_name not in unlocked_tools:
        raise RestrictionError("restriction.policy_denied")
    if authority.intent is not None:
        raise RestrictionError("restriction.intent_unavailable")
    # Staging only. Replace with enforced dispatch, never with a caller switch.
    raise RestrictionError("restriction.unavailable")
