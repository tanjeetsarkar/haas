"""Structured phase failure projection, independent of HTTP and provider text.

This contract is not wired into admission, events, or public responses yet.
Finalizer tool-call rejection uses the exact server-owned ``run_steps.error``
marker in addition to the v1 durable phase state; it is not inferred from text.
"""

from __future__ import annotations

from typing import Final, Literal

from app.core.run_phases import PhaseAdmissionError, PhaseState

PhaseErrorCode = Literal[
    "phase.no_successful_acquisition_result",
    "phase.insufficient_budget",
    "phase.finalizer_tool_calls",
    "phase.finalizer_failed",
    "phase.finalizer_outcome_unknown",
]

FINALIZER_TOOL_CALLS: Final[PhaseErrorCode] = "phase.finalizer_tool_calls"
# A rejected delegation is an operation error, not a phase-state failure.
DELEGATION_UNAVAILABLE: Final = "phase.delegation_unavailable"

_FAILURE_CODES: Final[dict[str, PhaseErrorCode]] = {
    "no_successful_acquisition_result": "phase.no_successful_acquisition_result",
    "insufficient_budget": "phase.insufficient_budget",
    "finalizer_failed": "phase.finalizer_failed",
    "finalizer_outcome_unknown": "phase.finalizer_outcome_unknown",
}


def phase_failure_code(
    state: PhaseState | None, *, finalizer_step_error: str | None = None,
) -> PhaseErrorCode | None:
    """Project validated durable state and optional exact finalizer step marker.

    The caller must pass the error from the tenant/run-scoped *claimed*
    finalizer step, not an arbitrary message, event, or most-recent step.
    Legacy and nonfailed phase state return None. A contradictory marker is an
    invariant error; callers must not silently report successful completion.
    """
    if finalizer_step_error == FINALIZER_TOOL_CALLS:
        if (
            state is None or state.phase != "failed" or state.failure_code != "finalizer_failed"
            or not state.finalizer_attempted
        ):
            raise ValueError("phase.inconsistent_finalizer_step")
        return FINALIZER_TOOL_CALLS
    if state is None or state.phase != "failed":
        return None
    assert state.failure_code is not None
    return _FAILURE_CODES[state.failure_code]


def phase_admission_failure_code(error: PhaseAdmissionError) -> Literal["phase.insufficient_budget"]:
    """Both known admission capacity errors have one stable external category."""
    if error.code not in ("insufficient_budget", "finalizer_reserve_insufficient"):
        raise ValueError("phase.unknown_admission_failure")
    return "phase.insufficient_budget"
