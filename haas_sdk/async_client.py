"""Asynchronous HaaS client (mirrors `haas_sdk.client`)."""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, TypeVar

import httpx

from haas_sdk import _specs as specs
from haas_sdk._core import (
    Call,
    ClientOptions,
    api_error,
    retry_delay,
    stream_cursor,
)
from haas_sdk._sse import SSEParser
from haas_sdk.compat import check_compat
from haas_sdk.errors import APIError, HaasConnectionError, StreamClosedError
from haas_sdk.handles import AsyncRunHandle
from haas_sdk.models import (
    AgentRevision,
    ApiKey,
    ApprovalReview,
    AuditEvent,
    Capabilities,
    Decision,
    DecisionReceipt,
    EventPage,
    Identity,
    IssuedApiKey,
    Membership,
    Page,
    PluginCatalog,
    PolicyRevision,
    Run,
    RunEvent,
    RunReceipt,
    RunSteps,
    RunUsage,
    Thread,
    ThreadHistory,
)

T = TypeVar("T")
MONEY = float | Decimal


class AsyncHaas:
    """Asyncio client for the HaaS `/v1` API.

    >>> async with AsyncHaas(api_key="hk_test_abc") as haas:
    ...     handle = await haas.runs.start("Summarise the release notes")
    ...     run = await handle.wait()
    ...     print(run.result)
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
        allow_test_keys: bool = False,
        verify_server: bool = False,
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
            allow_test_keys=allow_test_keys,
        )
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            base_url=self.options.base_url,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,  # SDK-SEC-06
        )
        self._sleep = sleep
        self.auth_expired = False
        self._verify_server = verify_server
        self._compat_checked = False
        self.runs = Runs(self)
        self.approvals = Approvals(self)
        self.threads = Threads(self)
        self.agents = Agents(self)
        self.plugins = Plugins(self)
        self.admin = Admin(self)
        self.preview = Preview(self)

    async def __aenter__(self) -> AsyncHaas:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    def __repr__(self) -> str:
        return (
            f"AsyncHaas(base_url={self.options.base_url!r}, "
            f"tenant={self.options.tenant!r})"
        )

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def set_credential(self, api_key: str | None) -> None:
        """Replace the in-memory credential and clear `auth_expired` (§6.6)."""
        self.options.api_key = api_key
        self.auth_expired = False

    async def me(self) -> Identity:
        return await self._run(specs.me())

    async def capabilities(self) -> Capabilities:
        return Capabilities.from_dict(await self._run(specs.capabilities()))

    async def require_compat(self) -> None:
        self._compat_checked = True  # set first: the check itself issues a request
        check_compat(await self.capabilities())

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
        if self._verify_server and not self._compat_checked:
            await self.require_compat()
        if self.options.api_key is None:
            warnings.warn(
                "X-Tenant-ID is a selector, not authentication.",
                UserWarning,
                stacklevel=3,
            )
        attempt = 0
        while True:
            try:
                response = await self._http.send(self._build(call), stream=stream)
            except httpx.TransportError as exc:
                delay = retry_delay(self.options, call, attempt, error=exc)
                if delay is None:
                    raise HaasConnectionError(str(exc) or type(exc).__name__) from exc
            else:
                if response.status_code == 401:
                    self.options.api_key = None
                    self.auth_expired = True
                    raise api_error(response)
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

    def _run(self, op: specs.Op[T]) -> Any:
        return self._client._run(op)


class Runs(_Resource):
    """`/v1/runs` operations (async)."""

    async def start(self, prompt: str, **kwargs: Any) -> AsyncRunHandle:
        receipt = await self.create(prompt, **kwargs)
        return AsyncRunHandle(
            self._client,
            receipt.run_id,
            thread_id=receipt.thread_id or None,
            receipt=receipt,
        )

    async def create(
        self,
        prompt: str,
        *,
        thread_id: str | None = None,
        agent_revision_id: str | None = None,
        expected_history_version: int | None = None,
        preload_skills: Iterable[str] | None = None,
        skills: Iterable[str] | None = None,
        primary_model: str | None = None,
        planner_model: str | None = None,
        max_steps: int | None = None,
        max_cost_usd: MONEY | None = None,
        deadline_sec: int | None = None,
        max_tool_invocations: int | None = None,
        webhook_url: str | None = None,
        secrets: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> RunReceipt:
        if agent_revision_id is not None and any(
            v is not None
            for v in (
                primary_model,
                planner_model,
                skills,
                preload_skills,
                max_steps,
                max_cost_usd,
            )
        ):
            raise ValueError(
                "agent_revision_id pins the configuration; passing primary_model, "
                "planner_model, skills, preload_skills, max_steps or max_cost_usd "
                "is not allowed (SDK-RUN-03)"
            )
        specs.validate_run_limits(
            max_steps=max_steps,
            max_cost_usd=max_cost_usd,
            deadline_sec=deadline_sec,
            max_tool_invocations=max_tool_invocations,
        )
        from haas_sdk.secrets import Secret

        secrets_body = (
            {
                k: (v.reveal() if isinstance(v, Secret) else v)
                for k, v in secrets.items()
            }
            if secrets
            else None
        )
        extra = {
            "thread_id": thread_id,
            "agent_revision_id": agent_revision_id,
            "expected_history_version": expected_history_version,
            "preload_skills": list(preload_skills) if preload_skills else None,
            "skills": list(skills) if skills else None,
            "primary_model": primary_model,
            "planner_model": planner_model,
            "max_steps": max_steps,
            "max_cost_usd": float(max_cost_usd) if max_cost_usd is not None else None,
            "deadline_sec": deadline_sec,
            "max_tool_invocations": max_tool_invocations,
            "webhook_url": webhook_url,
            "secrets": secrets_body,
        }
        return await self._run(
            specs.run_create(prompt, idempotency_key=idempotency_key, extra=extra)
        )

    def attach(self, run_id: str) -> AsyncRunHandle:
        return AsyncRunHandle(self._client, run_id)

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

    def iter(
        self, *, page_size: int | None = None, **filters: Any
    ) -> AsyncIterator[Run]:
        return self._paginate(lambda c: self.list(limit=page_size, cursor=c, **filters))

    async def _paginate(self, fetch: Callable[[str | None], Any]) -> AsyncIterator[Run]:
        cursor: str | None = None
        while True:
            page = await fetch(cursor)
            for item in page.items:
                yield item
            if not page.next_cursor or page.next_cursor == cursor:
                return
            cursor = page.next_cursor

    async def cancel(self, run_id: str) -> dict[str, Any]:
        return await self._run(specs.run_cancel(run_id))

    async def events(
        self, run_id: str, *, after: int = 0, limit: int | None = None
    ) -> EventPage:
        return await self._run(specs.run_events(run_id, after=after, limit=limit))

    def stream(
        self,
        run_id: str,
        *,
        last_event_id: str | int | None = None,
        output_deltas: bool = False,
    ) -> AsyncIterator[RunEvent]:
        return self._stream(
            run_id, last_event_id=last_event_id, output_deltas=output_deltas
        )

    async def _stream(
        self,
        run_id: str,
        *,
        last_event_id: str | int | None,
        output_deltas: bool,
    ) -> AsyncIterator[RunEvent]:
        client = self._client
        options = client.options
        last_id = None if last_event_id is None else stream_cursor(last_event_id)
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
                                str(
                                    event.data.get("reason")
                                    or "stream closed by server"
                                )
                            )
                        if event.is_delta:
                            # SDK-HDL-02: deltas never advance the cursor and are
                            # only emitted when output_deltas=True.
                            if output_deltas:
                                yield event
                            continue
                        if event.type == "stream_gap":
                            # SDK-HDL-03: jump the cursor to the server's latest.
                            latest = event.data.get("latest")
                            if isinstance(latest, int):
                                last_seq, last_id, failures = latest, str(latest), 0
                            yield event
                            continue
                        seq = event.sequence
                        if seq is not None:
                            if last_seq is not None and seq <= last_seq:
                                continue
                            last_seq, last_id, failures = seq, event.event_id, 0
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

    async def steps(self, run_id: str) -> RunSteps:
        return await self._run(specs.run_steps(run_id))

    async def usage(self, run_id: str) -> RunUsage:
        return await self._run(specs.run_usage(run_id))

    async def inspect(self, run_id: str) -> dict[str, Any]:
        return {
            "run": await self.get(run_id),
            "steps": await self.steps(run_id),
            "usage": await self.usage(run_id),
        }

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
    ) -> DecisionReceipt:
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


class Approvals(_Resource):
    """`/v1/approvals` operations (async; P2 digest binding)."""

    async def list(
        self,
        *,
        status: str | None = "pending",
        run_id: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Page[ApprovalReview]:
        return await self._run(
            specs.approval_list(
                status=status, run_id=run_id, limit=limit, cursor=cursor
            )
        )

    def iter(
        self, *, page_size: int | None = None, **filters: Any
    ) -> AsyncIterator[ApprovalReview]:
        return self._paginate(lambda c: self.list(limit=page_size, cursor=c, **filters))

    async def _paginate(
        self, fetch: Callable[[str | None], Any]
    ) -> AsyncIterator[ApprovalReview]:
        cursor: str | None = None
        while True:
            page = await fetch(cursor)
            for item in page.items:
                yield item
            if not page.next_cursor or page.next_cursor == cursor:
                return
            cursor = page.next_cursor

    async def review(self, batch_id: str) -> ApprovalReview:
        return await self._run(specs.approval_get(batch_id))

    async def decide(
        self,
        review: ApprovalReview,
        decision: str | Decision,
        *,
        reason: str | None = None,
        secrets: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> DecisionReceipt:
        if isinstance(decision, Decision):
            if decision.review.batch_id != review.batch_id:
                raise ValueError("Decision was built from a different ApprovalReview")
            reason = decision.reason if decision.reason is not None else reason
            secrets = decision.secrets if decision.secrets is not None else secrets
            wire = "approve" if decision.approve else "reject"
        else:
            wire = decision
        if review.expired or review.status != "pending":
            from haas_sdk.errors import ApprovalStaleError

            raise ApprovalStaleError(
                f"approval batch {review.batch_id} is {review.status!r}"
                + (" and expired" if review.expired else ""),
                status=409,
            )
        return await self._run(
            specs.approval_decide(
                review.batch_id,
                decision=wire,
                batch_digest=review.batch_digest,
                reason=reason,
                secrets=secrets,
                idempotency_key=idempotency_key,
            )
        )


class Threads(_Resource):
    """`/v1/threads` operations (async)."""

    async def list(
        self,
        *,
        search: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Page[Thread]:
        return await self._run(
            specs.thread_list(search=search, limit=limit, cursor=cursor)
        )

    def iter(
        self, *, page_size: int | None = None, **filters: Any
    ) -> AsyncIterator[Thread]:
        return self._paginate(lambda c: self.list(limit=page_size, cursor=c, **filters))

    async def _paginate(
        self, fetch: Callable[[str | None], Any]
    ) -> AsyncIterator[Thread]:
        cursor: str | None = None
        while True:
            page = await fetch(cursor)
            for item in page.items:
                yield item
            if not page.next_cursor or page.next_cursor == cursor:
                return
            cursor = page.next_cursor

    async def get(
        self,
        thread_id: str,
        *,
        offset: int | None = None,
        limit: int | None = None,
        history_version: int | None = None,
    ) -> ThreadHistory:
        return await self._run(
            specs.thread_get(
                thread_id, offset=offset, limit=limit, history_version=history_version
            )
        )

    async def branch(self, thread_id: str, *, expected_history_version: int) -> Thread:
        return await self._run(
            specs.thread_branch(
                thread_id, expected_history_version=expected_history_version
            )
        )


class Agents(_Resource):
    """`/v1/agents/revisions` operations (async)."""

    async def list(
        self,
        *,
        agent_name: str | None = None,
        state: str | Iterable[str] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Page[AgentRevision]:
        return await self._run(
            specs.agent_list(
                agent_name=agent_name, state=state, limit=limit, cursor=cursor
            )
        )

    def iter(
        self, *, page_size: int | None = None, **filters: Any
    ) -> AsyncIterator[AgentRevision]:
        return self._paginate(lambda c: self.list(limit=page_size, cursor=c, **filters))

    async def _paginate(
        self, fetch: Callable[[str | None], Any]
    ) -> AsyncIterator[AgentRevision]:
        cursor: str | None = None
        while True:
            page = await fetch(cursor)
            for item in page.items:
                yield item
            if not page.next_cursor or page.next_cursor == cursor:
                return
            cursor = page.next_cursor

    async def get(self, revision_id: str) -> AgentRevision:
        return await self._run(specs.agent_get(revision_id))

    async def create(
        self, agent_name: str, configuration: Mapping[str, Any]
    ) -> AgentRevision:
        return await self._run(specs.agent_create(agent_name, configuration))

    async def validate(self, revision_id: str) -> AgentRevision:
        return await self._run(specs.agent_action(revision_id, "validate"))

    async def publish(self, revision_id: str) -> AgentRevision:
        return await self._run(specs.agent_action(revision_id, "publish"))

    async def diff(self, left_id: str, right_id: str) -> str:
        import difflib
        import json

        left, right = await self.get(left_id), await self.get(right_id)

        def render(revision: AgentRevision) -> list[str]:
            return json.dumps(
                {
                    "configuration": revision.configuration,
                    "policy_rules": revision.policy_rules,
                    "registry_fingerprint": revision.registry_fingerprint,
                },
                indent=2,
                sort_keys=True,
            ).splitlines()

        return "\n".join(
            difflib.unified_diff(
                render(left),
                render(right),
                fromfile=left_id,
                tofile=right_id,
                lineterm="",
            )
        )


class Plugins(_Resource):
    """`/v1/plugins` catalog (async)."""

    async def catalog(self) -> PluginCatalog:
        return await self._run(specs.plugin_catalog())


class Admin(_Resource):
    """`/v1/admin/*` operations (async)."""

    def __init__(self, client: AsyncHaas) -> None:
        super().__init__(client)
        self.memberships = Memberships(client)
        self.api_keys = ApiKeys(client)
        self.policy = Policy(client)
        self.audit = Audit(client)


class Memberships(_Resource):
    async def list(self) -> list[Membership]:
        return await self._run(specs.admin_memberships_list())

    async def grant(
        self, *, subject: str, role: str, kind: str = "human"
    ) -> Membership:
        return await self._run(
            specs.admin_memberships_grant(subject=subject, role=role, kind=kind)
        )

    async def revoke(self, principal_id: str, role: str) -> None:
        await self._run(specs.admin_memberships_revoke(principal_id, role))


class ApiKeys(_Resource):
    async def list(self) -> list[ApiKey]:
        return await self._run(specs.admin_api_keys_list())

    async def create(
        self, *, principal_id: str, name: str, expires_in_days: int = 90
    ) -> IssuedApiKey:
        return await self._run(
            specs.admin_api_keys_create(
                principal_id=principal_id, name=name, expires_in_days=expires_in_days
            )
        )

    async def revoke(self, key_id: str) -> None:
        await self._run(specs.admin_api_keys_revoke(key_id))


class Policy(_Resource):
    async def get(self) -> PolicyRevision | None:
        return await self._run(specs.admin_policy_get())

    async def activate(self, rules: Mapping[str, Any]) -> PolicyRevision:
        return await self._run(specs.admin_policy_activate(rules))


class Audit(_Resource):
    async def list(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> Page[AuditEvent]:
        return await self._run(specs.admin_audit_list(limit=limit, cursor=cursor))

    def iter(self, *, page_size: int = 50, **filters: Any) -> AsyncIterator[AuditEvent]:
        return self._paginate(lambda c: self.list(limit=page_size, cursor=c, **filters))

    async def _paginate(
        self, fetch: Callable[[str | None], Any]
    ) -> AsyncIterator[AuditEvent]:
        cursor: str | None = None
        while True:
            page = await fetch(cursor)
            for item in page.items:
                yield item
            if not page.next_cursor or page.next_cursor == cursor:
                return
            cursor = page.next_cursor


class Preview:
    """Unstable namespaces (`haas.preview.*`, §3). May change in a minor release."""

    def __init__(self, client: AsyncHaas) -> None:
        from haas_sdk.preview.artifacts import Artifacts
        from haas_sdk.preview.egress import Egress
        from haas_sdk.preview.governance import Redaction, Retention
        from haas_sdk.preview.marketplace import Marketplace
        from haas_sdk.preview.mesh import Mesh
        from haas_sdk.preview.ops import Isolation, Ops
        from haas_sdk.preview.quotas import Quotas
        from haas_sdk.preview.secrets import Secrets
        from haas_sdk.preview.webhooks import WebhooksAdmin

        self.artifacts = Artifacts(client)
        self.webhooks = WebhooksAdmin(client)
        self.secrets = Secrets(client)
        self.egress = Egress(client)
        self.redaction = Redaction(client)
        self.retention = Retention(client)
        self.quotas = Quotas(client)
        self.marketplace = Marketplace(client)
        self.mesh = Mesh(client)
        self.ops = Ops(client)
        self.isolation = Isolation(client)
