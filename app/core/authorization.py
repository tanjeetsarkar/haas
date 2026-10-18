from __future__ import annotations

from collections.abc import Callable, Collection, Iterable
from enum import StrEnum
from typing import Any

from fastapi import HTTPException, Request

from app.core.identity import AuthenticatedIdentity


class Action(StrEnum):
    RUN_SUBMIT = "run.submit"
    RUN_READ = "run.read"
    RUN_APPROVAL_READ = "run.approval_read"
    RUN_STREAM = "run.stream"
    RUN_CANCEL = "run.cancel"
    RUN_APPROVE = "run.approve"
    PLUGIN_CATALOG_READ = "plugin_catalog.read"
    TENANT_ADMIN = "tenant.admin"
    AUDIT_READ = "audit.read"
    PLATFORM_OPERATE = "platform.operate"
    # Phase 4-6 and track actions (deny-by-default; granted below per role).
    ARTIFACT_READ = "artifact.read"
    ARTIFACT_WRITE = "artifact.write"
    SECRET_ADMIN = "secret.admin"
    WEBHOOK_ADMIN = "webhook.admin"
    RETENTION_ADMIN = "retention.admin"
    USAGE_READ = "usage.read"
    MARKETPLACE_READ = "marketplace.read"
    MARKETPLACE_INSTALL = "marketplace.install"
    MARKETPLACE_PUBLISH = "marketplace.publish"
    AGENT_COLLABORATE = "agent.collaborate"


_ROLE_ACTIONS: dict[str, frozenset[Action]] = {
    "tenant_admin": frozenset(
        {
            Action.RUN_SUBMIT,
            Action.RUN_READ,
            Action.RUN_STREAM,
            Action.RUN_CANCEL,
            Action.PLUGIN_CATALOG_READ,
            Action.TENANT_ADMIN,
            Action.AUDIT_READ,
            Action.ARTIFACT_READ,
            Action.ARTIFACT_WRITE,
            Action.SECRET_ADMIN,
            Action.WEBHOOK_ADMIN,
            Action.RETENTION_ADMIN,
            Action.USAGE_READ,
            Action.MARKETPLACE_READ,
            Action.MARKETPLACE_INSTALL,
            Action.AGENT_COLLABORATE,
        }
    ),
    "submitter": frozenset(
        {
            Action.RUN_SUBMIT,
            Action.RUN_READ,
            Action.RUN_STREAM,
            Action.RUN_CANCEL,
            Action.PLUGIN_CATALOG_READ,
            Action.ARTIFACT_READ,
            Action.ARTIFACT_WRITE,
            Action.USAGE_READ,
            Action.MARKETPLACE_READ,
            Action.AGENT_COLLABORATE,
        }
    ),
    "approver": frozenset({Action.RUN_APPROVAL_READ, Action.RUN_APPROVE}),
    "auditor": frozenset({Action.AUDIT_READ, Action.USAGE_READ}),
    "operator": frozenset({Action.PLATFORM_OPERATE}),
    # Marketplace publishers act on their own namespace; tenant data stays out of reach.
    "publisher": frozenset({Action.MARKETPLACE_READ, Action.MARKETPLACE_PUBLISH}),
    # Break-glass access requires a separate, time-limited grant that is not
    # implemented yet. The membership role alone deliberately grants no action.
    "break_glass": frozenset(),
}


def is_authorized(roles: Collection[str], action: Action) -> bool:
    return any(action in _ROLE_ACTIONS.get(role, frozenset()) for role in roles)


def get_authenticated_identity(request: Request) -> AuthenticatedIdentity | None:
    identity: Any = getattr(request.state, "identity", None)
    return identity if isinstance(identity, AuthenticatedIdentity) else None


def require_action(
    action: Action, *, allow_legacy_dev: bool = False
) -> Callable[[Request], None]:
    def dependency(request: Request) -> None:
        raw_identity: Any = getattr(request.state, "identity", None)
        if raw_identity is None and allow_legacy_dev:
            return
        identity = get_authenticated_identity(request)
        if identity is None:
            raise HTTPException(
                status_code=401, detail="Authenticated identity required."
            )
        if not is_authorized(identity.roles, action):
            raise HTTPException(
                status_code=403,
                detail=f"Role is not authorized for action '{action.value}'.",
            )

    return dependency


def require_any_action(
    actions: Iterable[Action], *, allow_legacy_dev: bool = False
) -> Callable[[Request], None]:
    allowed_actions = tuple(actions)

    def dependency(request: Request) -> None:
        raw_identity: Any = getattr(request.state, "identity", None)
        if raw_identity is None and allow_legacy_dev:
            return
        identity = get_authenticated_identity(request)
        if identity is None:
            raise HTTPException(
                status_code=401, detail="Authenticated identity required."
            )
        if not any(is_authorized(identity.roles, action) for action in allowed_actions):
            names = ", ".join(action.value for action in allowed_actions)
            raise HTTPException(
                status_code=403,
                detail=f"Role is not authorized for any of these actions: {names}.",
            )

    return dependency


def enforce_approval_separation_of_duties(
    identity: AuthenticatedIdentity | None,
    submitted_by_principal_id: str | None,
) -> None:
    if identity is None:
        return
    if submitted_by_principal_id is None:
        raise HTTPException(
            status_code=403,
            detail="Run has no authenticated submitter provenance and cannot be approved.",
        )
    if identity.principal_id == str(submitted_by_principal_id):
        raise HTTPException(
            status_code=403,
            detail="Run submitters cannot approve or reject their own pending actions.",
        )
