from __future__ import annotations

import json
import socket

import httpx
import pytest

from app.core.config import settings
from app.core.context import SecurityViolationError, ToolRemediationError

HTML = """<html><head><title> Release  Notes </title><meta name="description" content="What changed">
<script>var secret = 'IGNORE ME';</script></head><body>
<nav><a href="/nav">Nav link</a></nav>
<h1>Postgres 17</h1><p>Faster <b>vacuum</b> &amp; more.</p>
<ul><li>Feature A</li><li>Feature B</li></ul>
<a href="/docs/changelog">Changelog</a> <a href="https://other.example.org/x">Other</a>
<a href="mailto:a@b.c">mail</a><a href="/docs/changelog">dup</a>
<footer>Footer junk</footer></body></html>"""


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    table = {
        "example.com": "93.184.216.34",
        "internal.example.com": "10.0.0.5",
        "other.example.org": "93.184.216.35",
    }

    def fake(host, port, *a, **kw):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (table.get(host, "93.184.216.34"), port),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


def html_response(body=HTML, status=200, ctype="text/html; charset=utf-8", **headers):
    return httpx.Response(
        status, content=body.encode(), headers={"content-type": ctype, **headers}
    )


async def test_fetch_page_extracts_readable_text(make_harness):
    h = make_harness("web_tools.py", lambda r: html_response())
    out = json.loads(await h.call("web_fetch_page", url="https://example.com/notes"))
    assert out["title"] == "Release Notes" and out["description"] == "What changed"
    assert "# Postgres 17" in out["text"] and "- Feature A" in out["text"]
    assert "Faster vacuum & more." in out["text"]
    assert (
        "IGNORE ME" not in out["text"]
        and "Footer junk" not in out["text"]
        and "Nav link" not in out["text"]
    )
    assert out["next_offset"] is None
    assert "Chrome/120.0.0.0" in h.requests[0].headers["user-agent"]


async def test_fetch_page_paging(make_harness):
    long = "<p>" + " ".join(f"w{i}" for i in range(3000)) + "</p>"
    h = make_harness("web_tools.py", lambda r: html_response(long))
    first = json.loads(
        await h.call("web_fetch_page", url="https://example.com/", max_chars=1000)
    )
    assert len(first["text"]) == 1000 and first["next_offset"] == 1000
    second = json.loads(
        await h.call(
            "web_fetch_page",
            url="https://example.com/",
            max_chars=1000,
            offset=first["next_offset"],
        )
    )
    assert (
        second["text"] != first["text"]
        and second["total_chars"] == first["total_chars"]
    )


async def test_redirects_are_followed_and_revalidated(make_harness):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b"})
        if req.url.path == "/b":
            return httpx.Response(
                301, headers={"location": "https://internal.example.com/secret"}
            )
        raise AssertionError("must not reach the internal host")

    h = make_harness("web_tools.py", handler)
    with pytest.raises(SecurityViolationError, match="private address"):
        await h.call("web_fetch_page", url="https://example.com/a")
    assert [r.url.path for r in h.requests] == ["/a", "/b"]


async def test_blocks_private_targets_and_schemes(make_harness):
    h = make_harness("web_tools.py", lambda r: html_response())
    for url in (
        "http://169.254.169.254/latest",
        "http://localhost:8000/healthz",
        "file:///etc/passwd",
        "https://internal.example.com/",
    ):
        with pytest.raises(SecurityViolationError):
            await h.call("web_fetch_page", url=url)
    assert h.requests == []


async def test_redirect_loop_and_errors(make_harness):
    h = make_harness(
        "web_tools.py", lambda r: httpx.Response(302, headers={"location": "/loop"})
    )
    with pytest.raises(ToolRemediationError, match="Too many redirects"):
        await h.call("web_fetch_page", url="https://example.com/")
    h = make_harness("web_tools.py", lambda r: httpx.Response(404, text="no"))
    with pytest.raises(ToolRemediationError, match="HTTP 404"):
        await h.call("web_fetch_page", url="https://example.com/")
    h = make_harness(
        "web_tools.py",
        lambda r: httpx.Response(
            200, content=b"%PDF", headers={"content-type": "application/pdf"}
        ),
    )
    with pytest.raises(ToolRemediationError, match="Unsupported content type"):
        await h.call("web_fetch_page", url="https://example.com/")


async def test_json_and_plain_text_pass_through(make_harness):
    h = make_harness("web_tools.py", lambda r: httpx.Response(200, json={"a": 1}))
    out = json.loads(await h.call("web_fetch_page", url="https://example.com/api"))
    assert json.loads(out["text"]) == {"a": 1} and out["title"] == ""


async def test_extract_links_filters_and_dedupes(make_harness):
    h = make_harness("web_tools.py", lambda r: html_response())
    out = json.loads(await h.call("web_extract_links", url="https://example.com/notes"))
    urls = [x["url"] for x in out["links"]]
    assert urls == ["https://example.com/docs/changelog", "https://other.example.org/x"]
    only = json.loads(
        await h.call(
            "web_extract_links", url="https://example.com/notes", same_host_only=True
        )
    )
    assert [x["url"] for x in only["links"]] == ["https://example.com/docs/changelog"]
    flt = json.loads(
        await h.call(
            "web_extract_links", url="https://example.com/notes", contains="OTHER"
        )
    )
    assert flt["count"] == 1


async def test_web_search_calls_searxng_and_maps_freshness(make_harness, monkeypatch):
    monkeypatch.setattr(settings, "searxng_url", "http://example.com")

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "example.com"
        assert req.url.path == "/search"
        assert req.url.params["format"] == "json"
        assert req.url.params["q"] == "postgres 17"
        # freshness 'pw' maps to SearXNG time_range 'week'
        assert req.url.params["time_range"] == "week"
        # No Brave auth header is sent anymore.
        assert "x-subscription-token" not in req.headers
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "T",
                        "url": "https://x.org",
                        "content": "Hi  there",
                        "publishedDate": "2024-01-02T03:04:05",
                    }
                ]
            },
        )

    # No secrets supplied: SearXNG is keyless.
    h = make_harness("web_tools.py", handler)
    out = json.loads(await h.call("web_search", query="postgres 17", freshness="pw"))
    assert out["results"] == [
        {
            "title": "T",
            "url": "https://x.org",
            "snippet": "Hi there",
            "age": "2024-01-02T03:04:05",
        }
    ]
    with pytest.raises(ToolRemediationError, match="freshness"):
        await h.call("web_search", query="postgres 17", freshness="forever")


async def test_web_search_respects_searxng_url_override(make_harness, monkeypatch):
    monkeypatch.setattr(settings, "searxng_url", "http://other.example.org")

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "other.example.org"
        return httpx.Response(200, json={"results": []})

    h = make_harness("web_tools.py", handler)
    out = json.loads(await h.call("web_search", query="anything"))
    assert out["count"] == 0 and out["results"] == []


@pytest.mark.parametrize("status,match", [(429, "rate limit"), (500, "HTTP 500")])
async def test_web_search_errors(make_harness, monkeypatch, status, match):
    monkeypatch.setattr(settings, "searxng_url", "http://example.com")
    h = make_harness("web_tools.py", lambda r: httpx.Response(status))
    with pytest.raises(ToolRemediationError, match=match):
        await h.call("web_search", query="anything")


async def test_web_search_needs_no_secret(make_harness, monkeypatch):
    # web_search must work with an empty secret vault (SearXNG is keyless).
    monkeypatch.setattr(settings, "searxng_url", "http://example.com")
    h = make_harness(
        "web_tools.py",
        lambda r: httpx.Response(200, json={"results": []}),
        secrets={},
    )
    out = json.loads(await h.call("web_search", query="anything"))
    assert out["count"] == 0
