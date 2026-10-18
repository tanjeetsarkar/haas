import asyncio

from app.core.maintenance import maintenance_loop
from app.core.observability import configure_logging
from app.core.store import DistributedStore


async def run() -> None:
    configure_logging()
    store = DistributedStore()
    await store.init(pool_max=2, service=True)
    try:
        await maintenance_loop(store)
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(run())
