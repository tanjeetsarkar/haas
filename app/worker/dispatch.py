from __future__ import annotations

from typing import Any

from celery import Task
from celery.canvas import Signature


def signature_for(task: Task, *args: Any, **kwargs: Any) -> Signature:
    signature = task.s(*args, **kwargs)
    if signature is None:
        raise RuntimeError(f"Celery task {task.name!r} did not create a signature")
    return signature
