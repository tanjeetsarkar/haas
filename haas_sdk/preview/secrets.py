"""`preview.secrets`: tenant secret bindings (`/v1/secrets/bindings`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from haas_sdk import _specs as specs
from haas_sdk._core import Call, clean, seg
from haas_sdk.preview._base import _PreviewResource, _dict


@dataclass(frozen=True, slots=True)
class SecretBinding:
    binding_id: str
    name: str
    provider: str
    version: int
    allowed_plugins: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SecretBinding:
        return cls(
            binding_id=str(data.get("binding_id") or ""),
            name=str(data.get("name") or ""),
            provider=str(data.get("provider") or ""),
            version=int(data.get("version") or 0),
            allowed_plugins=tuple(str(p) for p in (data.get("allowed_plugins") or [])),
            allowed_tools=tuple(str(t) for t in (data.get("allowed_tools") or [])),
            raw=data,
        )


def _binding_list(response: Any) -> list[SecretBinding]:
    body = _dict(response)
    items = body.get("items") if isinstance(body, dict) else body
    return [SecretBinding.from_dict(i) for i in (items or []) if isinstance(i, dict)]


class Secrets(_PreviewResource):
    """Tenant-scoped secret bindings (values never leave the server vault)."""

    def list(self) -> list[SecretBinding]:
        return self._run(
            specs.Op(
                Call("GET", "/v1/secrets/bindings", idempotent=True), _binding_list
            )
        )

    def create(
        self,
        *,
        name: str,
        provider: str,
        encrypted_value: str | None = None,
        provider_reference: str | None = None,
        allowed_plugins: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        description: str = "",
    ) -> SecretBinding:
        return self._run(
            specs.Op(
                Call(
                    "POST",
                    "/v1/secrets/bindings",
                    json=clean(
                        {
                            "name": name,
                            "provider": provider,
                            "encrypted_value": encrypted_value,
                            "provider_reference": provider_reference,
                            "allowed_plugins": allowed_plugins,
                            "allowed_tools": allowed_tools,
                            "description": description,
                        }
                    ),
                ),
                lambda r: SecretBinding.from_dict(_dict(r)),
            )
        )

    def get(self, binding_id: str) -> SecretBinding:
        return self._run(
            specs.Op(
                Call("GET", f"/v1/secrets/bindings/{seg(binding_id)}", idempotent=True),
                lambda r: SecretBinding.from_dict(_dict(r)),
            )
        )

    def update(self, binding_id: str, **fields: Any) -> SecretBinding:
        return self._run(
            specs.Op(
                Call(
                    "PATCH",
                    f"/v1/secrets/bindings/{seg(binding_id)}",
                    json=clean(fields),
                ),
                lambda r: SecretBinding.from_dict(_dict(r)),
            )
        )

    def delete(self, binding_id: str) -> None:
        self._run(
            specs.Op(
                Call("DELETE", f"/v1/secrets/bindings/{seg(binding_id)}"),
                lambda _r: None,
            )
        )
