"""Pure failure contract, not public API or runtime wiring."""

import pytest

from app.core.phase_failures import (
    DELEGATION_UNAVAILABLE,
    FINALIZER_TOOL_CALLS,
    phase_admission_failure_code,
    phase_failure_code,
)
from app.core.llm import CompletionResult, FinalizerToolCallsError
from app.core.run_phases import PhaseAdmissionError, PhaseState


@pytest.mark.parametrize(("failure", "expected"), [
    ("no_successful_acquisition_result", "phase.no_successful_acquisition_result"),
    ("insufficient_budget", "phase.insufficient_budget"),
    ("finalizer_failed", "phase.finalizer_failed"),
    ("finalizer_outcome_unknown", "phase.finalizer_outcome_unknown"),
])
def test_durable_phase_state_maps_to_stable_code(failure, expected):
    state = PhaseState(phase="failed", failure_code=failure)
    assert phase_failure_code(state) == expected


@pytest.mark.parametrize("failure", ["insufficient_budget", "finalizer_reserve_insufficient"])
def test_admission_capacity_failure_maps_without_parsing_exception_text(failure):
    assert phase_admission_failure_code(PhaseAdmissionError(failure)) == "phase.insufficient_budget"


def test_rejected_tool_calls_use_exact_durable_finalizer_step_marker():
    state = PhaseState(
        phase="failed", failure_code="finalizer_failed", finalizer_attempted=True,
        finalizer_attempt_key="run:step:2:attempt:1", successful_tool_outcomes=1,
    )
    assert phase_failure_code(state, finalizer_step_error=FINALIZER_TOOL_CALLS) == FINALIZER_TOOL_CALLS
    assert phase_failure_code(state, finalizer_step_error="provider: phase.finalizer_tool_calls") == (
        "phase.finalizer_failed"
    )
    assert phase_failure_code(state) == "phase.finalizer_failed"


@pytest.mark.parametrize("state", [
    None, PhaseState(), PhaseState(phase="failed", failure_code="insufficient_budget"),
    PhaseState(phase="failed", failure_code="finalizer_failed"),
])
def test_contradictory_tool_marker_is_an_explicit_error(state):
    with pytest.raises(ValueError, match="phase.inconsistent_finalizer_step"):
        phase_failure_code(state, finalizer_step_error=FINALIZER_TOOL_CALLS)


def test_legacy_and_active_phase_are_not_phase_failures():
    assert phase_failure_code(None) is None
    assert phase_failure_code(PhaseState()) is None
    assert DELEGATION_UNAVAILABLE == "phase.delegation_unavailable"


def test_finalizer_exception_exposes_code_without_losing_completion():
    completion = CompletionResult(
        assistant_message={"tool_calls": [{"id": "test"}]}, model_used="test",
        cost_usd=0.01, prompt_tokens=1, cached_tokens=0,
    )
    error = FinalizerToolCallsError(completion)
    assert error.code == FINALIZER_TOOL_CALLS
    assert str(error) == FINALIZER_TOOL_CALLS
    assert error.completion is completion
