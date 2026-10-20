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

BATCH_DIGEST = "a" * 64


def _client(handler: Any, **kwargs: Any) -> Haas:
    return Haas(
        base_url="http://haas.test",
        api_key="hk_test_abc",
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


def test_headers_and_run_create_with_idempotency_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"run_id": "r1", "status": "queued"})

    with _client(handler) as haas:
        out = haas.runs.create("hello", max_steps=3, idempotency_key="k-1")
    assert out["run_id"] == "r1"
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


def test_retries_reuse_generated_idempotency_key_and_honor_retry_after() -> None:
    keys: list[str] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["idempotency-key"])
        if len(keys) < 3:
            return httpx.Response(503, headers={"Retry-After": "2"}, json={"detail": "busy"})
        return httpx.Response(202, json={"run_id": "r1", "status": "queued"})

    haas = Haas(
        base_url="http://haas.test",
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        max_retries=3,
    )
    assert haas.runs.create("x")["run_id"] == "r1"
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


def test_problem_json_maps_to_typed_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("missing"):
            return httpx.Response(
                404,
                json={"detail": "Run not found.", "code": "run_not_found", "request_id": "req-9"},
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


def test_rate_limit_exhaustion_raises_with_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "1"}, json={"detail": "slow down"})

    with _client(handler, max_retries=1) as haas, pytest.raises(RateLimitError) as info:
        haas.runs.list()
    assert info.value.retry_after == 1.0


def test_connection_errors_are_retried_then_wrapped() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("refused", request=request)

    with _client(handler, max_retries=2) as haas, pytest.raises(HaasConnectionError):
        haas.me()
    assert calls == 3


def test_list_filters_and_iter_follow_cursors() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        if params.get("cursor") == "c2":
            return httpx.Response(200, json={"items": [{"run_id": "r3", "status": "failed"}]})
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
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
        body = _sse(
            ("2", "model_response", {"text": "hi"}),
            ("3", "run_completed", {"result": "done"}),
            ("4", "never", {}),
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

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


def test_stream_stops_on_input_required_and_raises_on_stream_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "r-closed" in request.url.path:
            body = _sse(("1", "run_started", {}), (None, "stream_closed", {"reason": "re-auth"}))
        else:
            body = _sse(("5", "input_required", {"batch_id": "b1"}), ("6", "x", {}))
        return httpx.Response(200, content=body)

    with _client(handler) as haas:
        assert [e.type for e in haas.runs.stream("r-wait")] == ["input_required"]
        with pytest.raises(StreamClosedError, match="re-auth"):
            list(haas.runs.stream("r-closed"))


def test_stream_gives_up_after_max_reconnects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(("1", "run_started", {})))

    with _client(handler, stream_max_reconnects=2) as haas, pytest.raises(HaasConnectionError):
        list(haas.runs.stream("r1"))


def test_stream_client_errors_are_not_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "nope"})

    with _client(handler) as haas, pytest.raises(NotFoundError):
        list(haas.runs.stream("r1"))


def test_approve_fetches_digest_and_deny_sends_reason() -> None:
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"batch_id": "b1", "run_id": "r1", "status": "pending",
                      "batch_digest": BATCH_DIGEST},
            )
        posted.append(json.loads(request.content))
        assert request.headers["idempotency-key"]
        return httpx.Response(202, json={"status": "accepted"})

    with _client(handler) as haas:
        haas.approvals.approve("b1")
        haas.approvals.deny("b1", batch_digest="b" * 64, reason="no")
        with pytest.raises(ValueError):
            haas.approvals.decide("b1", "maybe", batch_digest=BATCH_DIGEST)
    assert posted == [
        {"decision": "approve", "batch_digest": BATCH_DIGEST},
        {"decision": "reject", "batch_digest": "b" * 64, "reason": "no"},
    ]


def test_artifacts_endpoints() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/v1/artifacts" and request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "artifact_id": "a1",
                    "run_id": "r1",
                    "name": "x.txt",
                    "size_bytes": 3,
                    "sha256": "abc123",
                    "content_type": "text/plain",
                    "object_key": "key1",
                    "source": "api",
                    "created_by": None,
                    "metadata": {},
                    "created_at": "2024-01-01T00:00:00Z",
                    "expires_at": None,
                    "deleted_at": None,
                    "tenant_id": "tenant1",
                },
            )
        if path == "/v1/artifacts":
            return httpx.Response(200, json={"items": [{"artifact_id": "a1", "run_id": "r1", "name": "x.txt", "size_bytes": 3, "sha256": "abc123", "content_type": "text/plain", "object_key": "key1", "source": "api", "created_by": None, "metadata": {}, "created_at": "2024-01-01T00:00:00Z", "expires_at": None, "deleted_at": None, "tenant_id": "tenant1"}]})
        if path == "/v1/artifacts/a1/download":
            return httpx.Response(206, content=b"bc")
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"artifact_id": "a1", "run_id": "r1", "name": "x.txt", "size_bytes": 3, "sha256": "abc123", "content_type": "text/plain", "object_key": "key1", "source": "api", "created_by": None, "metadata": {}, "created_at": "2024-01-01T00:00:00Z", "expires_at": None, "deleted_at": None, "tenant_id": "tenant1"})

    with _client(handler) as haas:
        uploaded = haas.artifacts.upload(run_id="r1", content=b"abc", filename="x.txt", content_type="text/plain")
        assert uploaded.artifact_id == "a1" and uploaded.size_bytes == 3
        assert [a.artifact_id for a in haas.artifacts.list("r1")] == ["a1"]
        assert haas.artifacts.get("a1").content_type == "text/plain"
        assert haas.artifacts.content("a1", start=1, end=2) == b"bc"
        haas.artifacts.delete("a1")
    # Check the upload request was made to /v1/artifacts with multipart form
    assert seen[0].url.path == "/v1/artifacts"
    assert seen[0].method == "POST"
    assert b'name="file"; filename="x.txt"' in seen[0].content
    assert seen[1].url.path == "/v1/artifacts" and seen[1].method == "GET"
    assert seen[2].url.path == "/v1/artifacts/a1" and seen[2].method == "GET"
    assert seen[3].url.path == "/v1/artifacts/a1/download" and seen[3].method == "GET"
    assert seen[3].headers["range"] == "bytes=1-2"
    assert seen[4].method == "DELETE"


def test_path_segments_are_escaped() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.raw_path.decode())
        return httpx.Response(200, json={"run_id": "x", "status": "running"})

    with _client(handler) as haas:
        haas.runs.get("../admin")
    assert paths == ["/v1/runs/..%2Fadmin"]


def test_wait_polls_until_terminal() -> None:
    statuses = iter(["queued", "running", "completed"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"run_id": "r1", "status": next(statuses)})

    with _client(handler) as haas:
        assert haas.runs.wait("r1", poll_interval=0).status == "completed"


async def test_async_client_mirrors_sync_behaviour() -> None:
    requests: list[httpx.Request] = []

    async def no_sleep(_: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/stream"):
            if len([r for r in requests if r.url.path.endswith("/stream")]) == 1:
                return httpx.Response(200, content=_sse(("1", "run_started", {})))
            return httpx.Response(200, content=_sse(("2", "run_completed", {"result": "ok"})))
        if path == "/v1/runs" and request.method == "POST":
            return httpx.Response(202, json={"run_id": "r1", "status": "queued"})
        if path == "/v1/approvals":
            if request.url.params.get("cursor"):
                return httpx.Response(200, json={"items": []})
            return httpx.Response(
                200,
                json={
                    "items": [{"batch_id": "b1", "run_id": "r1", "batch_digest": BATCH_DIGEST}],
                    "next_cursor": "n",
                },
            )
        return httpx.Response(200, json={"run_id": "r1", "status": "completed"})

    async with AsyncHaas(
        base_url="http://haas.test", transport=httpx.MockTransport(handler), sleep=no_sleep
    ) as haas:
        created = await haas.runs.create("hi")
        events = [e async for e in haas.runs.stream(created["run_id"])]
        batches = [b async for b in haas.approvals.iter()]
        run = await haas.runs.wait("r1")
    assert [e.type for e in events] == ["run_started", "run_completed"]
    stream_requests = [r for r in requests if r.url.path.endswith("/stream")]
    assert stream_requests[1].headers["last-event-id"] == "1"
    assert [b.batch_id for b in batches] == ["b1"]
    assert run.is_terminal
