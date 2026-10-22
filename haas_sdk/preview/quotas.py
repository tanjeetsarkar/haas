"""`preview.quotas`: tenant quota read/update (`/v1/quotas`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from haas_sdk import _specs as specs
from haas_sdk._core import Call, clean
from haas_sdk.preview._base import _PreviewResource, _dict


@dataclass(frozen=True, slots=True)
class TenantQuota:
    max_concurrent_runs: int | None
    max_inflight_runs: int | None
    requests_per_minute: int | None
    principal_requests_per_minute: int | None
    sse_connections: int | None
    weight: int
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TenantQuota:
        return cls(
            max_concurrent_runs=data.get("max_concurrent_runs"),
            max_inflight_runs=data.get("max_inflight_runs"),
            requests_per_minute=data.get("requests_per_minute"),
            principal_requests_per_minute=data.get("principal_requests_per_minute"),
            sse_connections=data.get("sse_connections"),
            weight=int(data.get("weight") or 0),
            raw=data,
        )


class Quotas(_PreviewResource):
    """Tenant quotas. Rate-limit headers are not implemented server-side (§19 S6)."""

    def get(self) -> TenantQuota:
        return self._run(
            specs.Op(
                Call("GET", "/v1/quotas", idempotent=True),
                lambda r: TenantQuota.from_dict(_dict(r)),
            )
        )

    def set(self, **fields: Any) -> TenantQuota:
        return self._run(
            specs.Op(
                Call("PUT", "/v1/quotas", json=clean(fields)),
                lambda r: TenantQuota.from_dict(_dict(r)),
            )
        )
