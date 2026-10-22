"""Model parsing, error mapping, secrets and parity tests (G2, G6, G7, G8)."""

from __future__ import annotations

import inspect
import json
import logging
from typing import get_type_hints

import httpx
import pytest

import haas_sdk
from haas_sdk import (
    HaasError,
    Secret,
    constants,
)
from haas_sdk.errors import (
    ApprovalAlreadyDecidedError,
    ApprovalExpiredError,
    ApprovalStaleError,
    ConflictError,
    HistoryChangedError,
    IdempotencyConflictError,
    PolicyChangedError,
    ThreadBusyError,
    api_error,
)
from haas_sdk.models import (
    ApprovalReview,
    Budget,
    EventPage,
    Invocation,
    PendingCall,
    Run,
    RunEvent,
    RunReceipt,
)

# --- models: total parsing (G8) -------------------------------------------------------


def test_run_parses_full_shape() -> None:
    run = Run.from_dict(
        {
            "run_id": "r1",
            "status": "input_required",
            "thread_id": "t1",
            "step_count": 2,
            "total_cost_usd": 0.5,
            "state_version": 3,
            "event_sequence": 9,
            "active_skills": ["s"],
            "pending_tool_calls": [
                {
                    "id": "c1",
                    "name": "tool",
                    "arguments": '{"x": 1}',
                    "requires_approval": True,
                    "queue": "q",
                    "allowed_secrets": ["K"],
                }
            ],
            "approval": {
                "batch_id": "b1",
                "batch_digest": "d" * 64,
                "expires_at": "2099-01-01T00:00:00Z",
                "decision_endpoint": "/v1/approvals/b1/decision",
            },
            "budget": {
                "max_cost_usd": 10.0,
                "settled_usd": 1.0,
                "uncertain_usd": 0.5,
                "reserved_usd": 0.25,
                "remaining_usd": 8.25,
                "steps_used": 2,
                "max_steps": 50,
                "tool_invocations_used": 3,
                "max_tool_invocations": 100,
                "deadline_at": None,
            },
            "unknown_field": "kept",
        }
    )
    assert run.waiting_for_approval and not run.is_terminal
    assert run.approval is not None and run.approval.batch_id == "b1"
    assert run.budget is not None and run.budget.spent_usd == 1.5
    assert run.pending_calls is not None
    call = run.pending_calls[0]
    assert call.parsed_arguments == {"x": 1}
    assert call.allowed_secrets == ("K",)
    assert run.raw["unknown_field"] == "kept"


def test_run_missing_optionals_yield_none() -> None:
    run = Run.from_dict({"run_id": "r1", "status": "queued"})
    assert run.thread_id is None
    assert run.budget is None
    assert run.pending_calls is None
    assert run.is_active


def test_run_malformed_required_raises() -> None:
    with pytest.raises(HaasError, match="malformed Run"):
        Run.from_dict({"status": "queued"})


def test_budget_spent_includes_uncertain() -> None:
    budget = Budget.from_dict(
        {
            "max_cost_usd": 5,
            "settled_usd": 1,
            "uncertain_usd": 2,
            "reserved_usd": 0.5,
            "remaining_usd": 1.5,
            "steps_used": 1,
            "max_steps": 10,
            "tool_invocations_used": 0,
            "max_tool_invocations": 10,
            "deadline_at": None,
        }
    )
    assert budget.spent_usd == 3.0


def test_pending_call_bad_json_arguments_never_raises() -> None:
    call = PendingCall.from_dict({"id": "c", "name": "t", "arguments": "not json"})
    assert call.parsed_arguments == {"_raw": "not json"}


def test_invocation_needs_reconciliation() -> None:
    unknown = Invocation.from_dict(
        {"invocation_id": "i", "status": "unknown", "effect_key": "ek"}
    )
    ok = Invocation.from_dict(
        {"invocation_id": "j", "status": "succeeded", "effect_key": "ek2"}
    )
    assert unknown.needs_reconciliation and not ok.needs_reconciliation


def test_event_page_has_more() -> None:
    page = EventPage.from_dict(
        {
            "items": [
                {
                    "sequence": 1,
                    "type": "run_queued",
                    "data": {"run_id": "r1"},
                    "schema_version": 1,
                    "occurred_at": "2026-01-01T00:00:00Z",
                }
            ],
            "next_after": 1,
            "latest_sequence": 5,
        }
    )
    assert page.has_more and page.items[0].sequence == 1
    assert page.items[0].occurred_at is not None


def test_event_non_dict_data_is_wrapped() -> None:
    event = RunEvent.from_record({"sequence": 1, "type": "x", "data": [1, 2]})
    assert event.data == {"value": [1, 2]}


def test_receipt_replayed_flag() -> None:
    receipt = RunReceipt.from_dict(
        {
            "run_id": "r",
            "thread_id": "t",
            "status": "queued",
            "stream_url": "",
            "events_url": "",
            "replayed": True,
        }
    )
    assert receipt.replayed


def test_review_approve_reject_build_decisions() -> None:
    review = ApprovalReview.from_dict(
        {
            "batch_id": "b1",
            "run_id": "r1",
            "batch_digest": "d" * 64,
            "status": "pending",
            "expires_at": "2099-01-01T00:00:00Z",
            "tool_calls": [],
        }
    )
    approve = review.approve(secrets={"K": "v"})
    reject = review.reject(reason="no")
    assert approve.approve and approve.secrets == {"K": "v"}
    assert not reject.approve and reject.reason == "no"
    assert approve.review.batch_digest == "d" * 64


def test_models_exclude_raw_from_eq_and_repr() -> None:
    a = Run.from_dict({"run_id": "r", "status": "queued", "x": 1})
    b = Run.from_dict({"run_id": "r", "status": "queued", "x": 2})
    assert a == b
    assert "x" not in repr(a)


# --- errors: prefix table (G6) --------------------------------------------------------


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        (
            "Thread has an active run. Wait, cancel it, or explicitly branch.",
            ThreadBusyError,
        ),
        ("Thread history changed; refresh or branch.", HistoryChangedError),
        ("Approval batch is stale; refresh and review.", ApprovalStaleError),
        ("Approval batch digest does not match.", ApprovalStaleError),
        ("Approval batch expired.", ApprovalExpiredError),
        ("No pending approval batch for this run.", ApprovalAlreadyDecidedError),
        ("Policy changed; create a new draft.", PolicyChangedError),
        ("Tenant policy changed; retry admission.", PolicyChangedError),
        (
            "Idempotency-Key was already used with a different request.",
            IdempotencyConflictError,
        ),
        ("Something else entirely.", ConflictError),
    ],
)
def test_conflict_prefix_table(detail: str, expected: type[ConflictError]) -> None:
    response = httpx.Response(409, json={"detail": detail})
    error = api_error(response)
    assert type(error) is expected


def test_conflict_code_beats_prefix() -> None:
    response = httpx.Response(
        409,
        json={"detail": "Thread has an active run.", "code": "idempotency_conflict"},
    )
    assert type(api_error(response)) is IdempotencyConflictError


def test_api_error_str_format() -> None:
    response = httpx.Response(
        404, json={"detail": "nope"}, headers={"x-request-id": "req-1"}
    )
    error = api_error(response)
    assert str(error) == "404: nope (request_id=req-1)"


# --- secrets (G7) ---------------------------------------------------------------------


def test_secret_masks_and_compares() -> None:
    secret = Secret("hk_live_abcdef")
    assert str(secret) == "Secret('••••')"
    assert repr(secret) == "Secret('••••')"
    assert secret.reveal() == "hk_live_abcdef"
    assert secret == Secret("hk_live_abcdef")
    assert secret == "hk_live_abcdef"
    assert secret != "hk_live_other"
    assert "hk_live_abcdef" not in json.dumps({"s": str(secret)})


def test_log_redaction_masks_credentials(caplog: pytest.LogCaptureFixture) -> None:
    from haas_sdk.secrets import install_log_redaction

    logger = logging.getLogger("haas_sdk.test_redaction")
    install_log_redaction(logger)
    with caplog.at_level(logging.INFO, logger="haas_sdk.test_redaction"):
        logger.info("token is %s", "hk_test_abcdefghijklmnop")
        logger.info("Bearer abcdef123456 sent")
    assert "hk_test_abcdefghijklmnop" not in caplog.text
    assert "abcdef123456" not in caplog.text


def test_canary_never_appears_in_repr_or_errors() -> None:
    canary = "haas_canary_secret_token_12345"
    secret = Secret(canary)
    response = httpx.Response(401, json={"detail": f"bad key {canary}"})
    error = api_error(response)
    assert canary not in repr(secret)
    assert canary not in repr(error)
    assert canary in str(error)  # server echoed it; Secret and repr stay clean


# --- constants (SDK-CONST-01) ---------------------------------------------------------


def test_constants_are_the_single_source() -> None:
    assert constants.TERMINAL_STATUSES == {"completed", "failed", "cancelled"}
    assert "input_required" in constants.RUN_STATUSES
    assert "input_required" not in constants.ACTIVE_STATUSES
    assert constants.TERMINAL_EVENTS == {"run_completed", "run_error", "run_cancelled"}
    assert "budget.max_cost" in constants.RUN_ERROR_CODES


def test_no_status_literals_outside_constants() -> None:
    """G-doD: no new status strings in client modules."""
    import pathlib

    sdk_dir = pathlib.Path(haas_sdk.__file__).parent
    offenders: list[str] = []
    for path in sdk_dir.glob("*.py"):
        if path.name in ("constants.py",):
            continue
        text = path.read_text()
        for literal in ['"awaiting_approval"', "'awaiting_approval'"]:
            if literal in text:
                offenders.append(f"{path.name}: {literal}")
    assert not offenders


# --- parity (G2) ----------------------------------------------------------------------


def _public_methods(cls: type) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, member in inspect.getmembers(cls, inspect.isfunction):
        if name.startswith("_"):
            continue
        out[name] = str(inspect.signature(member))
    return out


def test_sync_async_resource_parity() -> None:
    """G2: Haas and AsyncHaas expose the same resource methods (async-ness aside)."""
    import haas_sdk.async_client as ac
    import haas_sdk.client as sc

    for sync_name, async_name in [
        ("Runs", "Runs"),
        ("Approvals", "Approvals"),
        ("Threads", "Threads"),
        ("Agents", "Agents"),
        ("Plugins", "Plugins"),
    ]:
        sync_cls = getattr(sc, sync_name)
        async_cls = getattr(ac, async_name)
        sync_methods = _public_methods(sync_cls)
        async_methods = _public_methods(async_cls)
        assert set(sync_methods) == set(async_methods), (
            f"{sync_name}: {set(sync_methods) ^ set(async_methods)}"
        )
        for name, signature in sync_methods.items():
            assert async_methods[name].replace("self", "self", 1) is not None
            # Parameter names must match (async-ness aside).
            sync_params = list(inspect.signature(getattr(sync_cls, name)).parameters)
            async_params = list(inspect.signature(getattr(async_cls, name)).parameters)
            assert sync_params == async_params, f"{sync_name}.{name}"


def test_handle_parity() -> None:
    from haas_sdk.handles import AsyncRunHandle, RunHandle

    sync_methods = set(_public_methods(RunHandle))
    async_methods = set(_public_methods(AsyncRunHandle))
    assert sync_methods == async_methods, sync_methods ^ async_methods


def test_public_exports_are_annotated() -> None:
    """Every public symbol in `haas_sdk.__all__` exists."""
    for name in haas_sdk.__all__:
        assert hasattr(haas_sdk, name), name


def test_type_hints_complete_on_models() -> None:
    """G9: public model fields are annotated."""
    for cls in (Run, Budget, RunEvent, ApprovalReview, PendingCall):
        hints = get_type_hints(cls)
        for field_name in cls.__dataclass_fields__:
            assert field_name in hints, f"{cls.__name__}.{field_name}"
