"""d-api-ergonomics: cursor pagination and filters on the list endpoints."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

import app.main as main_module
from app.main import _decode_cursor, app


def _runs(count: int) -> list[dict[str, Any]]:
    rows = [
        {
            "run_id": f"run-{i:03d}",
            "thread_id": "thd-a" if i % 2 else "thd-b",
            "status": "completed" if i % 3 else "failed",
            "step_count": 1,
            "total_cost_usd": 0.0,
            "created_at": 1_700_000_000.0 + i,
            "updated_at": 1_700_000_000.0 + i,
        }
        for i in range(count)
    ]
    return sorted(rows, key=lambda r: (r["created_at"], r["run_id"]), reverse=True)


@pytest.fixture
def fake_runs(monkeypatch):
    rows = _runs(250)
    calls: list[int] = []

    async def list_runs(tenant_id, *, limit, before=None):
        calls.append(limit)
        selected = [
            r for r in rows if before is None or (r["created_at"], r["run_id"]) < before
        ]
        return [dict(r) for r in selected[:limit]]

    monkeypatch.setattr(main_module.store, "list_runs", list_runs)
    return rows, calls


def _get(path: str, **params: Any) -> httpx.Response:
    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get(path, params=params, headers={"X-Tenant-ID": "t"})

    return asyncio.run(request())


def _walk(path: str, **params: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = None
    for _ in range(100):
        body = _get(path, **params, **({"cursor": cursor} if cursor else {})).json()
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return items
    raise AssertionError("pagination did not terminate")


def test_runs_default_limit_and_bounds(fake_runs):
    rows, calls = fake_runs
    body = _get("/v1/runs").json()
    assert len(body["items"]) == 50
    assert body["next_cursor"]
    assert calls == [51]  # unfiltered pages stay a single limit+1 query
    assert len(_get("/v1/runs", limit=200).json()["items"]) == 200
    assert _get("/v1/runs", limit=201).status_code == 422
    assert _get("/v1/runs", limit=0).status_code == 422
    assert _get("/v1/runs", cursor="garbage").status_code == 422


def test_runs_cursor_walk_is_complete_and_ordered(fake_runs):
    rows, _ = fake_runs
    walked = _walk("/v1/runs", limit=37)
    assert [r["run_id"] for r in walked] == [r["run_id"] for r in rows]


def test_runs_filters_combine_with_cursor(fake_runs):
    rows, _ = fake_runs
    expected = [
        r["run_id"]
        for r in rows
        if r["status"] == "failed" and r["thread_id"] == "thd-a"
    ]
    walked = _walk("/v1/runs", limit=7, status="failed", thread_id="thd-a")
    assert [r["run_id"] for r in walked] == expected
    multi = _walk("/v1/runs", limit=50, status="failed,completed")
    assert len(multi) == len(rows)
    assert _walk("/v1/runs", status="nope") == []


def test_runs_created_window(fake_runs):
    rows, _ = fake_runs
    after = datetime.fromtimestamp(1_700_000_000 + 99, tz=timezone.utc)
    walked = _walk(
        "/v1/runs",
        limit=10,
        created_after=after.isoformat(),
        created_before=str(1_700_000_000 + 150),
    )
    assert [r["run_id"] for r in walked] == [
        f"run-{i:03d}" for i in range(149, 99, -1)
    ]


def test_runs_filter_scan_budget_returns_resumable_cursor(fake_runs, monkeypatch):
    monkeypatch.setattr(main_module, "_LIST_SCAN_BUDGET", 100)
    rows, _ = fake_runs
    first = _get("/v1/runs", limit=5, status="none-match").json()
    assert first["items"] == []
    assert first["next_cursor"] is not None
    assert _decode_cursor(first["next_cursor"])[1] == rows[99]["run_id"]


def test_agent_revision_filters(monkeypatch):
    revisions = [
        {
            "revision_id": f"00000000-0000-4000-8000-{i:012d}",
            "agent_name": "triage" if i % 2 else "billing",
            "version": i,
            "state": "published" if i % 4 == 1 else "draft",
            "digest": "x",
            "created_at": 1_700_000_000.0 + i,
        }
        for i in range(30)
    ][::-1]

    async def list_agent_revisions(tenant_id, *, limit, before=None):
        selected = [
            r
            for r in revisions
            if before is None or (r["created_at"], r["revision_id"]) < before
        ]
        return selected[:limit]

    monkeypatch.setattr(main_module.store, "list_agent_revisions", list_agent_revisions)
    walked = _walk(
        "/v1/agents/revisions", limit=2, agent_name="triage", state="published"
    )
    assert [r["version"] for r in walked] == [29, 25, 21, 17, 13, 9, 5, 1]
    assert len(_walk("/v1/agents/revisions", limit=7)) == 30


def test_approvals_cursor_and_run_filter(monkeypatch):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    batches = [
        {
            "batch_id": f"00000000-0000-4000-8000-{i:012d}",
            "run_id": f"run-{i % 3}",
            "status": "pending",
            # Pairs share a timestamp to exercise tie handling at window edges.
            "created_at": datetime.fromtimestamp(base + i // 2, tz=timezone.utc).isoformat(),
        }
        for i in range(41)
    ]
    windows: list[int] = []

    async def list_approval_batches(tenant_id, *, status, limit):
        windows.append(limit)
        ordered = sorted(batches, key=lambda b: b["created_at"], reverse=True)
        return [dict(b) for b in ordered[:limit]]

    monkeypatch.setattr(
        main_module.store, "list_approval_batches", list_approval_batches
    )
    first = _get("/v1/approvals").json()
    assert len(first["items"]) == 41 and first["next_cursor"] is None
    walked = _walk("/v1/approvals", limit=4)
    assert sorted(b["batch_id"] for b in walked) == sorted(b["batch_id"] for b in batches)
    assert len({b["batch_id"] for b in walked}) == 41
    filtered = _walk("/v1/approvals", limit=3, run_id="run-1")
    assert {b["run_id"] for b in filtered} == {"run-1"}
    assert len(filtered) == len([b for b in batches if b["run_id"] == "run-1"])
    assert _get("/v1/approvals", cursor="garbage").status_code == 422
