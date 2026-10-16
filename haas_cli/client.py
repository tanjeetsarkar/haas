from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

TERMINAL_EVENTS = ("run_completed", "run_error")
STREAM_ID_RE = re.compile(r"\d+-\d+")


class HaasAPIError(Exception):
    """The API answered with an error (or could not be reached)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class SSEEvent:
    event: str
    data: dict[str, Any] = field(default_factory=dict)
    id: str | None = None


class SSEParser:
    """Incremental `text/event-stream` parser fed one line at a time."""

    def __init__(self) -> None:
        self._id: str | None = None
        self._event = "message"
        self._data: list[str] = []

    def feed(self, line: str) -> SSEEvent | None:
        if line == "":
            if not self._data and self._id is None:
                self._event = "message"
                return None
            raw = "\n".join(self._data)
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"raw": raw}
            if not isinstance(data, dict):
                data = {"value": data}
            ev = SSEEvent(self._event, data, self._id)
            self._id, self._event, self._data = None, "message", []
            return ev
        if line.startswith(":"):
            return None
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "id":
            self._id = value
        elif name == "event":
            self._event = value
        elif name == "data":
            self._data.append(value)
        return None


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300] or resp.reason_phrase
    detail = body.get("detail", body) if isinstance(body, dict) else body
    return detail if isinstance(detail, str) else json.dumps(detail)


class HaasClient:
    def __init__(
        self,
        base_url: str,
        tenant: str = "default-tenant",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_reconnects: int = 5,
        reconnect_delay: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tenant = tenant
        self.max_reconnects = max_reconnects
        self.reconnect_delay = reconnect_delay
        # No read timeout: the stream is silent while the server blocks on Redis.
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            transport=transport,
            timeout=httpx.Timeout(30.0, read=None),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Tenant-ID": self.tenant}

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            resp = await self._http.request(method, path, headers=self._headers, **kw)
        except httpx.HTTPError as exc:
            raise HaasAPIError(
                f"Cannot reach {self.base_url}: {exc or type(exc).__name__}"
            ) from exc
        if resp.status_code >= 400:
            raise HaasAPIError(_detail(resp), resp.status_code)
        return resp.json()

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/healthz")

    async def plugins(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/plugins")

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/runs/{run_id}")

    async def create_run(
        self,
        prompt: str,
        *,
        thread_id: str | None = None,
        preload_skills: list[str] | None = None,
        primary_model: str | None = None,
        planner_model: str | None = None,
        max_steps: int | None = None,
        max_cost_usd: float | None = None,
        secrets: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "prompt": prompt,
            "thread_id": thread_id,
            "preload_skills": preload_skills or [],
            "primary_model": primary_model,
            "planner_model": planner_model,
            "max_steps": max_steps,
            "max_cost_usd": max_cost_usd,
            "secrets": secrets or {},
        }
        return await self._request(
            "POST", "/v1/runs", json={k: v for k, v in body.items() if v is not None}
        )

    async def resume(
        self,
        run_id: str,
        approved: bool,
        rejection_reason: str | None = None,
        secrets: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"approved": approved, "secrets": secrets or {}}
        if rejection_reason:
            body["rejection_reason"] = rejection_reason
        return await self._request("POST", f"/v1/runs/{run_id}/resume", json=body)

    async def stream_events(
        self, run_id: str, last_event_id: str = "0-0"
    ) -> AsyncIterator[SSEEvent]:
        """
        Yields events until the run completes/errors or pauses at `input_required`.
        Reconnects from the last seen event id if the connection drops mid-run.
        """
        cursor = last_event_id if STREAM_ID_RE.fullmatch(last_event_id) else "0-0"
        failures = 0
        while True:
            parser = SSEParser()
            try:
                async with self._http.stream(
                    "GET",
                    f"/v1/runs/{run_id}/stream",
                    headers={**self._headers, "Last-Event-ID": cursor},
                ) as resp:
                    if resp.status_code >= 400:
                        await resp.aread()
                        raise HaasAPIError(_detail(resp), resp.status_code)
                    async for line in resp.aiter_lines():
                        ev = parser.feed(line)
                        if ev is None:
                            continue
                        failures = 0
                        if ev.id:
                            cursor = ev.id
                        yield ev
                        if ev.event in TERMINAL_EVENTS or ev.event == "input_required":
                            return
            except httpx.HTTPError as exc:
                failures += 1
                if failures > self.max_reconnects:
                    raise HaasAPIError(
                        f"Lost connection to the event stream: {exc or type(exc).__name__}"
                    ) from exc
                await asyncio.sleep(self.reconnect_delay)
                continue

            # Server closed the stream cleanly without a final event.
            run = await self.get_run(run_id)
            if run["status"] != "running":
                return
            await asyncio.sleep(self.reconnect_delay)
