"""Phase 3 durable workflow: drives the real orchestrator and tool executor over PostgreSQL.

The outbox publisher is replaced by an in-memory bus so the test is the broker: each
committed outbox row is delivered (or redelivered) explicitly, which lets the tests prove
duplicate delivery, lease fencing, approvals, cancellation and reconciler recovery.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
from pydantic import BaseModel

import app.main as main_module
from app.core.identity import AuthenticatedIdentity
from app.core.llm import CompletionResult
from app.core.plugins import PluginRegistry, RegisteredTool
from app.core.envelope import verify_envelope
from app.core.store import DistributedStore, StoreConflictError
from app.core.workflow import (
    Effects,
    IdempotencyConflictError,
    IdempotentReplay,
    LeaseLostError,
)
from app.worker import tasks

from test_phase2_database import DSN, admit, db  # noqa: F401  (fixture re-export)

pytestmark = pytest.mark.skipif(
    not DSN, reason="Requires disposable PostgreSQL qualification DB"
)
SERVICE_PASSWORD = "phase3-test-only"


class EchoArgs(BaseModel):
    x: str = ""


def _tool(name: str, *, approval: bool = False) -> RegisteredTool:
    def handler(args: EchoArgs) -> str:
        return f"{name}:{args.x}"

    return RegisteredTool(
        name=name,
        description=f"{name} tool for workflow tests.",
        args_schema=EchoArgs,
        handler=handler,
        injects_context=False,
        requires_approval=approval,
        is_core=True,
    )


class FakeRedis:
    """Effect-key store used by the tool executor (SET NX, cached results, cancel flag)."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def exists(self, key: str) -> int:
        return int(key in self.data)

    async def set(self, key: str, value: str, nx: bool = False, ex: int = 0) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def setex(self, key: str, _ttl: int, value: str) -> None:
        self.data[key] = value

    async def aclose(self) -> None:
        pass


def _completion(
    content: str | None = None,
    tool_calls: list[tuple[str, dict[str, Any]]] = (),
    cost: float = 0.0001,
) -> CompletionResult:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": f"call_{uuid4().hex[:8]}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for name, args in tool_calls
        ]
    return CompletionResult(message, "model-a", cost, 10, 0, completion_tokens=4)


@dataclass
class Runtime:
    owner: asyncpg.Connection
    store: DistributedStore
    service: DistributedStore
    slug: str
    principal: Any
    script: list[CompletionResult]
    redis: FakeRedis
    bus: list[dict[str, Any]] = field(default_factory=list)
    delivered: list[dict[str, Any]] = field(default_factory=list)

    async def publish(self, message: dict[str, Any]) -> None:
        self.bus.append(message)

    async def deliver(self, message: dict[str, Any]) -> Any:
        self.delivered.append(message)
        payload = message["payload"]
        if message["destination"] == "orchestrator":
            return await tasks._orchestrate(
                str(message["message_id"]),
                payload["run_id"],
                payload["action"],
                int(payload["expected_state_version"]),
                "qa-worker",
            )
        if message["destination"] == "tool":
            body = verify_envelope(payload["envelope"])
            status, outcome = await tasks._execute_envelope(body, {})
            return await self.service.record_tool_outcome(
                run_id=body["run_id"],
                invocation_id=body["invocation_id"],
                status=status,
                outcome=outcome,
            )
        return None

    async def pump(self, limit: int = 40) -> None:
        for _ in range(limit):
            if not self.bus:
                return
            await self.deliver(self.bus.pop(0))
        raise AssertionError("workflow did not quiesce")

    def take(self, destination: str) -> list[dict[str, Any]]:
        taken = [m for m in self.bus if m["destination"] == destination]
        self.bus[:] = [m for m in self.bus if m["destination"] != destination]
        return taken

    async def admit(self, **kwargs: Any) -> str:
        run, thread = "r_" + uuid4().hex, "t_" + uuid4().hex
        await admit(self.store, self.slug, thread, run, **kwargs)
        return run

    async def events(self, run_id: str) -> list[dict[str, Any]]:
        return await self.store.list_run_events(run_id, limit=500)


@pytest_asyncio.fixture
async def rt(db, monkeypatch):  # noqa: F811
    owner, store, slug, _other, _tenant, _o, principal, _policy = db
    role = f"qa_svc_{uuid4().hex}"
    await owner.execute(
        f"CREATE ROLE {role} LOGIN PASSWORD '{SERVICE_PASSWORD}' "
        "IN ROLE haas_execution_service"
    )
    pools: list[asyncpg.Pool] = []
    script: list[CompletionResult] = []
    fake_redis = FakeRedis()
    runtime: Runtime

    async def open_store() -> DistributedStore:
        service = DistributedStore()
        service.pg_pool = await asyncpg.create_pool(
            DSN, user=role, password=SERVICE_PASSWORD, min_size=1, max_size=2
        )
        pools.append(service.pg_pool)
        service.redis = AsyncMock()
        service.outbox_publisher = runtime.publish
        return service

    async def chat_completion(_client, **_kwargs):
        if not script:
            raise AssertionError("unexpected model call")
        return script.pop(0)

    registry = PluginRegistry()
    for tool in (_tool("echo"), _tool("danger", approval=True)):
        registry.tools[tool.name] = tool
    monkeypatch.setattr(tasks, "registry", registry)
    monkeypatch.setattr(tasks, "_open_store", open_store)
    monkeypatch.setattr(tasks.gateway, "chat_completion", chat_completion)
    monkeypatch.setattr(tasks.aioredis, "from_url", lambda *_a, **_k: fake_redis)
    runtime = Runtime(
        owner=owner,
        store=store,
        service=None,  # type: ignore[arg-type]
        slug=slug,
        principal=principal,
        script=script,
        redis=fake_redis,
    )
    store.outbox_publisher = runtime.publish
    runtime.service = await open_store()
    try:
        yield runtime
    finally:
        await runtime.service.close()
        for pool in pools:
            if not pool._closed:  # noqa: SLF001
                await pool.close()
        await owner.execute(f"DROP OWNED BY {role}")
        await owner.execute(f"DROP ROLE {role}")


async def test_dispatch_aggregate_complete_with_duplicate_delivery(rt: Runtime):
    rt.script += [
        _completion(tool_calls=[("echo", {"x": "a"}), ("echo", {"x": "b"})]),
        _completion("all done"),
    ]
    run_id = await rt.admit()
    assert (await rt.store.get_run(run_id))["status"] == "queued"
    first = rt.bus[0]
    await rt.pump()

    run = await rt.store.get_run(run_id)
    assert run["status"] == "completed", run
    assert run["result_text"] == "all done"
    assert run["lease_owner"] is None
    tool_messages = [m for m in run["messages"] if m["role"] == "tool"]
    assert [m["content"] for m in tool_messages] == ["echo:a", "echo:b"]

    events = await rt.events(run_id)
    sequences = [e["sequence"] for e in events]
    assert sequences == list(range(1, len(events) + 1))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "run_queued" and kinds[-1] == "run_completed"
    assert kinds.count("tool_dispatched") == 2
    assert kinds.count("tool_result") == 2
    dispatched = [e for e in events if e["type"] == "tool_dispatched"]
    assert {e["data"]["tool"] for e in dispatched} == {"echo"}
    assert all(e["data"]["queue"] == "q_tools_fast" for e in dispatched)
    assert all(e["schema_version"] == 1 for e in events)

    steps = (await rt.store.list_run_steps(run_id))["steps"]
    assert [(s["step_number"], s["status"]) for s in steps] == [
        (1, "tools_dispatched"),
        (2, "completed"),
    ]
    budget = await rt.store.run_budget(await rt.store.get_run(run_id))
    assert budget["reserved_usd"] == 0
    assert budget["settled_usd"] == pytest.approx(0.0002)

    # Redelivered orchestrator and tool messages are no-ops (consumer inbox + fencing).
    assert (await rt.deliver(first))["status"] == "skipped"
    tool_message = next(m for m in rt.delivered if m["destination"] == "tool")
    assert await rt.deliver(tool_message) is False
    assert len(await rt.events(run_id)) == len(events)


async def test_tool_effect_key_prevents_double_execution(rt: Runtime):
    rt.script += [_completion(tool_calls=[("echo", {"x": "once"})])]
    run_id = await rt.admit()
    await rt.deliver(rt.bus.pop(0))
    [tool_message] = rt.take("tool")
    body = verify_envelope(tool_message["payload"]["envelope"])
    first = await tasks._execute_envelope(body, {})
    second = await tasks._execute_envelope(body, {})
    assert first == second and first[0] == "succeeded"

    # A delivery that started the effect but never stored a result is reported unknown.
    rt.redis.data.pop(f"haas:effect-result:{body['effect_key']}")
    status, outcome = await tasks._execute_envelope(body, {})
    assert status == "unknown" and outcome["is_error"]
    rt.script.append(_completion("finished"))
    await rt.service.record_tool_outcome(
        run_id=run_id,
        invocation_id=body["invocation_id"],
        status=status,
        outcome=outcome,
    )
    await rt.pump()
    run = await rt.store.get_run(run_id)
    assert run["status"] == "completed"
    results = [e for e in await rt.events(run_id) if e["type"] == "tool_result"]
    assert results[0]["data"]["status"] == "unknown"


async def test_tampered_envelope_is_rejected(rt: Runtime):
    rt.script += [_completion(tool_calls=[("echo", {"x": "safe"})])]
    await rt.admit()
    await rt.deliver(rt.bus.pop(0))
    [tool_message] = rt.take("tool")
    envelope = json.loads(json.dumps(tool_message["payload"]["envelope"]))
    envelope["body"]["tool_call"]["arguments"] = json.dumps({"x": "evil"})
    result = tasks.execute_tool_task.run(envelope=envelope, scoped_secrets={})
    assert result["status"] == "rejected"
    assert rt.redis.data == {}


async def test_approval_digest_binding_idempotency_and_resume(rt: Runtime):
    rt.script += [_completion(tool_calls=[("danger", {"x": "prod"})])]
    run_id = await rt.admit()
    await rt.pump()
    run = await rt.store.get_run(run_id)
    assert run["status"] == "input_required"
    assert run["pending_tool_calls"][0]["function"]["name"] == "danger"
    pending = await rt.store.pending_approval_batch(run_id)
    assert pending is not None
    events = await rt.events(run_id)
    required = next(e for e in events if e["type"] == "input_required")
    assert required["data"]["pending_tool_calls"]
    assert required["data"]["batch_id"] == pending["batch_id"]

    decide = dict(
        run_id=None,
        batch_id=pending["batch_id"],
        approved=True,
        reason=None,
        actor_principal_id=str(rt.principal),
        require_binding=True,
    )
    with pytest.raises(StoreConflictError):
        await rt.store.decide_approval(
            rt.slug,
            **decide,
            batch_digest_value="0" * 64,
            request_id=str(uuid4()),
            idempotency_key=None,
        )
    with pytest.raises(StoreConflictError):
        await rt.store.decide_approval(
            rt.slug,
            **{**decide, "batch_id": None, "run_id": run_id},
            batch_digest_value=None,
            request_id=str(uuid4()),
            idempotency_key=None,
        )
    decided = await rt.store.decide_approval(
        rt.slug,
        **decide,
        batch_digest_value=pending["batch_digest"],
        request_id=str(uuid4()),
        idempotency_key="approve-1",
    )
    assert decided["replayed"] is False and decided["run_id"] == run_id
    replay = await rt.store.decide_approval(
        rt.slug,
        **decide,
        batch_digest_value=pending["batch_digest"],
        request_id=str(uuid4()),
        idempotency_key="approve-1",
    )
    assert replay["replayed"] is True
    with pytest.raises(IdempotencyConflictError):
        await rt.store.decide_approval(
            rt.slug,
            **{**decide, "approved": False},
            batch_digest_value=pending["batch_digest"],
            request_id=str(uuid4()),
            idempotency_key="approve-1",
        )
    with pytest.raises(StoreConflictError):
        await rt.store.decide_approval(
            rt.slug,
            **decide,
            batch_digest_value=pending["batch_digest"],
            request_id=str(uuid4()),
            idempotency_key="approve-2",
        )

    rt.script.append(_completion("deployed"))
    await rt.pump()
    run = await rt.store.get_run(run_id)
    assert run["status"] == "completed", run
    kinds = [e["type"] for e in await rt.events(run_id)]
    resumed = kinds[kinds.index("input_required") + 1 :]
    assert resumed[0] == "hitl_approved"
    assert "tool_dispatched" in resumed and "tool_result" in resumed
    batch = await rt.store.get_approval_batch(rt.slug, pending["batch_id"])
    assert batch is not None and batch["status"] == "approved"
    assert batch["decision"]["decision"] == "approved"
    async with rt.store.connection() as conn:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE approval_decisions SET decision = 'rejected' "
                    "WHERE batch_id = $1::uuid",
                    pending["batch_id"],
                )
    # Even the schema owner cannot rewrite a recorded decision.
    with pytest.raises(asyncpg.RaiseError, match="immutable"):
        await rt.owner.execute(
            "UPDATE approval_decisions SET decision = 'rejected' WHERE batch_id = $1::uuid",
            pending["batch_id"],
        )


async def test_rejection_feeds_denial_back_to_model(rt: Runtime):
    rt.script += [_completion(tool_calls=[("danger", {"x": "drop"})])]
    run_id = await rt.admit()
    await rt.pump()
    pending = await rt.store.pending_approval_batch(run_id)
    assert pending is not None
    await rt.store.decide_approval(
        rt.slug,
        run_id=run_id,
        batch_id=None,
        batch_digest_value=None,
        approved=False,
        reason="too risky",
        actor_principal_id=str(rt.principal),
        request_id=str(uuid4()),
        idempotency_key=None,
        require_binding=False,
    )
    rt.script.append(_completion("understood"))
    await rt.pump()
    run = await rt.store.get_run(run_id)
    assert run["status"] == "completed"
    denial = next(m for m in run["messages"] if m["role"] == "tool")
    assert denial["content"] == "Action Denied by Human Operator: too risky"
    assert not [d for d in rt.delivered if d["destination"] == "tool"]


async def test_cancel_while_tools_outstanding_finishes_cancelled(rt: Runtime):
    rt.script += [_completion(tool_calls=[("echo", {"x": "slow"})])]
    run_id = await rt.admit()
    await rt.deliver(rt.bus.pop(0))
    assert await rt.store.request_cancel(run_id, rt.slug) == "cancelling"
    assert (await rt.store.get_run(run_id))["status"] == "cancelling"
    assert await rt.store.request_cancel(run_id, rt.slug) == "cancelling"
    await rt.pump()
    run = await rt.store.get_run(run_id)
    assert run["status"] == "cancelled"
    thread = await rt.store.get_thread(run["thread_id"], rt.slug)
    assert thread["active_run_id"] is None
    kinds = [e["type"] for e in await rt.events(run_id)]
    assert kinds[-1] == "run_cancelled"
    assert await rt.store.request_cancel(run_id, rt.slug) == "terminal"


async def test_cancel_queued_run_is_immediate(rt: Runtime):
    run_id = await rt.admit()
    assert await rt.store.request_cancel(run_id, rt.slug) == "cancelled"
    assert (await rt.store.get_run(run_id))["status"] == "cancelled"
    # The queued orchestrate message is now stale and must not start a step.
    assert (await rt.deliver(rt.bus.pop(0)))["status"] == "skipped"
    assert rt.script == []


async def test_budget_is_reserved_before_dispatch_and_enforced(rt: Runtime):
    rt.script += [
        _completion(tool_calls=[("echo", {"x": "1"})], cost=0.002),
        _completion(tool_calls=[("echo", {"x": "2"})], cost=0.002),
    ]
    run, thread = "r_" + uuid4().hex, "t_" + uuid4().hex
    history = await rt.store.get_or_create_thread_messages(thread, rt.slug)
    await rt.store.create_run(
        run_id=run,
        thread_id=thread,
        tenant_id=rt.slug,
        traceparent="qa",
        primary_model="model-a",
        planner_model="model-b",
        initial_skills=[],
        initial_messages=[*history, {"role": "user", "content": "go"}],
        max_steps=5,
        max_cost_usd=0.003,
        webhook_url=None,
    )
    await rt.pump()
    record = await rt.store.get_run(run)
    assert record["status"] == "failed"
    assert record["result_text"].startswith("Exceeded max_cost_usd budget")
    budget = await rt.store.run_budget(record)
    assert budget["reserved_usd"] == 0
    # Reported cost is authoritative: the overshooting step is settled, then the run fails.
    assert budget["settled_usd"] == pytest.approx(0.004)
    assert rt.script == []

    # A reservation that cannot fit fails before any model call is made.
    rt.script.append(_completion("never sent"))
    tiny = await rt.admit()
    await rt.owner.execute(
        "UPDATE runs SET max_cost_usd = 0.0005 WHERE run_id = $1", tiny
    )
    await rt.pump()
    record = await rt.store.get_run(tiny)
    assert record["status"] == "failed"
    assert "before model dispatch" in record["result_text"]
    assert len(rt.script) == 1
    rt.script.clear()


async def test_max_steps_and_deadline_fail_closed(rt: Runtime):
    rt.script += [_completion(tool_calls=[("echo", {"x": "loop"})])]
    run_id = await rt.admit()
    await rt.owner.execute("UPDATE runs SET max_steps = 1 WHERE run_id = $1", run_id)
    await rt.pump()
    run = await rt.store.get_run(run_id)
    assert run["status"] == "failed" and "max_steps" in run["result_text"]

    late = await rt.admit(deadline_sec=60)
    await rt.owner.execute(
        "UPDATE runs SET deadline_at = now() - interval '1 second' WHERE run_id = $1",
        late,
    )
    await rt.pump()
    assert (await rt.store.get_run(late))["result_text"] == "Run deadline exceeded."


async def test_lease_fencing_and_reconciler_recovery(rt: Runtime):
    run_id = await rt.admit()
    message = rt.bus.pop(0)
    payload = message["payload"]
    acquired = await rt.service.acquire_lease(
        message_id=str(message["message_id"]),
        run_id=run_id,
        action=payload["action"],
        expected_state_version=payload["expected_state_version"],
        owner="crashed-worker",
    )
    assert acquired is not None
    lease, _run = acquired
    # Same message again: consumer inbox dedupe.
    assert (
        await rt.service.acquire_lease(
            message_id=str(message["message_id"]),
            run_id=run_id,
            action=payload["action"],
            expected_state_version=payload["expected_state_version"],
            owner="other",
        )
        is None
    )
    await rt.owner.execute(
        "UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE run_id = $1",
        run_id,
    )
    counts = await rt.service.reconcile()
    assert counts["recovered"] == 1
    record = await rt.store.get_run(run_id)
    assert record["status"] == "queued" and record["lease_owner"] is None
    # The crashed worker wakes up: every write through its stale lease is fenced.
    with pytest.raises(LeaseLostError):
        await rt.service.emit_step_started(lease, {"step": 1})
    assert await rt.service.fail_with_lease(lease, "late") is False

    rt.script.append(_completion("recovered"))
    await rt.pump()
    assert (await rt.store.get_run(run_id))["status"] == "completed"
    kinds = [e["type"] for e in await rt.events(run_id)]
    assert "run_recovering" in kinds


async def test_reconciler_marks_lost_tool_outcomes_unknown(rt: Runtime):
    rt.script += [_completion(tool_calls=[("echo", {"x": "lost"})])]
    run_id = await rt.admit()
    await rt.deliver(rt.bus.pop(0))
    assert rt.take("tool")  # the tool worker crashed with the message
    await rt.owner.execute(
        "UPDATE tool_invocations SET dispatched_at = now() - interval '1 day' "
        "WHERE run_id = $1",
        run_id,
    )
    counts = await rt.service.reconcile()
    assert counts["unknown_tools"] == 1
    rt.script.append(_completion("continuing"))
    await rt.pump()
    run = await rt.store.get_run(run_id)
    assert run["status"] == "completed"
    tool_message = next(m for m in run["messages"] if m["role"] == "tool")
    assert "unknown" in tool_message["content"].lower()


async def test_crashed_model_attempt_is_settled_uncertain_and_counted(rt: Runtime):
    run_id = await rt.admit()
    message = rt.bus.pop(0)
    acquired = await rt.service.acquire_lease(
        message_id=str(message["message_id"]),
        run_id=run_id,
        action=message["payload"]["action"],
        expected_state_version=message["payload"]["expected_state_version"],
        owner="crashed-worker",
    )
    assert acquired is not None
    ticket = await rt.service.begin_step(
        acquired[0], step=1, model="model-a", estimate_usd=Decimal("0.05")
    )
    assert ticket is not None
    await rt.owner.execute(
        "UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE run_id = $1",
        run_id,
    )
    assert (await rt.service.reconcile())["recovered"] == 1

    rt.script.append(_completion("second attempt", cost=0.001))
    await rt.pump()
    run = await rt.store.get_run(run_id)
    assert run["status"] == "completed"
    statuses = await rt.owner.fetch(
        "SELECT status FROM run_steps WHERE run_id = $1 ORDER BY attempt", run_id
    )
    assert [r["status"] for r in statuses][0] == "abandoned"
    budget = await rt.store.run_budget(run)
    assert budget["uncertain_usd"] == pytest.approx(0.05)
    assert budget["reserved_usd"] == 0
    # A charge that may have happened is spent, never silently refunded.
    assert budget["remaining_usd"] == pytest.approx(
        budget["max_cost_usd"] - budget["settled_usd"] - 0.05
    )


async def test_expired_approval_fails_run_and_events_are_immutable(rt: Runtime):
    rt.script += [_completion(tool_calls=[("danger", {"x": "late"})])]
    run_id = await rt.admit()
    await rt.pump()
    pending = await rt.store.pending_approval_batch(run_id)
    assert pending is not None
    await rt.owner.execute(
        "UPDATE approval_batches SET created_at = now() - interval '1 hour', "
        "expires_at = now() - interval '1 second' WHERE run_id = $1",
        run_id,
    )
    assert (await rt.service.reconcile())["approvals_expired"] == 1
    run = await rt.store.get_run(run_id)
    assert run["status"] == "failed" and "expired" in run["result_text"]
    batch = await rt.store.get_approval_batch(rt.slug, pending["batch_id"])
    assert batch is not None and batch["status"] == "expired"
    kinds = [e["type"] for e in await rt.events(run_id)]
    assert kinds.index("approval_expired") < kinds.index("run_error")
    with pytest.raises(StoreConflictError):
        await rt.store.decide_approval(
            rt.slug,
            run_id=None,
            batch_id=pending["batch_id"],
            approved=True,
            reason=None,
            actor_principal_id=str(rt.principal),
            require_binding=True,
            batch_digest_value=pending["batch_digest"],
            request_id=str(uuid4()),
            idempotency_key=None,
        )
    for statement in (
        "UPDATE run_events SET type = 'forged' WHERE run_id = $1",
        "DELETE FROM run_events WHERE run_id = $1",
    ):
        with pytest.raises(asyncpg.RaiseError, match="immutable"):
            await rt.owner.execute(statement, run_id)


async def test_outbox_relay_retries_until_published(rt: Runtime):
    failures = {"left": 1}

    async def flaky(message: dict[str, Any]) -> None:
        if failures["left"]:
            failures["left"] -= 1
            raise ConnectionError("broker down")
        await rt.publish(message)

    rt.store.outbox_publisher = flaky
    run_id = await rt.admit()
    assert rt.bus == []
    row = await rt.owner.fetchrow(
        "SELECT state, attempts, last_error FROM outbox_messages WHERE run_id = $1",
        run_id,
    )
    assert row["state"] == "pending" and row["attempts"] == 1
    assert "broker down" in row["last_error"]
    await rt.owner.execute(
        "UPDATE outbox_messages SET next_attempt_at = now() WHERE run_id = $1", run_id
    )
    rt.service.outbox_publisher = flaky
    assert await rt.service.relay_outbox() >= 1
    assert [m["payload"]["run_id"] for m in rt.bus].count(run_id) == 1
    row = await rt.owner.fetchrow(
        "SELECT state, attempts FROM outbox_messages WHERE run_id = $1", run_id
    )
    assert row["state"] == "dispatched" and row["attempts"] == 2


async def test_admission_idempotency_replay_and_conflict(rt: Runtime):
    admission = {
        "principal_key": str(rt.principal),
        "idempotency_key": "create-1",
        "request_digest": "a" * 64,
        "response": {"run_id": "first"},
    }
    await rt.admit(admission=admission)
    with pytest.raises(IdempotentReplay) as replay:
        await rt.admit(admission=admission)
    assert replay.value.response == {"run_id": "first"}
    with pytest.raises(IdempotencyConflictError):
        await rt.admit(admission={**admission, "request_digest": "b" * 64})
    found = await rt.store.find_admission(rt.slug, str(rt.principal), "create-1")
    assert found is not None and found["request_digest"] == "a" * 64


async def test_tenant_suspension_cancels_active_runs(rt: Runtime):
    queued = await rt.admit()
    rt.script += [_completion(tool_calls=[("danger", {"x": "wait"})])]
    paused = await rt.admit()
    rt.bus[:] = [m for m in rt.bus if m["payload"]["run_id"] == paused]
    await rt.pump()
    assert (await rt.store.get_run(paused))["status"] == "input_required"
    async with rt.service.connection() as conn:
        tenant_uuid = await conn.fetchval(
            "SELECT tenant_uuid FROM runs WHERE run_id = $1", queued
        )
    async with rt.service.connection() as conn:
        fx_count = await rt.service._cancel_tenant_runs(conn, Effects(), tenant_uuid)
    assert fx_count == 2
    assert (await rt.store.get_run(queued))["status"] == "cancelled"
    assert (await rt.store.get_run(paused))["status"] == "cancelled"
    assert await rt.store.pending_approval_batch(paused) is None


# ------------------------------------------------------------------ API surface


async def _api(rt: Runtime, monkeypatch, roles: set[str], principal: str):
    async def authenticate(_credential, tenant):
        return AuthenticatedIdentity(
            tenant_id=tenant,
            principal_id=principal,
            issuer="https://qa.invalid",
            subject=principal,
            roles=frozenset(roles),
            credential_type="oidc",
        )

    monkeypatch.setattr(main_module, "store", rt.store)
    monkeypatch.setattr(
        main_module.identity_authenticator, "authenticate", authenticate
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main_module.app),
        base_url="http://qa",
        headers={"Authorization": "Bearer qa", "X-Tenant-ID": rt.slug},
    )


async def test_api_approval_inbox_inspector_and_sse_replay(rt: Runtime, monkeypatch):
    submitter = uuid4()
    await rt.owner.execute(
        "INSERT INTO principals (principal_id,issuer,subject,kind) "
        "VALUES ($1,'https://qa.invalid',$2,'human')",
        submitter,
        str(submitter),
    )
    rt.script += [_completion(tool_calls=[("danger", {"x": "api"})])]
    run_id = await rt.admit(submitted_by_principal_id=str(submitter))
    await rt.pump()

    async with await _api(rt, monkeypatch, {"approver"}, str(submitter)) as self_client:
        inbox = await self_client.get("/v1/approvals")
        assert inbox.status_code == 200, inbox.text
        [item] = [i for i in inbox.json()["items"] if i["run_id"] == run_id]
        own = await self_client.post(
            f"/v1/approvals/{item['batch_id']}/decision",
            json={"decision": "approve", "batch_digest": item["batch_digest"]},
        )
        assert own.status_code == 403

    async with await _api(rt, monkeypatch, {"approver"}, str(rt.principal)) as client:
        summary = await client.get(f"/v1/runs/{run_id}")
        assert summary.status_code == 200
        assert summary.json()["approval"]["batch_id"] == item["batch_id"]
        stale = await client.post(
            f"/v1/approvals/{item['batch_id']}/decision",
            json={"decision": "approve", "batch_digest": "f" * 64},
        )
        assert stale.status_code == 409
        headers = {"Idempotency-Key": "api-approve"}
        body = {"decision": "approve", "batch_digest": item["batch_digest"]}
        first, second = await asyncio.gather(
            client.post(
                f"/v1/approvals/{item['batch_id']}/decision", json=body, headers=headers
            ),
            client.post(
                f"/v1/approvals/{item['batch_id']}/decision", json=body, headers=headers
            ),
        )
        assert sorted([first.status_code, second.status_code]) == [202, 202]
        assert {
            first.headers.get("idempotent-replayed"),
            second.headers.get("idempotent-replayed"),
        } == {None, "true"}
        detail = await client.get(f"/v1/approvals/{item['batch_id']}")
        assert detail.json()["status"] == "approved"
        pending = (await client.get("/v1/approvals")).json()["items"]
        assert item["batch_id"] not in {i["batch_id"] for i in pending}
        history = (await client.get("/v1/approvals?status=all")).json()["items"]
        [decided] = [i for i in history if i["batch_id"] == item["batch_id"]]
        assert decided["decision"]["decision"] == "approved"
        assert (await client.get("/v1/approvals?status=bogus")).status_code == 422

    rt.script.append(_completion("api done"))
    await rt.pump()
    async with await _api(
        rt, monkeypatch, {"tenant_admin"}, str(rt.principal)
    ) as client:
        run = (await client.get(f"/v1/runs/{run_id}")).json()
        assert run["status"] == "completed" and run["budget"]["reserved_usd"] == 0
        assert run["traceparent"] == "qa"
        steps = (await client.get(f"/v1/runs/{run_id}/steps")).json()
        assert len(steps["steps"]) == 2 and steps["invocations"]
        usage = (await client.get(f"/v1/runs/{run_id}/usage")).json()
        assert {e["unit"] for e in usage["entries"]} >= {
            "model_usd",
            "prompt_tokens",
            "completion_tokens",
        }
        assert {s["completion_tokens"] for s in steps["steps"]} == {4}
        page = (await client.get(f"/v1/runs/{run_id}/events?after=2&limit=3")).json()
        assert [e["sequence"] for e in page["items"]] == [3, 4, 5]
        assert page["next_after"] == 5

        stream = await client.get(f"/v1/runs/{run_id}/stream")
        frames = [f for f in stream.text.split("\n\n") if f.startswith("id:")]
        assert [int(f.split("\n")[0][4:]) for f in frames] == list(
            range(1, run["event_sequence"] + 1)
        )
        assert frames[-1].split("\n")[1] == "event: run_completed"
        resumed = await client.get(
            f"/v1/runs/{run_id}/stream", headers={"Last-Event-ID": "4"}
        )
        assert resumed.text.startswith("id: 5\n")


async def test_terminal_webhook_bodies_conform_to_contract(rt: Runtime):
    jsonschema = pytest.importorskip("jsonschema")
    from contract_support import load_contract

    validator = jsonschema.Draft202012Validator(
        load_contract("run-terminal-webhook.v1")
    )
    rt.script += [_completion("shipped")]
    ok = await rt.admit()
    rt.script += [_completion(tool_calls=[("echo", {"x": "a"})])]
    capped = await rt.admit()
    await rt.owner.execute(
        "UPDATE runs SET webhook_url = 'https://hooks.invalid/x' WHERE run_id = ANY($1)",
        [ok, capped],
    )
    await rt.owner.execute("UPDATE runs SET max_steps = 1 WHERE run_id = $1", capped)
    await rt.pump()
    bodies = {
        m["payload"]["body"]["run_id"]: m["payload"]["body"]
        for m in rt.delivered
        if m["destination"] == "webhook"
    }
    assert bodies[ok] == {
        "schema": "run-terminal-webhook.v1",
        "run_id": ok,
        "status": "completed",
        "output": "shipped",
    }
    assert bodies[capped]["status"] == "failed"
    assert bodies[capped]["code"] == "budget.max_steps"
    for body in bodies.values():
        assert not list(validator.iter_errors(body)), body
