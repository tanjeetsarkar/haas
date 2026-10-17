from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import asyncpg
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.store import DistributedStore

log = logging.getLogger("haas.maintenance")


def garbage_collect_scratchpad(
    directory: Path, *, ttl_sec: int, now: float | None = None
) -> int:
    if not directory.exists():
        return 0
    cutoff = (time.time() if now is None else now) - ttl_sec
    removed = 0
    for entry in directory.iterdir():
        if entry.suffix != ".txt" or entry.is_symlink():
            continue
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed


async def maintenance_loop(store: DistributedStore) -> None:
    while True:
        try:
            stale = await store.reap_orphaned_runs(
                time.time() - settings.orphan_run_timeout_sec
            )
            for run in stale:
                await store.clear_ephemeral_secrets(run["run_id"])
                await store.publish_event(
                    run["run_id"],
                    "run_error",
                    {"error": "Run expired by orphan reaper."},
                )
            removed = await asyncio.to_thread(
                garbage_collect_scratchpad,
                settings.scratchpad_dir,
                ttl_sec=settings.scratchpad_ttl_sec,
            )
            if stale or removed:
                log.info(
                    "maintenance completed",
                    extra={
                        "reaped_runs": len(stale),
                        "removed_scratchpad_files": removed,
                    },
                )
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            RedisError,
            OSError,
            TimeoutError,
        ):
            log.exception("maintenance cycle failed")
        await asyncio.sleep(settings.maintenance_interval_sec)
