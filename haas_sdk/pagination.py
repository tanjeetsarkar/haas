"""Cursor pagination helpers (§11).

`iter()` loops until `next_cursor` is ``None`` or repeats. End-of-list is never
inferred from a short page (SDK-PAG-01): filtered scans can return fewer than
``limit`` items with a cursor.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TypeVar

from haas_sdk.models import Page

T = TypeVar("T")


def paginate(
    fetch: Callable[[str | None], Page[T]],
) -> Iterator[T]:
    """Yield every item across pages, following opaque cursors."""
    cursor: str | None = None
    while True:
        page = fetch(cursor)
        yield from page.items
        if not page.next_cursor or page.next_cursor == cursor:
            return
        cursor = page.next_cursor
