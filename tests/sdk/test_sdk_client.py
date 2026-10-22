"""Unit tests for the haas_sdk 0.2 surface (mock transport, no server)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from haas_sdk import (
    AsyncHaas,
    Haas,
    HaasConnectionError,
    NotFoundError,
    RateLimitError,
    StreamClosedError,
    ValidationError,
)
from haas_sdk.errors import (
    ApprovalStaleError,
    ConfigError,
    IncompatibleServerError,
    RunFailed,
)

BATCH_DIGEST = "a" * 64


def _client(handler: Any, **kwargs: Any) -> Haas:
    kwargs.setdefault("api_key", "hk_test_abc")
    return Haas(
        base_url="http://haas.test",
        tenant="acme",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
        **kwargs,
    )


def _sse(*frames: tuple[str | None, str, dict[str, Any]]) -> bytes:
    out = [": keep-alive\n\n"]
    for event_id, kind, data in frames:
        head = f"id: {event_id}\n" if event_id is not None else ""
        out.append(f"{head}event: {kind}\ndata: {json.dumps(data)}\n\n")
    return "".join(out).encode()


# --- configuration (SDK-CFG-01..06) ---------------------------------------------------


def test_headers_and_run_create_with_idempotency_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            202,
            json={
                "run_id": "r1",
                "thread_id": "t1",
                "status": "queued",
                "stream_url": "/v1/runs/r1/stream",
                "events_url": "/v1/runs/r1/events",
            },
        )

    with _client(handler) as haas:
        receipt = haas.runs.create("hello", max_steps=3, idempotency_key="k-1")
    assert receipt.run_id == "r1" and receipt.status == "queued"
    request = seen[0]
    assert request.headers["authorization"] == "Bearer " + "hk_test_abc"
    assert request.headers["x-tenant-id"] == "acme"
    assert request.headers["idempotency-key"] == "k-1"
    assert request.headers["user-agent"].startswith("haas-sdk-python/")
    assert json.loads(request.content) == {"prompt": "hello", "max_steps": 3}


def test_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAAS_URL", "http://env.test/")
    monkeypatch.setenv("HAAS_API_KEY", "hk_test_env")
    monkeypatch.setenv("HAAS_TENANT", "envtenant")
    haas = Haas(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert haas.options.base_url == "http://env.test"
    assert haas.options.tenant == "envtenant"
    assert haas.options.api_key == "hk_test_env"
    haas.close()


def test_conflicting_env_credentials_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAAS_API_KEY", "hk_test_a")
    monkeypatch.setenv("HAAS_BEARER_TOKEN", "hk_test_b")
    with pytest.raises(ConfigError, match="different values"):
        Haas(transport=httpx.MockTransport(lambda r: httpx.Response(200)))


def test_base_url_validation() -> None:
    for bad in [
        "ftp://x",
        "http://user:pass@h.test",
        "http://h.test/path",
        "http://h.test/?q=1",
        "not a url",
    ]:
        with pytest.raises(ConfigError):
            Haas(
                base_url=bad,
                transport=httpx.MockTransport(lambda r: httpx.Response(200)),
            )


def test_test_keys_rejected_against_https_non_loopback() -> None:
    with pytest.raises(ConfigError, match="test keys are rejected"):
        Haas(
            base_url="https://api.haas.example",
            api_key="hk_test_abc",
            transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        )
    # Loopback https and explicit opt-out are fine.
    Haas(
        base_url="https://localhost:8000",
        api_key="hk_test_abc",
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    ).close()
    Haas(
        base_url="https://api.haas.example",
        api_key="hk_test_abc",
        allow_test_keys=True,
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    ).close()


def test_repr_hides_credential() -> None:
    haas = _client(lambda r: httpx.Response(200))
    assert "hk_test_abc" not in repr(haas)
    haas.close()


def test_no_credential_warns_once() -> None:
    with pytest.warns(UserWarning, match="not authentication"):
        with _client(lambda r: httpx.Response(200), api_key=None) as haas:
            haas.me()


# --- retries and idempotency (G3) -----------------------------------------------------


def test_retries_reuse_generated_idempotency_key_and_honor_retry_after() -> None:
    keys: list[str] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["idempotency-key"])
        if len(keys) < 3:
            return httpx.Response(
                503, headers={"Retry-After": "2"}, json={"detail": "busy"}
            )
        return httpx.Response(
            202,
            json={
                "run_id": "r1",
                "thread_id": "t",
                "status": "queued",
                "stream_url": "",
                "events_url": "",
            },
        )

    haas = Haas(
        base_url="http://haas.test",
        api_key="hk_test_abc",
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        max_retries=3,
    )
    assert haas.runs.create("x").run_id == "r1"
    assert len(set(keys)) == 1 and keys[0].startswith("sdk-")
    assert sleeps == [2.0, 2.0]


def test_non_idempotent_post_is_not_retried_on_5xx() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={"detail": "boom"})

    with _client(handler, max_retries=3) as haas, pytest.raises(Exception) as info:
        haas.agents.validate("rev-1")
    assert calls == 1
    assert getattr(info.value, "status", None) == 500


def test_429_is_retried_even_without_idempotency() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"detail": "slow down"})
        return httpx.Response(
            200,
            json={"revision_id": "rev-1", "agent_name": "a"},
        )

    with _client(handler, max_retries=2) as haas:
        haas.agents.validate("rev-1")
    assert calls == 2


def test_401_is_never_retried_and_sets_auth_expired() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"detail": "bad key"})

    with _client(handler, max_retries=3) as haas, pytest.raises(Exception) as info:
        haas.me()
    assert calls == 1
    assert info.value.status == 401
    assert haas.auth_expired is True
    haas.set_credential("hk_test_new")
    assert haas.auth_expired is False


def test_connection_errors_are_retried_then_wrapped() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("refused", request=request)

    with _client(handler, max_retries=2) as haas, pytest.raises(HaasConnectionError):
        haas.me()
    assert calls == 3


def test_idempotency_key_validation() -> None:
    with _client(lambda r: httpx.Response(202, json={})) as haas:
        with pytest.raises(ValueError):
            haas.runs.create("x", idempotency_key="bad key with spaces")


# --- errors (G6) ----------------------------------------------------------------------


def test_problem_json_maps_to_typed_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("missing"):
            return httpx.Response(
                404,
                json={
                    "detail": "Run not found.",
                    "code": "run_not_found",
                    "request_id": "req-9",
                },
                headers={"content-type": "application/problem+json"},
            )
        return httpx.Response(422, json={"detail": [{"loc": ["body"], "msg": "bad"}]})

    with _client(handler, max_retries=0) as haas:
        with pytest.raises(NotFoundError) as info:
            haas.runs.get("missing")
        assert info.value.code == "run_not_found"
        assert info.value.request_id == "req-9"
        assert "Run not found." in str(info.value)
        with pytest.raises(ValidationError):
            haas.runs.get("other")


def test_openai_envelope_body_is_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": {
                    "message": "The run is waiting for human approval.",
                    "type": "approval_required",
                    "code": "approval_required",
                }
            },
        )

    with _client(handler, max_retries=0) as haas:
        with pytest.raises(Exception) as info:
            haas.me()
    assert info.value.code == "approval_required"
    assert "human approval" in str(info.value)


def test_non_json_body_is_truncated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"<html>" + b"x" * 900)

    with _client(handler, max_retries=0) as haas, pytest.raises(Exception) as info:
        haas.me()
    assert len(str(info.value)) <= 520


def test_conflict_prefix_table_maps_to_subclasses() -> None:
    cases = [
        (
            "Thread has an active run. Wait, cancel it, or explicitly branch.",
            "ThreadBusyError",
        ),
        ("Thread history changed; refresh or branch.", "HistoryChangedError"),
        ("Approval batch is stale; refresh and review.", "ApprovalStaleError"),
        ("Approval batch digest does not match.", "ApprovalStaleError"),
        ("Approval batch expired.", "ApprovalExpiredError"),
        ("No pending approval batch for this run.", "ApprovalAlreadyDecidedError"),
        ("Policy changed; create a new draft.", "PolicyChangedError"),
        (
            "Idempotency-Key was already used with a different request.",
            "IdempotencyConflictError",
        ),
    ]
    for detail, expected in cases:

        def handler(request: httpx.Request, detail: str = detail) -> httpx.Response:
            return httpx.Response(409, json={"detail": detail})

        with _client(handler, max_retries=0) as haas:
            with pytest.raises(Exception) as info:
                haas.runs.cancel("r1")
        assert type(info.value).__name__ == expected, detail


def test_rate_limit_exhaustion_raises_with_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, headers={"Retry-After": "1"}, json={"detail": "slow down"}
        )

    with _client(handler, max_retries=1) as haas, pytest.raises(RateLimitError) as info:
        haas.runs.list()
    assert info.value.retry_after == 1.0


# --- pagination (SDK-PAG-01) ----------------------------------------------------------


def test_list_filters_and_iter_follow_cursors() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        if params.get("cursor") == "c2":
            return httpx.Response(
                200, json={"items": [{"run_id": "r3", "status": "failed"}]}
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {"run_id": "r1", "status": "completed"},
                    {"run_id": "r2", "status": "failed"},
                ],
                "next_cursor": "c2",
            },
        )

    with _client(handler) as haas:
        page = haas.runs.list(status=["completed", "failed"], thread_id="t1", limit=2)
        assert [r.run_id for r in page.items] == ["r1", "r2"] and page.has_more
        ids = [r.run_id for r in haas.runs.iter(page_size=2, status="failed")]
    assert ids == ["r1", "r2", "r3"]
    assert seen[0] == {"limit": "2", "status": "completed,failed", "thread_id": "t1"}
    assert seen[-1]["cursor"] == "c2"


# --- streaming (G4) -------------------------------------------------------------------


def test_stream_resumes_with_last_event_id_and_dedupes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            body = _sse(
                ("1", "run_started", {"run_id": "r1"}),
                (None, "run.output.delta", {"delta": "he"}),
                ("2", "model_response", {"text": "hi"}),
            )
            return httpx.Response(
                200, content=body, headers={"content-type": "text/event-stream"}
            )
        body = _sse(
            ("2", "model_response", {"text": "hi"}),
            ("3", "run_completed", {"result": "done"}),
            ("4", "never", {}),
        )
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    with _client(handler) as haas:
        events = list(haas.runs.stream("r1", output_deltas=True))
    assert [e.type for e in events] == [
        "run_started",
        "run.output.delta",
        "model_response",
        "run_completed",
    ]
    assert "last-event-id" not in requests[0].headers
    assert requests[0].url.params["output_deltas"] == "true"
    assert requests[1].headers["last-event-id"] == "2"


def test_deltas_hidden_by_default_and_never_advance_cursor() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            body = _sse(
                ("1", "run_started", {}),
                (None, "run.output.delta", {"text": "he"}),
            )
        else:
            body = _sse(("2", "run_completed", {"result": "ok"}))
        return httpx.Response(200, content=body)

    with _client(handler) as haas:
        events = list(haas.runs.stream("r1"))
    assert [e.type for e in events] == ["run_started", "run_completed"]
    assert requests[1].headers["last-event-id"] == "1"


def test_stream_stops_on_input_required_and_raises_on_stream_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "r-closed" in request.url.path:
            body = _sse(
                ("1", "run_started", {}), (None, "stream_closed", {"reason": "re-auth"})
            )
        else:
            body = _sse(("5", "input_required", {"batch_id": "b1"}), ("6", "x", {}))
        return httpx.Response(200, content=body)

    with _client(handler) as haas:
        assert [e.type for e in haas.runs.stream("r-wait")] == ["input_required"]
        with pytest.raises(StreamClosedError, match="re-auth"):
            list(haas.runs.stream("r-closed"))


def test_stream_gap_advances_cursor() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            body = _sse((None, "stream_gap", {"requested": 0, "latest": 7}))
        else:
            body = _sse(("8", "run_completed", {"result": "ok"}))
        return httpx.Response(200, content=body)

    with _client(handler) as haas:
        events = list(haas.runs.stream("r1"))
    assert [e.type for e in events] == ["stream_gap", "run_completed"]
    assert requests[1].headers["last-event-id"] == "7"


def test_stream_gives_up_after_max_reconnects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(("1", "run_started", {})))

    with (
        _client(handler, stream_max_reconnects=2) as haas,
        pytest.raises(HaasConnectionError),
    ):
        list(haas.runs.stream("r1"))


def test_stream_client_errors_are_not_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "nope"})

    with _client(handler) as haas, pytest.raises(NotFoundError):
        list(haas.runs.stream("r1"))


def test_non_numeric_last_event_id_normalizes_to_zero() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("last-event-id"))
        return httpx.Response(200, content=_sse(("1", "run_completed", {})))

    with _client(handler) as haas:
        list(haas.runs.stream("r1", last_event_id="3-2"))
    assert seen == ["0"]


# --- approvals (G5) -------------------------------------------------------------------


def _review(**overrides: Any) -> dict[str, Any]:
    review = {
        "batch_id": "b1",
        "run_id": "r1",
        "step_number": 1,
        "status": "pending",
        "batch_digest": BATCH_DIGEST,
        "expires_at": "2099-01-01T00:00:00Z",
        "expired": False,
        "tool_calls": [
            {"id": "c1", "name": "prepare_refund_batch", "arguments": {"x": 1}}
        ],
    }
    review.update(overrides)
    return review


def test_decide_sends_reviewed_digest_and_decision_object() -> None:
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_review())
        posted.append(json.loads(request.content))
        assert request.headers["idempotency-key"]
        return httpx.Response(
            202, json={"run_id": "r1", "batch_id": "b1", "decision": "approve"}
        )

    with _client(handler) as haas:
        review = haas.approvals.review("b1")
        haas.approvals.decide(review, review.approve())
        haas.approvals.decide(review, review.reject(reason="no"))
        with pytest.raises(ValueError):
            haas.approvals.decide(review, "maybe")
    assert posted == [
        {"decision": "approve", "batch_digest": BATCH_DIGEST},
        {"decision": "reject", "batch_digest": BATCH_DIGEST, "reason": "no"},
    ]


def test_decide_refuses_stale_or_decided_reviews_locally() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_review(status="approved"))

    with _client(handler) as haas:
        review = haas.approvals.review("b1")
        with pytest.raises(ApprovalStaleError):
            haas.approvals.decide(review, review.approve())


def test_decide_refuses_expired_reviews_locally() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_review(expired=True))

    with _client(handler) as haas:
        review = haas.approvals.review("b1")
        with pytest.raises(ApprovalStaleError, match="expired"):
            haas.approvals.decide(review, review.approve())


def test_reason_length_is_validated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_review())

    with _client(handler) as haas:
        review = haas.approvals.review("b1")
        with pytest.raises(ValueError):
            haas.approvals.decide(review, review.reject(reason="x" * 2001))


# --- run limits (SDK-RUN-03/04) -------------------------------------------------------


def test_pinned_agent_rejects_overrides() -> None:
    with _client(lambda r: httpx.Response(202, json={})) as haas:
        with pytest.raises(ValueError, match="pins"):
            haas.runs.create("x", agent_revision_id="rev-1", max_steps=5)


def test_run_limit_bounds_are_validated_client_side() -> None:
    with _client(lambda r: httpx.Response(202, json={})) as haas:
        for kwargs in [
            {"max_steps": 0},
            {"max_steps": 51},
            {"max_cost_usd": 0},
            {"max_cost_usd": 11},
            {"deadline_sec": 5},
            {"deadline_sec": 100000},
            {"max_tool_invocations": 0},
        ]:
            with pytest.raises(ValueError):
                haas.runs.create("x", **kwargs)


# --- compat (SDK-COMPAT-01) -----------------------------------------------------------


def test_require_compat_passes_and_fails() -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "version": "1",
                "actions": [],
                "event_schemas": ["run-events.v1"],
                "features": [],
                "durable_replay": True,
            },
        )

    with _client(ok) as haas:
        haas.require_compat()

    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "version": "2",
                "actions": [],
                "event_schemas": [],
                "features": [],
                "durable_replay": False,
            },
        )

    with _client(bad) as haas, pytest.raises(IncompatibleServerError):
        haas.require_compat()


def test_verify_server_calls_compat_on_first_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "version": "1",
                "actions": [],
                "event_schemas": ["run-events.v1"],
                "features": [],
                "durable_replay": True,
            },
        )

    with _client(handler, verify_server=True) as haas:
        haas.me()


# --- misc -----------------------------------------------------------------------------


def test_path_segments_are_escaped() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.raw_path.decode())
        return httpx.Response(200, json={"run_id": "x", "status": "running"})

    with _client(handler) as haas:
        haas.runs.get("../admin")
    assert paths == ["/v1/runs/..%2Fadmin"]


def test_inspect_composes_three_calls() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/steps"):
            return httpx.Response(200, json={"steps": [], "invocations": []})
        if request.url.path.endswith("/usage"):
            return httpx.Response(
                200,
                json={
                    "budget": {
                        "max_cost_usd": 10.0,
                        "settled_usd": 0.0,
                        "uncertain_usd": 0.0,
                        "reserved_usd": 0.0,
                        "remaining_usd": 10.0,
                        "steps_used": 0,
                        "max_steps": 50,
                        "tool_invocations_used": 0,
                        "max_tool_invocations": 100,
                        "deadline_at": None,
                    },
                    "entries": [],
                },
            )
        return httpx.Response(200, json={"run_id": "r1", "status": "completed"})

    with _client(handler) as haas:
        out = haas.runs.inspect("r1")
    assert out["run"].run_id == "r1"
    assert sorted(seen) == [
        "/v1/runs/r1",
        "/v1/runs/r1/steps",
        "/v1/runs/r1/usage",
    ]


def test_wait_raises_run_failed_on_error_event() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stream"):
            body = _sse(
                ("1", "run_error", {"error": "boom", "code": "budget.max_cost"})
            )
            return httpx.Response(200, content=body)
        return httpx.Response(
            200, json={"run_id": "r1", "status": "failed", "total_cost_usd": 1.0}
        )

    with _client(handler) as haas:
        handle = haas.runs.attach("r1")
        with pytest.raises(RunFailed) as info:
            handle.wait(timeout=5)
    assert info.value.code == "budget.max_cost"


def test_wait_raises_run_failed_on_cancelled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stream"):
            body = _sse(("1", "run_cancelled", {"run_id": "r1"}))
            return httpx.Response(200, content=body)
        return httpx.Response(200, json={"run_id": "r1", "status": "cancelled"})

    with _client(handler) as haas:
        handle = haas.runs.attach("r1")
        with pytest.raises(RunFailed) as info:
            handle.wait(timeout=5)
    assert info.value.code == "run.cancelled"


def test_wait_without_handler_raises_approval_required() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stream"):
            body = _sse(
                (
                    "1",
                    "input_required",
                    {"batch_id": "b1", "batch_digest": BATCH_DIGEST},
                )
            )
            return httpx.Response(200, content=body)
        return httpx.Response(200, json={"run_id": "r1", "status": "input_required"})

    with _client(handler) as haas:
        handle = haas.runs.attach("r1")
        with pytest.raises(Exception, match="waiting for approval"):
            handle.wait(timeout=5)


def test_wait_with_handler_decides_and_continues() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/stream"):
            if request.headers.get("last-event-id") == "1":
                body = _sse(("2", "run_completed", {"result": "done"}))
            else:
                body = _sse(
                    (
                        "1",
                        "input_required",
                        {"batch_id": "b1", "batch_digest": BATCH_DIGEST},
                    )
                )
            return httpx.Response(200, content=body)
        if path == "/v1/approvals/b1":
            return httpx.Response(200, json=_review())
        if path.endswith("/decision"):
            return httpx.Response(
                202, json={"run_id": "r1", "batch_id": "b1", "decision": "approve"}
            )
        return httpx.Response(
            200, json={"run_id": "r1", "status": "completed", "result": "done"}
        )

    with _client(handler) as haas:
        handle = haas.runs.attach("r1")
        run = handle.wait(timeout=5, on_approval=lambda review: review.approve())
    assert run.status == "completed" and run.result == "done"


def test_wait_handler_returning_none_leaves_pending() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/stream"):
            body = _sse(
                (
                    "1",
                    "input_required",
                    {"batch_id": "b1", "batch_digest": BATCH_DIGEST},
                )
            )
            return httpx.Response(200, content=body)
        if path == "/v1/approvals/b1":
            return httpx.Response(200, json=_review())
        return httpx.Response(200, json={"run_id": "r1", "status": "input_required"})

    with _client(handler) as haas:
        handle = haas.runs.attach("r1")
        run = handle.wait(timeout=5, on_approval=lambda review: None)
    assert run.status == "input_required"


def test_wait_timeout_raises_and_run_keeps_running() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stream"):
            return httpx.Response(200, content=b": keep-alive\n\n")
        return httpx.Response(200, json={"run_id": "r1", "status": "running"})

    with _client(handler) as haas:
        handle = haas.runs.attach("r1")
        with pytest.raises(TimeoutError):
            handle.wait(timeout=0.2)


def test_handle_cancel_swallows_conflict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/cancel"):
            return httpx.Response(409, json={"detail": "Run is already terminal."})
        return httpx.Response(200, json={"run_id": "r1", "status": "cancelled"})

    with _client(handler) as haas:
        handle = haas.runs.attach("r1")
        run = handle.cancel()
    assert run.status == "cancelled"


def test_reconciliation_needed_lists_unknown_invocations() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "steps": [],
                "invocations": [
                    {
                        "invocation_id": "i1",
                        "status": "unknown",
                        "effect_key": "ek1",
                        "step_number": 1,
                        "ordinal": 0,
                        "tool_name": "t",
                        "queue": "q",
                        "attempt": 1,
                    },
                    {
                        "invocation_id": "i2",
                        "status": "succeeded",
                        "effect_key": "ek2",
                        "step_number": 1,
                        "ordinal": 1,
                        "tool_name": "t",
                        "queue": "q",
                        "attempt": 1,
                    },
                ],
            },
        )

    with _client(handler) as haas:
        handle = haas.runs.attach("r1")
        needed = handle.reconciliation_needed()
    assert [i.effect_key for i in needed] == ["ek1"]


# --- async parity ---------------------------------------------------------------------


async def test_async_client_mirrors_sync_behaviour() -> None:
    requests: list[httpx.Request] = []

    async def no_sleep(_: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/stream"):
            stream_calls = [r for r in requests if r.url.path.endswith("/stream")]
            if len(stream_calls) == 1:
                return httpx.Response(200, content=_sse(("1", "run_started", {})))
            return httpx.Response(
                200, content=_sse(("2", "run_completed", {"result": "ok"}))
            )
        if path == "/v1/runs" and request.method == "POST":
            return httpx.Response(
                202,
                json={
                    "run_id": "r1",
                    "thread_id": "t",
                    "status": "queued",
                    "stream_url": "",
                    "events_url": "",
                },
            )
        if path == "/v1/approvals":
            if request.url.params.get("cursor"):
                return httpx.Response(200, json={"items": []})
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "batch_id": "b1",
                            "run_id": "r1",
                            "batch_digest": BATCH_DIGEST,
                            "expires_at": "2099-01-01T00:00:00Z",
                            "status": "pending",
                        }
                    ],
                    "next_cursor": "n",
                },
            )
        return httpx.Response(200, json={"run_id": "r1", "status": "completed"})

    async with AsyncHaas(
        base_url="http://haas.test",
        api_key="hk_test_abc",
        transport=httpx.MockTransport(handler),
        sleep=no_sleep,
    ) as haas:
        created = await haas.runs.create("hi")
        events = [e async for e in haas.runs.stream(created.run_id)]
        batches = [b async for b in haas.approvals.iter()]
        run = await haas.runs.attach("r1").wait(timeout=5)
    assert [e.type for e in events] == ["run_started", "run_completed"]
    stream_requests = [r for r in requests if r.url.path.endswith("/stream")]
    assert stream_requests[1].headers["last-event-id"] == "1"
    assert [b.batch_id for b in batches] == ["b1"]
    assert run.is_terminal
