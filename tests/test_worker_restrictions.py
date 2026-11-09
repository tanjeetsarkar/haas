"""Actual worker boundaries with a fake plugin/RPC; no production allow path."""

import time
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import BaseModel

from app.core import worker_restrictions as checks
from app.core.envelope import arguments_digest, sign_envelope, verify_envelope
from app.core.plugins import PluginRegistry, RegisteredTool
from app.core.run_restrictions import ExactIntent, IntentContext, RunAuthority, canonical_arguments
from app.core.worker_authorization import WorkerAuthorizationError, check_worker_authorization
from app.worker import tasks


class Args(BaseModel):
    value: int = 1


@pytest.fixture
def worker(monkeypatch):
    events = []
    reg = PluginRegistry()
    reg.fingerprint = "b" * 64
    reg.plugins["fake"] = SimpleNamespace(digest="a" * 64)

    async def handler(args):
        events.append("handler")
        return "ok"

    reg.tools["act"] = RegisteredTool("act", "", Args, handler, False, plugin_id="fake")
    monkeypatch.setattr(reg, "resolve_unlocked_tool_names", lambda *a: {"act"})
    monkeypatch.setattr(reg, "resolve_allowed_secrets", lambda *a: set())
    monkeypatch.setattr(tasks, "registry", reg)
    monkeypatch.setattr(tasks.settings, "sandbox_mode", "inprocess")
    intent = ExactIntent(
        tool_name="act", arguments_json='{"value":1}',
        context=IntentContext(
            tenant_id=str(uuid4()), submitter_id="submitter", run_id="run",
            policy_revision=str(uuid4()), registry_fingerprint=reg.fingerprint,
            plugin_id="fake", plugin_digest="a" * 64, destination=canonical_arguments({
                "version": 1, "kind": "native", "plugin_id": "fake", "plugin_digest": "a" * 64,
                "tool_name": "act", "module": handler.__module__, "qualname": handler.__qualname__,
                "execution": reg.tools["act"].execution, "queue": reg.tools["act"].queue,
                "injects_context": reg.tools["act"].injects_context,
                "schema_sha256": hashlib.sha256(json.dumps(
                    Args.model_json_schema(by_alias=True), sort_keys=True,
                    separators=(",", ":"), ensure_ascii=False, allow_nan=False,
                ).encode()).hexdigest(),
            }),
        ),
    )
    body = {
        "invocation_id": str(uuid4()), "effect_key": "effect", "run_id": "run",
        "tenant_id": intent.context.tenant_id, "thread_id": "thread", "traceparent": "",
        "tool_call": {"id": "call", "name": "act", "arguments": '{"value":1}'},
        "arguments_digest": arguments_digest("act", '{"value":1}'),
        "plugin_digest": "a" * 64, "expires_at": time.time() + 120,
        "allowed_secrets": [], "queue": reg.tools["act"].queue,
        "authority_json": RunAuthority(tools=("act",), intent=intent).model_dump(mode="json"),
    }

    class Redis:
        def __init__(self):
            self.values = {}

        async def exists(self, key):
            return False

        async def set(self, key, value, **kw):
            if key in self.values:
                return False
            self.values[key] = value
            return True

        async def get(self, key):
            return self.values.get(key)

        async def setex(self, key, ttl, value):
            self.values[key] = value

        async def aclose(self):
            pass

    redis = Redis()
    monkeypatch.setattr(tasks.aioredis, "from_url", lambda *a, **k: redis)

    async def rpc(config, binding, operation):
        events.append(operation)
        assert binding["arguments_sha256"]
        assert binding["intent_binding"] == intent.binding

    async def redeem(*a, **kw):
        events.append("redeem")
        return {}

    def no_store():
        pytest.fail("restricted worker must not open PostgreSQL")

    monkeypatch.setattr(checks, "check_worker_authorization", rpc)
    monkeypatch.setattr(tasks, "redeem_grants", redeem)
    monkeypatch.setattr(tasks, "DistributedStore", no_store)
    return body, events, reg


async def execute(body):
    return await tasks._execute_envelope(
        verify_envelope(sign_envelope(body)), credential_grants=["opaque"],
    )


async def test_worker_orders_checks_before_grants_and_handler_and_dedupes(worker):
    body, events, _ = worker
    first = await execute(body)
    assert first[0] == "succeeded"
    assert events == ["pre_grant", "redeem", "final_entry", "handler"]
    assert await execute(body) == first
    assert events == ["pre_grant", "redeem", "final_entry", "handler"]


@pytest.mark.parametrize("stage", ["pre_grant", "final_entry"])
@pytest.mark.parametrize("mode", ["inprocess", "sandbox"])
async def test_worker_rpc_denial_never_enters_handler(worker, monkeypatch, stage, mode):
    body, events, _ = worker
    monkeypatch.setattr(tasks.settings, "sandbox_mode", mode)

    async def deny(config, binding, operation):
        events.append(operation)
        if operation == stage:
            raise WorkerAuthorizationError("restriction.authorization_denied")

    monkeypatch.setattr(checks, "check_worker_authorization", deny)
    status, outcome = await execute(body)
    assert status == "failed" and outcome["content"] == "restriction.authorization_denied"
    assert events == (["pre_grant"] if stage == "pre_grant" else ["pre_grant", "redeem", "final_entry"])


@pytest.mark.parametrize("raw", ['{}', '{"value":"1"}', '{"value":2}', '{"value":1,"extra":2}',
                                      '{"value":1,"value":1}', "{value:1}"])
async def test_worker_rejects_repaired_defaulted_coerced_or_altered_args_before_grants(worker, raw):
    body, events, _ = worker
    body["tool_call"]["arguments"] = raw
    body["arguments_digest"] = arguments_digest("act", raw)
    assert (await execute(body))[0] == "failed"
    assert events == []


@pytest.mark.parametrize("authority", [
    None, {}, {"version": 1, "tools": []}, {"version": 1, "tools": None},
])
async def test_present_incomplete_authority_never_falls_back(worker, authority):
    body, events, _ = worker
    body["authority_json"] = authority
    assert (await execute(body))[0] == "failed"
    assert events == []


@pytest.mark.parametrize("change", ["arguments", "schema", "plugin", "registry", "unlock", "policy"])
async def test_worker_rechecks_binding_after_redemption(worker, monkeypatch, change):
    body, events, reg = worker

    async def redeem(*a, **kw):
        events.append("redeem")
        if change == "arguments":
            body["tool_call"]["arguments"] = '{"value":2}'
        elif change == "schema":
            reg.tools["act"].args_schema = type("Changed", (BaseModel,), {})
        elif change == "plugin":
            reg.plugins["fake"].digest = "c" * 64
        elif change == "registry":
            reg.fingerprint = "c" * 64
        elif change == "unlock":
            monkeypatch.setattr(reg, "resolve_unlocked_tool_names", lambda *a: set())
        else:
            # RPC is responsible for current persisted tenant/policy state.
            async def denied(*a):
                raise WorkerAuthorizationError("restriction.authorization_denied")
            monkeypatch.setattr(checks, "check_worker_authorization", denied)
        return {}

    monkeypatch.setattr(tasks, "redeem_grants", redeem)
    assert (await execute(body))[0] == "failed"
    assert events == ["pre_grant", "redeem"]


async def test_worker_sandbox_entry_checked_and_setup_failure_never_falls_back(worker, monkeypatch):
    body, events, _ = worker
    monkeypatch.setattr(tasks.settings, "sandbox_mode", "sandbox")

    async def sandbox(**kw):
        events.append("sandbox")
        raise tasks.SandboxSetupError("synthetic")

    monkeypatch.setattr(tasks, "execute_in_sandbox", sandbox)
    assert (await execute(body))[0] == "failed"
    assert events == ["pre_grant", "redeem", "final_entry", "sandbox"]


async def test_worker_checks_even_without_grants(worker):
    body, events, _ = worker
    assert (await tasks._execute_envelope(
        verify_envelope(sign_envelope(body)), credential_grants=[],
    ))[0] == "succeeded"
    assert events == ["pre_grant", "final_entry", "handler"]


@pytest.mark.parametrize("stage", ["before", "during_rpc", "after_redemption"])
@pytest.mark.parametrize("change", ["handler", "queue", "injection", "credentials"])
async def test_registered_target_or_credential_change_never_enters_handler(worker, monkeypatch, stage, change):
    body, events, reg = worker

    def mutate():
        if change == "handler":
            reg.tools["act"].handler = lambda args: events.append("wrong_handler")
        elif change == "queue":
            reg.tools["act"].queue = "q_tools_sandbox"
        elif change == "injection":
            reg.tools["act"].injects_context = not reg.tools["act"].injects_context
        else:
            monkeypatch.setattr(reg, "resolve_allowed_secrets", lambda *a: {"EXTRA"})

    if stage == "before":
        mutate()
    elif stage == "during_rpc":
        async def rpc(*args):
            events.append("pre_grant")
            mutate()
        monkeypatch.setattr(checks, "check_worker_authorization", rpc)
    else:
        async def redeem(*args, **kwargs):
            events.append("redeem")
            mutate()
            return {}
        monkeypatch.setattr(tasks, "redeem_grants", redeem)
    assert (await execute(body))[0] == "failed"
    assert events == ([] if stage == "before" else ["pre_grant"]
                      if stage == "during_rpc" else ["pre_grant", "redeem"])


@pytest.mark.parametrize("protocol", ["http", "mcp_http"])
@pytest.mark.parametrize("change", ["none", "url", "protocol", "schema"])
async def test_fixed_endpoint_target_rechecked_before_credentials(worker, monkeypatch, protocol, change):
    body, events, reg = worker
    tool = reg.tools["act"]
    tool.endpoint_url, tool.endpoint_protocol = "https://example.com/action", protocol
    authority = RunAuthority.model_validate_json(json.dumps(body["authority_json"]))
    context = authority.intent.context.model_copy(update={"destination": canonical_arguments({
        "version": 1, "url": tool.endpoint_url, "protocol": protocol, "queue": tool.queue,
        "schema_sha256": json.loads(authority.intent.context.destination)["schema_sha256"],
    })})
    intent = authority.intent.model_copy(update={"context": context})
    body["authority_json"] = authority.model_copy(update={"intent": intent}).model_dump(mode="json")

    async def rpc(config, binding, operation):
        events.append(operation)
    monkeypatch.setattr(checks, "check_worker_authorization", rpc)
    if change == "url":
        tool.endpoint_url = "https://example.com/other"
    elif change == "protocol":
        tool.endpoint_protocol = "mcp_http" if protocol == "http" else "http"
    elif change == "schema":
        tool.args_schema = type("Changed", (BaseModel,), {})
    assert (await execute(body))[0] == ("succeeded" if change == "none" else "failed")
    assert events == (["pre_grant", "redeem", "final_entry", "handler"] if change == "none" else [])


async def test_legacy_envelope_keeps_coercion_and_skips_rpc(worker, monkeypatch):
    body, events, _ = worker
    body.pop("authority_json")
    body["tool_call"]["arguments"] = '{"value":"1"}'
    body["arguments_digest"] = arguments_digest("act", '{"value":"1"}')

    class Store:
        async def init(self, **kw):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(tasks, "DistributedStore", Store)
    assert (await execute(body))[0] == "succeeded"
    assert events == ["redeem", "handler"]


def test_invocation_builder_keeps_persisted_authority_inside_signature(worker):
    body, _, _ = worker
    lease = SimpleNamespace(tenant_id=body["tenant_id"], run_id="run", thread_id="thread", epoch=1)
    run = {"active_skills": [], "traceparent": "", "authority_json": body["authority_json"]}
    invocation = tasks.build_invocation(
        lease, run, step=1, ordinal=0, tool_call_id="call", tool_name="act", arguments='{"value":1}',
    )
    assert verify_envelope(invocation["envelope"])["authority_json"] == body["authority_json"]
    run.pop("authority_json")
    legacy = tasks.build_invocation(
        lease, run, step=1, ordinal=0, tool_call_id="call", tool_name="act", arguments='{"value":1}',
    )
    assert "authority_json" not in verify_envelope(legacy["envelope"])


async def test_worker_grant_error_cannot_enter_final_check_or_handler(worker, monkeypatch):
    body, events, _ = worker

    async def broken(*a, **kw):
        events.append("redeem")
        raise tasks.GrantError("synthetic", "synthetic")

    monkeypatch.setattr(tasks, "redeem_grants", broken)
    assert (await execute(body))[0] == "failed"
    assert events == ["pre_grant", "redeem"]


@pytest.mark.parametrize("mode", ["inprocess", "sandbox"])
async def test_real_client_missing_configuration_denies_before_any_grant(worker, monkeypatch, mode):
    body, events, _ = worker
    monkeypatch.setattr(tasks.settings, "sandbox_mode", mode)
    monkeypatch.setattr(tasks.settings, "internal_authorization_url", None)
    # Restore the actual client, not the fixture's allowing RPC.
    monkeypatch.setattr(checks, "check_worker_authorization", check_worker_authorization)
    assert (await execute(body))[0] == "failed"
    assert events == []
