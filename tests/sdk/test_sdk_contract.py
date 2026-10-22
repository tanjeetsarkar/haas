"""The async SDK against the real FastAPI app (store stubbed) to pin wire contracts."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

import app.main as main_module
from app.main import app
from haas_sdk import AsyncHaas, NotFoundError

ROWS = [
    {
        "run_id": f"run-{i:02d}",
        "thread_id": "thd-a" if i % 2 else "thd-b",
        "status": "completed" if i % 3 else "failed",
        "step_count": 1,
        "total_cost_usd": 0.0,
        "created_at": 1_700_000_000.0 + i,
        "updated_at": 1_700_000_000.0 + i,
    }
    for i in reversed(range(30))
]


@pytest.fixture
def stub_store(monkeypatch: pytest.MonkeyPatch) -> None:
    async def list_runs(
        tenant_id: str, *, limit: int, before: Any = None
    ) -> list[dict[str, Any]]:
        rows = [
            r for r in ROWS if before is None or (r["created_at"], r["run_id"]) < before
        ]
        return [dict(r) for r in rows[:limit]]

    async def get_run(run_id: str) -> dict[str, Any] | None:
        if run_id != "run-01":
            return None
        return {**ROWS[-2], "tenant_id": "t", "event_sequence": 2}

    async def list_run_events(
        run_id: str, *, after: int, limit: int
    ) -> list[dict[str, Any]]:
        items = [
            {"sequence": 1, "type": "run_started", "data": {}, "schema_version": 1},
            {
                "sequence": 2,
                "type": "run_completed",
                "data": {"result": "ok"},
                "schema_version": 1,
            },
        ]
        return [i for i in items if i["sequence"] > after][:limit]

    monkeypatch.setattr(main_module.store, "list_runs", list_runs)
    monkeypatch.setattr(main_module.store, "get_run", get_run)
    monkeypatch.setattr(main_module.store, "list_run_events", list_run_events)


async def test_async_sdk_pages_filters_and_errors_against_app(stub_store: None) -> None:
    # No credential: in dev mode the server accepts header tenancy without auth.
    async with AsyncHaas(
        base_url="http://test",
        tenant="t",
        transport=httpx.ASGITransport(app=app),
    ) as haas:
        page = await haas.runs.list(limit=5, status="failed", thread_id="thd-a")
        assert page.items and all(
            r.status == "failed" and r.thread_id == "thd-a" for r in page.items
        )
        every = [r.run_id async for r in haas.runs.iter(page_size=7)]
        assert every == [r["run_id"] for r in ROWS]
        events = await haas.runs.events("run-01", after=1)
        assert [e.type for e in events.items] == ["run_completed"]
        assert events.items[0].sequence == 2
        with pytest.raises(NotFoundError) as info:
            await haas.runs.get("nope")
        assert info.value.request_id
