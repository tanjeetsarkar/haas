"""`preview.ops`: fleet operations (`/v1/ops/*`) and `preview.isolation`."""

from __future__ import annotations

from typing import Any

from haas_sdk import _specs as specs
from haas_sdk._core import Call, clean, seg
from haas_sdk.preview._base import _PreviewResource, _dict


class Ops(_PreviewResource):
    """Fleet operations: cells, placements, fencing, metrics, restore epochs."""

    def cells(self) -> list[dict[str, Any]]:
        return self._run(
            specs.Op(
                Call("GET", "/v1/ops/cells", idempotent=True),
                _PreviewResource._items,
            )
        )

    def create_cell(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._run(specs.Op(Call("POST", "/v1/ops/cells", json=body), _dict))

    def cell(self, cell_id: str) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call("GET", f"/v1/ops/cells/{seg(cell_id)}", idempotent=True), _dict
            )
        )

    def update_cell(self, cell_id: str, **fields: Any) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call("PATCH", f"/v1/ops/cells/{seg(cell_id)}", json=clean(fields)),
                _dict,
            )
        )

    def placements(self) -> list[dict[str, Any]]:
        return self._run(
            specs.Op(
                Call("GET", "/v1/ops/placements", idempotent=True),
                _PreviewResource._items,
            )
        )

    def move_tenant(self, tenant_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call("POST", f"/v1/ops/placements/{seg(tenant_id)}/move", json=body),
                _dict,
            )
        )

    def finish_tenant_move(
        self, tenant_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "POST",
                    f"/v1/ops/placements/{seg(tenant_id)}/move/finish",
                    json=body,
                ),
                _dict,
            )
        )

    def fence_runs(self, body: dict[str, Any]) -> dict[str, int]:
        return self._run(specs.Op(Call("POST", "/v1/ops/fence-runs", json=body), _dict))

    def metrics(self) -> dict[str, Any]:
        return self._run(
            specs.Op(Call("GET", "/v1/ops/metrics", idempotent=True), _dict)
        )

    def restore_epoch(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            specs.Op(Call("POST", "/v1/ops/restore-epoch", json=body), _dict)
        )


class Isolation(_PreviewResource):
    """Plugin isolation: catalog, materialize, pack, sandbox protocol."""

    def catalog(self, plugin_id: str | None = None) -> list[dict[str, Any]]:
        path = "/v1/isolation/catalog"
        if plugin_id is not None:
            path += f"/{seg(plugin_id)}"
        return self._run(
            specs.Op(
                Call("GET", path, idempotent=True),
                _PreviewResource._items,
            )
        )

    def materialize(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            specs.Op(Call("POST", "/v1/isolation/materialize", json=body), _dict)
        )

    def pack(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._run(specs.Op(Call("POST", "/v1/isolation/pack", json=body), _dict))

    def sandbox_execute(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            specs.Op(Call("POST", "/v1/isolation/sandbox/execute", json=body), _dict)
        )

    def sandbox_protocol(self, run_id: str, invocation_id: str) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "GET",
                    f"/v1/isolation/sandbox/{seg(run_id)}/{seg(invocation_id)}",
                    idempotent=True,
                ),
                _dict,
            )
        )

    def delete_sandbox(self, run_id: str, invocation_id: str) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "DELETE",
                    f"/v1/isolation/sandbox/{seg(run_id)}/{seg(invocation_id)}",
                ),
                _dict,
            )
        )
