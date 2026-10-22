"""`preview.webhooks`: admin CRUD for endpoints, signing keys, deliveries (§9.9).

Verification itself is stable (`haas_sdk.webhooks`). The server lacks per-route
RBAC on these routes today, which is why this namespace stays preview (§19 S1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from haas_sdk import _specs as specs
from haas_sdk._core import Call, clean, seg
from haas_sdk.models import Page
from haas_sdk.preview._base import _PreviewResource, _dict


@dataclass(frozen=True, slots=True)
class WebhookEndpoint:
    endpoint_id: str
    url: str
    events: tuple[str, ...]
    enabled: bool
    description: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WebhookEndpoint:
        return cls(
            endpoint_id=str(data.get("endpoint_id") or ""),
            url=str(data.get("url") or ""),
            events=tuple(str(e) for e in (data.get("events") or [])),
            enabled=bool(data.get("enabled", True)),
            description=str(data.get("description") or ""),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class WebhookSigningKey:
    key_id: str
    status: str
    retire_at: str | None
    revoked_at: str | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WebhookSigningKey:
        return cls(
            key_id=str(data.get("key_id") or ""),
            status=str(data.get("status") or ""),
            retire_at=data.get("retire_at"),
            revoked_at=data.get("revoked_at"),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class WebhookDelivery:
    delivery_id: str
    endpoint_id: str | None
    event_type: str
    url: str
    status: str
    attempts: int
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WebhookDelivery:
        return cls(
            delivery_id=str(data.get("delivery_id") or ""),
            endpoint_id=data.get("endpoint_id"),
            event_type=str(data.get("event_type") or ""),
            url=str(data.get("url") or ""),
            status=str(data.get("status") or ""),
            attempts=int(data.get("attempts") or 0),
            raw=data,
        )


def _endpoint_page(response: Any) -> Page[WebhookEndpoint]:
    body = _dict(response)
    items = body.get("items") or []
    return Page(
        items=tuple(WebhookEndpoint.from_dict(i) for i in items if isinstance(i, dict)),
        next_cursor=body.get("next_cursor"),
    )


def _key_list(response: Any) -> list[WebhookSigningKey]:
    body = _dict(response)
    items = body.get("items") if isinstance(body, dict) else body
    return [
        WebhookSigningKey.from_dict(i) for i in (items or []) if isinstance(i, dict)
    ]


def _delivery_page(response: Any) -> Page[WebhookDelivery]:
    body = _dict(response)
    items = body.get("items") or []
    return Page(
        items=tuple(WebhookDelivery.from_dict(i) for i in items if isinstance(i, dict)),
        next_cursor=body.get("next_cursor"),
    )


class WebhooksAdmin(_PreviewResource):
    """Admin CRUD for webhook endpoints, signing keys and deliveries."""

    # -- endpoints -----------------------------------------------------------------

    def list_endpoints(self) -> Page[WebhookEndpoint]:
        return self._run(
            specs.Op(
                Call("GET", "/v1/webhooks/endpoints", idempotent=True), _endpoint_page
            )
        )

    def create_endpoint(
        self, url: str, *, events: list[str] | None = None, description: str = ""
    ) -> WebhookEndpoint:
        return self._run(
            specs.Op(
                Call(
                    "POST",
                    "/v1/webhooks/endpoints",
                    json=clean(
                        {"url": url, "events": events, "description": description}
                    ),
                ),
                lambda r: WebhookEndpoint.from_dict(_dict(r)),
            )
        )

    def get_endpoint(self, endpoint_id: str) -> WebhookEndpoint:
        return self._run(
            specs.Op(
                Call(
                    "GET", f"/v1/webhooks/endpoints/{seg(endpoint_id)}", idempotent=True
                ),
                lambda r: WebhookEndpoint.from_dict(_dict(r)),
            )
        )

    def update_endpoint(self, endpoint_id: str, **fields: Any) -> WebhookEndpoint:
        return self._run(
            specs.Op(
                Call(
                    "PATCH",
                    f"/v1/webhooks/endpoints/{seg(endpoint_id)}",
                    json=clean(fields),
                ),
                lambda r: WebhookEndpoint.from_dict(_dict(r)),
            )
        )

    def delete_endpoint(self, endpoint_id: str) -> None:
        self._run(
            specs.Op(
                Call("DELETE", f"/v1/webhooks/endpoints/{seg(endpoint_id)}"),
                lambda _r: None,
            )
        )

    # -- signing keys ----------------------------------------------------------------

    def list_signing_keys(self) -> list[WebhookSigningKey]:
        return self._run(
            specs.Op(
                Call("GET", "/v1/webhooks/signing-keys", idempotent=True), _key_list
            )
        )

    def create_signing_key(
        self, key_id: str, secret_envelope: str
    ) -> WebhookSigningKey:
        return self._run(
            specs.Op(
                Call(
                    "POST",
                    "/v1/webhooks/signing-keys",
                    json={"key_id": key_id, "secret_envelope": secret_envelope},
                ),
                lambda r: WebhookSigningKey.from_dict(_dict(r)),
            )
        )

    def rotate_signing_key(
        self, key_id: str, secret_envelope: str
    ) -> WebhookSigningKey:
        return self._run(
            specs.Op(
                Call(
                    "POST",
                    f"/v1/webhooks/signing-keys/{seg(key_id)}/rotate",
                    json={"secret_envelope": secret_envelope},
                ),
                lambda r: WebhookSigningKey.from_dict(_dict(r)),
            )
        )

    def revoke_signing_key(self, key_id: str) -> None:
        self._run(
            specs.Op(
                Call(
                    "POST", f"/v1/webhooks/signing-keys/{seg(key_id)}/revoke", json={}
                ),
                lambda _r: None,
            )
        )

    # -- deliveries ------------------------------------------------------------------

    def list_deliveries(
        self,
        *,
        run_id: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Page[WebhookDelivery]:
        params = clean({"run_id": run_id, "limit": limit, "cursor": cursor})
        return self._run(
            specs.Op(
                Call("GET", "/v1/webhooks/deliveries", params=params, idempotent=True),
                _delivery_page,
            )
        )

    def get_delivery(self, delivery_id: str) -> WebhookDelivery:
        return self._run(
            specs.Op(
                Call(
                    "GET",
                    f"/v1/webhooks/deliveries/{seg(delivery_id)}",
                    idempotent=True,
                ),
                lambda r: WebhookDelivery.from_dict(_dict(r)),
            )
        )

    def replay_deliveries(self, delivery_ids: list[str]) -> dict[str, Any]:
        return self._run(
            specs.Op(
                Call(
                    "POST",
                    "/v1/webhooks/deliveries/replay",
                    json={"delivery_ids": delivery_ids},
                ),
                _dict,
            )
        )
