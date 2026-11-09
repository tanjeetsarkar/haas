"""Use only HAAS_TEST_POSTGRES_DSN pointing at a disposable migrated database."""

import asyncio
import json
import os
import subprocess
import sys

import asyncpg
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine

from test_phase2_database import DSN, admit, db as db

pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL")


async def downgrade_authority():
    """Test 035's refusal independently of later migrations' data guards."""
    engine = create_async_engine(DSN.replace("postgresql://", "postgresql+asyncpg://", 1))

    def run(connection):
        module = ScriptDirectory.from_config(Config("alembic.ini")).get_revision("20261109_01").module
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()

    try:
        async with engine.begin() as connection:
            await connection.run_sync(run)
    finally:
        await engine.dispose()


async def test_nullable_immutable_authority_and_existing_rls(db):
    owner, store, slug, other_slug, tenant, *_ = db
    legacy, restricted = (f"authority-{kind}-{tenant}" for kind in ("legacy", "restricted"))
    await store.get_or_create_thread_messages(legacy, slug)
    await admit(store, slug, legacy, legacy)
    assert await owner.fetchval(
        "SELECT authority_json FROM runs WHERE run_id=$1", legacy
    ) is None
    with pytest.raises(asyncpg.CheckViolationError):
        await owner.execute(
            "UPDATE runs SET authority_json='{}' WHERE run_id=$1", legacy
        )
    # Clone a valid fixture on INSERT, never mutate authority into an existing run.
    await store.get_or_create_thread_messages(restricted, slug)
    columns = [r["column_name"] for r in await owner.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='runs' ORDER BY ordinal_position"
    )]
    replacements = {
        "run_id": f"'{restricted}'", "run_key": "gen_random_uuid()",
        "thread_id": f"'{restricted}'",
        "thread_key": f"(SELECT thread_key FROM threads WHERE thread_id='{restricted}')",
        "authority_json": """'{"version":1,"tools":[]}'::jsonb""",
    }
    await owner.execute(
        f"INSERT INTO runs ({','.join(columns)}) SELECT "
        + ",".join(replacements.get(c, c) for c in columns)
        + " FROM runs WHERE run_id=$1", legacy
    )
    for statement in (
        "authority_json=NULL", "authority_json='{}'", "registry_fingerprint='changed'",
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            await owner.execute(
                f"UPDATE runs SET {statement} WHERE run_id=$1", restricted
            )
    await owner.execute(
        "UPDATE runs SET step_count=step_count+1 WHERE run_id=$1", restricted
    )
    async with store.connection() as conn:
        assert json.loads(await conn.fetchval(
            "SELECT authority_json FROM runs WHERE run_id=$1", restricted
        )) == {"version": 1, "tools": []}
        assert await conn.fetchval(
            "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid='runs'::regclass"
        )
        await conn.execute("SELECT set_config('haas.tenant',$1,true)", other_slug)
        assert await conn.fetchval(
            "SELECT count(*) FROM runs WHERE run_id=$1", restricted
        ) == 0
    with pytest.raises(Exception, match="Cannot remove persisted run authority"):
        await downgrade_authority()
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "POSTGRES_DSN": DSN}, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert await owner.fetchval("SELECT version_num FROM alembic_version") == "20261109_02"


async def test_authority_downgrade_waits_for_inflight_insert_before_check(db):
    """Invoke 035's downgrade directly: a later migration lock must not mask this race."""
    owner, store, slug, *_ = db
    source, restricted = "authority-source-" + slug, "authority-race-" + slug
    await store.get_or_create_thread_messages(source, slug)
    await admit(store, slug, source, source)
    writer = await asyncpg.connect(DSN)
    transaction = writer.transaction()
    await transaction.start()
    task = None

    try:
        await writer.execute(
            "INSERT INTO runs SELECT (jsonb_populate_record(NULL::runs, to_jsonb(r) || "
            "jsonb_build_object('run_id',$2::text,'run_key',gen_random_uuid(),"
            "'status','cancelled','authority_json','{\"version\":1,\"tools\":[]}'::jsonb))).* "
            "FROM runs r WHERE run_id=$1", source, restricted,
        )
        task = asyncio.create_task(downgrade_authority())

        async def wait_for_lock():
            while not await owner.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE relation='runs'::regclass "
                "AND mode='AccessExclusiveLock' AND NOT granted)"
            ):
                if task.done():
                    await task
                    pytest.fail("downgrade did not wait for the in-flight writer")
                await asyncio.sleep(0.05)

        await asyncio.wait_for(wait_for_lock(), 20)
        await transaction.commit()
        with pytest.raises(Exception, match="Cannot remove persisted run authority"):
            await asyncio.wait_for(task, 30)
        assert await owner.fetchval(
            "SELECT authority_json IS NOT NULL FROM runs WHERE run_id=$1", restricted,
        )
        assert await owner.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='runs_immutable_authority')"
        )
    finally:
        if writer.is_in_transaction():
            await transaction.rollback()
        await writer.close()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await owner.execute("DELETE FROM runs WHERE run_id=$1", restricted)
