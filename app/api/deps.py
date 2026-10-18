"""Shared dependencies for feature routers (avoids importing app.main at module load)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, Header

from app.core.authorization import Action, require_action, require_any_action
from app.core.config import settings

if TYPE_CHECKING:
    from app.core.store import DistributedStore


def get_store() -> "DistributedStore":
    """The process-wide store owned by app.main (tests may monkeypatch ``app.main.store``)."""
    from app import main

    return main.store


def requires(action: Action):
    return Depends(
        require_action(action, allow_legacy_dev=settings.haas_profile == "dev")
    )


def requires_any(*actions: Action):
    return Depends(
        require_any_action(actions, allow_legacy_dev=settings.haas_profile == "dev")
    )


def tenant_header(x_tenant_id: str = Header(default="default-tenant")) -> str:
    return x_tenant_id
