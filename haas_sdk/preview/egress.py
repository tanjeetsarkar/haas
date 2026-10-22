"""`preview.egress`: egress policy and host checks (`/v1/egress/*`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from haas_sdk import _specs as specs
from haas_sdk._core import Call
from haas_sdk.preview._base import _PreviewResource, _dict


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    allow_hosts: tuple[str, ...]
    allow_cidrs: tuple[str, ...]
    max_bytes_per_run: int | None
    version: int
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EgressPolicy:
        return cls(
            allow_hosts=tuple(str(h) for h in (data.get("allow_hosts") or [])),
            allow_cidrs=tuple(str(c) for c in (data.get("allow_cidrs") or [])),
            max_bytes_per_run=data.get("max_bytes_per_run"),
            version=int(data.get("version") or 0),
            raw=data,
        )


class Egress(_PreviewResource):
    """Tenant egress policy and one-shot host checks."""

    def policy(self) -> EgressPolicy:
        return self._run(
            specs.Op(
                Call("GET", "/v1/egress/policy", idempotent=True),
                lambda r: EgressPolicy.from_dict(_dict(r)),
            )
        )

    def set_policy(
        self,
        *,
        allow_hosts: list[str] | None = None,
        allow_cidrs: list[str] | None = None,
        max_bytes_per_run: int | None = None,
    ) -> EgressPolicy:
        return self._run(
            specs.Op(
                Call(
                    "PUT",
                    "/v1/egress/policy",
                    json={
                        "allow_hosts": allow_hosts or [],
                        "allow_cidrs": allow_cidrs or [],
                        "max_bytes_per_run": max_bytes_per_run,
                    },
                ),
                lambda r: EgressPolicy.from_dict(_dict(r)),
            )
        )

    def check(self, host: str) -> dict[str, Any]:
        """Ask the server whether a destination host is allowed."""
        return self._run(
            specs.Op(Call("POST", "/v1/egress/check", json={"host": host}), _dict)
        )
