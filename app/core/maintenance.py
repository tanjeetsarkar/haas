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


async def maintenance_cycle(
    store: DistributedStore, *, collect_garbage: bool
) -> dict[str, int]:
    counts = await store.reconcile()
    counts["outbox_relayed"] = await store.relay_outbox(limit=500)
    for area in (
        "isolation",
        "access",
        "artifacts",
        "operations",
        "marketplace",
        "agent_mesh",
    ):
        job = getattr(store, f"maintenance_{area}", None)
        if job is not None:
            counts.update(await job())
    if collect_garbage:
        counts["removed_scratchpad_files"] = await asyncio.to_thread(
            garbage_collect_scratchpad,
            settings.scratchpad_dir,
            ttl_sec=settings.scratchpad_ttl_sec,
        )
    return counts


async def maintenance_loop(store: DistributedStore) -> None:
    """Reconciler + outbox relay; every repair is a fenced transition, so replicas may overlap."""
    last_gc = 0.0
    while True:
        try:
            now = time.monotonic()
            collect = now - last_gc >= settings.maintenance_interval_sec
            counts = await maintenance_cycle(store, collect_garbage=collect)
            if collect:
                last_gc = now
            if any(counts.values()):
                log.info("maintenance completed", extra=counts)
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            RedisError,
            OSError,
            TimeoutError,
        ):
            log.exception("maintenance cycle failed")
        await asyncio.sleep(settings.reconcile_interval_sec)
