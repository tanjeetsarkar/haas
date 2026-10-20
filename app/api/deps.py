"""Shared dependencies for feature routers (avoids importing app.main at module load)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterator

from fastapi import Depends, Header, HTTPException, Request
from pydantic import BaseModel

from app.core.authorization import Action, require_action, require_any_action
from app.core.config import settings
from app.core.database_context import tenant_context

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


def tenant_header(x_haas_tenant: str = Header(default="default-tenant")) -> str:
    """Read tenant from the request header.

    The header is ``x-haas-tenant`` (``x-tenant-id`` is not used because it is
    ambiguous with multi-tenant setups).  In dev mode we also honour the
    legacy ``x-tenant-id`` header for backwards compatibility.
    """
    return x_haas_tenant


def get_tenant_id(tenant_id: str = Depends(tenant_header)) -> str:
    """Simply return the tenant header value (no context management)."""
    return tenant_id


class PrincipalInfo(BaseModel):
    """Authenticated principal info for feature routers."""
    principal_id: str
    roles: list[str]
    tenant_id: str


def get_principal(request: Request) -> PrincipalInfo:
    """Returns the authenticated principal from the request state.

    Raises HTTPException 401 if not authenticated (unless in dev mode
    with ``allow_legacy_dev`` on the actions).
    """
    from app.core.authorization import get_authenticated_identity

    identity = get_authenticated_identity(request)
    if identity is None:
        if settings.haas_profile == "dev":
            # In dev mode, honour the x-haas-tenant header for the tenant
            # context while using a synthetic identity for auth checks.
            tenant_id = request.headers.get("x-haas-tenant", "default-tenant")
            return PrincipalInfo(
                principal_id="dev-user",
                roles=["tenant_admin", "submitter"],
                tenant_id=tenant_id,
            )
        raise HTTPException(status_code=401, detail="Authenticated identity required.")
    return PrincipalInfo(
        principal_id=str(identity.principal_id),
        roles=list(identity.roles),
        tenant_id=str(identity.tenant_id),
    )


def tenant_scope(
    principal: PrincipalInfo = Depends(get_principal),
) -> Iterator[PrincipalInfo]:
    """Set the tenant context for the duration of the request, then reset.

    Any code that calls ``store.connection()`` inside this dependency's
    lifetime automatically runs with RLS scoped to ``principal.tenant_id``.
    """
    token = tenant_context.set(principal.tenant_id)
    try:
        yield principal
    finally:
        tenant_context.reset(token)
