"""Audit writer tenant resolution contract (no database required)."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from app.core.store import DistributedStore, StoreConflictError


class FakeConn:
    def __init__(self, resolved: UUID | None) -> None:
        self.resolved = resolved
        self.fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, query: str, *args: Any) -> UUID | None:
        self.fetchval_calls.append((query, args))
        return self.resolved

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        return "INSERT 0 1"


async def _write(conn: FakeConn, tenant: UUID | str | None) -> None:
    await DistributedStore._insert_audit_event(
        conn,
        tenant_id=tenant,
        actor_principal_id=uuid4(),
        action="thread.created",
        target_type="thread",
        target_id="thd_qa",
        request_id=uuid4(),
        details={"b": 1, "a": 2},
    )


@pytest.mark.parametrize("selector", ["local-dev", "{uuid}"])
async def test_string_selector_resolved_with_rls_resolver(selector: str) -> None:
    tenant = uuid4()
    conn = FakeConn(tenant)
    await _write(conn, selector.format(uuid=tenant))
    assert conn.fetchval_calls == [
        ("SELECT resolve_haas_tenant_uuid($1)", (selector.format(uuid=tenant),))
    ]
    ((query, args),) = conn.executed
    assert "INSERT INTO audit_events" in query
    assert args[1] == tenant
    assert args[7] == '{"a":2,"b":1}'


async def test_uuid_tenant_passes_through_without_lookup() -> None:
    tenant = uuid4()
    conn = FakeConn(None)
    await _write(conn, tenant)
    assert conn.fetchval_calls == []
    assert conn.executed[0][1][1] == tenant


@pytest.mark.parametrize("selector", ["unknown-tenant", None])
async def test_unresolvable_tenant_fails_closed_before_insert(
    selector: str | None,
) -> None:
    conn = FakeConn(None)
    with pytest.raises(StoreConflictError, match="Audit tenant"):
        await _write(conn, selector)
    assert conn.executed == []
