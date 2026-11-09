"""C1 is fail-closed staging, not authorization to execute a restricted action."""

import json
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.core import credentials
from app.core.envelope import EnvelopeError, arguments_digest, sign_envelope
from app.core.run_restrictions import (
    RestrictionError, RunAuthority, check_dispatch_authority,
)
from app.core.tenant_policy import TenantPolicyRules, policy_digest
from app.core.workflow import Effects, Lease
from app.worker import tasks
from test_phase2_database import DSN, admit, db as db, service_store
from test_run_authority_storage import bound


@pytest.mark.parametrize("tools,name,policy,unlocked,code", [
    ((), "control", {"control"}, {"control"}, "tool_denied"),
    (("a",), "b", {"b"}, {"b"}, "tool_denied"),
    (("a",), "a", set(), {"a"}, "policy_denied"),
    (("a",), "a", {"a"}, set(), "policy_denied"),
    (None, "a", {"a"}, {"a"}, "unavailable"),
    (("a",), "a", {"a"}, {"a"}, "unavailable"),
])
def test_conjunctive_ceiling_and_storage_hold(tools, name, policy, unlocked, code):
    with pytest.raises(RestrictionError, match="restriction." + code):
        check_dispatch_authority(
            RunAuthority(tools=tools), name, policy_tools=policy, unlocked_tools=unlocked,
        )
    check_dispatch_authority(None, name, policy_tools=(), unlocked_tools=())


def fixture():
    authority = RunAuthority(tools=("a",))
    run = {
        "run_id": "r", "tenant_id": "alias", "tenant_uuid": "tenant",
        "tenant_slug": "slug", "authority_json": authority.model_dump_json(),
        "active_skills": [], "skill_allowlist": None, "policy_revision_id": "policy",
    }
    body = {
        "run_id": "r", "tenant_id": "tenant", "invocation_id": "i",
        "tool_call": {"id": "c", "name": "a", "arguments": "{}"},
        "arguments_digest": arguments_digest("a", "{}"),
        "expires_at": 4_000_000_000, "authority": authority.model_dump(mode="json"),
    }
    rules = TenantPolicyRules()
    store = Mock()
    store.get_run = AsyncMock(return_value=run)
    store.get_active_tenant_policy = AsyncMock(return_value={
        "policy_revision_id": "policy", "version": 1,
        "rules": rules.model_dump(mode="json"), "digest": policy_digest(rules),
    })
    for name in ("resolve_secret_bindings", "get_scoped_secrets",
                 "record_credential_grants", "egress_policy_for_run"):
        setattr(store, name, AsyncMock(side_effect=AssertionError(name)))
    return store, run, body


@pytest.mark.parametrize("case", [
    "restricted", "missing_run", "foreign_message", "foreign_envelope", "wrong_run",
    "omitted", "altered", "malformed", "revoked", "policy_changed", "intent", "signature",
])
async def test_pregrant_denials_have_zero_secret_grant_or_publish_calls(monkeypatch, case):
    store, run, body = fixture()
    if case == "missing_run":
        store.get_run.return_value = None
    elif case == "foreign_envelope":
        body["tenant_id"] = "foreign"
    elif case == "wrong_run":
        body["run_id"] = "foreign"
    elif case == "omitted":
        body.pop("authority")
    elif case == "altered":
        body["authority"]["tools"] = []
    elif case == "malformed":
        run["authority_json"] = '{"version":99,"tools":[]}'
    elif case == "revoked":
        store.get_active_tenant_policy.return_value = None
    elif case == "policy_changed":
        run["policy_revision_id"] = "old-policy"
    elif case == "intent":
        authority = bound("tenant", "person", "policy", "r")
        run["authority_json"] = authority.model_dump_json()
        body["authority"] = authority.model_dump(mode="json")
    envelope = sign_envelope(body)
    if case == "signature":
        envelope["body"]["tool_call"]["arguments"] = '{"tampered":true}'
    sealed = AsyncMock(side_effect=AssertionError("sealed"))
    monkeypatch.setattr(credentials, "seal_grant", sealed)
    execute = Mock()
    monkeypatch.setattr(tasks, "execute_tool_task", execute)
    with pytest.raises((ValueError, EnvelopeError)):
        await tasks.make_outbox_publisher(store)({
            "message_id": "message", "destination": "tool", "run_id": "r",
            "tenant_id": "foreign" if case == "foreign_message" else "alias",
            "payload": {"envelope": envelope, "secret_keys": ["credential"], "queue": "q"},
        })
    sealed.assert_not_awaited()
    execute.apply_async.assert_not_called()
    for name in ("resolve_secret_bindings", "get_scoped_secrets",
                 "record_credential_grants", "egress_policy_for_run"):
        getattr(store, name).assert_not_awaited()


async def test_transactional_guard_checks_before_any_write():
    from app.core.workflow import WorkflowMixin
    workflow, conn = WorkflowMixin(), Mock()
    conn.execute = AsyncMock()
    for authority in (RunAuthority(tools=()), RunAuthority(), bound("t", "s", "p", "r")):
        with pytest.raises(RestrictionError):
            await workflow._dispatch(
                conn, Effects(), Lease("t", "r", "th", 1, 1, "worker"),
                {"authority_json": authority.model_dump_json()}, [{"tool_name": "a"}],
            )
    conn.execute.assert_not_awaited()


def test_even_matching_intent_cannot_bypass_unimplemented_approval_binding():
    authority = bound("t", "s", "p", "r")
    with pytest.raises(RestrictionError, match="restriction.intent_unavailable"):
        check_dispatch_authority(
            authority, "synthetic_tool",
            policy_tools={"synthetic_tool"}, unlocked_tools={"synthetic_tool"},
        )


@pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")
async def test_real_transaction_denies_outbox_even_after_forged_approval_resume(db):
    owner, store, slug, _, _, _, principal, policy = db
    run = uuid4().hex
    await store.get_or_create_thread_messages(run, slug)
    await admit(store, slug, run, run, authority=RunAuthority(tools=()),
                submitted_by_principal_id=str(principal), policy_revision_id=str(policy),
                registry_fingerprint="registry")
    async with service_store(owner) as service:
        async with service.connection() as conn:
            row = await conn.fetchrow("SELECT * FROM runs WHERE run_id=$1 FOR UPDATE", run)
            with pytest.raises(RestrictionError, match="restriction.tool_denied"):
                await service._dispatch(
                    conn, Effects(), Lease(slug, run, run, 0, 0, "forged"),
                    row, [{"tool_name": "a"}],
                )
        service.resolve_secret_bindings = AsyncMock(side_effect=AssertionError("secret lookup"))
        service.record_credential_grants = AsyncMock(side_effect=AssertionError("grant write"))
        body = {
            "run_id": run, "tenant_id": str(row["tenant_uuid"]), "invocation_id": str(uuid4()),
            "tool_call": {"id": "c", "name": "a", "arguments": "{}"},
            "arguments_digest": arguments_digest("a", "{}"), "expires_at": 4_000_000_000,
            "authority": RunAuthority(tools=()).model_dump(mode="json"),
        }
        with pytest.raises(RestrictionError):
            await credentials.prepare_tool_dispatch(service, {
                "run_id": run, "tenant_id": slug,
                "payload": {"envelope": sign_envelope(body), "secret_keys": ["credential"]},
            })
        service.resolve_secret_bindings.assert_not_awaited()
        service.record_credential_grants.assert_not_awaited()
    assert await owner.fetchval(
        "SELECT count(*) FROM outbox_messages WHERE run_id=$1 AND destination='tool'", run,
    ) == 0
    assert await owner.fetchval(
        "SELECT count(*) FROM credential_grants WHERE run_id=$1", run,
    ) == 0
    assert json.loads(await owner.fetchval(
        "SELECT authority_json FROM runs WHERE run_id=$1", run,
    ))["tools"] == []
