"""Deprecation signalling for HTTP routes (RFC 9745 `Deprecation`, RFC 8594 `Sunset`).

Usage::

    @app.get("/v1/old", deprecated=True, dependencies=[deprecated_route(OLD)])

FastAPI merges headers set on the injected `Response` into JSON/dict results; routes
that return a raw `Response`/`StreamingResponse` must add `deprecation_headers(...)`
themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

from fastapi import Depends, Response

POLICY_URL = "https://docs.haas.dev/api/deprecation-policy"
MIN_SUNSET_DAYS = 180


@dataclass(frozen=True)
class RouteDeprecation:
    since: datetime
    sunset: datetime | None = None
    successor: str | None = None
    policy_url: str = POLICY_URL

    def __post_init__(self) -> None:
        for value in (self.since, self.sunset):
            if value is not None and value.tzinfo is None:
                raise ValueError("Deprecation dates must be timezone-aware.")
        if self.sunset is not None:
            if (self.sunset - self.since).days < MIN_SUNSET_DAYS:
                raise ValueError(
                    f"Sunset must be at least {MIN_SUNSET_DAYS} days after deprecation."
                )


def deprecation_headers(dep: RouteDeprecation) -> dict[str, str]:
    headers = {"Deprecation": f"@{int(dep.since.timestamp())}"}
    if dep.sunset is not None:
        headers["Sunset"] = format_datetime(dep.sunset.astimezone(UTC), usegmt=True)
    links = [f'<{dep.policy_url}>; rel="deprecation"; type="text/html"']
    if dep.successor:
        links.append(f'<{dep.successor}>; rel="successor-version"')
    headers["Link"] = ", ".join(links)
    return headers


def deprecated_route(dep: RouteDeprecation) -> Any:
    headers = deprecation_headers(dep)

    async def mark(response: Response) -> None:
        response.headers.update(headers)

    return Depends(mark)
