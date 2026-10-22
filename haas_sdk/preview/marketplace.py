"""`preview.marketplace`: listings, installations, publishers, search (§9.10).

Promotion is gated on server auth fixes (§19 S1): several marketplace handlers
lack `require_action` today.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from haas_sdk import _specs as specs
from haas_sdk._core import Call, clean, seg
from haas_sdk.preview._base import _PreviewResource, _dict


@dataclass(frozen=True, slots=True)
class Listing:
    listing_id: str
    plugin_id: str
    publisher_id: str
    name: str
    display_name: str
    status: str
    visibility: str
    install_count: int
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Listing:
        return cls(
            listing_id=str(data.get("listing_id") or ""),
            plugin_id=str(data.get("plugin_id") or ""),
            publisher_id=str(data.get("publisher_id") or ""),
            name=str(data.get("name") or ""),
            display_name=str(data.get("display_name") or ""),
            status=str(data.get("status") or ""),
            visibility=str(data.get("visibility") or ""),
            install_count=int(data.get("install_count") or 0),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class Installation:
    installation_id: str
    listing_id: str
    status: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Installation:
        return cls(
            installation_id=str(data.get("installation_id") or ""),
            listing_id=str(data.get("listing_id") or ""),
            status=str(data.get("status") or ""),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class Publisher:
    publisher_id: str
    name: str
    status: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Publisher:
        return cls(
            publisher_id=str(data.get("publisher_id") or ""),
            name=str(data.get("name") or ""),
            status=str(data.get("status") or ""),
            raw=data,
        )


def _listing_page(response: Any) -> Any:
    body = _dict(response)
    items = body.get("items") or []
    return items


class Marketplace(_PreviewResource):
    """Marketplace surface (thin typed wrappers over the OpenAPI snapshot)."""

    def listings(
        self, *, limit: int | None = None, cursor: str | None = None
    ) -> list[dict[str, Any]]:
        params = clean({"limit": limit, "cursor": cursor})
        return self._run(
            specs.Op(
                Call("GET", "/v1/marketplace/listings", params=params, idempotent=True),
                _PreviewResource._items,
            )
        )

    def get_listing(self, listing_id: str) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "GET",
                    f"/v1/marketplace/listings/{seg(listing_id)}",
                    idempotent=True,
                ),
                _dict,
            )
        )

    def create_listing(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            specs.Op(Call("POST", "/v1/marketplace/listings", json=body), _dict)
        )

    def update_listing(self, listing_id: str, **fields: Any) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "PATCH",
                    f"/v1/marketplace/listings/{seg(listing_id)}",
                    json=clean(fields),
                ),
                _dict,
            )
        )

    def installations(
        self, *, limit: int | None = None, cursor: str | None = None
    ) -> list[dict[str, Any]]:
        params = clean({"limit": limit, "cursor": cursor})
        return self._run(
            specs.Op(
                Call(
                    "GET",
                    "/v1/marketplace/installations",
                    params=params,
                    idempotent=True,
                ),
                _PreviewResource._items,
            )
        )

    def install(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            specs.Op(Call("POST", "/v1/marketplace/installations", json=body), _dict)
        )

    def get_installation(self, installation_id: str) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "GET",
                    f"/v1/marketplace/installations/{seg(installation_id)}",
                    idempotent=True,
                ),
                _dict,
            )
        )

    def update_installation(
        self, installation_id: str, **fields: Any
    ) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "PATCH",
                    f"/v1/marketplace/installations/{seg(installation_id)}",
                    json=clean(fields),
                ),
                _dict,
            )
        )

    def publishers(self) -> list[dict[str, Any]]:
        return self._run(
            specs.Op(
                Call("GET", "/v1/marketplace/publishers", idempotent=True),
                _PreviewResource._items,
            )
        )

    def get_publisher(self, publisher_id: str) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "GET",
                    f"/v1/marketplace/publishers/{seg(publisher_id)}",
                    idempotent=True,
                ),
                _dict,
            )
        )

    def create_publisher(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            specs.Op(Call("POST", "/v1/marketplace/publishers", json=body), _dict)
        )

    def publisher_keys(self, publisher_id: str) -> list[dict[str, Any]]:
        return self._run(
            specs.Op(
                Call(
                    "GET",
                    f"/v1/marketplace/publishers/{seg(publisher_id)}/keys",
                    idempotent=True,
                ),
                _PreviewResource._items,
            )
        )

    def create_publisher_key(
        self, publisher_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "POST",
                    f"/v1/marketplace/publishers/{seg(publisher_id)}/keys",
                    json=body,
                ),
                _dict,
            )
        )

    def revoke_publisher_key(self, publisher_id: str, key_id: str) -> None:
        self._run(
            specs.Op(
                Call(
                    "POST",
                    f"/v1/marketplace/publishers/{seg(publisher_id)}/keys/{seg(key_id)}/revoke",
                    json={},
                ),
                lambda _r: None,
            )
        )

    def search(
        self, query: str, *, limit: int | None = None, offset: int | None = None
    ) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "POST",
                    "/v1/marketplace/search",
                    json=clean({"query": query, "limit": limit, "offset": offset}),
                ),
                _dict,
            )
        )

    def revision(self, revision_id: str) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "GET",
                    f"/v1/marketplace/revisions/{seg(revision_id)}",
                    idempotent=True,
                ),
                _dict,
            )
        )

    def review_revision(self, revision_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "POST",
                    f"/v1/marketplace/revisions/{seg(revision_id)}/review",
                    json=body,
                ),
                _dict,
            )
        )

    def yank_revision(self, revision_id: str, reason: str = "") -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "POST",
                    f"/v1/marketplace/revisions/{seg(revision_id)}/yank",
                    json=clean({"reason": reason}),
                ),
                _dict,
            )
        )

    def abuse_reports(self) -> list[dict[str, Any]]:
        return self._run(
            specs.Op(
                Call("GET", "/v1/marketplace/abuse-reports", idempotent=True),
                _PreviewResource._items,
            )
        )

    def create_abuse_report(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            specs.Op(Call("POST", "/v1/marketplace/abuse-reports", json=body), _dict)
        )

    def notifications(self) -> list[dict[str, Any]]:
        return self._run(
            specs.Op(
                Call("GET", "/v1/marketplace/notifications", idempotent=True),
                _PreviewResource._items,
            )
        )

    def acknowledge_notification(self, notification_id: str) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "POST",
                    f"/v1/marketplace/notifications/{seg(notification_id)}/acknowledge",
                    json={},
                ),
                _dict,
            )
        )

    def transparency_log(self) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call("GET", "/v1/marketplace/transparency-log", idempotent=True), _dict
            )
        )
