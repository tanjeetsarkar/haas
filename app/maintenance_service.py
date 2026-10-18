import asyncio

from app.core.maintenance import maintenance_loop
from app.core.observability import configure_logging
from app.core.store import DistributedStore
from app.worker.tasks import make_outbox_publisher


async def run() -> None:
    configure_logging()
    store = DistributedStore()
    await store.init(pool_max=2, service=True)
    store.outbox_publisher = make_outbox_publisher(store)
    try:
        await maintenance_loop(store)
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(run())
