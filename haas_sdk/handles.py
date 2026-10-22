"""`RunHandle` / `AsyncRunHandle`: the central ergonomic types (§9.2)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

from haas_sdk._core import stream_cursor
from haas_sdk.errors import (
    ApprovalExpiredError,
    ApprovalRequired,
    ApprovalStaleError,
    ConflictError,
    HaasConnectionError,
    RunFailed,
    StreamClosedError,
)
from haas_sdk.models import (
    ApprovalReview,
    Decision,
    Invocation,
    Run,
    RunEvent,
    RunReceipt,
    RunSteps,
    RunUsage,
)

if TYPE_CHECKING:
    from haas_sdk.async_client import AsyncHaas
    from haas_sdk.client import Haas

ApprovalHandler = Callable[[ApprovalReview], "Decision | None"]
AsyncApprovalHandler = Callable[[ApprovalReview], "Decision | None"]

TERMINAL_PAUSE = "input_required"


def _terminal_outcome(run: Run, code: str | None = None) -> Run:
    """Step 3 of the wait() algorithm: resolve a terminal run or raise RunFailed."""
    if run.status == "completed":
        return run
    failure_code = code or ("run.cancelled" if run.status == "cancelled" else None)
    raise RunFailed(run, failure_code)


class RunHandle:
    """A handle on a submitted or attached run (sync). Holds no secrets (SDK-HDL-06)."""

    def __init__(
        self,
        client: Haas,
        run_id: str,
        *,
        thread_id: str | None = None,
        receipt: RunReceipt | None = None,
    ) -> None:
        self._client = client
        self.run_id = run_id
        self.thread_id = thread_id
        self.receipt = receipt

    def __repr__(self) -> str:
        return f"RunHandle(run_id={self.run_id!r})"

    def get(self) -> Run:
        return self._client.runs.get(self.run_id)

    def events(
        self, *, output_deltas: bool = False, from_sequence: int | None = None
    ) -> Iterator[RunEvent]:
        return self._client.runs.stream(
            self.run_id,
            last_event_id=from_sequence,
            output_deltas=output_deltas,
        )

    def wait(
        self,
        *,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
        on_approval: ApprovalHandler | None = None,
        on_event: Callable[[RunEvent], None] | None = None,
    ) -> Run:
        """Follow the run to a terminal state (normative algorithm in §9.2).

        Never auto-approves (SDK-HDL-05). On timeout the run keeps running on
        the server; `TimeoutError` is raised.
        """
        return _wait_sync(
            self._client,
            self.run_id,
            timeout=timeout,
            poll_interval=poll_interval,
            on_approval=on_approval,
            on_event=on_event,
        )

    def wait_until_paused(self, *, timeout: float = 300.0) -> Run:
        """Return when the run is `input_required` (or terminal)."""
        deadline = time.monotonic() + timeout
        while True:
            run = self.get()
            if run.is_terminal or run.waiting_for_approval:
                return run
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"run {self.run_id} still {run.status} after {timeout}s"
                )
            self._client._sleep(0.5)

    def cancel(self) -> Run:
        """Cancel the run; a 409 (already terminal) returns the final state (SDK-RUN-07)."""
        try:
            self._client.runs.cancel(self.run_id)
        except ConflictError:
            pass
        deadline = time.monotonic() + 60.0
        while True:
            run = self.get()
            if run.is_terminal or run.status == "cancelling":
                return run
            if time.monotonic() >= deadline:
                return run
            self._client._sleep(0.5)

    def steps(self) -> RunSteps:
        return self._client.runs.steps(self.run_id)

    def usage(self) -> RunUsage:
        return self._client.runs.usage(self.run_id)

    def reconciliation_needed(self) -> list[Invocation]:
        """Invocations whose outcome is `unknown`: reconcile by `effect_key` (P3)."""
        return [i for i in self.steps().invocations if i.needs_reconciliation]


def _wait_sync(
    client: Haas,
    run_id: str,
    *,
    timeout: float,
    poll_interval: float,
    on_approval: ApprovalHandler | None,
    on_event: Callable[[RunEvent], None] | None,
) -> Run:
    deadline = time.monotonic() + timeout
    cursor = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"run {run_id} still active after {timeout}s")
        try:
            outcome = _follow_stream(
                client,
                run_id,
                from_sequence=cursor,
                deadline=deadline,
                on_approval=on_approval,
                on_event=on_event,
            )
        except HaasConnectionError:
            # The stream gave up (reconnect budget); fall back to polling so the
            # timeout still applies and the run keeps running on the server.
            if time.monotonic() >= deadline:
                raise TimeoutError(f"run {run_id} still active after {timeout}s")
            run = client.runs.get(run_id)
            if run.is_terminal:
                return _terminal_outcome(run)
            if run.event_sequence is not None:
                cursor = run.event_sequence
            client._sleep(poll_interval)
            continue
        if isinstance(outcome, Run):
            return outcome
        cursor = outcome


def _follow_stream(
    client: Haas,
    run_id: str,
    *,
    from_sequence: int,
    deadline: float,
    on_approval: ApprovalHandler | None,
    on_event: Callable[[RunEvent], None] | None,
) -> Run | int:
    """Stream events until terminal/pause; return the Run, or the next cursor.

    Steps 1-6 of the normative wait() algorithm.
    """
    last_seq = from_sequence
    stream = client.runs.stream(run_id, last_event_id=from_sequence or None)
    while True:
        try:
            event = next(stream)
        except StopIteration:
            # Step 6: clean end without a terminal event; poll once.
            return _poll_after_stream(client, run_id, deadline)
        if on_event is not None and not event.is_delta:
            on_event(event)
        if event.sequence is not None:
            if event.sequence <= last_seq:
                continue  # SDK-HDL-01: dedupe replays
            last_seq = event.sequence
        if event.is_terminal:
            run = client.runs.get(run_id)
            return _terminal_outcome(
                run, event.data.get("code") if event.type == "run_error" else None
            )
        if event.type == "input_required":
            return _handle_pause(
                client,
                run_id,
                event,
                on_approval,
                resume_from=last_seq,
                deadline=deadline,
            )
        if event.type == "stream_gap":
            # SDK-HDL-03: jump the cursor to the server's latest.
            latest = event.data.get("latest")
            if isinstance(latest, int):
                last_seq = latest
        if event.type == "stream_closed":
            raise StreamClosedError(
                str(event.data.get("reason") or "stream closed by server")
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(f"run {run_id} still active")


def _poll_after_stream(client: Haas, run_id: str, deadline: float) -> Run | int:
    """Step 6: the stream ended cleanly; resolve from the authoritative Run."""
    run = client.runs.get(run_id)
    if run.is_terminal:
        return _terminal_outcome(run)
    if run.event_sequence is not None:
        return run.event_sequence
    return 0


def _handle_pause(
    client: Haas,
    run_id: str,
    event: RunEvent,
    on_approval: ApprovalHandler | None,
    *,
    resume_from: int,
    deadline: float,
) -> Run | int:
    """Step 4-5: review, decide, and continue; never auto-approve."""
    batch_id = str(event.data.get("batch_id") or "")
    run = client.runs.get(run_id)
    if on_approval is None:
        raise ApprovalRequired(run)
    review = client.approvals.review(batch_id)
    decision = on_approval(review)
    if decision is None:
        return run  # left pending on the server
    try:
        client.approvals.decide(review, decision)
    except ConflictError as exc:
        # Step 5: someone else may have decided; re-read and adapt.
        fresh = client.runs.get(run_id)
        if not fresh.waiting_for_approval:
            if fresh.is_terminal:
                return _terminal_outcome(fresh)
            return resume_from
        if isinstance(exc, ApprovalStaleError | ApprovalExpiredError):
            raise
        raise
    return resume_from


class AsyncRunHandle:
    """A handle on a submitted or attached run (async). Holds no secrets."""

    def __init__(
        self,
        client: AsyncHaas,
        run_id: str,
        *,
        thread_id: str | None = None,
        receipt: RunReceipt | None = None,
    ) -> None:
        self._client = client
        self.run_id = run_id
        self.thread_id = thread_id
        self.receipt = receipt

    def __repr__(self) -> str:
        return f"AsyncRunHandle(run_id={self.run_id!r})"

    async def get(self) -> Run:
        return await self._client.runs.get(self.run_id)

    def events(self, *, output_deltas: bool = False, from_sequence: int | None = None):
        return self._client.runs.stream(
            self.run_id,
            last_event_id=from_sequence,
            output_deltas=output_deltas,
        )

    async def wait(
        self,
        *,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
        on_approval: AsyncApprovalHandler | None = None,
        on_event: Callable[[RunEvent], None] | None = None,
    ) -> Run:
        """Follow the run to a terminal state (async mirror of the sync algorithm)."""
        import asyncio

        deadline = asyncio.get_event_loop().time() + timeout
        cursor = 0
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"run {self.run_id} still active after {timeout}s")
            outcome = await _follow_stream_async(
                self._client,
                self.run_id,
                from_sequence=cursor,
                deadline=deadline,
                on_approval=on_approval,
                on_event=on_event,
            )
            if isinstance(outcome, Run):
                return outcome
            cursor = outcome

    async def wait_until_paused(self, *, timeout: float = 300.0) -> Run:
        import asyncio

        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            run = await self.get()
            if run.is_terminal or run.waiting_for_approval:
                return run
            if asyncio.get_event_loop().time() >= deadline:
                raise TimeoutError(
                    f"run {self.run_id} still {run.status} after {timeout}s"
                )
            await self._client._sleep(0.5)

    async def cancel(self) -> Run:
        try:
            await self._client.runs.cancel(self.run_id)
        except ConflictError:
            pass
        import asyncio

        deadline = asyncio.get_event_loop().time() + 60.0
        while True:
            run = await self.get()
            if run.is_terminal or run.status == "cancelling":
                return run
            if asyncio.get_event_loop().time() >= deadline:
                return run
            await self._client._sleep(0.5)

    async def steps(self) -> RunSteps:
        return await self._client.runs.steps(self.run_id)

    async def usage(self) -> RunUsage:
        return await self._client.runs.usage(self.run_id)

    async def reconciliation_needed(self) -> list[Invocation]:
        steps = await self.steps()
        return [i for i in steps.invocations if i.needs_reconciliation]


async def _follow_stream_async(
    client: AsyncHaas,
    run_id: str,
    *,
    from_sequence: int,
    deadline: float,
    on_approval: AsyncApprovalHandler | None,
    on_event: Callable[[RunEvent], None] | None,
) -> Run | int:
    import asyncio

    last_seq = from_sequence
    stream = client.runs.stream(run_id, last_event_id=from_sequence or None)
    while True:
        try:
            event = await anext(stream)
        except StopAsyncIteration:
            run = await client.runs.get(run_id)
            if run.is_terminal:
                return _terminal_outcome(run)
            return run.event_sequence if run.event_sequence is not None else 0
        if on_event is not None and not event.is_delta:
            on_event(event)
        if event.sequence is not None:
            if event.sequence <= last_seq:
                continue
            last_seq = event.sequence
        if event.is_terminal:
            run = await client.runs.get(run_id)
            return _terminal_outcome(
                run, event.data.get("code") if event.type == "run_error" else None
            )
        if event.type == "input_required":
            return await _handle_pause_async(
                client,
                run_id,
                event,
                on_approval,
                resume_from=last_seq,
                deadline=deadline,
            )
        if event.type == "stream_gap":
            latest = event.data.get("latest")
            if isinstance(latest, int):
                last_seq = latest
        if event.type == "stream_closed":
            raise StreamClosedError(
                str(event.data.get("reason") or "stream closed by server")
            )
        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(f"run {run_id} still active")


async def _handle_pause_async(
    client: AsyncHaas,
    run_id: str,
    event: RunEvent,
    on_approval: AsyncApprovalHandler | None,
    *,
    resume_from: int,
    deadline: float,
) -> Run | int:
    run = await client.runs.get(run_id)
    if on_approval is None:
        raise ApprovalRequired(run)
    review = await client.approvals.review(str(event.data.get("batch_id") or ""))
    decision: Decision | None = on_approval(review)
    if hasattr(decision, "__await__"):
        decision = await decision  # type: ignore[misc]
    if decision is None:
        return run
    try:
        await client.approvals.decide(review, decision)
    except ConflictError as exc:
        fresh = await client.runs.get(run_id)
        if not fresh.waiting_for_approval:
            if fresh.is_terminal:
                return _terminal_outcome(fresh)
            return resume_from
        if isinstance(exc, ApprovalStaleError | ApprovalExpiredError):
            raise
        raise
    return resume_from


__all__ = [
    "AsyncRunHandle",
    "RunHandle",
    "stream_cursor",
]
