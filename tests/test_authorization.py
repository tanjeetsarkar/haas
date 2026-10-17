from __future__ import annotations

from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.main as main_module
from app.core.authorization import (
    Action,
    enforce_approval_separation_of_duties,
    is_authorized,
    require_action,
)
from app.core.identity import AuthenticatedIdentity

IDENTITY = AuthenticatedIdentity(
    tenant_id="acme",
    principal_id="principal-1",
    issuer="https://identity.example.test/",
    subject="user-1",
    roles=frozenset({"tenant_admin"}),
    credential_type="oidc",
)


def _request(identity: AuthenticatedIdentity | None) -> Request:
    async def receive():
        return {"type": "http.request", "body": b""}

    state = {"identity": identity} if identity is not None else {}
    return Request({"type": "http", "headers": [], "state": state}, receive)


@pytest.mark.parametrize(
    ("role", "action", "allowed"),
    [
        ("tenant_admin", Action.RUN_SUBMIT, True),
        ("tenant_admin", Action.RUN_APPROVE, False),
        ("tenant_admin", Action.AUDIT_READ, True),
        ("submitter", Action.RUN_CANCEL, True),
        ("submitter", Action.RUN_APPROVE, False),
        ("approver", Action.RUN_APPROVAL_READ, True),
        ("approver", Action.RUN_APPROVE, True),
        ("approver", Action.RUN_STREAM, False),
        ("auditor", Action.AUDIT_READ, True),
        ("auditor", Action.RUN_READ, False),
        ("operator", Action.PLATFORM_OPERATE, True),
        ("operator", Action.RUN_READ, False),
        ("break_glass", Action.RUN_READ, False),
        ("unknown_role", Action.RUN_SUBMIT, False),
    ],
)
def test_role_action_policy(role: str, action: Action, allowed: bool):
    assert is_authorized({role}, action) is allowed


@pytest.mark.parametrize("action", list(Action))
def test_break_glass_role_has_no_actions_without_a_separate_grant(action: Action):
    assert not is_authorized({"break_glass"}, action)


def test_action_dependency_requires_identity_except_in_explicit_dev_mode():
    require_action(Action.RUN_SUBMIT, allow_legacy_dev=True)(_request(None))

    with pytest.raises(HTTPException) as missing:
        require_action(Action.RUN_SUBMIT)(_request(None))
    assert missing.value.status_code == 401

    approver = replace(IDENTITY, roles=frozenset({"approver"}))
    with pytest.raises(HTTPException) as denied:
        require_action(Action.RUN_SUBMIT)(_request(approver))
    assert denied.value.status_code == 403


def test_approval_separation_of_duties_fails_closed_for_missing_or_same_submitter():
    approver = replace(IDENTITY, roles=frozenset({"approver"}))
    with pytest.raises(HTTPException) as missing_submitter:
        enforce_approval_separation_of_duties(approver, None)
    assert missing_submitter.value.status_code == 403

    with pytest.raises(HTTPException) as self_approval:
        enforce_approval_separation_of_duties(approver, approver.principal_id)
    assert self_approval.value.status_code == 403

    enforce_approval_separation_of_duties(approver, "another-principal")


async def _request_app(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    *,
    roles: set[str],
    json_body=None,
    tenant_header: str = "acme",
):
    identity = replace(IDENTITY, roles=frozenset(roles))

    async def authenticate(_credential: str, _tenant: str) -> AuthenticatedIdentity:
        return identity

    monkeypatch.setattr(
        main_module.identity_authenticator, "authenticate", authenticate
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main_module.app),
        base_url="http://test",
    ) as client:
        return await client.request(
            method,
            path,
            headers={
                "Authorization": "Bearer test-credential",
                "X-Tenant-ID": tenant_header,
            },
            json=json_body,
        )


@pytest.mark.parametrize("role", ["auditor", "operator", "break_glass"])
async def test_non_run_roles_cannot_list_runs(monkeypatch, role: str):
    async def unexpected_list_runs(*_args, **_kwargs):
        raise AssertionError("denied request reached run listing")

    monkeypatch.setattr(main_module.store, "list_runs", unexpected_list_runs)
    response = await _request_app(monkeypatch, "GET", "/v1/runs", roles={role})

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("method", "path", "role", "json_body"),
    [
        ("GET", "/v1/runs/run_foreign", "submitter", None),
        ("POST", "/v1/runs/run_foreign/cancel", "submitter", None),
        ("POST", "/v1/runs/run_foreign/resume", "approver", {"approved": True}),
        ("GET", "/v1/runs/run_foreign/stream", "submitter", None),
    ],
)
async def test_foreign_run_ids_disclose_no_data_or_effects(
    monkeypatch, method: str, path: str, role: str, json_body: dict | None
):
    async def foreign_run(_run_id: str):
        return {
            "run_id": "run_foreign",
            "tenant_id": "other-tenant",
            "submitted_by_principal_id": "submitter-2",
            "status": "input_required",
            "pending_tool_calls": [],
        }

    async def unexpected_effect(*_args, **_kwargs):
        raise AssertionError("foreign run reached a mutation")

    monkeypatch.setattr(main_module.store, "get_run", foreign_run)
    monkeypatch.setattr(main_module.store, "cancel_run", unexpected_effect)
    monkeypatch.setattr(main_module.store, "claim_resume", unexpected_effect)
    response = await _request_app(
        monkeypatch,
        method,
        path,
        roles={role},
        json_body=json_body,
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Run not found."


async def test_authenticated_identity_overrides_untrusted_tenant_header(monkeypatch):
    async def owned_run(run_id: str):
        return {
            "run_id": run_id,
            "tenant_id": "acme",
            "status": "completed",
            "thread_id": "thread-1",
            "step_count": 1,
            "total_cost_usd": 0.01,
            "active_skills": [],
            "pending_tool_calls": None,
            "result_text": "tenant-owned result",
        }

    monkeypatch.setattr(main_module.store, "get_run", owned_run)
    response = await _request_app(
        monkeypatch,
        "GET",
        "/v1/runs/run_123",
        roles={"submitter"},
        tenant_header="other-tenant",
    )

    assert response.status_code == 200
    assert response.json()["result"] == "tenant-owned result"


async def test_approver_can_read_only_a_pending_approval_summary(monkeypatch):
    pending = [{"id": "call-1", "function": {"name": "tool", "arguments": "{}"}}]

    async def get_run(run_id: str):
        return {
            "run_id": run_id,
            "tenant_id": "acme",
            "status": "input_required",
            "pending_tool_calls": pending,
            "step_count": 3,
            "total_cost_usd": 0.1,
            "active_skills": ["skill"],
            "result_text": "private result",
        }

    monkeypatch.setattr(main_module.store, "get_run", get_run)
    response = await _request_app(
        monkeypatch, "GET", "/v1/runs/run_123", roles={"approver"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "run_id": "run_123",
        "status": "input_required",
        "pending_tool_calls": pending,
    }


async def test_approver_cannot_stream_run_events(monkeypatch):
    response = await _request_app(
        monkeypatch, "GET", "/v1/runs/run_123/stream", roles={"approver"}
    )

    assert response.status_code == 403


async def test_approver_cannot_decide_own_submission(monkeypatch):
    async def get_run(run_id: str):
        return {
            "run_id": run_id,
            "tenant_id": "acme",
            "submitted_by_principal_id": "principal-1",
            "status": "input_required",
            "pending_tool_calls": [],
        }

    async def unexpected_claim(*_args, **_kwargs):
        raise AssertionError("self-approval reached resume claim")

    monkeypatch.setattr(main_module.store, "get_run", get_run)
    monkeypatch.setattr(main_module.store, "claim_resume", unexpected_claim)
    response = await _request_app(
        monkeypatch,
        "POST",
        "/v1/runs/run_123/resume",
        roles={"approver"},
        json_body={"approved": True},
    )

    assert response.status_code == 403


async def test_approval_decision_passes_verified_actor_and_request_id_to_store(
    monkeypatch,
):
    class ResumeStore:
        def __init__(self) -> None:
            self.claim: dict[str, Any] | None = None

        async def get_run(self, run_id: str):
            return {
                "run_id": run_id,
                "tenant_id": "acme",
                "submitted_by_principal_id": "submitter-2",
                "status": "input_required",
                "pending_tool_calls": [],
                "step_count": 2,
                "active_skills": [],
                "messages": [],
                "fingerprints": [],
                "skill_allowlist": [],
                "thread_id": "thread-1",
                "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
            }

        async def claim_resume(self, run_id: str, tenant_id: str, **values: Any):
            self.claim = {
                "run_id": run_id,
                "tenant_id": tenant_id,
                **values,
            }
            return True

        async def save_checkpoint(self, *_args: Any, **_kwargs: Any):
            pass

        async def publish_event(self, *_args: Any, **_kwargs: Any):
            pass

    class Task:
        def apply_async(self, **_kwargs: Any):
            pass

    store = ResumeStore()
    monkeypatch.setattr(main_module, "store", store)
    monkeypatch.setattr(main_module, "llm_step_task", Task())
    response = await _request_app(
        monkeypatch,
        "POST",
        "/v1/runs/run_123/resume",
        roles={"approver"},
        json_body={"approved": False, "rejection_reason": "not authorized"},
    )

    assert response.status_code == 202
    assert response.headers["x-request-id"]
    assert store.claim == {
        "run_id": "run_123",
        "tenant_id": "acme",
        "actor_principal_id": "principal-1",
        "request_id": response.headers["x-request-id"],
        "approved": False,
        "pending_tool_count": 0,
    }
