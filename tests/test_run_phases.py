from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from app.core.run_phases import (
    PhaseAdmissionError,
    PhaseBudget,
    PhaseConfig,
    PhaseFailureCode,
    PhaseQuota,
    PhaseState,
    PhaseTransitionError,
    advance_after_acquisition,
    calculate_quota,
    cancel_acquisition,
    mark_finalizer_outcome_unknown,
    settle_finalizer,
    validate_phase_transition,
)


@pytest.mark.parametrize(
    ("cost", "reserve"),
    [(Decimal("0.15"), Decimal("0.03")), (Decimal("0.10"), Decimal("0.02"))],
)
def test_quota_carves_exactly_twenty_percent_from_unchanged_totals(cost, reserve):
    total = PhaseBudget(max_steps=6, max_cost_usd=cost, max_tool_invocations=12)
    quota = calculate_quota(
        PhaseConfig(), total, estimated_finalizer_cost_usd=reserve
    )
    assert quota.finalization_reserved_cost_usd == reserve
    assert quota.acquisition_max_cost_usd + reserve == cost
    assert quota.acquisition_max_steps + quota.finalization_reserved_steps == 6
    assert quota.acquisition_max_tool_invocations == 12
    assert quota.finalization_max_tool_invocations == 0


def test_quota_uses_exact_decimal_and_rejects_insufficient_estimate_or_step():
    total = PhaseBudget(
        max_steps=2, max_cost_usd=Decimal("0.123456789"), max_tool_invocations=3
    )
    quota = calculate_quota(
        PhaseConfig(), total, estimated_finalizer_cost_usd=Decimal("0.0246913578")
    )
    assert quota.finalization_reserved_cost_usd == Decimal("0.0246913578")
    assert quota.acquisition_max_cost_usd == Decimal("0.0987654312")
    assert quota.acquisition_max_steps == 1
    with pytest.raises(PhaseAdmissionError) as error:
        calculate_quota(
            PhaseConfig(), total, estimated_finalizer_cost_usd=Decimal("0.02469135781")
        )
    assert error.value.code == "finalizer_reserve_insufficient"
    with pytest.raises(PhaseAdmissionError) as error:
        calculate_quota(
            PhaseConfig(),
            PhaseBudget(max_steps=1, max_cost_usd=Decimal("1"), max_tool_invocations=0),
            estimated_finalizer_cost_usd=Decimal("0"),
        )
    assert error.value.code == "insufficient_budget"


def test_large_decimal_budget_is_not_rounded_and_estimate_must_be_decimal():
    amount = Decimal("123456789012345678901234567890.123456789")
    quota = calculate_quota(
        PhaseConfig(),
        PhaseBudget(max_steps=2, max_cost_usd=amount, max_tool_invocations=0),
        estimated_finalizer_cost_usd=Decimal("0"),
    )
    assert quota.finalization_reserved_cost_usd == Decimal(
        "24691357802469135780246913578.0246913578"
    )
    with localcontext() as context:
        context.prec = 50
        assert (
            quota.acquisition_max_cost_usd + quota.finalization_reserved_cost_usd
        ) == amount
    with pytest.raises(PhaseAdmissionError):
        calculate_quota(
            PhaseConfig(),
            PhaseBudget(max_steps=2, max_cost_usd=Decimal("1"), max_tool_invocations=0),
            estimated_finalizer_cost_usd=0.2,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("value", [Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity")])
def test_budget_fields_reject_negative_or_nonfinite_cost(value):
    with pytest.raises(ValidationError):
        PhaseBudget(max_steps=1, max_cost_usd=value, max_tool_invocations=0)
    with pytest.raises(ValidationError):
        PhaseQuota(
            acquisition_max_steps=0,
            acquisition_max_cost_usd=value,
            acquisition_max_tool_invocations=0,
            finalization_reserved_cost_usd=Decimal("0"),
        )


def test_transition_requires_success_before_no_tool_finalization():
    failed = advance_after_acquisition(
        PhaseState(),
        successful_non_control_tool_outcome=False,
        no_tool_response=True,
        acquisition_quota_exhausted=False,
        finalizer_capacity_available=True,
    )
    assert failed.phase == "failed"
    assert failed.failure_code == PhaseFailureCode.NO_SUCCESSFUL_ACQUISITION_RESULT
    started = advance_after_acquisition(
        PhaseState(),
        successful_non_control_tool_outcome=True,
        no_tool_response=True,
        acquisition_quota_exhausted=False,
        finalizer_capacity_available=True,
        finalizer_attempt_key="attempt-1",
    )
    assert started.phase == "finalization"
    assert started.state_version == 1
    assert started.successful_tool_outcomes == 1
    assert started.finalizer_attempted
    assert started.finalizer_attempt_key == "attempt-1"


def test_exhaustion_after_success_starts_one_finalizer_or_fails_for_capacity():
    acquired = advance_after_acquisition(
        PhaseState(),
        successful_non_control_tool_outcome=True,
        no_tool_response=False,
        acquisition_quota_exhausted=False,
        finalizer_capacity_available=True,
    )
    started = advance_after_acquisition(
        acquired,
        successful_non_control_tool_outcome=False,
        no_tool_response=False,
        acquisition_quota_exhausted=True,
        finalizer_capacity_available=True,
        finalizer_attempt_key="attempt-2",
    )
    assert started.phase == "finalization"
    assert started.state_version == 2
    denied = advance_after_acquisition(
        acquired,
        successful_non_control_tool_outcome=False,
        no_tool_response=True,
        acquisition_quota_exhausted=False,
        finalizer_capacity_available=False,
    )
    assert denied.failure_code == PhaseFailureCode.INSUFFICIENT_BUDGET
    with pytest.raises(PhaseTransitionError):
        advance_after_acquisition(
            started,
            successful_non_control_tool_outcome=False,
            no_tool_response=True,
            acquisition_quota_exhausted=False,
            finalizer_capacity_available=True,
            finalizer_attempt_key="attempt-3",
        )


def test_finalizer_is_one_shot_and_unknown_outcome_fails_closed():
    started = advance_after_acquisition(
        PhaseState(successful_tool_outcomes=1),
        successful_non_control_tool_outcome=False,
        no_tool_response=True,
        acquisition_quota_exhausted=False,
        finalizer_capacity_available=True,
        finalizer_attempt_key="attempt-4",
    )
    completed = settle_finalizer(started, succeeded=True)
    assert completed.phase == "completed"
    assert completed.state_version == started.state_version + 1
    assert completed.finalizer_attempted
    unknown = mark_finalizer_outcome_unknown(started)
    assert unknown.phase == "failed"
    assert unknown.finalizer_attempted
    assert unknown.finalizer_attempt_key == started.finalizer_attempt_key
    assert unknown.failure_code == PhaseFailureCode.FINALIZER_OUTCOME_UNKNOWN
    with pytest.raises(PhaseTransitionError):
        settle_finalizer(unknown, succeeded=True)


def test_models_are_strict_versioned_and_reject_invalid_transitions():
    with pytest.raises(ValidationError):
        PhaseConfig(version=2)
    with pytest.raises(ValidationError):
        PhaseBudget(max_steps=True, max_cost_usd=Decimal("1"), max_tool_invocations=1)
    with pytest.raises(ValidationError):
        PhaseState(phase="completed")
    with pytest.raises(ValidationError):
        PhaseState(phase="finalization", successful_tool_outcomes=0,
                   finalizer_attempted=True, finalizer_attempt_key="attempt")
    with pytest.raises(ValidationError):
        PhaseState(phase="finalization", successful_tool_outcomes=1,
                   finalizer_attempted=True, finalizer_attempt_key="attempt",
                   state_version=-1)
    with pytest.raises(PhaseTransitionError):
        settle_finalizer(PhaseState(), succeeded=True)
    cancelled = cancel_acquisition(PhaseState())
    assert cancelled.phase == "cancelled"
    assert cancelled.state_version == 1
    acquired = advance_after_acquisition(
        PhaseState(),
        successful_non_control_tool_outcome=True,
        no_tool_response=True,
        acquisition_quota_exhausted=False,
        finalizer_capacity_available=True,
        finalizer_attempt_key="attempt-cancel",
    )
    with pytest.raises(PhaseTransitionError):
        cancel_acquisition(acquired)


def test_state_transition_validator_rejects_retries_resets_and_version_rollback():
    current = PhaseState(
        state_version=4,
        phase="finalization",
        successful_tool_outcomes=1,
        finalizer_attempted=True,
        finalizer_attempt_key="attempt-once",
    )
    reset = PhaseState(
        state_version=5,
        phase="acquisition",
        successful_tool_outcomes=1,
    )
    replaced = PhaseState(
        state_version=5,
        phase="finalization",
        successful_tool_outcomes=1,
        finalizer_attempted=True,
        finalizer_attempt_key="attempt-retry",
    )
    rollback = PhaseState(
        state_version=3,
        phase="finalization",
        successful_tool_outcomes=1,
        finalizer_attempted=True,
        finalizer_attempt_key="attempt-once",
    )
    for candidate in (reset, replaced, rollback):
        with pytest.raises(PhaseTransitionError):
            validate_phase_transition(current, candidate)
    completed = settle_finalizer(current, succeeded=True)
    assert validate_phase_transition(current, completed) == completed
    with pytest.raises(PhaseTransitionError):
        validate_phase_transition(
            completed,
            PhaseState(
                state_version=completed.state_version + 1,
                phase="finalization",
                successful_tool_outcomes=1,
                finalizer_attempted=True,
                finalizer_attempt_key="attempt-once",
            ),
        )
