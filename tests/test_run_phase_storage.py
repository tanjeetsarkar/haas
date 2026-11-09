"""Phase storage remains held; DB tests require a disposable PostgreSQL."""

import asyncio
import json
from uuid import uuid4

import asyncpg
import pytest
from pydantic import ValidationError

from app.core.run_phases import (
    PhaseConfig,
    PhaseState,
    PhaseTransitionError,
    advance_after_acquisition,
)
from app.core.store import (
    DistributedStore,
    StoreConflictError,
    StoreNotFoundError,
    decode_run_phases,
)
from test_phase2_database import DSN, admit, db as db, service_store

pg = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


def test_decode_reads_only_strict_run_columns():
    assert decode_run_phases({
        "phase_config_json": None,
        "phase_state_json": None,
        "messages_checkpoint_json": '{"phase":"forged"}',
    }) == (None, None)
    config, state = decode_run_phases({
        "phase_config_json": PhaseConfig().model_dump_json(),
        "phase_state_json": PhaseState().model_dump_json(),
    })
    assert config == PhaseConfig()
    assert state == PhaseState()
    for row in (
        {"phase_config_json": "null", "phase_state_json": PhaseState().model_dump_json()},
        {"phase_config_json": "{}", "phase_state_json": PhaseState().model_dump_json()},
        {"phase_config_json": PhaseConfig().model_dump_json(), "phase_state_json": None},
        {"phase_config_json": PhaseConfig().model_dump_json(), "phase_state_json": "{}"},
        {"phase_config_json": json.dumps({**PhaseConfig().model_dump(), "version": True}),
         "phase_state_json": PhaseState().model_dump_json()},
        {"phase_config_json": PhaseConfig().model_dump_json(),
         "phase_state_json": json.dumps({**PhaseState().model_dump(), "version": 1.0})},
    ):
        with pytest.raises((ValueError, ValidationError)):
            decode_run_phases(row)


@pg
async def test_held_storage_cas_restart_cancel_and_no_lease(db):
    owner, store, slug, other_slug, *_ = db
    run = "phase-storage-" + uuid4().hex
    await store.get_or_create_thread_messages(run, slug)
    await admit(store, slug, run, run, phase_config=PhaseConfig())
    try:
        saved = await store.get_run(run)
        assert saved["phase_config"] == PhaseConfig()
        assert saved["phase_state"] == PhaseState()
        assert saved["next_action"] == "none"
        assert await owner.fetchval(
            "SELECT count(*) FROM outbox_messages WHERE run_id=$1", run
        ) == 0
        async with service_store(owner) as service:
            for action in ("none", "step"):
                assert await service.acquire_lease(
                    message_id=uuid4().hex, run_id=run, action=action,
                    expected_state_version=0, owner="forged-wake",
                ) is None
            await owner.execute("UPDATE runs SET updated_at=0 WHERE run_id=$1", run)
            assert (await service.reconcile())["requeued"] == 0

        one = advance_after_acquisition(
            PhaseState(), successful_non_control_tool_outcome=True,
            no_tool_response=True, acquisition_quota_exhausted=False,
            finalizer_capacity_available=True, finalizer_attempt_key="attempt-once",
        )
        competing = PhaseState(**{**one.model_dump(), "finalizer_attempt_key": "other"})

        async def claim(candidate):
            return await store.transition_held_run_phase(
                tenant_id=slug, run_id=run, expected_state_version=0,
                following=candidate,
            )

        outcomes = await asyncio.gather(claim(one), claim(competing), return_exceptions=True)
        assert sum(isinstance(item, PhaseState) for item in outcomes) == 1
        assert sum(isinstance(item, StoreConflictError) for item in outcomes) == 1
        claimed = next(item for item in outcomes if isinstance(item, PhaseState))
        with pytest.raises(StoreNotFoundError):
            await store.transition_held_run_phase(
                tenant_id=other_slug, run_id=run, expected_state_version=1,
                following=claimed,
            )
        await owner.execute(
            "UPDATE runs SET messages_checkpoint_json=$2::jsonb WHERE run_id=$1",
            run, json.dumps([{"phase_state": {"finalizer_attempted": False}}]),
        )
        restarted = DistributedStore()
        async with store.connection() as conn:
            role = await conn.fetchval("SELECT current_user")
        restarted.pg_pool = await asyncpg.create_pool(
            DSN, user=role, password="phase2-test-only", min_size=1, max_size=1
        )
        restarted.redis = store.redis
        try:
            reloaded = await restarted.get_run(run)
            assert reloaded["phase_state"] == claimed
            assert reloaded["phase_config"] == PhaseConfig()
            with pytest.raises(PhaseTransitionError):
                await restarted.transition_held_run_phase(
                    tenant_id=slug, run_id=run, expected_state_version=1,
                    following=PhaseState(state_version=2),
                )
            assert await restarted.request_cancel(run, slug) == "cancelled"
            assert (await restarted.get_run(run))["phase_state"] == claimed
            with pytest.raises(StoreConflictError, match="run_not_held"):
                await restarted.transition_held_run_phase(
                    tenant_id=slug, run_id=run, expected_state_version=1, following=claimed,
                )
        finally:
            await restarted.pg_pool.close()
    finally:
        # Disposable fixture cleanup only; production event immutability is unchanged.
        async with owner.transaction():
            await owner.execute("UPDATE threads SET active_run_id=NULL WHERE thread_id=$1", run)
            await owner.execute("ALTER TABLE run_events DISABLE TRIGGER run_events_immutable")
            await owner.execute("DELETE FROM run_events WHERE run_id=$1", run)
            await owner.execute("ALTER TABLE run_events ENABLE TRIGGER run_events_immutable")
            await owner.execute("DELETE FROM consumer_inbox WHERE run_id=$1", run)
            await owner.execute("DELETE FROM runs WHERE run_id=$1", run)


@pg
async def test_legacy_and_phase_admission_remain_separate(db):
    owner, store, slug, *_ = db
    run = "phase-legacy-" + uuid4().hex
    await store.get_or_create_thread_messages(run, slug)
    await admit(store, slug, run, run)
    saved = await store.get_run(run)
    assert saved["phase_config"] is None and saved["phase_state"] is None
    assert saved["next_action"] == "step"
    assert await owner.fetchval(
        "SELECT count(*) FROM outbox_messages WHERE run_id=$1", run
    ) == 1
    with pytest.raises(ValueError, match="phase.runtime_not_enabled"):
        await admit(
            store, slug, run, "not-admitted", phase_config=PhaseConfig(),
            admission={"request_digest": "x", "principal_key": "x",
                       "idempotency_key": "x", "response": {}},
        )
