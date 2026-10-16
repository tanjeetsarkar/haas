"""GitHub tools (REST API v3). Loaded by `PluginRegistry.discover_all` via `register(registry)`.

Read-only tools run on the fast queue; `github_create_issue` and `github_add_comment` write to
GitHub and therefore require human approval. The token comes from the run secret `GITHUB_TOKEN`
(declared in the `github-ops` skill's `allowed_secrets`).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.context import RunContext, ToolRemediationError

API = "https://api.github.com"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BODY_CHARS = 3000
COMMENT_CHARS = 800


def _clip(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _repo(repo: str) -> str:
    repo = repo.strip().removeprefix("https://github.com/").strip("/")
    if not REPO_RE.match(repo):
        raise ToolRemediationError(
            f"Invalid repo '{repo}'. Use the form 'owner/name', e.g. 'octocat/hello-world'."
        )
    return repo


def _user(obj: dict[str, Any] | None) -> str | None:
    return (obj or {}).get("login")


async def _request(
    ctx: RunContext,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    assert ctx.http_client is not None
    resp = await ctx.http_client.request(
        method,
        f"{API}{path}",
        params=params,
        json=body,
        headers={
            "Authorization": f"Bearer {ctx.get_secret('GITHUB_TOKEN')}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "haas-github-ops",
        },
        timeout=20.0,
        follow_redirects=False,
    )
    if resp.status_code < 400:
        return resp.json()
    try:
        message = resp.json().get("message", "")
    except ValueError:
        message = resp.text[:200]
    if resp.status_code == 401:
        raise ToolRemediationError(
            "GitHub rejected GITHUB_TOKEN (401). Ask the operator for a valid token."
        )
    if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
        raise ToolRemediationError(
            f"GitHub rate limit exhausted; resets at epoch {resp.headers.get('x-ratelimit-reset')}. "
            "Do not retry now; report this to the user."
        )
    if resp.status_code in (403, 404):
        raise ToolRemediationError(
            f"GitHub returned {resp.status_code} for {path}: {message}. The resource does not exist "
            "or the token lacks access; check the repo name and the token's scopes."
        )
    if resp.status_code == 422:
        raise ToolRemediationError(
            f"GitHub rejected the request (422): {message} {resp.text[:300]}"
        )
    resp.raise_for_status()
    return None


class RepoArgs(BaseModel):
    repo: str = Field(
        ..., description="Repository as 'owner/name', e.g. 'octocat/hello-world'."
    )


class ListIssuesArgs(RepoArgs):
    state: Literal["open", "closed", "all"] = Field(
        "open", description="Issue state filter."
    )
    labels: str | None = Field(
        None, description="Comma-separated label names, e.g. 'bug,p1'."
    )
    limit: int = Field(
        20, ge=1, le=50, description="Maximum number of issues to return."
    )


class ListPullsArgs(RepoArgs):
    state: Literal["open", "closed", "all"] = Field(
        "open", description="Pull request state filter."
    )
    limit: int = Field(
        20, ge=1, le=50, description="Maximum number of pull requests to return."
    )


class GetIssueArgs(RepoArgs):
    number: int = Field(..., ge=1, description="Issue number.")
    include_comments: bool = Field(
        False, description="Also return the most recent comments."
    )
    comments_limit: int = Field(
        5, ge=1, le=20, description="How many recent comments to include."
    )


class GetPullArgs(RepoArgs):
    number: int = Field(..., ge=1, description="Pull request number.")


class CiStatusArgs(RepoArgs):
    ref: str = Field(
        ...,
        min_length=1,
        description="Branch name, tag or commit SHA, e.g. 'main' or 'a1b2c3d'.",
    )


class CreateIssueArgs(RepoArgs):
    title: str = Field(..., min_length=3, max_length=256, description="Issue title.")
    body: str = Field(
        ..., min_length=1, max_length=20000, description="Issue body in Markdown."
    )
    labels: list[str] = Field(
        default_factory=list, max_length=10, description="Existing labels to apply."
    )


class AddCommentArgs(RepoArgs):
    number: int = Field(..., ge=1, description="Issue or pull request number.")
    body: str = Field(
        ..., min_length=1, max_length=20000, description="Comment body in Markdown."
    )


def _issue_row(i: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": i["number"],
        "title": i["title"],
        "state": i["state"],
        "author": _user(i.get("user")),
        "labels": [lb["name"] for lb in i.get("labels", [])],
        "comments": i.get("comments", 0),
        "updated": i.get("updated_at"),
        "url": i["html_url"],
    }


def register(registry) -> None:
    @registry.tool(
        name="github_list_issues",
        description="Lists issues (not pull requests) of a GitHub repository with state/label filters. Read-only.",
        args_schema=ListIssuesArgs,
    )
    async def github_list_issues(ctx: RunContext, args: ListIssuesArgs) -> str:
        params: dict[str, Any] = {
            "state": args.state,
            "per_page": min(
                100, args.limit * 2
            ),  # the endpoint also returns PRs; over-fetch then filter
            "sort": "updated",
        }
        if args.labels:
            params["labels"] = args.labels
        data = await _request(
            ctx, "GET", f"/repos/{_repo(args.repo)}/issues", params=params
        )
        rows = [_issue_row(i) for i in data if "pull_request" not in i][: args.limit]
        return json.dumps({"repo": args.repo, "count": len(rows), "issues": rows})

    @registry.tool(
        name="github_get_issue",
        description="Returns one issue's title, state, labels, assignees and body, optionally with the latest comments. Read-only.",
        args_schema=GetIssueArgs,
    )
    async def github_get_issue(ctx: RunContext, args: GetIssueArgs) -> str:
        repo = _repo(args.repo)
        issue = await _request(ctx, "GET", f"/repos/{repo}/issues/{args.number}")
        out = {
            **_issue_row(issue),
            "is_pull_request": "pull_request" in issue,
            "assignees": [_user(a) for a in issue.get("assignees", [])],
            "milestone": (issue.get("milestone") or {}).get("title"),
            "body": _clip(issue.get("body"), BODY_CHARS),
        }
        if args.include_comments and issue.get("comments"):
            total = issue["comments"]
            page = max(1, -(-total // args.comments_limit))
            comments = await _request(
                ctx,
                "GET",
                f"/repos/{repo}/issues/{args.number}/comments",
                params={"per_page": args.comments_limit, "page": page},
            )
            out["recent_comments"] = [
                {
                    "author": _user(c.get("user")),
                    "created": c["created_at"],
                    "body": _clip(c.get("body"), COMMENT_CHARS),
                }
                for c in comments
            ]
        return json.dumps(out)

    @registry.tool(
        name="github_list_pull_requests",
        description="Lists pull requests of a GitHub repository (number, title, author, draft flag, branches). Read-only.",
        args_schema=ListPullsArgs,
    )
    async def github_list_pull_requests(ctx: RunContext, args: ListPullsArgs) -> str:
        data = await _request(
            ctx,
            "GET",
            f"/repos/{_repo(args.repo)}/pulls",
            params={
                "state": args.state,
                "per_page": args.limit,
                "sort": "updated",
                "direction": "desc",
            },
        )
        rows = [
            {
                "number": p["number"],
                "title": p["title"],
                "state": p["state"],
                "draft": p.get("draft", False),
                "author": _user(p.get("user")),
                "head": p["head"]["ref"],
                "base": p["base"]["ref"],
                "updated": p.get("updated_at"),
                "url": p["html_url"],
            }
            for p in data
        ]
        return json.dumps(
            {"repo": args.repo, "count": len(rows), "pull_requests": rows}
        )

    @registry.tool(
        name="github_get_pull_request",
        description=(
            "Returns a pull request with description, size, mergeability, changed files (first 30) and the latest "
            "review state per reviewer. Read-only; use it before summarising or reviewing a PR."
        ),
        args_schema=GetPullArgs,
    )
    async def github_get_pull_request(ctx: RunContext, args: GetPullArgs) -> str:
        repo = _repo(args.repo)
        base = f"/repos/{repo}/pulls/{args.number}"
        pr, files, reviews = await asyncio.gather(
            _request(ctx, "GET", base),
            _request(ctx, "GET", f"{base}/files", params={"per_page": 30}),
            _request(ctx, "GET", f"{base}/reviews", params={"per_page": 100}),
        )
        latest: dict[str, str] = {}
        for (
            r
        ) in reviews:  # chronological: the last non-comment review per reviewer wins
            if r.get("state") != "COMMENTED" and _user(r.get("user")):
                latest[_user(r["user"])] = r["state"]
        return json.dumps(
            {
                "number": pr["number"],
                "title": pr["title"],
                "state": "merged" if pr.get("merged") else pr["state"],
                "draft": pr.get("draft", False),
                "author": _user(pr.get("user")),
                "head": pr["head"]["ref"],
                "base": pr["base"]["ref"],
                "mergeable_state": pr.get("mergeable_state"),
                "commits": pr.get("commits"),
                "additions": pr.get("additions"),
                "deletions": pr.get("deletions"),
                "changed_files": pr.get("changed_files"),
                "files": [
                    {
                        "path": f["filename"],
                        "status": f["status"],
                        "+": f["additions"],
                        "-": f["deletions"],
                    }
                    for f in files
                ],
                "reviews": latest,
                "body": _clip(pr.get("body"), BODY_CHARS),
                "url": pr["html_url"],
            }
        )

    @registry.tool(
        name="github_get_ci_status",
        description=(
            "Summarises CI for a branch/tag/commit: overall state plus failing and pending check runs/statuses "
            "with links. Read-only."
        ),
        args_schema=CiStatusArgs,
    )
    async def github_get_ci_status(ctx: RunContext, args: CiStatusArgs) -> str:
        repo = _repo(args.repo)
        ref = args.ref.strip()
        if ".." in ref or ref.startswith("/"):
            raise ToolRemediationError("ref must be a branch, tag or commit SHA.")
        checks, status = await asyncio.gather(
            _request(
                ctx,
                "GET",
                f"/repos/{repo}/commits/{ref}/check-runs",
                params={"per_page": 100},
            ),
            _request(ctx, "GET", f"/repos/{repo}/commits/{ref}/status"),
        )
        failing, pending, passed = [], [], 0
        for c in checks.get("check_runs", []):
            if c["status"] != "completed":
                pending.append({"name": c["name"], "status": c["status"]})
            elif c["conclusion"] in ("success", "neutral", "skipped"):
                passed += 1
            else:
                failing.append(
                    {
                        "name": c["name"],
                        "conclusion": c["conclusion"],
                        "url": c.get("html_url"),
                    }
                )
        for s in status.get("statuses", []):
            if s["state"] == "pending":
                pending.append({"name": s["context"], "status": "pending"})
            elif s["state"] == "success":
                passed += 1
            else:
                failing.append(
                    {
                        "name": s["context"],
                        "conclusion": s["state"],
                        "url": s.get("target_url"),
                    }
                )
        overall = (
            "failing"
            if failing
            else "pending"
            if pending
            else "passing"
            if passed
            else "no_checks"
        )
        return json.dumps(
            {
                "repo": repo,
                "ref": ref,
                "sha": status.get("sha"),
                "overall": overall,
                "passed": passed,
                "failing": failing[:20],
                "pending": pending[:20],
            }
        )

    @registry.tool(
        name="github_create_issue",
        description="Creates a new GitHub issue. WRITES to GitHub and requires human approval.",
        args_schema=CreateIssueArgs,
        requires_approval=True,
    )
    async def github_create_issue(ctx: RunContext, args: CreateIssueArgs) -> str:
        data = await _request(
            ctx,
            "POST",
            f"/repos/{_repo(args.repo)}/issues",
            body={"title": args.title, "body": args.body, "labels": args.labels},
        )
        return json.dumps(
            {"number": data["number"], "url": data["html_url"], "state": data["state"]}
        )

    @registry.tool(
        name="github_add_comment",
        description="Posts a comment on an issue or pull request. WRITES to GitHub and requires human approval.",
        args_schema=AddCommentArgs,
        requires_approval=True,
    )
    async def github_add_comment(ctx: RunContext, args: AddCommentArgs) -> str:
        data = await _request(
            ctx,
            "POST",
            f"/repos/{_repo(args.repo)}/issues/{args.number}/comments",
            body={"body": args.body},
        )
        return json.dumps({"comment_id": data["id"], "url": data["html_url"]})
