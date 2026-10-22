"""Shared plumbing for preview namespaces: a thin typed wrapper base."""

from __future__ import annotations

from typing import Any, TypeVar

from haas_sdk import _specs as specs
from haas_sdk._core import Call
from haas_sdk.preview import warn_preview

T = TypeVar("T")


class _PreviewResource:
    """Base for preview wrappers: same transport plumbing, preview warning."""

    def __init__(self, client: Any) -> None:
        warn_preview()
        self._client = client

    def _run(self, op: specs.Op[T]) -> T:
        return self._client._run(op)

    def _call(self, method: str, path: str, **kwargs: Any) -> Call:
        return Call(method, path, **kwargs)

    @staticmethod
    def _items(response: Any) -> list[dict[str, Any]]:
        body = response.json() if response.content else {}
        items = body.get("items") if isinstance(body, dict) else body
        return [i for i in (items or []) if isinstance(i, dict)]


def _dict(response: Any) -> dict[str, Any]:
    body = response.json() if response.content else {}
    return body if isinstance(body, dict) else {"value": body}


def _list_items(key: str = "items") -> Any:
    def parse(response: Any) -> list[dict[str, Any]]:
        body = _dict(response)
        items = body.get(key) if isinstance(body, dict) else body
        return [i for i in (items or []) if isinstance(i, dict)]

    return parse
