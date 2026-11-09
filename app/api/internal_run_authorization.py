"""Internal authenticated decision seam. All allows remain held pending 035e/c2c."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.api.deps import get_store
from app.core.config import settings
from app.core.database_context import tenant_context
from app.core.envelope import EnvelopeError
from app.core.intent_eligibility import validate_dispatch_eligibility
from app.core.run_restrictions import canonical_arguments, decode_authority
from app.core.worker_identity import ReplayStore, WorkerIdentityError, authenticate_worker_request

router = APIRouter(include_in_schema=False)
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class AuthorizationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    operation: Literal["pre_grant", "final_entry"]
    request_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{16,128}$")
    tenant_id: str
    run_id: str = Field(min_length=1, max_length=128)
    invocation_id: str
    tool_name: str = Field(min_length=1, max_length=255)
    arguments_sha256: Digest
    intent_binding: Digest
    policy_revision_id: str
    registry_fingerprint: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{16}|[0-9a-f]{64})$")]
    plugin_digest: Digest
    envelope_expires_at: int | float

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("Invalid version")
        return value

    @field_validator("tenant_id", "invocation_id", "policy_revision_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("Noncanonical identifier")
        return value

    @field_validator("envelope_expires_at")
    @classmethod
    def future_expiry(cls, value: int | float) -> int | float:
        import math
        if isinstance(value, bool) or not math.isfinite(value) or value <= time.time():
            raise ValueError("Invalid expiry")
        return value


def evaluate_check(check: AuthorizationCheck, snapshot: Any) -> str:
    """Compare persisted bindings; even a matching snapshot cannot activate work."""
    try:
        if snapshot is None or str(snapshot["tenant_uuid"]) != check.tenant_id:
            return "restriction.denied"
        authority = decode_authority(snapshot["authority_json"])
        if authority is None or authority.intent is None:
            return "restriction.denied"
        intent = authority.intent
        context = intent.context
        if (
            snapshot["run_id"] != check.run_id
            or snapshot["tool_name"] != check.tool_name or intent.tool_name != check.tool_name
            or str(snapshot["invocation_id"]) != check.invocation_id
            or context.tenant_id != check.tenant_id or context.run_id != check.run_id
            or context.submitter_id != str(snapshot["submitted_by_principal_id"])
            or context.policy_revision != check.policy_revision_id
            or str(snapshot["policy_revision_id"]) != check.policy_revision_id
            or str(snapshot["active_policy_revision_id"]) != check.policy_revision_id
            or context.registry_fingerprint != check.registry_fingerprint
            or snapshot["registry_fingerprint"] != check.registry_fingerprint
            or context.plugin_digest != check.plugin_digest
            or intent.binding != check.intent_binding
            or hashlib.sha256(intent.arguments_json.encode("utf-8")).hexdigest() != check.arguments_sha256
            or canonical_arguments(json.loads(snapshot["arguments"])) != intent.arguments_json
            or snapshot["status"] != "running" or snapshot["tenant_status"] != "active"
            or snapshot["deadline_at"] is not None and snapshot["deadline_at"].timestamp() <= time.time()
            or snapshot["invocation_status"] != "dispatched"
            or snapshot["approval_status"] != "approved"
            or snapshot["decision"] != "approved"
            or snapshot["decided_by"] is None
            or str(snapshot["decided_by"]) == context.submitter_id
            or snapshot["approval_expires_at"].timestamp() <= time.time()
            or snapshot["batch_digest"] != snapshot["decision_digest"]
        ):
            return "restriction.denied"
    except (ValueError, TypeError, KeyError, AttributeError, EnvelopeError):
        return "restriction.denied"
    # Preliminary match only. Live transactional proof validation follows.
    return "restriction.unavailable"


@router.post("/internal/v1/run-authorizations/check")
async def check_authorization(request: Request) -> dict[str, Any]:
    headers = request.headers.getlist("authorization")
    if len(headers) != 1 or not headers[0].startswith("Bearer "):
        raise HTTPException(401, "worker_auth.denied")
    try:
        body = await request.json()
        store = get_store()
        await authenticate_worker_request(
            settings, cast(ReplayStore, store.rds), headers[0][7:], body,
            method=request.method, path=request.url.path,
        )
    except (WorkerIdentityError, ValueError, TypeError):
        raise HTTPException(401, "worker_auth.denied") from None
    try:
        check = AuthorizationCheck.model_validate(body)
    except ValidationError:
        raise HTTPException(422, "restriction.invalid_request") from None
    token = tenant_context.set(check.tenant_id)
    try:
        async with store.connection() as conn:
            snapshot = await conn.fetchrow(
                "SELECT r.*, t.status AS tenant_status, t.active_policy_revision_id, "
                "i.invocation_id, i.tool_name, i.arguments, i.status AS invocation_status, "
                "b.status AS approval_status, b.expires_at AS approval_expires_at, b.batch_digest, "
                "d.decision, d.decided_by, d.batch_digest AS decision_digest "
                "FROM runs r JOIN tenants t ON t.tenant_id=r.tenant_uuid "
                "JOIN tool_invocations i ON i.run_id=r.run_id AND i.tenant_id=r.tenant_id "
                "LEFT JOIN approval_batches b ON b.batch_id=i.approval_batch_id AND b.tenant_id=r.tenant_id "
                "LEFT JOIN approval_decisions d ON d.batch_id=b.batch_id AND d.tenant_id=r.tenant_id "
                "WHERE r.tenant_uuid=$1 AND r.run_id=$2 AND i.invocation_id=$3",
                UUID(check.tenant_id), check.run_id, UUID(check.invocation_id),
            )
            code = evaluate_check(check, snapshot)
            if code == "restriction.unavailable":
                try:
                    await validate_dispatch_eligibility(store, conn, check)
                except (ValueError, TypeError, KeyError, AttributeError):
                    code = "restriction.denied"
            # Operational release and exactly-once dispatch are not implemented.
            # Even live eligibility is not a release permit; keep ALLOW held.
    except Exception:
        raise HTTPException(503, "restriction.unavailable") from None
    finally:
        tenant_context.reset(token)
    checked_at = time.time()
    return {
        "version": 1, "request_id": check.request_id, "operation": check.operation,
        "decision": "deny", "code": code,
        "binding_sha256": hashlib.sha256(canonical_arguments(body).encode("utf-8")).hexdigest(),
        "checked_at": checked_at, "valid_until": min(checked_at, check.envelope_expires_at),
    }
