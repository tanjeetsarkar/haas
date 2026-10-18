from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, NoReturn
from urllib.parse import quote

import httpx

TERMINAL_EVENTS = ("run_completed", "run_error", "run_cancelled")
# SSE ids are the durable per-run event sequence (`run-events.v1`).
STREAM_ID_RE = re.compile(r"\d{1,18}")
ACTIVE_RUN_STATUSES = ("queued", "running", "cancelling")
OUTPUT_DELTA_EVENT = "run.output.delta"
OUTPUT_DELTA_FEATURE = "output-deltas.v1"


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
        credential: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_reconnects: int = 5,
        reconnect_delay: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tenant = tenant
        self.credential = credential
        self.auth_expired = False
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
        headers = {"X-Tenant-ID": self.tenant}
        if self.credential:
            headers["Authorization"] = f"Bearer {self.credential}"
        return headers

    def set_credential(self, credential: str | None) -> None:
        self.credential = credential
        self.auth_expired = False

    def set_connection(self, base_url: str, tenant: str) -> None:
        normalized_url = base_url.rstrip("/")
        self.base_url = normalized_url
        self.tenant = tenant
        self._http.base_url = httpx.URL(f"{normalized_url}/")

    def _raise_api_error(self, response: httpx.Response) -> NoReturn:
        if response.status_code == 401:
            self.credential = None
            self.auth_expired = True
        raise HaasAPIError(_detail(response), response.status_code)

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            resp = await self._http.request(method, path, headers=self._headers, **kw)
        except httpx.HTTPError as exc:
            raise HaasAPIError(
                f"Cannot reach {self.base_url}: {exc or type(exc).__name__}"
            ) from exc
        if resp.status_code >= 400:
            self._raise_api_error(resp)
        return resp.json()

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/healthz")

    async def plugins(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/plugins")

    async def identity(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/me")

    async def capabilities(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/capabilities")

    async def list_threads(
        self, *, cursor: str | None = None, search: str = ""
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/v1/threads",
            params={
                "search": search,
                **({"cursor": cursor} if cursor else {}),
            },
        )

    async def thread_history(
        self,
        thread_id: str,
        *,
        offset: int = 0,
        history_version: int | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/v1/threads/{quote(thread_id, safe='')}",
            params={
                "offset": offset,
                **(
                    {"history_version": history_version}
                    if history_version is not None
                    else {}
                ),
            },
        )

    async def branch_thread(
        self, thread_id: str, history_version: int
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/v1/threads/{quote(thread_id, safe='')}/branches",
            json={"expected_history_version": history_version},
        )

    async def list_agent_revisions(self, cursor: str | None = None) -> dict[str, Any]:
        return await self._request(
            "GET", "/v1/agents/revisions", params={"cursor": cursor} if cursor else {}
        )

    async def agent_revision(self, revision_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/v1/agents/revisions/{quote(revision_id, safe='')}"
        )

    async def draft_agent(
        self, name: str, configuration: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/agents/revisions",
            json={"agent_name": name, "configuration": configuration},
        )

    async def transition_agent(
        self, revision_id: str, operation: str
    ) -> dict[str, Any]:
        if operation not in {"validate", "publish"}:
            raise ValueError("Agent operation must be validate or publish.")
        return await self._request(
            "POST",
            f"/v1/agents/revisions/{quote(revision_id, safe='')}/{operation}",
        )

    async def list_memberships(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/admin/memberships")

    async def grant_membership(
        self, *, subject: str, role: str, kind: str = "human"
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/admin/memberships",
            json={"subject": subject, "role": role, "kind": kind},
        )

    async def revoke_membership(self, principal_id: str, role: str) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/v1/admin/memberships/{quote(principal_id, safe='')}/{quote(role, safe='')}",
        )

    async def list_api_keys(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/admin/api-keys")

    async def create_api_key(
        self,
        *,
        principal_id: str,
        name: str,
        expires_in_days: int = 90,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/admin/api-keys",
            json={
                "principal_id": principal_id,
                "name": name,
                "expires_in_days": expires_in_days,
            },
        )

    async def revoke_api_key(self, key_id: str) -> dict[str, Any]:
        return await self._request(
            "DELETE", f"/v1/admin/api-keys/{quote(key_id, safe='')}"
        )

    async def get_tenant_policy(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/admin/policy")

    async def activate_tenant_policy(self, rules: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PUT", "/v1/admin/policy", json={"rules": rules})

    async def list_audit_events(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, int | str] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/v1/audit-events", params=params)

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/runs/{run_id}")

    async def list_runs(
        self, *, limit: int = 20, cursor: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, int | str] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/v1/runs", params=params)

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/v1/runs/{run_id}/cancel")

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
        agent_revision_id: str | None = None,
        expected_history_version: int | None = None,
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
        if agent_revision_id is not None:
            body = {
                "prompt": prompt,
                "thread_id": thread_id,
                "agent_revision_id": agent_revision_id,
                "secrets": secrets or {},
            }
        if expected_history_version is not None:
            body["expected_history_version"] = expected_history_version
        return await self._request(
            "POST", "/v1/runs", json={k: v for k, v in body.items() if v is not None}
        )

    async def resume(
        self,
        run_id: str,
        approved: bool,
        rejection_reason: str | None = None,
        secrets: dict[str, str] | None = None,
        *,
        batch_id: str | None = None,
        batch_digest: str | None = None,
    ) -> dict[str, Any]:
        """Resolve the pending approval; `batch_id`/`batch_digest` bind it to what was reviewed."""
        body: dict[str, Any] = {"approved": approved, "secrets": secrets or {}}
        if rejection_reason:
            body["rejection_reason"] = rejection_reason
        if batch_id is not None:
            body["batch_id"] = batch_id
        if batch_digest is not None:
            body["batch_digest"] = batch_digest
        return await self._request("POST", f"/v1/runs/{run_id}/resume", json=body)

    async def list_approvals(
        self, *, status: str = "pending", limit: int = 50
    ) -> dict[str, Any]:
        params: dict[str, int | str] = {"status": status, "limit": limit}
        return await self._request("GET", "/v1/approvals", params=params)

    async def get_approval(self, batch_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/approvals/{quote(batch_id, safe='')}")

    async def decide_approval(
        self,
        batch_id: str,
        *,
        approve: bool,
        batch_digest: str,
        reason: str | None = None,
        secrets: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "decision": "approve" if approve else "reject",
            "batch_digest": batch_digest,
            "secrets": secrets or {},
        }
        if reason:
            body["reason"] = reason
        return await self._request(
            "POST", f"/v1/approvals/{quote(batch_id, safe='')}/decision", json=body
        )

    async def run_events(
        self, run_id: str, *, after: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/v1/runs/{run_id}/events",
            params={"after": after, "limit": limit},
        )

    async def run_steps(self, run_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/runs/{run_id}/steps")

    async def run_usage(self, run_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/runs/{run_id}/usage")

    async def stream_events(
        self, run_id: str, last_event_id: str = "0", *, output_deltas: bool = False
    ) -> AsyncIterator[SSEEvent]:
        """
        Yields events until the run completes/errors or pauses at `input_required`.
        Reconnects from the last seen durable sequence if the connection drops mid-run.
        With `output_deltas`, advisory `run.output.delta` frames (no SSE id) are
        interleaved; they never advance the durable cursor.
        """
        cursor = last_event_id if STREAM_ID_RE.fullmatch(last_event_id) else "0"
        failures = 0
        while True:
            parser = SSEParser()
            try:
                async with self._http.stream(
                    "GET",
                    f"/v1/runs/{run_id}/stream",
                    headers={**self._headers, "Last-Event-ID": cursor},
                    params={"output_deltas": "true"} if output_deltas else None,
                ) as resp:
                    if resp.status_code >= 400:
                        await resp.aread()
                        self._raise_api_error(resp)
                    async for line in resp.aiter_lines():
                        ev = parser.feed(line)
                        if ev is None:
                            continue
                        failures = 0
                        if ev.event == "stream_closed":
                            reason = (
                                ev.data.get("reason")
                                if isinstance(ev.data, dict)
                                else None
                            )
                            self.auth_expired = True
                            raise HaasAPIError(
                                f"Event stream closed by the server: {reason or 'reauthentication failed'}",
                                401,
                            )
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
            if run["status"] not in ACTIVE_RUN_STATUSES:
                return
            await asyncio.sleep(self.reconnect_delay)
