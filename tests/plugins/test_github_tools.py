from __future__ import annotations

import json

import httpx
import pytest

from app.core.context import SecurityViolationError, ToolRemediationError

TOKEN = {"GITHUB_TOKEN": "ghp_test"}


def issue(n, title="t", pr=False, comments=0, labels=("bug",)):
    d = {
        "number": n,
        "title": title,
        "state": "open",
        "user": {"login": "alice"},
        "labels": [{"name": lb} for lb in labels],
        "comments": comments,
        "updated_at": "2026-01-01T00:00:00Z",
        "html_url": f"https://github.com/o/r/issues/{n}",
        "body": "B" * 5000,
        "assignees": [{"login": "bob"}],
    }
    if pr:
        d["pull_request"] = {}
    return d


def test_tool_flags(make_harness):
    h = make_harness("github_tools.py", secrets=TOKEN)
    flags = {n: t.requires_approval for n, t in h.reg.tools.items()}
    assert flags == {
        "github_list_issues": False,
        "github_get_issue": False,
        "github_list_pull_requests": False,
        "github_get_pull_request": False,
        "github_get_ci_status": False,
        "github_create_issue": True,
        "github_add_comment": True,
    }
    for t in h.reg.tools.values():
        t.to_openai_schema()


async def test_list_issues_filters_prs_and_sends_auth(make_harness):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/o/r/issues"
        assert req.url.params["labels"] == "bug" and req.url.params["state"] == "open"
        return httpx.Response(200, json=[issue(1), issue(2, pr=True), issue(3)])

    h = make_harness("github_tools.py", handler, secrets=TOKEN)
    out = json.loads(
        await h.call("github_list_issues", repo="o/r", labels="bug", limit=5)
    )
    assert [i["number"] for i in out["issues"]] == [1, 3]
    req = h.requests[0]
    assert req.headers["authorization"] == "Bearer ghp_test"
    assert req.headers["x-github-api-version"] == "2022-11-28"


async def test_repo_url_form_accepted_and_invalid_rejected(make_harness):
    h = make_harness(
        "github_tools.py", lambda r: httpx.Response(200, json=[]), secrets=TOKEN
    )
    await h.call("github_list_issues", repo="https://github.com/o/r/")
    assert h.requests[0].url.path == "/repos/o/r/issues"
    with pytest.raises(ToolRemediationError, match="owner/name"):
        await h.call("github_list_issues", repo="../etc/passwd")


async def test_get_issue_truncates_body_and_fetches_last_comments_page(make_harness):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/comments"):
            assert req.url.params["page"] == "3" and req.url.params["per_page"] == "5"
            return httpx.Response(
                200,
                json=[{"user": {"login": "carol"}, "created_at": "x", "body": "hi"}],
            )
        return httpx.Response(200, json=issue(7, comments=11))

    h = make_harness("github_tools.py", handler, secrets=TOKEN)
    out = json.loads(
        await h.call("github_get_issue", repo="o/r", number=7, include_comments=True)
    )
    assert len(out["body"]) == 3000 and out["body"].endswith("…")
    assert out["recent_comments"][0]["author"] == "carol"
    assert out["assignees"] == ["bob"]


async def test_get_pull_request_aggregates_reviews(make_harness):
    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p.endswith("/files"):
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "a.py",
                        "status": "modified",
                        "additions": 3,
                        "deletions": 1,
                    }
                ],
            )
        if p.endswith("/reviews"):
            return httpx.Response(
                200,
                json=[
                    {"user": {"login": "r1"}, "state": "CHANGES_REQUESTED"},
                    {"user": {"login": "r1"}, "state": "APPROVED"},
                    {"user": {"login": "r2"}, "state": "COMMENTED"},
                ],
            )
        return httpx.Response(
            200,
            json={
                "number": 9,
                "title": "Fix",
                "state": "open",
                "merged": False,
                "draft": False,
                "user": {"login": "alice"},
                "head": {"ref": "fix"},
                "base": {"ref": "main"},
                "mergeable_state": "clean",
                "commits": 1,
                "additions": 3,
                "deletions": 1,
                "changed_files": 1,
                "body": "desc",
                "html_url": "u",
            },
        )

    h = make_harness("github_tools.py", handler, secrets=TOKEN)
    out = json.loads(await h.call("github_get_pull_request", repo="o/r", number=9))
    assert out["reviews"] == {"r1": "APPROVED"}
    assert out["files"][0] == {"path": "a.py", "status": "modified", "+": 3, "-": 1}
    assert out["mergeable_state"] == "clean"


async def test_ci_status_summary(make_harness):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "name": "lint",
                            "status": "completed",
                            "conclusion": "success",
                        },
                        {
                            "name": "tests",
                            "status": "completed",
                            "conclusion": "failure",
                            "html_url": "u1",
                        },
                        {"name": "deploy", "status": "in_progress", "conclusion": None},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={"sha": "abc", "statuses": [{"context": "ci/x", "state": "success"}]},
        )

    h = make_harness("github_tools.py", handler, secrets=TOKEN)
    out = json.loads(await h.call("github_get_ci_status", repo="o/r", ref="main"))
    assert out["overall"] == "failing" and out["passed"] == 2
    assert (
        out["failing"][0]["name"] == "tests" and out["pending"][0]["name"] == "deploy"
    )
    with pytest.raises(ToolRemediationError):
        await h.call("github_get_ci_status", repo="o/r", ref="a..b")


async def test_write_tools_post_payloads(make_harness):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if req.url.path.endswith("/comments"):
            assert body == {"body": "thanks"}
            return httpx.Response(201, json={"id": 5, "html_url": "c"})
        assert body["labels"] == ["bug"]
        return httpx.Response(
            201, json={"number": 42, "html_url": "i", "state": "open"}
        )

    h = make_harness("github_tools.py", handler, secrets=TOKEN)
    assert (
        json.loads(
            await h.call(
                "github_create_issue",
                repo="o/r",
                title="Broken",
                body="x",
                labels=["bug"],
            )
        )["number"]
        == 42
    )
    assert (
        json.loads(
            await h.call("github_add_comment", repo="o/r", number=3, body="thanks")
        )["comment_id"]
        == 5
    )


@pytest.mark.parametrize(
    "status,headers,match",
    [
        (401, {}, "rejected GITHUB_TOKEN"),
        (404, {}, "token lacks access"),
        (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "99"}, "rate limit"),
        (422, {}, "422"),
    ],
)
async def test_http_errors_become_remediation(make_harness, status, headers, match):
    h = make_harness(
        "github_tools.py",
        lambda r: httpx.Response(status, json={"message": "nope"}, headers=headers),
        secrets=TOKEN,
    )
    with pytest.raises(ToolRemediationError, match=match):
        await h.call("github_list_issues", repo="o/r")


async def test_missing_or_undeclared_secret(make_harness):
    h = make_harness(
        "github_tools.py", lambda r: httpx.Response(200, json=[]), secrets={}
    )
    with pytest.raises(SecurityViolationError):
        await h.call("github_list_issues", repo="o/r")
