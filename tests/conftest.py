"""Puts tests/ on sys.path so test modules can import `plugin_helpers`.

Also enforces the published `run-events.v1` contract on every event any test emits:
violations are collected (never raised inside the workflow transaction) and fail the
test at teardown, so the whole PostgreSQL workflow suite doubles as producer
conformance for docs/contracts/run-events.v1.schema.json.
"""

from __future__ import annotations

import pytest

from contract_support import event_violations


@pytest.fixture(autouse=True)
def _run_events_contract(monkeypatch):
    try:
        import jsonschema  # noqa: F401

        from app.core.workflow import WorkflowMixin, _with_error_code
    except ImportError:
        yield
        return
    violations: list[str] = []
    original = WorkflowMixin._append_events

    async def checked(self, conn, fx, tenant_id, run_id, events):
        for kind, payload in events or []:
            stored = _with_error_code(payload) if kind == "run_error" else payload
            violations.extend(event_violations(kind, stored))
        return await original(self, conn, fx, tenant_id, run_id, events)

    monkeypatch.setattr(WorkflowMixin, "_append_events", checked)
    yield
    assert not violations, "run-events.v1 contract violations:\n" + "\n".join(
        violations
    )
