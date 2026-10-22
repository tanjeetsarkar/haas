"""`preview.redaction` and `preview.retention`: data-governance namespaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from haas_sdk import _specs as specs
from haas_sdk._core import Call, clean, seg
from haas_sdk.preview._base import _PreviewResource, _dict


@dataclass(frozen=True, slots=True)
class RedactionRule:
    rule_id: str
    name: str
    pattern: str
    replacement: str
    applies_to: tuple[str, ...]
    enabled: bool
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RedactionRule:
        return cls(
            rule_id=str(data.get("rule_id") or ""),
            name=str(data.get("name") or ""),
            pattern=str(data.get("pattern") or ""),
            replacement=str(data.get("replacement") or ""),
            applies_to=tuple(str(a) for a in (data.get("applies_to") or [])),
            enabled=bool(data.get("enabled", True)),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    run_events_ttl_days: int | None
    artifacts_ttl_days: int | None
    messages_ttl_days: int | None
    artifact_quota_bytes: int | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RetentionPolicy:
        return cls(
            run_events_ttl_days=data.get("run_events_ttl_days"),
            artifacts_ttl_days=data.get("artifacts_ttl_days"),
            messages_ttl_days=data.get("messages_ttl_days"),
            artifact_quota_bytes=data.get("artifact_quota_bytes"),
            raw=data,
        )


def _rule_list(response: Any) -> list[RedactionRule]:
    body = _dict(response)
    items = body.get("items") if isinstance(body, dict) else body
    return [RedactionRule.from_dict(i) for i in (items or []) if isinstance(i, dict)]


class Redaction(_PreviewResource):
    """Tenant redaction rules plus a server-side test endpoint."""

    def rules(self) -> list[RedactionRule]:
        return self._run(
            specs.Op(Call("GET", "/v1/redaction/rules", idempotent=True), _rule_list)
        )

    def create_rule(
        self,
        *,
        name: str,
        pattern: str,
        replacement: str = "[REDACTED]",
        applies_to: list[str] | None = None,
        enabled: bool = True,
    ) -> RedactionRule:
        return self._run(
            specs.Op(
                Call(
                    "POST",
                    "/v1/redaction/rules",
                    json=clean(
                        {
                            "name": name,
                            "pattern": pattern,
                            "replacement": replacement,
                            "applies_to": applies_to,
                            "enabled": enabled,
                        }
                    ),
                ),
                lambda r: RedactionRule.from_dict(_dict(r)),
            )
        )

    def test(self, text: str) -> dict[str, Any]:
        """Run the tenant's rules against sample text without persisting it."""
        return self._run(
            specs.Op(Call("POST", "/v1/redaction/test", json={"text": text}), _dict)
        )


class Retention(_PreviewResource):
    """Tenant retention policy, erasure requests, legal holds and tombstones."""

    def policy(self) -> RetentionPolicy:
        return self._run(
            specs.Op(
                Call("GET", "/v1/retention/policy", idempotent=True),
                lambda r: RetentionPolicy.from_dict(_dict(r)),
            )
        )

    def set_policy(
        self,
        *,
        run_events_ttl_days: int | None = None,
        artifacts_ttl_days: int | None = None,
        messages_ttl_days: int | None = None,
        artifact_quota_bytes: int | None = None,
    ) -> RetentionPolicy:
        return self._run(
            specs.Op(
                Call(
                    "PUT",
                    "/v1/retention/policy",
                    json=clean(
                        {
                            "run_events_ttl_days": run_events_ttl_days,
                            "artifacts_ttl_days": artifacts_ttl_days,
                            "messages_ttl_days": messages_ttl_days,
                            "artifact_quota_bytes": artifact_quota_bytes,
                        }
                    ),
                ),
                lambda r: RetentionPolicy.from_dict(_dict(r)),
            )
        )

    def erasure_requests(self) -> list[dict[str, Any]]:
        return self._run(
            specs.Op(
                Call("GET", "/v1/retention/erasure-requests", idempotent=True),
                _PreviewResource._items,
            )
        )

    def create_erasure_request(self, subject: str) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "POST",
                    "/v1/retention/erasure-requests",
                    json={"subject": subject},
                ),
                _dict,
            )
        )

    def legal_holds(self) -> list[dict[str, Any]]:
        return self._run(
            specs.Op(
                Call("GET", "/v1/retention/legal-holds", idempotent=True),
                _PreviewResource._items,
            )
        )

    def create_legal_hold(self, *, name: str, reason: str = "") -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "POST",
                    "/v1/retention/legal-holds",
                    json=clean({"name": name, "reason": reason}),
                ),
                _dict,
            )
        )

    def release_legal_hold(self, hold_id: str) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "POST", f"/v1/retention/legal-holds/{seg(hold_id)}/release", json={}
                ),
                _dict,
            )
        )

    def tombstones(self) -> list[dict[str, Any]]:
        return self._run(
            specs.Op(
                Call("GET", "/v1/retention/tombstones", idempotent=True),
                _PreviewResource._items,
            )
        )
