"""Public HaaS identity verification contract tests."""

import json

import httpx
import pytest
from fastapi import HTTPException
from httpx import AsyncByteStream

from app.identity import verify_identity, verify_public_run

IDENTITY = {
    "tenant_id": "tenant-exact",
    "principal_id": "principal-exact",
    "roles": ["submitter"],
    "capabilities": {"actions": ["run.submit"]},
}


def transport_for(response: httpx.Response) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://trusted.example/v1/me"
        assert request.headers["authorization"] == "Bearer synthetic"
        assert request.headers["x-tenant-id"] == "selector"
        return response

    return httpx.MockTransport(handle)


class UnbufferedJSONStream(AsyncByteStream):
    """Deliver wire chunks without attaching a pre-buffered response body."""

    def __init__(self, value: dict) -> None:
        encoded = json.dumps(value).encode()
        self.chunks = [encoded[:9], encoded[9:]]

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_verified_public_identity_preserves_exact_ids() -> None:
    identity = await verify_identity(
        "Bearer synthetic",
        "selector",
        origin="https://trusted.example/v1/me",
        transport=transport_for(httpx.Response(200, json=IDENTITY)),
    )
    assert identity.tenant_id == "tenant-exact"
    assert identity.principal_id == "principal-exact"


@pytest.mark.asyncio
async def test_identity_verifier_parses_consumed_unbuffered_stream() -> None:
    response = httpx.Response(200, stream=UnbufferedJSONStream(IDENTITY))
    identity = await verify_identity(
        "Bearer synthetic",
        "selector",
        origin="https://trusted.example/v1/me",
        transport=transport_for(response),
    )
    assert identity == type(identity)("tenant-exact", "principal-exact")


@pytest.mark.parametrize(("status", "expected"), [(401, 401), (403, 403), (302, 503), (500, 503)])
@pytest.mark.asyncio
async def test_upstream_status_fails_closed(status: int, expected: int) -> None:
    with pytest.raises(HTTPException) as error:
        await verify_identity(
            "Bearer synthetic",
            "selector",
            origin="https://trusted.example/v1/me",
            transport=httpx.MockTransport(lambda _: httpx.Response(status)),
        )
    assert error.value.status_code == expected


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"tenant_id": "t", "principal_id": "p"},
        {"tenant_id": "", "principal_id": "p", "capabilities": {"actions": ["run.submit"]}},
        {"tenant_id": "t", "principal_id": " ", "capabilities": {"actions": ["run.submit"]}},
        {"tenant_id": "t", "principal_id": "p", "capabilities": {"actions": []}},
    ],
)
@pytest.mark.asyncio
async def test_malformed_identity_or_missing_atlas_action_returns_503(body: dict) -> None:
    with pytest.raises(HTTPException) as error:
        await verify_identity(
            "Bearer synthetic",
            None,
            origin="https://trusted.example/v1/me",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
        )
    assert error.value.status_code == 503


@pytest.mark.parametrize("authorization", [None, "", "Basic synthetic", "Bearer ", "Bearer x\r\nInjected: y"])
@pytest.mark.asyncio
async def test_missing_or_malformed_auth_fails_before_network(authorization: str | None) -> None:
    called = False

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=IDENTITY)

    with pytest.raises(HTTPException) as error:
        await verify_identity(
            authorization,
            None,
            origin="https://trusted.example/v1/me",
            transport=httpx.MockTransport(handle),
        )
    assert error.value.status_code == 401
    assert not called


@pytest.mark.asyncio
async def test_oversized_identity_body_is_rejected() -> None:
    content = json.dumps(IDENTITY) + (" " * (64 * 1024))
    with pytest.raises(HTTPException) as error:
        await verify_identity(
            "Bearer synthetic",
            None,
            origin="https://trusted.example/v1/me",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content)),
        )
    assert error.value.status_code == 503


def test_http_identity_origin_is_loopback_and_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_HAAS_API_ORIGIN", "http://127.0.0.1:8000")
    monkeypatch.delenv("ATLAS_ALLOW_LOCAL_HAAS_HTTP", raising=False)
    with pytest.raises(HTTPException):
        from app.identity import _trusted_me_url

        _trusted_me_url()
    monkeypatch.setenv("ATLAS_ALLOW_LOCAL_HAAS_HTTP", "true")
    from app.identity import _trusted_me_url

    assert _trusted_me_url() == "http://127.0.0.1:8000/v1/me"
    monkeypatch.setenv("ATLAS_HAAS_API_ORIGIN", "http://192.0.2.1:8000")
    with pytest.raises(HTTPException):
        _trusted_me_url()


def test_origin_with_path_or_userinfo_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.identity import _trusted_me_url

    for origin in ("https://user@trusted.example", "https://trusted.example/path"):
        monkeypatch.setenv("ATLAS_HAAS_API_ORIGIN", origin)
        with pytest.raises(HTTPException):
            _trusted_me_url()


@pytest.mark.asyncio
async def test_terminal_run_status_is_confirmed_by_public_run_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATLAS_HAAS_API_ORIGIN", "https://trusted.example")
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://trusted.example/v1/runs/run-1"
        assert request.headers["authorization"] == "Bearer synthetic"
        assert request.headers["x-tenant-id"] == "selector"
        return httpx.Response(200, json={"run_id": "run-1", "status": "completed"})

    await verify_public_run(
        "Bearer synthetic",
        "selector",
        "run-1",
        terminal_status="completed",
        transport=httpx.MockTransport(handle),
    )


@pytest.mark.asyncio
async def test_run_verifier_parses_consumed_unbuffered_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_HAAS_API_ORIGIN", "https://trusted.example")

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://trusted.example/v1/runs/run-1"
        return httpx.Response(
            200,
            stream=UnbufferedJSONStream({
                "run_id": "run-1",
                "thread_id": "thread-1",
                "status": "running",
            }),
        )

    assert await verify_public_run(
        "Bearer synthetic",
        None,
        "run-1",
        transport=httpx.MockTransport(handle),
    ) == {"status": "running", "thread_id": "thread-1"}


@pytest.mark.parametrize(
    ("body", "expected_status", "http_status"),
    [
        ({"run_id": "other", "status": "completed"}, "completed", 503),
        ({"run_id": "run-1", "status": "running"}, "completed", 409),
        ({"run_id": "run-1", "status": "failed"}, "completed", 409),
    ],
)
@pytest.mark.asyncio
async def test_client_cannot_assert_unverified_terminal_outcome(
    body: dict, expected_status: str, http_status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATLAS_HAAS_API_ORIGIN", "https://trusted.example")
    with pytest.raises(HTTPException) as error:
        await verify_public_run(
            "Bearer synthetic",
            None,
            "run-1",
            terminal_status=expected_status,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
        )
    assert error.value.status_code == http_status
