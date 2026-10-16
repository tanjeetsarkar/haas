from celery import Celery
from celery.signals import worker_process_init
from kombu import Queue

from app.core.config import settings
from app.core.plugins import registry

celery_app = Celery(
    "haas_worker",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Critical for AI agents: never hoard tasks; acknowledge only after step completion
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    # Chord & result expiration
    result_expires=3600,
    broker_transport_options={
        "visibility_timeout": 600,  # 10 min safety margin
    },
    task_queues=(
        Queue("q_orchestrator"),
        Queue("q_tools_fast"),
        Queue("q_tools_sandbox"),
    ),
    task_default_queue="q_orchestrator",
)


@worker_process_init.connect
def init_worker_plugins(**kwargs) -> None:
    """Loads all SKILL.md, Python tools, and MCP manifests when each worker process forks."""
    settings.plugins_dir.mkdir(parents=True, exist_ok=True)
    settings.scratchpad_dir.mkdir(parents=True, exist_ok=True)
    registry.discover_all(settings.plugins_dir)
