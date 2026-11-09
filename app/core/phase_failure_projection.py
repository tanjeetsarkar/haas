"""Tenant-scoped read-only projection of durable phase failures; not API wiring."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.phase_failures import FINALIZER_TOOL_CALLS, PhaseErrorCode, phase_failure_code
from app.core.store import StoreNotFoundError, decode_run_phases

if TYPE_CHECKING:
    from app.core.store import DistributedStore

FINALIZER_FAILED_MARKER = "phase.finalizer_failed"


class PhaseFailureProjectionError(ValueError):
    """The claimed finalizer's durable failure marker is unavailable."""


async def read_phase_failure_code(
    store: DistributedStore, *, tenant_id: str, run_id: str,
) -> PhaseErrorCode | None:
    """Read the run and its exact claimed step from one tenant-scoped snapshot.

    Only terminal failed phase runs project a phase code; legacy and other run
    states return None. Missing, malformed, or mismatched finalizer markers fail
    closed instead of guessing. Callers must not use this as a run-visibility
    check or a substitute for authorization.
    """
    async with store.connection() as conn:
        row = await conn.fetchrow(
            "SELECT r.status,r.phase_config_json,r.phase_state_json,s.error AS step_error,"
            "s.status AS step_status FROM runs r LEFT JOIN run_steps s "
            "ON s.tenant_id=r.tenant_id AND s.run_id=r.run_id "
            "AND r.phase_state_json->>'finalizer_attempt_key' = "
            "(r.run_id || ':step:' || s.step_number::text || ':attempt:' || s.attempt::text) "
            "AND EXISTS (SELECT 1 FROM usage_ledger l "
            "WHERE l.tenant_id=r.tenant_id AND l.run_id=r.run_id "
            "AND l.unit='model_usd' "
            "AND l.source_attempt_key=r.phase_state_json->>'finalizer_attempt_key') "
            "WHERE r.run_id=$1 AND COALESCE(r.tenant_uuid::text,r.tenant_id)="
            "COALESCE(resolve_haas_tenant_uuid($2)::text,$2)",
            run_id, tenant_id,
        )
    if row is None:
        raise StoreNotFoundError("Run not found.")
    _, state = decode_run_phases(row)
    marker = row["step_error"]
    # A marker on the exact claimed step is meaningful even when the phase
    # state says something else. Never hide a contradiction behind a fallback.
    if marker is not None:
        if (
            state is None or not state.finalizer_attempted or state.phase != "failed"
            or row["step_status"] != "failed"
            or marker not in (FINALIZER_TOOL_CALLS, FINALIZER_FAILED_MARKER)
            or (
                marker == FINALIZER_TOOL_CALLS and state.failure_code != "finalizer_failed"
            )
            or (
                marker == FINALIZER_FAILED_MARKER
                and state.failure_code not in ("finalizer_failed", "finalizer_outcome_unknown")
            )
        ):
            raise PhaseFailureProjectionError("phase.inconsistent_finalizer_step")
    if row["status"] != "failed" or state is None or state.phase != "failed":
        return None
    if state.failure_code == "finalizer_failed":
        if (
            not state.finalizer_attempted or row["step_status"] != "failed"
            or marker not in (FINALIZER_TOOL_CALLS, FINALIZER_FAILED_MARKER)
        ):
            raise PhaseFailureProjectionError("phase.inconsistent_finalizer_step")
        return phase_failure_code(state, finalizer_step_error=marker)
    return phase_failure_code(state)
