"""`preview.mesh`: agent-to-agent mesh namespaces (`/v1/agent-mesh/*`)."""

from __future__ import annotations

from typing import Any

from haas_sdk import _specs as specs
from haas_sdk._core import Call, clean, seg
from haas_sdk.preview._base import _PreviewResource, _dict


class Mesh(_PreviewResource):
    """Agent mesh: topologies, delegations, messages, mailbox, cards, workspace."""

    def topologies(self) -> list[dict[str, Any]]:
        return self._run(
            specs.Op(
                Call("GET", "/v1/agent-mesh/topologies", idempotent=True),
                _PreviewResource._items,
            )
        )

    def create_topology(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            specs.Op(Call("POST", "/v1/agent-mesh/topologies", json=body), _dict)
        )

    def delegations(self) -> list[dict[str, Any]]:
        return self._run(
            specs.Op(
                Call("GET", "/v1/agent-mesh/delegations", idempotent=True),
                _PreviewResource._items,
            )
        )

    def create_delegation(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            specs.Op(Call("POST", "/v1/agent-mesh/delegations", json=body), _dict)
        )

    def get_delegation(self, delegation_id: str) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "GET",
                    f"/v1/agent-mesh/delegations/{seg(delegation_id)}",
                    idempotent=True,
                ),
                _dict,
            )
        )

    def messages(
        self, *, limit: int | None = None, cursor: str | None = None
    ) -> list[dict[str, Any]]:
        params = clean({"limit": limit, "cursor": cursor})
        return self._run(
            specs.Op(
                Call("GET", "/v1/agent-mesh/messages", params=params, idempotent=True),
                _PreviewResource._items,
            )
        )

    def send_message(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            specs.Op(Call("POST", "/v1/agent-mesh/messages", json=body), _dict)
        )

    def send_mailbox(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            specs.Op(Call("POST", "/v1/agent-mesh/mailbox/send", json=body), _dict)
        )

    def mailbox(self, mailbox_id: str) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "GET",
                    f"/v1/agent-mesh/mailbox/{seg(mailbox_id)}",
                    idempotent=True,
                ),
                _dict,
            )
        )

    def agent_cards(self) -> list[dict[str, Any]]:
        return self._run(
            specs.Op(
                Call("GET", "/v1/agent-mesh/agent-cards", idempotent=True),
                _PreviewResource._items,
            )
        )

    def create_agent_card(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            specs.Op(Call("POST", "/v1/agent-mesh/agent-cards", json=body), _dict)
        )

    def agent_card(self, card_id: str) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "GET",
                    f"/v1/agent-mesh/agent-cards/{seg(card_id)}",
                    idempotent=True,
                ),
                _dict,
            )
        )

    def agent_card_for_revision(self, agent_revision_id: str) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "GET",
                    f"/v1/agent-mesh/a2a/agent-cards/{seg(agent_revision_id)}",
                    idempotent=True,
                ),
                _dict,
            )
        )

    def mcp_servers(self) -> list[dict[str, Any]]:
        return self._run(
            specs.Op(
                Call("GET", "/v1/agent-mesh/mcp/servers", idempotent=True),
                _PreviewResource._items,
            )
        )

    def create_mcp_server(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            specs.Op(Call("POST", "/v1/agent-mesh/mcp/servers", json=body), _dict)
        )

    def observability(self, run_id: str) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "GET",
                    f"/v1/agent-mesh/observability/{seg(run_id)}",
                    idempotent=True,
                ),
                _dict,
            )
        )

    def workspace(
        self, run_id: str, agent_id: str, key: str | None = None
    ) -> dict[str, Any]:
        path = f"/v1/agent-mesh/workspace/{seg(run_id)}/{seg(agent_id)}"
        if key is not None:
            path += f"/{seg(key)}"
        return self._run(specs.Op(Call("GET", path, idempotent=True), _dict))

    def create_workspace_entry(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            specs.Op(Call("POST", "/v1/agent-mesh/workspace", json=body), _dict)
        )
