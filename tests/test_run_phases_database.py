"""Use only HAAS_TEST_POSTGRES_DSN pointing at a disposable migrated database."""

import asyncio
import json
import os
import signal
import subprocess
import sys

import asyncpg
import pytest

from test_phase2_database import DSN, admit, db as db

pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


def jsonb(value: dict[str, object]) -> str:
    return "'" + json.dumps(value, separators=(",", ":")) + "'::jsonb"


CONFIG = jsonb({
    "version": 1,
    "mode": "bounded_acquisition_finalization",
    "reserve_basis_points": 2000,
})
INITIAL = jsonb({
    "version": 1, "state_version": 0, "phase": "acquisition",
    "successful_tool_outcomes": 0, "finalizer_attempted": False,
    "finalizer_attempt_key": None, "failure_code": None,
})
CLAIMED = jsonb({
    "version": 1, "state_version": 1, "phase": "finalization",
    "successful_tool_outcomes": 1, "finalizer_attempted": True,
    "finalizer_attempt_key": "attempt-1", "failure_code": None,
})


async def insert_phase_run(owner, source_run: str, run_id: str, config: str, state: str):
    columns = [
        row["column_name"] for row in await owner.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='runs' ORDER BY ordinal_position"
        )
    ]
    overrides = {
        "run_id": f"'{run_id}'",
        "run_key": "gen_random_uuid()",
        "status": "'cancelled'",
        "phase_config_json": config,
        "phase_state_json": state,
    }
    await owner.execute(
        f"INSERT INTO runs ({','.join(columns)}) SELECT "
        + ",".join(overrides.get(column, column) for column in columns)
        + " FROM runs WHERE run_id=$1",
        source_run,
    )


async def assert_phase_downgrade_refused(owner):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "20261109_01"],
        env={**os.environ, "POSTGRES_DSN": DSN},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Cannot remove persisted run phase data" in result.stderr
    assert await owner.fetchval(
        "SELECT version_num FROM alembic_version"
    ) == "20261109_02"


async def test_valid_insert_and_reject_malformed_or_unsupported_insert(db):
    owner, store, slug, _, *_ = db
    source = "phase-source-" + slug
    configured = "phase-valid-" + slug
    await store.get_or_create_thread_messages(source, slug)
    await admit(store, slug, source, source)
    assert await owner.fetchval(
        "SELECT phase_config_json IS NULL AND phase_state_json IS NULL "
        "FROM runs WHERE run_id=$1", source
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await owner.execute(
            f"UPDATE runs SET phase_config_json={CONFIG} WHERE run_id=$1",
            source,
        )

    await insert_phase_run(owner, source, configured, CONFIG, INITIAL)
    with pytest.raises(asyncpg.CheckViolationError):
        await owner.execute(
            "UPDATE runs SET phase_config_json=NULL WHERE run_id=$1", configured
        )
    for bad_config in (
        jsonb({}),
        jsonb({"version": 2}),
        jsonb({
            "version": 1.0,
            "mode": "bounded_acquisition_finalization",
            "reserve_basis_points": 2000,
        }),
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            await insert_phase_run(owner, source, "bad-config-" + slug, bad_config, INITIAL)
    bad_states = (
        jsonb({
            "version": 2, "state_version": 0, "phase": "acquisition",
            "successful_tool_outcomes": 0, "finalizer_attempted": False,
            "finalizer_attempt_key": None, "failure_code": None,
        }),
        jsonb({
            "version": 1, "state_version": 0, "phase": "finalization",
            "successful_tool_outcomes": 0, "finalizer_attempted": True,
            "finalizer_attempt_key": "attempt", "failure_code": None,
        }),
        jsonb({
            "version": 1, "state_version": 0, "phase": "acquisition",
            "successful_tool_outcomes": 0, "finalizer_attempted": True,
            "finalizer_attempt_key": "attempt", "failure_code": None,
        }),
    )
    for bad_state in bad_states:
        with pytest.raises(asyncpg.CheckViolationError):
            await insert_phase_run(
                owner, source, "bad-state-" + slug, CONFIG, bad_state
            )
    assert await owner.fetchval(
        "SELECT phase_config_json IS NOT NULL AND phase_state_json IS NOT NULL "
        "FROM runs WHERE run_id=$1", configured
    )
    await owner.execute("DELETE FROM runs WHERE run_id=$1", configured)


async def test_phase_updates_are_monotonic_and_attempt_is_one_shot(db):
    owner, store, slug, _, *_ = db
    source = "phase-transition-source-" + slug
    configured = "phase-transition-" + slug
    await store.get_or_create_thread_messages(source, slug)
    await admit(store, slug, source, source)
    await insert_phase_run(owner, source, configured, CONFIG, INITIAL)
    await owner.execute(
        f"UPDATE runs SET phase_state_json={CLAIMED} WHERE run_id=$1", configured
    )
    invalid_updates = (
        jsonb({
            "version": 1, "state_version": 2, "phase": "acquisition",
            "successful_tool_outcomes": 1, "finalizer_attempted": False,
            "finalizer_attempt_key": None, "failure_code": None,
        }),
        jsonb({
            "version": 1, "state_version": 2, "phase": "finalization",
            "successful_tool_outcomes": 1, "finalizer_attempted": True,
            "finalizer_attempt_key": "replacement", "failure_code": None,
        }),
        jsonb({
            "version": 1, "state_version": 0, "phase": "finalization",
            "successful_tool_outcomes": 1, "finalizer_attempted": True,
            "finalizer_attempt_key": "attempt-1", "failure_code": None,
        }),
    )
    for state in invalid_updates:
        with pytest.raises(asyncpg.CheckViolationError):
            await owner.execute(
                f"UPDATE runs SET phase_state_json={state} WHERE run_id=$1",
                configured,
            )
    completed = jsonb({
        "version": 1, "state_version": 2, "phase": "completed",
        "successful_tool_outcomes": 1, "finalizer_attempted": True,
        "finalizer_attempt_key": "attempt-1", "failure_code": None,
    })
    await owner.execute(
        f"UPDATE runs SET phase_state_json={completed} WHERE run_id=$1", configured
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await owner.execute(
            f"UPDATE runs SET phase_state_json={CLAIMED} WHERE run_id=$1", configured
        )
    async with store.connection() as conn:
        assert await conn.fetchval(
            "SELECT relrowsecurity AND relforcerowsecurity "
            "FROM pg_class WHERE oid='runs'::regclass"
        )
    await assert_phase_downgrade_refused(owner)
    await owner.execute("DELETE FROM runs WHERE run_id=$1", configured)


async def test_downgrade_refuses_config_only_data(db):
    owner, store, slug, _, *_ = db
    source = "phase-config-only-source-" + slug
    configured = "phase-config-only-" + slug
    await store.get_or_create_thread_messages(source, slug)
    await admit(store, slug, source, source)
    await insert_phase_run(owner, source, configured, CONFIG, INITIAL)
    await owner.execute(
        "ALTER TABLE runs DROP CONSTRAINT runs_phase_config_state_pair_ck"
    )
    await owner.execute(
        "ALTER TABLE runs DISABLE TRIGGER runs_immutable_phase_config_and_monotonic_state"
    )
    try:
        await owner.execute(
            "UPDATE runs SET phase_state_json=NULL WHERE run_id=$1", configured
        )
        await owner.execute(
            "ALTER TABLE runs ENABLE TRIGGER runs_immutable_phase_config_and_monotonic_state"
        )
        await assert_phase_downgrade_refused(owner)
    finally:
        await owner.execute(
            "ALTER TABLE runs ENABLE TRIGGER runs_immutable_phase_config_and_monotonic_state"
        )
        await owner.execute("DELETE FROM runs WHERE run_id=$1", configured)
        await owner.execute("""
            ALTER TABLE runs ADD CONSTRAINT runs_phase_config_state_pair_ck
                CHECK ((phase_state_json IS NULL) = (phase_config_json IS NULL))
        """)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "POSTGRES_DSN": DSN},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


async def test_downgrade_waits_for_inflight_phase_insert_and_refuses_data_loss(db):
    owner, store, slug, _, *_ = db
    source = "phase-race-source-" + slug
    configured = "phase-race-" + slug
    await store.get_or_create_thread_messages(source, slug)
    await admit(store, slug, source, source)
    writer = await asyncpg.connect(DSN)
    transaction = writer.transaction()
    await transaction.start()
    process = None
    try:
        # This uncommitted INSERT holds a write lock, but is invisible to a
        # concurrent READ COMMITTED data check until the writer commits.
        await insert_phase_run(writer, source, configured, CONFIG, INITIAL)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "alembic", "downgrade", "20261109_01",
            env={**os.environ, "POSTGRES_DSN": DSN},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async def wait_for_table_lock():
            while not await owner.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE "
                "relation='runs'::regclass AND mode='AccessExclusiveLock' "
                "AND NOT granted)"
            ):
                if process.returncode is not None:
                    pytest.fail("downgrade exited before waiting for the writer")
                await asyncio.sleep(0.05)

        await asyncio.wait_for(wait_for_table_lock(), timeout=20)
        await transaction.commit()
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        assert process.returncode != 0
        assert b"Cannot remove persisted run phase data" in stderr
        assert await owner.fetchval(
            "SELECT version_num FROM alembic_version"
        ) == "20261109_02"
        assert await owner.fetchval(
            "SELECT phase_config_json IS NOT NULL AND phase_state_json IS NOT NULL "
            "FROM runs WHERE run_id=$1", configured
        )
    finally:
        if writer.is_in_transaction():
            await transaction.rollback()
        await writer.close()
        if process is not None and process.returncode is None:
            os.kill(process.pid, signal.SIGKILL)
            await process.wait()
        await owner.execute("DELETE FROM runs WHERE run_id=$1", configured)
