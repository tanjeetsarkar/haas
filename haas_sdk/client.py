"""Synchronous HaaS client."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator, Mapping
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


class Haas:
    """Blocking client for the HaaS ``/v1`` API.

    >>> with Haas(base_url="http://localhost:8000", api_key="hk_test_...") as haas:
    ...     run = haas.runs.create("Summarise the release notes")
    ...     for event in haas.runs.stream(run["run_id"]):
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
        transport: httpx.BaseTransport | None = None,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
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
        self._http = http_client or httpx.Client(
            base_url=self.options.base_url, timeout=timeout, transport=transport
        )
        self._sleep = sleep
        self.runs = Runs(self)
        self.approvals = Approvals(self)
        self.threads = Threads(self)
        self.agents = Agents(self)
        self.artifacts = Artifacts(self)

    def __enter__(self) -> Haas:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def me(self) -> dict[str, Any]:
        return self._run(specs.me())

    def capabilities(self) -> dict[str, Any]:
        return self._run(specs.capabilities())

    def health(self, path: str = "/readyz") -> dict[str, Any]:
        return self._run(specs.health(path))

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

    def _send(self, call: Call, *, stream: bool = False) -> httpx.Response:
        attempt = 0
        while True:
            try:
                response = self._http.send(self._build(call), stream=stream)
            except httpx.TransportError as exc:
                delay = retry_delay(self.options, call, attempt, error=exc)
                if delay is None:
                    raise HaasConnectionError(str(exc) or type(exc).__name__) from exc
            else:
                if response.status_code < 400:
                    return response
                if stream:
                    response.read()
                    response.close()
                delay = retry_delay(self.options, call, attempt, response=response)
                if delay is None:
                    raise api_error(response)
            self._sleep(delay)
            attempt += 1

    def _run(self, op: specs.Op[T]) -> T:
        return op.parse(self._send(op.call))


class _Resource:
    def __init__(self, client: Haas) -> None:
        self._client = client

    def _run(self, op: specs.Op[T]) -> T:
        return self._client._run(op)


def _paginate(fetch: Callable[[str | None], Page[T]]) -> Iterator[T]:
    cursor: str | None = None
    while True:
        page = fetch(cursor)
        yield from page.items
        if not page.next_cursor or page.next_cursor == cursor:
            return
        cursor = page.next_cursor


class Runs(_Resource):
    def create(
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
        return self._run(specs.run_create(prompt, idempotency_key=idempotency_key, extra=extra))

    def get(self, run_id: str) -> Run:
        return self._run(specs.run_get(run_id))

    def list(
        self,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        status: str | Iterable[str] | None = None,
        thread_id: str | None = None,
        created_after: datetime | float | str | None = None,
        created_before: datetime | float | str | None = None,
    ) -> Page[Run]:
        return self._run(
            specs.run_list(
                limit=limit,
                cursor=cursor,
                status=status,
                thread_id=thread_id,
                created_after=created_after,
                created_before=created_before,
            )
        )

    def iter(self, *, page_size: int | None = None, **filters: Any) -> Iterator[Run]:
        return _paginate(lambda c: self.list(limit=page_size, cursor=c, **filters))

    def cancel(self, run_id: str) -> dict[str, Any]:
        return self._run(specs.run_cancel(run_id))

    def resume(
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
        return self._run(
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

    def events(self, run_id: str, *, after: int = 0, limit: int | None = None) -> list[RunEvent]:
        return self._run(specs.run_events(run_id, after=after, limit=limit))

    def steps(self, run_id: str) -> dict[str, Any]:
        return self._run(specs.run_subresource(run_id, "steps"))

    def usage(self, run_id: str) -> dict[str, Any]:
        return self._run(specs.run_subresource(run_id, "usage"))

    def tree(self, run_id: str) -> dict[str, Any]:
        return self._run(specs.run_subresource(run_id, "tree"))

    def stream(
        self,
        run_id: str,
        *,
        last_event_id: str | int | None = None,
        output_deltas: bool = False,
    ) -> Iterator[RunEvent]:
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
                response = client._send(call, stream=True)
                try:
                    parser = SSEParser()
                    for line in response.iter_lines():
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
                    response.close()
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
            client._sleep(options.stream_reconnect_delay * failures)

    def wait(
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
            run = self.get(run_id)
            if run.is_terminal or (until_input_required and run.waiting_for_approval):
                return run
            if time.monotonic() >= deadline:
                raise TimeoutError(f"run {run_id} still {run.status} after {timeout}s")
            self._client._sleep(poll_interval)


class Approvals(_Resource):
    def list(
        self,
        *,
        status: str | None = "pending",
        run_id: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Page[ApprovalBatch]:
        return self._run(
            specs.approval_list(status=status, run_id=run_id, limit=limit, cursor=cursor)
        )

    def iter(self, *, page_size: int | None = None, **filters: Any) -> Iterator[ApprovalBatch]:
        return _paginate(lambda c: self.list(limit=page_size, cursor=c, **filters))

    def get(self, batch_id: str) -> ApprovalBatch:
        return self._run(specs.approval_get(batch_id))

    def decide(
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
        digest = batch_digest or self.get(batch_id).batch_digest
        return self._run(
            specs.approval_decide(
                batch_id,
                decision=decision,
                batch_digest=digest,
                reason=reason,
                secrets=secrets,
                idempotency_key=idempotency_key,
            )
        )

    def approve(self, batch_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.decide(batch_id, "approve", **kwargs)

    def deny(self, batch_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.decide(batch_id, "reject", **kwargs)


class Threads(_Resource):
    def list(
        self, *, search: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> Page[Thread]:
        return self._run(specs.thread_list(search=search, limit=limit, cursor=cursor))

    def iter(self, *, page_size: int | None = None, **filters: Any) -> Iterator[Thread]:
        return _paginate(lambda c: self.list(limit=page_size, cursor=c, **filters))

    def get(
        self,
        thread_id: str,
        *,
        offset: int | None = None,
        limit: int | None = None,
        history_version: int | None = None,
    ) -> dict[str, Any]:
        return self._run(
            specs.thread_get(
                thread_id, offset=offset, limit=limit, history_version=history_version
            )
        )

    def branch(self, thread_id: str, *, expected_history_version: int) -> dict[str, Any]:
        return self._run(
            specs.thread_branch(thread_id, expected_history_version=expected_history_version)
        )


class Agents(_Resource):
    def list(
        self,
        *,
        agent_name: str | None = None,
        state: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Page[AgentRevision]:
        return self._run(
            specs.agent_list(agent_name=agent_name, state=state, limit=limit, cursor=cursor)
        )

    def iter(self, *, page_size: int | None = None, **filters: Any) -> Iterator[AgentRevision]:
        return _paginate(lambda c: self.list(limit=page_size, cursor=c, **filters))

    def get(self, revision_id: str) -> AgentRevision:
        return self._run(specs.agent_get(revision_id))

    def create(self, spec: Mapping[str, Any]) -> AgentRevision:
        return self._run(specs.agent_create(spec))

    def validate(self, revision_id: str) -> dict[str, Any]:
        return self._run(specs.agent_action(revision_id, "validate"))

    def publish(self, revision_id: str) -> dict[str, Any]:
        return self._run(specs.agent_action(revision_id, "publish"))


class Artifacts(_Resource):
    def list(self, run_id: str) -> list[Artifact]:
        return self._run(specs.artifact_list(run_id))

    def upload(
        self,
        run_id: str,
        content: bytes | BinaryIO,
        *,
        filename: str,
        content_type: str = "application/octet-stream",
    ) -> Artifact:
        return self._run(
            specs.artifact_upload(
                run_id, filename=filename, content=content, content_type=content_type
            )
        )

    def get(self, artifact_id: str) -> Artifact:
        return self._run(specs.artifact_get(artifact_id))

    def content(
        self, artifact_id: str, *, start: int | None = None, end: int | None = None
    ) -> bytes:
        """Download bytes; ``start``/``end`` (inclusive) send an HTTP ``Range`` request."""
        return self._run(specs.artifact_content(artifact_id, start=start, end=end))

    def delete(self, artifact_id: str) -> None:
        self._run(specs.artifact_delete(artifact_id))
