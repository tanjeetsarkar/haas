"""Asynchronous HaaS client (mirrors :mod:`haas_sdk.client`)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from datetime import datetime
from types import TracebackType
from typing import Any, BinaryIO, TypeVar

import httpx

from haas_sdk import _specs as specs
from haas_sdk._core import Call, ClientOptions, api_error, retry_delay
from haas_sdk._sse import SSEParser
from haas_sdk.errors import APIError, HaasConnectionError, StreamClosedError
from haas_sdk.models import (
    AgentRevision,
    ApprovalBatch,
    Artifact,
    Page,
    Run,
    RunEvent,
    Thread,
)

T = TypeVar("T")


class AsyncHaas:
    """Asyncio client for the HaaS ``/v1`` API.

    >>> async with AsyncHaas(api_key="hk_test_...") as haas:
    ...     run = await haas.runs.create("Summarise the release notes")
    ...     async for event in haas.runs.stream(run["run_id"]):
    ...         print(event.type)
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        tenant: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        stream_max_reconnects: int = 5,
        stream_reconnect_delay: float = 1.0,
        default_headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.options = ClientOptions.resolve(
            base_url,
            api_key,
            tenant,
            timeout=timeout,
            max_retries=max_retries,
            stream_max_reconnects=stream_max_reconnects,
            stream_reconnect_delay=stream_reconnect_delay,
            default_headers=default_headers,
        )
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            base_url=self.options.base_url, timeout=timeout, transport=transport
        )
        self._sleep = sleep
        self.runs = Runs(self)
        self.approvals = Approvals(self)
        self.threads = Threads(self)
        self.agents = Agents(self)
        self.artifacts = Artifacts(self)

    async def __aenter__(self) -> AsyncHaas:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def me(self) -> dict[str, Any]:
        return await self._run(specs.me())

    async def capabilities(self) -> dict[str, Any]:
        return await self._run(specs.capabilities())

    async def health(self, path: str = "/readyz") -> dict[str, Any]:
        return await self._run(specs.health(path))

    # -- plumbing ----------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return path if self._owns_http else self.options.base_url + path

    def _build(self, call: Call) -> httpx.Request:
        return self._http.build_request(
            call.method,
            self._url(call.path),
            params=call.params,
            json=call.json,
            files=call.files,
            headers={**self.options.headers(), **call.headers},
        )

    async def _send(self, call: Call, *, stream: bool = False) -> httpx.Response:
        attempt = 0
        while True:
            try:
                response = await self._http.send(self._build(call), stream=stream)
            except httpx.TransportError as exc:
                delay = retry_delay(self.options, call, attempt, error=exc)
                if delay is None:
                    raise HaasConnectionError(str(exc) or type(exc).__name__) from exc
            else:
                if response.status_code < 400:
                    return response
                if stream:
                    await response.aread()
                    await response.aclose()
                delay = retry_delay(self.options, call, attempt, response=response)
                if delay is None:
                    raise api_error(response)
            await self._sleep(delay)
            attempt += 1

    async def _run(self, op: specs.Op[T]) -> T:
        return op.parse(await self._send(op.call))


class _Resource:
    def __init__(self, client: AsyncHaas) -> None:
        self._client = client

    async def _run(self, op: specs.Op[T]) -> T:
        return await self._client._run(op)


async def _paginate(fetch: Callable[[str | None], Awaitable[Page[T]]]) -> AsyncIterator[T]:
    cursor: str | None = None
    while True:
        page = await fetch(cursor)
        for item in page.items:
            yield item
        if not page.next_cursor or page.next_cursor == cursor:
            return
        cursor = page.next_cursor


class Runs(_Resource):
    async def create(
        self,
        prompt: str,
        *,
        thread_id: str | None = None,
        agent_revision_id: str | None = None,
        idempotency_key: str | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Submit a run. A fresh ``Idempotency-Key`` is generated (and reused on retry)."""
        extra = {"thread_id": thread_id, "agent_revision_id": agent_revision_id, **params}
        return await self._run(specs.run_create(prompt, idempotency_key=idempotency_key, extra=extra))

    async def get(self, run_id: str) -> Run:
        return await self._run(specs.run_get(run_id))

    async def list(
        self,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        status: str | Iterable[str] | None = None,
        thread_id: str | None = None,
        created_after: datetime | float | str | None = None,
        created_before: datetime | float | str | None = None,
    ) -> Page[Run]:
        return await self._run(
            specs.run_list(
                limit=limit,
                cursor=cursor,
                status=status,
                thread_id=thread_id,
                created_after=created_after,
                created_before=created_before,
            )
        )

    def iter(self, *, page_size: int | None = None, **filters: Any) -> AsyncIterator[Run]:
        return _paginate(lambda c: self.list(limit=page_size, cursor=c, **filters))

    async def cancel(self, run_id: str) -> dict[str, Any]:
        return await self._run(specs.run_cancel(run_id))

    async def resume(
        self,
        run_id: str,
        *,
        approved: bool,
        batch_id: str | None = None,
        batch_digest: str | None = None,
        rejection_reason: str | None = None,
        secrets: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._run(
            specs.run_resume(
                run_id,
                approved=approved,
                batch_id=batch_id,
                batch_digest=batch_digest,
                rejection_reason=rejection_reason,
                secrets=secrets,
                idempotency_key=idempotency_key,
            )
        )

    async def events(self, run_id: str, *, after: int = 0, limit: int | None = None) -> list[RunEvent]:
        return await self._run(specs.run_events(run_id, after=after, limit=limit))

    async def steps(self, run_id: str) -> dict[str, Any]:
        return await self._run(specs.run_subresource(run_id, "steps"))

    async def usage(self, run_id: str) -> dict[str, Any]:
        return await self._run(specs.run_subresource(run_id, "usage"))

    async def tree(self, run_id: str) -> dict[str, Any]:
        return await self._run(specs.run_subresource(run_id, "tree"))

    async def stream(
        self,
        run_id: str,
        *,
        last_event_id: str | int | None = None,
        output_deltas: bool = False,
    ) -> AsyncIterator[RunEvent]:
        """Yield run events until a terminal event or ``input_required``.

        Dropped connections are resumed with ``Last-Event-ID``; replayed frames are
        de-duplicated by sequence so every durable event is yielded exactly once.
        """
        client = self._client
        options = client.options
        last_id = None if last_event_id is None else str(last_event_id)
        last_seq = int(last_id) if last_id is not None and last_id.isdigit() else None
        failures = 0
        while True:
            call = specs.run_stream_call(
                run_id, last_event_id=last_id, output_deltas=output_deltas
            )
            error: Exception | None = None
            try:
                response = await client._send(call, stream=True)
                try:
                    parser = SSEParser()
                    async for line in response.aiter_lines():
                        event = parser.feed(line)
                        if event is None:
                            continue
                        if event.type == "stream_closed":
                            raise StreamClosedError(
                                str(event.data.get("reason") or "stream closed by server")
                            )
                        seq = event.sequence
                        if seq is not None:
                            if last_seq is not None and seq <= last_seq:
                                continue
                            last_seq, last_id, failures = seq, event.id, 0
                        yield event
                        if event.is_terminal or event.type == "input_required":
                            return
                finally:
                    await response.aclose()
            except (HaasConnectionError, httpx.TransportError) as exc:
                error = exc
            except APIError as exc:
                if exc.status < 500 and exc.status != 429:
                    raise
                error = exc
            if failures >= options.stream_max_reconnects:
                raise HaasConnectionError(
                    f"stream for run {run_id} ended after {failures} reconnects"
                ) from error
            failures += 1
            await client._sleep(options.stream_reconnect_delay * failures)

    async def wait(
        self,
        run_id: str,
        *,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
        until_input_required: bool = True,
    ) -> Run:
        """Poll until the run is terminal (or waiting for approval)."""
        deadline = time.monotonic() + timeout
        while True:
            run = await self.get(run_id)
            if run.is_terminal or (until_input_required and run.waiting_for_approval):
                return run
            if time.monotonic() >= deadline:
                raise TimeoutError(f"run {run_id} still {run.status} after {timeout}s")
            await self._client._sleep(poll_interval)


class Approvals(_Resource):
    async def list(
        self,
        *,
        status: str | None = "pending",
        run_id: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Page[ApprovalBatch]:
        return await self._run(
            specs.approval_list(status=status, run_id=run_id, limit=limit, cursor=cursor)
        )

    def iter(self, *, page_size: int | None = None, **filters: Any) -> AsyncIterator[ApprovalBatch]:
        return _paginate(lambda c: self.list(limit=page_size, cursor=c, **filters))

    async def get(self, batch_id: str) -> ApprovalBatch:
        return await self._run(specs.approval_get(batch_id))

    async def decide(
        self,
        batch_id: str,
        decision: str,
        *,
        batch_digest: str | None = None,
        reason: str | None = None,
        secrets: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Decide a batch. Without ``batch_digest`` the current digest is fetched first;
        pass the digest you reviewed to guarantee you approve exactly what you saw."""
        digest = batch_digest or (await self.get(batch_id)).batch_digest
        return await self._run(
            specs.approval_decide(
                batch_id,
                decision=decision,
                batch_digest=digest,
                reason=reason,
                secrets=secrets,
                idempotency_key=idempotency_key,
            )
        )

    async def approve(self, batch_id: str, **kwargs: Any) -> dict[str, Any]:
        return await self.decide(batch_id, "approve", **kwargs)

    async def deny(self, batch_id: str, **kwargs: Any) -> dict[str, Any]:
        return await self.decide(batch_id, "reject", **kwargs)


class Threads(_Resource):
    async def list(
        self, *, search: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> Page[Thread]:
        return await self._run(specs.thread_list(search=search, limit=limit, cursor=cursor))

    def iter(self, *, page_size: int | None = None, **filters: Any) -> AsyncIterator[Thread]:
        return _paginate(lambda c: self.list(limit=page_size, cursor=c, **filters))

    async def get(
        self,
        thread_id: str,
        *,
        offset: int | None = None,
        limit: int | None = None,
        history_version: int | None = None,
    ) -> dict[str, Any]:
        return await self._run(
            specs.thread_get(
                thread_id, offset=offset, limit=limit, history_version=history_version
            )
        )

    async def branch(self, thread_id: str, *, expected_history_version: int) -> dict[str, Any]:
        return await self._run(
            specs.thread_branch(thread_id, expected_history_version=expected_history_version)
        )


class Agents(_Resource):
    async def list(
        self,
        *,
        agent_name: str | None = None,
        state: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Page[AgentRevision]:
        return await self._run(
            specs.agent_list(agent_name=agent_name, state=state, limit=limit, cursor=cursor)
        )

    def iter(self, *, page_size: int | None = None, **filters: Any) -> AsyncIterator[AgentRevision]:
        return _paginate(lambda c: self.list(limit=page_size, cursor=c, **filters))

    async def get(self, revision_id: str) -> AgentRevision:
        return await self._run(specs.agent_get(revision_id))

    async def create(self, spec: Mapping[str, Any]) -> AgentRevision:
        return await self._run(specs.agent_create(spec))

    async def validate(self, revision_id: str) -> dict[str, Any]:
        return await self._run(specs.agent_action(revision_id, "validate"))

    async def publish(self, revision_id: str) -> dict[str, Any]:
        return await self._run(specs.agent_action(revision_id, "publish"))


class Artifacts(_Resource):
    async def list(self, run_id: str) -> list[Artifact]:
        return await self._run(specs.artifact_list(run_id))

    async def upload(
        self,
        run_id: str,
        content: bytes | BinaryIO,
        *,
        filename: str,
        content_type: str = "application/octet-stream",
    ) -> Artifact:
        return await self._run(
            specs.artifact_upload(
                run_id, filename=filename, content=content, content_type=content_type
            )
        )

    async def get(self, artifact_id: str) -> Artifact:
        return await self._run(specs.artifact_get(artifact_id))

    async def content(
        self, artifact_id: str, *, start: int | None = None, end: int | None = None
    ) -> bytes:
        """Download bytes; ``start``/``end`` (inclusive) send an HTTP ``Range`` request."""
        return await self._run(specs.artifact_content(artifact_id, start=start, end=end))

    async def delete(self, artifact_id: str) -> None:
        await self._run(specs.artifact_delete(artifact_id))
