"""FakeHaas semantics tests (SDK-TEST-01..03, G4/G5 via the fake)."""

from __future__ import annotations

import pytest

from haas_sdk import (
    ApprovalStaleError,
    ConflictError,
    Haas,
    RunFailed,
)
from haas_sdk.testing import Approval, Call, FakeHaas, Step


def _fake() -> FakeHaas:
    return FakeHaas(tenant="t1", strict=True)


def test_fake_completes_a_scripted_run() -> None:
    fake = _fake()
    fake.script_run(
        steps=[
            Step(
                model="m",
                tool_calls=[Call("get_weather_forecast", {"location": "Pune"})],
            )
        ],
        result="sunny",
        cost_usd=0.002,
    )
    with fake.client() as haas:
        handle = haas.runs.start("What is the weather?")
        run = handle.wait(timeout=5)
    assert run.status == "completed" and run.result == "sunny"
    assert run.total_cost_usd == 0.002


def test_fake_approval_flow_with_handler() -> None:
    fake = _fake()
    fake.script_run(
        steps=[Step(model="m")],
        approval=Approval(
            calls=[Call("prepare_refund_batch", {"invoice_ids": ["inv_1"]})]
        ),
        result="refunded",
    )
    with fake.client() as haas:
        handle = haas.runs.start("refund please")
        run = handle.wait(timeout=5, on_approval=lambda review: review.approve())
    assert run.status == "completed" and run.result == "refunded"
    assert fake.decisions[0]["approve"] is True
    assert fake.decisions[0]["digest"]


def test_fake_digest_mismatch_is_conflict() -> None:
    fake = _fake()
    fake.script_run(approval=Approval(calls=[Call("t", {})]))
    with fake.client() as haas:
        handle = haas.runs.start("x")
        with pytest.raises(Exception, match="waiting for approval"):
            handle.wait(timeout=5)
        run = handle.get()
        review = haas.approvals.review(run.approval.batch_id)  # type: ignore[union-attr]
        tampered = type(review)(
            batch_id=review.batch_id,
            batch_digest="0" * 64,
            run_id=review.run_id,
            step_number=review.step_number,
            calls=review.calls,
            expires_at=review.expires_at,
            submitted_by_principal_id=review.submitted_by_principal_id,
            status=review.status,
            expired=review.expired,
        )
        with pytest.raises(ConflictError):
            haas.approvals.decide(tampered, tampered.approve())


def test_fake_single_winner_decision() -> None:
    fake = _fake()
    fake.script_run(approval=Approval(calls=[Call("t", {})]))
    with fake.client() as haas:
        handle = haas.runs.start("x")
        with pytest.raises(Exception, match="waiting for approval"):
            handle.wait(timeout=5)
        run = handle.get()
        review = haas.approvals.review(run.approval.batch_id)  # type: ignore[union-attr]
        haas.approvals.decide(review, review.approve())
        with pytest.raises(ConflictError):
            haas.approvals.decide(review, review.reject())


def test_fake_idempotency_replay_and_conflict() -> None:
    fake = _fake()
    fake.script_run(result="ok")
    with fake.client() as haas:
        first = haas.runs.create("hello", idempotency_key="k-1")
        replay = haas.runs.create("hello", idempotency_key="k-1")
        assert replay.run_id == first.run_id
        assert replay.replayed
        with pytest.raises(ConflictError, match="Idempotency-Key"):
            haas.runs.create("different", idempotency_key="k-1")


def test_fake_stream_resumes_from_last_event_id() -> None:
    fake = _fake()
    fake.script_run(steps=[Step(model="m")], result="done")
    with fake.client() as haas:
        handle = haas.runs.start("x")
        first = list(handle.events())
        assert first[0].sequence == 1
        resumed = list(haas.runs.stream(handle.run_id, last_event_id=first[0].sequence))
        assert all(e.sequence > first[0].sequence for e in resumed)
        assert resumed[-1].type == "run_completed"


def test_fake_strict_rejects_unknown_routes() -> None:
    fake = _fake()
    with fake.client() as haas, pytest.raises(Exception):
        haas.me()


def test_fake_fail_next_injects_fault() -> None:
    fake = _fake()
    fake.script_run(result="ok")
    fake.fail_next(status=503, headers={"Retry-After": "0"}, body={"detail": "busy"})
    with fake.client() as haas:
        run = haas.runs.attach(next(iter(fake.runs))).get()
    assert run.status == "completed"


def test_fake_tenant_isolation() -> None:
    fake = FakeHaas(tenant="t1", strict=True)
    fake.script_run(result="ok")
    import httpx

    outsider = Haas(
        base_url="http://fake",
        api_key="hk_test_fake",
        tenant="other",
        transport=httpx.MockTransport(fake.handler),
        sleep=lambda _: None,
    )
    with outsider, pytest.raises(Exception) as info:
        outsider.runs.get(next(iter(fake.runs)))
    assert info.value.status == 404


def test_fake_wait_without_handler_raises_approval_required() -> None:
    fake = _fake()
    fake.script_run(approval=Approval(calls=[Call("t", {})]))
    with fake.client() as haas:
        handle = haas.runs.start("x")
        with pytest.raises(Exception, match="waiting for approval"):
            handle.wait(timeout=5)


def test_fake_rejected_decision_cancels_run() -> None:
    fake = _fake()
    fake.script_run(approval=Approval(calls=[Call("t", {})]), result="never")
    with fake.client() as haas:
        handle = haas.runs.start("x")
        with pytest.raises(RunFailed) as info:
            handle.wait(
                timeout=5, on_approval=lambda review: review.reject(reason="no")
            )
    assert info.value.code == "run.cancelled"
    assert fake.decisions[0]["approve"] is False


def test_fake_stale_review_refused_locally() -> None:
    fake = _fake()
    fake.script_run(approval=Approval(calls=[Call("t", {})]))
    with fake.client() as haas:
        handle = haas.runs.start("x")
        with pytest.raises(Exception, match="waiting for approval"):
            handle.wait(timeout=5)
        run = handle.get()
        review = haas.approvals.review(run.approval.batch_id)  # type: ignore[union-attr]
        haas.approvals.decide(review, review.approve())
        # The review is now decided; a second decide is refused locally.
        with pytest.raises(ApprovalStaleError):
            haas.approvals.decide(review, review.reject())
