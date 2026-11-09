"""Fenced acquisition response settlement. Phase admission remains held."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Callable

from app.core.llm import CompletionResult
from app.core.run_phases import (
    PhaseBudget, advance_after_acquisition, calculate_quota, validate_phase_transition,
)
from app.core.store import decode_run_phases
from app.core.workflow import Effects, Lease, LeaseLostError, StepTicket, dec

if TYPE_CHECKING:
    from app.core.store import DistributedStore


async def fail_acquisition_unavailable(store: DistributedStore, lease: Lease) -> None:
    """Make a denied reservation terminal without authorizing a model call."""
    fx = Effects()
    async with store.connection() as conn:
        row = await store._lock_run(conn, lease.run_id, lease.tenant_id)
        if (
            row is None or row["status"] != "running" or row["lease_epoch"] != lease.epoch
            or row["state_version"] != lease.version
        ):
            raise LeaseLostError(lease.run_id)
        _, state = decode_run_phases(row)
        if state is None or state.phase != "acquisition":
            raise ValueError("phase.not_acquiring")
        failed = advance_after_acquisition(
            state, successful_non_control_tool_outcome=False,
            no_tool_response=False, acquisition_quota_exhausted=True,
            finalizer_capacity_available=False,
        )
        validate_phase_transition(state, failed)
        await conn.execute(
            "UPDATE runs SET phase_state_json=$2::jsonb WHERE run_id=$1",
            row["run_id"], failed.model_dump_json(),
        )
        text = "Orchestrator step failed: acquisition reservation or deadline unavailable."
        await store._finalize(conn, fx, row, "failed", text, events=[("run_error", {"error": text})])
    await store._after_commit(fx)


async def commit_acquisition_response(
    store: DistributedStore, lease: Lease, ticket: StepTicket,
    completion: CompletionResult,
    build: Callable[[dict[str, Any], int], dict[str, Any]],
) -> str:
    """Settle one paid acquisition response and atomically accept or deny its batch."""
    fx = Effects()
    result = "failed"
    async with store.connection() as conn:
        row = await store._lock_run(conn, lease.run_id, lease.tenant_id)
        if (
            row is None or row["status"] != "running" or row["lease_epoch"] != lease.epoch
            or row["state_version"] != lease.version
        ):
            raise LeaseLostError(lease.run_id)
        config, state = decode_run_phases(row)
        if config is None or state is None or state.phase != "acquisition":
            raise ValueError("phase.not_acquiring")
        valid = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM run_steps s JOIN usage_ledger l "
            "ON l.tenant_id=s.tenant_id AND l.run_id=s.run_id WHERE s.tenant_id=$1 "
            "AND s.run_id=$2 AND s.step_number=$3 AND s.attempt=$4 AND s.lease_epoch=$5 "
            "AND s.status='model_running' AND s.reserved_usd=$6 "
            "AND l.source_attempt_key=$7 AND l.unit='model_usd' AND l.settlement_status='reserved')",
            row["tenant_id"], row["run_id"], ticket.step, ticket.attempt, lease.epoch,
            ticket.reserved_usd, ticket.attempt_key,
        )
        if not valid:
            raise LeaseLostError(lease.run_id)
        quota = calculate_quota(
            config,
            PhaseBudget(
                max_steps=int(row["max_steps"]), max_cost_usd=dec(row["max_cost_usd"]),
                max_tool_invocations=int(row["max_tool_invocations"]),
            ),
            estimated_finalizer_cost_usd=dec(0),
        )
        message = completion.assistant_message
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list) or not all(isinstance(call, dict) for call in calls):
            raise ValueError("phase.invalid_tool_calls")
        actual = dec(completion.cost_usd) if completion.cost_reported else ticket.reserved_usd
        if not actual.is_finite() or actual < 0:
            raise ValueError("phase.invalid_actual_cost")
        spent = await conn.fetchval(
            "SELECT $1::numeric + $2::numeric + $3::numeric",
            dec(row["total_cost_usd"]), await store._uncertain_cost(conn, row), actual,
        )
        over_cost = spent > quota.acquisition_max_cost_usd
        over_batch = int(row["tool_invocations_used"]) + len(calls) > int(row["max_tool_invocations"])
        trigger = not calls or over_batch
        will_dispatch = bool(calls) and not over_batch and not over_cost
        # Build only after deciding the whole batch fits; all writes remain in this transaction.
        invocations = [build(call, i) for i, call in enumerate(calls)] if will_dispatch else []
        approval = any(inv["requires_approval"] for inv in invocations)
        step_status = (
            "failed" if over_cost or (trigger and not state.successful_tool_outcomes)
            else "waiting_approval" if approval else "tools_dispatched" if will_dispatch
            else "completed"
        )
        delta, _ = await store._settle(
            conn, row, ticket, cost_usd=completion.cost_usd,
            cost_reported=completion.cost_reported, prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens, cached_tokens=completion.cached_tokens,
            step_status=step_status,
        )
        reserved = await conn.fetchval(
            "SELECT GREATEST($1::numeric - $2::numeric,0)",
            row["budget_reserved_usd"], ticket.reserved_usd,
        )
        messages = json.loads(row["messages_checkpoint_json"]) if isinstance(
            row["messages_checkpoint_json"], str
        ) else row["messages_checkpoint_json"]
        if not over_batch:
            messages = [*messages, message]
        if over_cost or (trigger and not state.successful_tool_outcomes):
            failed = advance_after_acquisition(
                state, successful_non_control_tool_outcome=False,
                no_tool_response=not calls, acquisition_quota_exhausted=bool(calls),
                finalizer_capacity_available=not over_cost,
            )
            # With no evidence the helper prioritizes that failure; both fail closed.
            validate_phase_transition(state, failed)
            await conn.execute(
                "UPDATE runs SET phase_state_json=$2::jsonb WHERE run_id=$1",
                row["run_id"], failed.model_dump_json(),
            )
            text = "Orchestrator step failed: acquisition budget or evidence unavailable."
            await store._finalize(
                conn, fx, row, "failed", text, cost_delta=delta,
                step_count=ticket.step, events=[("run_error", {"error": text})],
            )
        elif trigger:
            await store._bump(
                conn, lease,
                "step_count=$2,messages_checkpoint_json=$3::jsonb,"
                "total_cost_usd=total_cost_usd+$4::numeric,budget_reserved_usd=$5::numeric,"
                "next_action='step'",
                ticket.step, json.dumps(messages), delta, reserved,
            )
            result = "finalize"
        elif approval:
            await store._create_approval(conn, fx, lease, row, ticket.step, calls, invocations)
            await store._bump(
                conn, lease,
                "status='input_required',step_count=$2,messages_checkpoint_json=$3::jsonb,"
                "pending_tool_calls_json=$4::jsonb,total_cost_usd=total_cost_usd+$5::numeric,"
                "budget_reserved_usd=$6::numeric,next_action='none'",
                ticket.step, json.dumps(messages), json.dumps(calls), delta, reserved, release=True,
            )
            result = "approval"
        else:
            await store._insert_invocations(conn, lease, row, ticket.step, invocations, "dispatched")
            await store._dispatch(conn, fx, lease, row, invocations)
            await store._bump(
                conn, lease,
                "step_count=$2,messages_checkpoint_json=$3::jsonb,"
                "total_cost_usd=total_cost_usd+$4::numeric,budget_reserved_usd=$5::numeric,"
                "next_action='none',pending_tool_calls_json=NULL,"
                "tool_invocations_used=tool_invocations_used+$6",
                ticket.step, json.dumps(messages), delta, reserved, len(invocations), release=True,
            )
            result = "dispatch"
    await store._after_commit(fx)
    return result
