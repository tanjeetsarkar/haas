"""Pure acquisition evidence and quota decisions; not wired to dispatch.

Inputs must be read from durable server-owned rows inside a fenced transaction.
In particular, ``accounted_cost_usd`` must be summed exactly by the ledger, and
callers must atomically deduplicate outcome consumption across replays.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.run_phases import PhaseQuota


class AcquisitionOutcome(BaseModel):
    """Classification supplied by the authoritative invocation and registry."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    invocation_id: str = Field(min_length=1)
    status: Literal["succeeded", "failed", "unknown", "cancelled", "denied"]
    is_control: bool
    is_error: bool


class AcquisitionUsage(BaseModel):
    """Durably accounted totals; cost is already an exact ledger sum."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    steps_used: int = Field(ge=0)
    tool_invocations_used: int = Field(ge=0)
    accounted_cost_usd: Decimal

    @model_validator(mode="after")
    def valid_cost(self) -> AcquisitionUsage:
        if not self.accounted_cost_usd.is_finite() or self.accounted_cost_usd < 0:
            raise ValueError("phase.invalid_accounted_cost")
        return self


def count_successful_acquisition_outcomes(outcomes: list[AcquisitionOutcome]) -> int:
    """Count only distinct successful non-control, non-error invocations."""
    identifiers = [outcome.invocation_id for outcome in outcomes]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("phase.duplicate_outcome")
    return sum(
        outcome.status == "succeeded" and not outcome.is_control and not outcome.is_error
        for outcome in outcomes
    )


def acquisition_action(
    *, quota: PhaseQuota, usage: AcquisitionUsage, successful_outcomes: int,
    no_tool_response: bool,
) -> Literal["continue", "finalize", "fail_no_success"]:
    """Decide whether an established response or exhausted quota ends acquisition.

    This does not claim a finalizer or test whether the next estimated model
    call fits. That check remains part of the transactional ledger.
    """
    if type(successful_outcomes) is not int or successful_outcomes < 0:
        raise ValueError("phase.invalid_success_count")
    if type(no_tool_response) is not bool:
        raise ValueError("phase.invalid_response_flag")
    exhausted = (
        usage.steps_used >= quota.acquisition_max_steps
        or usage.tool_invocations_used >= quota.acquisition_max_tool_invocations
        or usage.accounted_cost_usd >= quota.acquisition_max_cost_usd
    )
    if not (no_tool_response or exhausted):
        return "continue"
    return "finalize" if successful_outcomes else "fail_no_success"


def acquisition_preflight(
    *, quota: PhaseQuota, usage: AcquisitionUsage, successful_outcomes: int,
    next_cost_fits: bool, next_batch_size: int | None = None,
) -> Literal["continue", "finalize", "fail_no_success"]:
    """Apply exact ledger fit and whole-batch invocation checks before dispatch.

    The ledger must calculate ``next_cost_fits`` in the same locked transaction
    with an exact numeric sum; this helper does not perform Decimal addition.
    A batch that would overrun the invocation allowance is denied in full.
    """
    if type(next_cost_fits) is not bool:
        raise ValueError("phase.invalid_cost_fit")
    if next_batch_size is not None and (
        type(next_batch_size) is not int or next_batch_size < 0
    ):
        raise ValueError("phase.invalid_batch_size")
    action = acquisition_action(
        quota=quota, usage=usage, successful_outcomes=successful_outcomes,
        no_tool_response=False,
    )
    batch_over = next_batch_size is not None and (
        usage.tool_invocations_used + next_batch_size > quota.acquisition_max_tool_invocations
    )
    if action != "continue" or (next_cost_fits and not batch_over):
        return action
    return "finalize" if successful_outcomes else "fail_no_success"
