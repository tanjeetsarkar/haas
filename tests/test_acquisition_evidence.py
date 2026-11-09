"""Pure acquisition decisions; integration remains held."""

from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from app.core.acquisition_evidence import (
    AcquisitionOutcome,
    AcquisitionUsage,
    acquisition_action,
    acquisition_preflight,
    count_successful_acquisition_outcomes,
)
from app.core.run_phases import PhaseBudget, PhaseConfig, calculate_quota


def quota():
    return calculate_quota(
        PhaseConfig(),
        PhaseBudget(max_steps=4, max_cost_usd=Decimal("0.3"), max_tool_invocations=5),
        estimated_finalizer_cost_usd=Decimal("0.06"),
    )


@pytest.mark.parametrize(
    "status,control,is_error,expected",
    [
        ("succeeded", False, False, 1),
        ("succeeded", True, False, 0),
        ("succeeded", False, True, 0),
        ("failed", False, False, 0),
        ("unknown", False, False, 0),
        ("cancelled", False, False, 0),
        ("denied", False, False, 0),
    ],
)
def test_evidence_requires_success_not_control_and_no_error(status, control, is_error, expected):
    item = AcquisitionOutcome(
        invocation_id="invocation-1", status=status, is_control=control, is_error=is_error,
    )
    assert count_successful_acquisition_outcomes([item]) == expected


def test_duplicate_or_unclassified_evidence_is_rejected():
    item = AcquisitionOutcome(
        invocation_id="invocation-1", status="succeeded", is_control=False, is_error=False,
    )
    with pytest.raises(ValueError, match="duplicate_outcome"):
        count_successful_acquisition_outcomes([item, item])
    with pytest.raises(ValidationError):
        AcquisitionOutcome.model_validate({
            "invocation_id": "id", "status": "succeeded", "is_error": False,
        })
    with pytest.raises(ValidationError):
        AcquisitionOutcome.model_validate({
            "invocation_id": "id", "status": "succeeded", "is_control": 0, "is_error": False,
        })


@pytest.mark.parametrize(
    "steps,invocations,cost",
    [(3, 0, "0"), (0, 5, "0"), (0, 0, "0.24"), (0, 0, "0.24000000000000000000000000001")],
)
@pytest.mark.parametrize("success,expected", [(0, "fail_no_success"), (1, "finalize")])
def test_every_exhausted_quota_requires_success(steps, invocations, cost, success, expected):
    with localcontext() as context:
        context.prec = 5
        assert acquisition_action(
            quota=quota(),
            usage=AcquisitionUsage(
                steps_used=steps, tool_invocations_used=invocations,
                accounted_cost_usd=Decimal(cost),
            ),
            successful_outcomes=success, no_tool_response=False,
        ) == expected


@pytest.mark.parametrize("success", [0, 1])
def test_no_tool_response_or_no_trigger(success):
    usage = AcquisitionUsage(
        steps_used=1, tool_invocations_used=1,
        accounted_cost_usd=Decimal("0.23999999999999999999999999999"),
    )
    assert acquisition_action(
        quota=quota(), usage=usage, successful_outcomes=success, no_tool_response=False,
    ) == "continue"
    assert acquisition_action(
        quota=quota(), usage=usage, successful_outcomes=success, no_tool_response=True,
    ) == ("finalize" if success else "fail_no_success")


@pytest.mark.parametrize("value", [Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_bad_accounted_cost_rejected(value):
    with pytest.raises(ValidationError):
        AcquisitionUsage(steps_used=0, tool_invocations_used=0, accounted_cost_usd=value)


def test_bad_success_or_response_flags_rejected():
    usage = AcquisitionUsage(steps_used=0, tool_invocations_used=0, accounted_cost_usd=Decimal(0))
    with pytest.raises(ValueError, match="invalid_success_count"):
        acquisition_action(quota=quota(), usage=usage, successful_outcomes=True, no_tool_response=False)
    with pytest.raises(ValueError, match="invalid_response_flag"):
        acquisition_action(quota=quota(), usage=usage, successful_outcomes=0, no_tool_response=1)


@pytest.mark.parametrize("success", [0, 1])
def test_preflight_reservation_and_whole_batch_limit(success):
    usage = AcquisitionUsage(
        steps_used=1, tool_invocations_used=3, accounted_cost_usd=Decimal("0.12"),
    )
    run = lambda fits, batch: acquisition_preflight(  # noqa: E731
        quota=quota(), usage=usage, successful_outcomes=success,
        next_cost_fits=fits, next_batch_size=batch,
    )
    assert run(True, 2) == "continue"
    assert run(True, 3) == ("finalize" if success else "fail_no_success")
    assert run(False, None) == ("finalize" if success else "fail_no_success")


def test_preflight_rejects_malformed_decisions():
    usage = AcquisitionUsage(steps_used=0, tool_invocations_used=0, accounted_cost_usd=Decimal(0))
    with pytest.raises(ValueError, match="invalid_cost_fit"):
        acquisition_preflight(
            quota=quota(), usage=usage, successful_outcomes=0, next_cost_fits=1,
        )
    for bad in (-1, True):
        with pytest.raises(ValueError, match="invalid_batch_size"):
            acquisition_preflight(
                quota=quota(), usage=usage, successful_outcomes=0, next_cost_fits=True,
                next_batch_size=bad,
            )
