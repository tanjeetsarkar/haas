"""Persisted approved DB intent through real worker, synthetic RPC only."""

import json

import pytest

from app.core import worker_restrictions as checks
from app.core.envelope import verify_envelope
from app.worker import tasks
from test_intent_release import approved, execution_store as execution_store
from test_phase2_database import DSN, db as db
import test_held_native_agent as native

pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


class EffectsRedis:
    def __init__(self):
        self.values = {}

    async def exists(self, key):
        return False

    async def set(self, key, value, **kwargs):
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


@pytest.mark.parametrize("lost_result", [False, True])
async def test_actual_worker_redelivery_has_one_effect_and_unknown_is_terminal(
    db, monkeypatch, execution_store, lost_result,
):
    owner, store, *_ = db
    effects, operations = [], []

    async def handler(ctx, args):
        effects.append(ctx.effect_key)
        return "synthetic external effect"

    # Register BEFORE immutable target pinning, never replace a pinned handler.
    monkeypatch.setattr(native, "native_handler", handler)
    registry, request = await approved(db, monkeypatch)
    await store.release_approved_intent(**request)
    payload = json.loads(await owner.fetchval(
        "SELECT payload FROM outbox_messages WHERE run_id=$1", request["run_id"],
    ))
    body = verify_envelope(payload["envelope"])
    redis = EffectsRedis()
    monkeypatch.setattr(tasks.aioredis, "from_url", lambda *args, **kwargs: redis)
    monkeypatch.setattr(tasks, "registry", registry)
    monkeypatch.setattr(tasks.settings, "sandbox_mode", "inprocess")

    async def rpc(config, binding, operation):
        operations.append(operation)
        assert binding["intent_binding"] == body["intent_binding"]

    monkeypatch.setattr(checks, "check_worker_authorization", rpc)
    first = await tasks._execute_envelope(body, credential_grants=[])
    assert first[0] == "succeeded"
    assert effects == [body["effect_key"]] and operations == ["pre_grant", "final_entry"]
    if lost_result:
        redis.values.pop("haas:effect-result:" + body["effect_key"])
    second = await tasks._execute_envelope(body, credential_grants=[])
    assert second[0] == ("unknown" if lost_result else "succeeded")
    assert effects == [body["effect_key"]] and operations == ["pre_grant", "final_entry"]
    assert await execution_store.record_tool_outcome(
        run_id=request["run_id"], invocation_id=body["invocation_id"],
        status=second[0], outcome=second[1],
    )
    saved = await store.get_run(request["run_id"])
    assert saved["status"] == ("failed" if lost_result else "completed")
    assert saved["next_action"] == "none"
    with pytest.raises(ValueError):
        await store.release_approved_intent(**request)
    assert len(effects) == 1
    assert await owner.fetchval("SELECT count(*) FROM run_steps WHERE run_id=$1", request["run_id"]) == 0
