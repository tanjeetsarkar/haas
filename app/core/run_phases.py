"""Pure bounded-acquisition configuration, quotas, and transitions; no runtime wiring."""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RESERVE_BASIS_POINTS = 2_000
BASIS_POINTS = 10_000


class PhaseModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class PhaseConfig(PhaseModel):
    """Immutable opt-in configuration; serialized form is versioned."""

    version: Literal[1] = 1
    mode: Literal["bounded_acquisition_finalization"] = "bounded_acquisition_finalization"
    reserve_basis_points: Literal[2000] = RESERVE_BASIS_POINTS


class PhaseBudget(PhaseModel):
    """Unchanged total run ceilings supplied by the existing run request."""

    max_steps: int = Field(ge=0)
    max_cost_usd: Decimal
    max_tool_invocations: int = Field(ge=0)

    @model_validator(mode="after")
    def finite_nonnegative_cost(self) -> PhaseBudget:
        if not self.max_cost_usd.is_finite() or self.max_cost_usd < 0:
            raise ValueError("phase.budget_cost")
        return self


class PhaseQuota(PhaseModel):
    """Acquisition limits carved out of, never added to, the run totals."""

    acquisition_max_steps: int = Field(ge=0)
    acquisition_max_cost_usd: Decimal
    acquisition_max_tool_invocations: int = Field(ge=0)
    finalization_reserved_steps: Literal[1] = 1
    finalization_reserved_cost_usd: Decimal
    finalization_max_tool_invocations: Literal[0] = 0

    @model_validator(mode="after")
    def finite_nonnegative_costs(self) -> PhaseQuota:
        costs = (
            self.acquisition_max_cost_usd,
            self.finalization_reserved_cost_usd,
        )
        if any(not cost.is_finite() or cost < 0 for cost in costs):
            raise ValueError("phase.quota_cost")
        return self


class PhaseAdmissionError(ValueError):
    def __init__(self, code: Literal["insufficient_budget", "finalizer_reserve_insufficient"]):
        self.code = code
        super().__init__(code)


class PhaseTransitionError(ValueError):
    """Raised when an event is not legal for the persisted phase state."""


class PhaseFailureCode:
    NO_SUCCESSFUL_ACQUISITION_RESULT = "no_successful_acquisition_result"
    INSUFFICIENT_BUDGET = "insufficient_budget"
    FINALIZER_FAILED = "finalizer_failed"
    FINALIZER_OUTCOME_UNKNOWN = "finalizer_outcome_unknown"


PhaseFailure = Literal[
    "no_successful_acquisition_result",
    "insufficient_budget",
    "finalizer_failed",
    "finalizer_outcome_unknown",
]


class PhaseState(PhaseModel):
    """Mutable, versioned progress intended for durable run-owned storage."""

    version: Literal[1] = 1
    state_version: int = Field(default=0, ge=0)
    phase: Literal[
        "acquisition", "finalization", "completed", "failed", "cancelled"
    ] = "acquisition"
    successful_tool_outcomes: int = Field(default=0, ge=0)
    finalizer_attempted: bool = False
    finalizer_attempt_key: str | None = Field(default=None, min_length=1)
    failure_code: PhaseFailure | None = None

    @model_validator(mode="after")
    def consistent_state(self) -> PhaseState:
        if self.finalizer_attempted != (self.finalizer_attempt_key is not None):
            raise ValueError("phase.state_attempt_key")
        if self.phase in ("finalization", "completed") and not self.finalizer_attempted:
            raise ValueError("phase.state_attempt_missing")
        if self.phase in ("finalization", "completed") and self.successful_tool_outcomes < 1:
            raise ValueError("phase.state_success_required")
        if self.phase in ("acquisition", "cancelled") and self.finalizer_attempted:
            raise ValueError("phase.state_unexpected_attempt")
        if self.phase == "failed" and self.failure_code is None:
            raise ValueError("phase.state_failure")
        if self.phase != "failed" and self.failure_code is not None:
            raise ValueError("phase.state_unexpected_failure")
        return self


def _next_state(state: PhaseState, **changes: object) -> PhaseState:
    return PhaseState(**{
        **state.model_dump(),
        **changes,
        "state_version": state.state_version + 1,
    })


def calculate_quota(
    config: PhaseConfig,
    budget: PhaseBudget,
    *,
    estimated_finalizer_cost_usd: Decimal,
) -> PhaseQuota:
    """Reserve one original step and exactly 20% of original cost."""
    if (
        not isinstance(estimated_finalizer_cost_usd, Decimal)
        or not estimated_finalizer_cost_usd.is_finite()
        or estimated_finalizer_cost_usd < 0
    ):
        raise PhaseAdmissionError("finalizer_reserve_insufficient")
    with localcontext() as context:
        context.prec = max(len(budget.max_cost_usd.as_tuple().digits) + 2, 28)
        reserve_fraction = Decimal(config.reserve_basis_points) / Decimal(BASIS_POINTS)
        reserve = budget.max_cost_usd * reserve_fraction
        acquisition_cost = budget.max_cost_usd - reserve
    if budget.max_steps < 2:
        raise PhaseAdmissionError("insufficient_budget")
    if estimated_finalizer_cost_usd > reserve:
        raise PhaseAdmissionError("finalizer_reserve_insufficient")
    return PhaseQuota(
        acquisition_max_steps=budget.max_steps - 1,
        acquisition_max_cost_usd=acquisition_cost,
        acquisition_max_tool_invocations=budget.max_tool_invocations,
        finalization_reserved_cost_usd=reserve,
    )


def validate_phase_transition(previous: PhaseState, following: PhaseState) -> PhaseState:
    """Validate a prospective persisted-state update against its predecessor."""
    if following == previous:
        return following
    legal_phases: dict[str, tuple[str, ...]] = {
        "acquisition": ("acquisition", "finalization", "failed", "cancelled"),
        "finalization": ("finalization", "completed", "failed"),
        "completed": (),
        "failed": (),
        "cancelled": (),
    }
    if following.state_version != previous.state_version + 1:
        raise PhaseTransitionError("phase.state_version")
    if following.phase not in legal_phases[previous.phase]:
        raise PhaseTransitionError("phase.transition_regression")
    if following.successful_tool_outcomes < previous.successful_tool_outcomes:
        raise PhaseTransitionError("phase.success_count_regression")
    if previous.phase != "acquisition" and (
        following.successful_tool_outcomes != previous.successful_tool_outcomes
    ):
        raise PhaseTransitionError("phase.success_count_after_acquisition")
    if previous.finalizer_attempted and (
        not following.finalizer_attempted
        or following.finalizer_attempt_key != previous.finalizer_attempt_key
    ):
        raise PhaseTransitionError("phase.finalizer_attempt_replaced")
    if not previous.finalizer_attempted and following.finalizer_attempted and (
        following.phase != "finalization"
    ):
        raise PhaseTransitionError("phase.finalizer_attempt_phase")
    return following


def advance_after_acquisition(
    state: PhaseState,
    *,
    successful_non_control_tool_outcome: bool,
    no_tool_response: bool,
    acquisition_quota_exhausted: bool,
    finalizer_capacity_available: bool,
    finalizer_attempt_key: str | None = None,
) -> PhaseState:
    """Continue acquisition or durably consume the one finalizer attempt."""
    if state.phase != "acquisition":
        raise PhaseTransitionError("phase.transition_not_acquiring")
    successes = state.successful_tool_outcomes + int(successful_non_control_tool_outcome)
    trigger = no_tool_response or acquisition_quota_exhausted
    if not trigger:
        if successes == state.successful_tool_outcomes:
            return state
        return _next_state(state, successful_tool_outcomes=successes)
    if successes == 0:
        return _next_state(
            state,
            phase="failed",
            successful_tool_outcomes=successes,
            failure_code=PhaseFailureCode.NO_SUCCESSFUL_ACQUISITION_RESULT,
        )
    if not finalizer_capacity_available:
        return _next_state(
            state,
            phase="failed",
            successful_tool_outcomes=successes,
            failure_code=PhaseFailureCode.INSUFFICIENT_BUDGET,
        )
    if not finalizer_attempt_key:
        raise PhaseTransitionError("phase.finalizer_key_required")
    return _next_state(
        state,
        phase="finalization",
        successful_tool_outcomes=successes,
        finalizer_attempted=True,
        finalizer_attempt_key=finalizer_attempt_key,
    )


def settle_finalizer(state: PhaseState, *, succeeded: bool) -> PhaseState:
    """Settle a started attempt; never creates a retry path."""
    if state.phase != "finalization" or not state.finalizer_attempted:
        raise PhaseTransitionError("phase.finalizer_not_started")
    if succeeded:
        return _next_state(state, phase="completed")
    return _next_state(
        state, phase="failed", failure_code=PhaseFailureCode.FINALIZER_FAILED
    )


def mark_finalizer_outcome_unknown(state: PhaseState) -> PhaseState:
    """Fail closed on recovery after dispatch may have begun; do not retry."""
    if state.phase != "finalization" or not state.finalizer_attempted:
        raise PhaseTransitionError("phase.finalizer_not_started")
    return _next_state(
        state,
        phase="failed",
        failure_code=PhaseFailureCode.FINALIZER_OUTCOME_UNKNOWN,
    )


def cancel_acquisition(state: PhaseState) -> PhaseState:
    """Cancel before the finalizer claim; a consumed attempt cannot be reset."""
    if state.phase != "acquisition" or state.finalizer_attempted:
        raise PhaseTransitionError("phase.cancel_not_acquiring")
    return _next_state(state, phase="cancelled")
