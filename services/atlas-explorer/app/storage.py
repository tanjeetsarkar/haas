"""Tenant/owner-scoped PostgreSQL transactions for Atlas workspaces."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import asyncpg
from fastapi import HTTPException

from app.identity import VerifiedIdentity
from app.models import WorkspaceCreate, WorkspaceMutation

MIGRATION_ID = "001_workspace"


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _receipt(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "workspace_id": str(row["workspace_id"]),
        "mutation_id": row["mutation_id"],
        "revision": row["revision"],
        "saved_at": row["saved_at"].isoformat(),
    }


class WorkspaceStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> WorkspaceStore:
        pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=10,
            command_timeout=5,
            init=_initialize_connection,
        )
        store = cls(pool)
        try:
            await store._verify_runtime_role()
        except Exception:
            await pool.close()
            raise
        return store

    async def close(self) -> None:
        await self.pool.close()

    async def _verify_runtime_role(self) -> None:
        async with self.pool.acquire() as conn:
            role = await conn.fetchrow(
                "SELECT r.rolsuper, r.rolbypassrls, "
                "EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='atlas_explorer' AND c.relname IN "
                "('workspaces','workspace_mutations','workspace_tenant_usage') "
                "AND c.relowner=r.oid) AS owns_table, "
                "(SELECT count(*)=3 AND bool_and(c.relrowsecurity AND c.relforcerowsecurity) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='atlas_explorer' AND c.relname IN "
                "('workspaces','workspace_mutations','workspace_tenant_usage')) AS forced_rls "
                "FROM pg_roles r WHERE r.rolname=current_user"
            )
            migrated = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM atlas_explorer.schema_migrations WHERE version=$1)",
                MIGRATION_ID,
            )
            if (
                not role
                or role["rolsuper"]
                or role["rolbypassrls"]
                or role["owns_table"]
                or not role["forced_rls"]
                or not migrated
            ):
                raise RuntimeError("Atlas runtime role or schema is not safely provisioned.")

    @asynccontextmanager
    async def transaction(self, identity: VerifiedIdentity) -> AsyncIterator[Any]:
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SELECT set_config('atlas.tenant_id', $1, true)", identity.tenant_id)
                    await conn.execute("SELECT set_config('atlas.principal_id', $1, true)", identity.principal_id)
                    yield conn
        except asyncpg.CheckViolationError as error:
            constraint_name = getattr(error, "constraint_name", None)
            if constraint_name == "workspace_state_size":
                raise HTTPException(413, {"code": "workspace_too_large"}) from None
            if constraint_name == "workspace_mutation_payload_size":
                raise HTTPException(413, {"code": "mutation_payload_too_large"}) from None
            raise

    async def create(self, identity: VerifiedIdentity, request: WorkspaceCreate) -> dict[str, Any]:
        body = request.model_dump(mode="json")
        digest = _digest(body)
        workspace_id = uuid.uuid4()
        async with self.transaction(identity) as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", identity.tenant_id)
            existing = await conn.fetchrow(
                "SELECT w.workspace_id, m.mutation_id, m.revision, m.saved_at, w.create_digest "
                "FROM atlas_explorer.workspaces w JOIN atlas_explorer.workspace_mutations m "
                "ON m.tenant_id=w.tenant_id AND m.workspace_id=w.workspace_id "
                "AND m.mutation_id=w.create_mutation_id "
                "WHERE w.tenant_id=$1 AND w.owner_principal_id=$2 AND w.create_mutation_id=$3",
                identity.tenant_id, identity.principal_id, request.mutation_id,
            )
            if existing:
                if existing["create_digest"] != digest:
                    raise HTTPException(409, {"code": "mutation_id_reused"})
                return _receipt(existing)

            owned_count = await conn.fetchval(
                "SELECT count(*) FROM atlas_explorer.workspaces "
                "WHERE tenant_id=$1 AND owner_principal_id=$2",
                identity.tenant_id, identity.principal_id,
            )
            if (owned_count or 0) >= 100:
                raise HTTPException(429, {"code": "workspace_quota"})
            state = {"title": request.title, "draft": request.draft}
            saved_at = await conn.fetchval(
                "INSERT INTO atlas_explorer.workspaces "
                "(tenant_id,workspace_id,owner_principal_id,title,schema_version,state,revision,"
                "create_mutation_id,create_digest) VALUES($1,$2,$3,$4,1,$5::jsonb,1,$6,$7) "
                "RETURNING created_at",
                identity.tenant_id, workspace_id, identity.principal_id, request.title,
                state, request.mutation_id, digest,
            )
            if saved_at is None:
                raise RuntimeError("Workspace creation did not return a timestamp.")
            receipt = {
                "workspace_id": str(workspace_id),
                "mutation_id": request.mutation_id,
                "revision": 1,
                "saved_at": saved_at.isoformat(),
            }
            await conn.execute(
                "INSERT INTO atlas_explorer.workspace_mutations "
                "(tenant_id,workspace_id,owner_principal_id,mutation_id,request_digest,kind,payload,revision,receipt,saved_at) "
                "VALUES($1,$2,$3,$4,$5,'workspace_created',$6::jsonb,1,$7::jsonb,$8)",
                identity.tenant_id, workspace_id, identity.principal_id, request.mutation_id,
                digest, state, receipt, saved_at,
            )
            total_bytes = await conn.fetchval(
                "SELECT persisted_bytes FROM atlas_explorer.workspace_tenant_usage WHERE tenant_id=$1",
                identity.tenant_id,
            )
            if (total_bytes or 0) > 100 * 1024 * 1024:
                raise HTTPException(429, {"code": "tenant_storage_quota"})
            return receipt

    async def get(self, identity: VerifiedIdentity, workspace_id: uuid.UUID) -> dict[str, Any]:
        async with self.transaction(identity) as conn:
            row = await conn.fetchrow(
                "SELECT workspace_id,title,state,revision,created_at,updated_at "
                "FROM atlas_explorer.workspaces WHERE tenant_id=$1 AND owner_principal_id=$2 "
                "AND workspace_id=$3",
                identity.tenant_id, identity.principal_id, workspace_id,
            )
        if row is None:
            raise HTTPException(404, "Workspace not found.")
        return {
            "workspace_id": str(row["workspace_id"]),
            "title": row["title"],
            "state": row["state"],
            "revision": row["revision"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }

    async def catalog(
        self, identity: VerifiedIdentity, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        marker: tuple[datetime, uuid.UUID] | None = None
        if cursor:
            try:
                decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                marker = (datetime.fromisoformat(decoded["updated_at"]), uuid.UUID(decoded["workspace_id"]))
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                raise HTTPException(400, {"code": "invalid_cursor"}) from None
        async with self.transaction(identity) as conn:
            if marker:
                rows = await conn.fetch(
                    "SELECT workspace_id,title,revision,created_at,updated_at FROM atlas_explorer.workspaces "
                    "WHERE tenant_id=$1 AND owner_principal_id=$2 AND (updated_at,workspace_id)<($3,$4) "
                    "ORDER BY updated_at DESC,workspace_id DESC LIMIT $5",
                    identity.tenant_id, identity.principal_id, marker[0], marker[1], limit + 1,
                )
            else:
                rows = await conn.fetch(
                    "SELECT workspace_id,title,revision,created_at,updated_at FROM atlas_explorer.workspaces "
                    "WHERE tenant_id=$1 AND owner_principal_id=$2 "
                    "ORDER BY updated_at DESC,workspace_id DESC LIMIT $3",
                    identity.tenant_id, identity.principal_id, limit + 1,
                )
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            token = json.dumps({
                "updated_at": last["updated_at"].isoformat(),
                "workspace_id": str(last["workspace_id"]),
            }, separators=(",", ":")).encode()
            next_cursor = base64.urlsafe_b64encode(token).decode().rstrip("=")
        return {
            "items": [
                {
                    "workspace_id": str(row["workspace_id"]),
                    "title": row["title"],
                    "revision": row["revision"],
                    "created_at": row["created_at"].isoformat(),
                    "updated_at": row["updated_at"].isoformat(),
                }
                for row in page
            ],
            "next_cursor": next_cursor,
        }

    async def journal(
        self, identity: VerifiedIdentity, workspace_id: uuid.UUID, after_revision: int, limit: int
    ) -> dict[str, Any]:
        async with self.transaction(identity) as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM atlas_explorer.workspaces WHERE tenant_id=$1 "
                "AND owner_principal_id=$2 AND workspace_id=$3",
                identity.tenant_id, identity.principal_id, workspace_id,
            )
            if not exists:
                raise HTTPException(404, "Workspace not found.")
            rows = await conn.fetch(
                "SELECT mutation_id,kind,payload,revision,saved_at FROM atlas_explorer.workspace_mutations "
                "WHERE tenant_id=$1 AND workspace_id=$2 AND owner_principal_id=$3 AND revision>$4 "
                "ORDER BY revision LIMIT $5",
                identity.tenant_id, workspace_id, identity.principal_id, after_revision, limit + 1,
            )
        has_more = len(rows) > limit
        page = rows[:limit]
        return {
            "items": [
                {
                    "mutation_id": row["mutation_id"],
                    "kind": row["kind"],
                    "payload": row["payload"],
                    "revision": row["revision"],
                    "saved_at": row["saved_at"].isoformat(),
                }
                for row in page
            ],
            "next_revision": page[-1]["revision"] if has_more and page else None,
        }

    async def operation_run_id(
        self, identity: VerifiedIdentity, workspace_id: uuid.UUID, operation_id: str
    ) -> str:
        workspace = await self.get(identity, workspace_id)
        operation = workspace["state"].get("operations", {}).get(operation_id)
        if not operation:
            raise HTTPException(404, "Operation not found.")
        run_id = operation.get("run_id")
        if not run_id:
            raise HTTPException(409, {"code": "operation_run_unknown"})
        return str(run_id)

    async def mutate(
        self, identity: VerifiedIdentity, workspace_id: uuid.UUID, request: WorkspaceMutation
    ) -> dict[str, Any]:
        body = request.canonical()
        digest = _digest(body)
        payload = body["payload"]
        async with self.transaction(identity) as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", identity.tenant_id)
            row = await conn.fetchrow(
                "SELECT workspace_id,owner_principal_id,title,state,revision "
                "FROM atlas_explorer.workspaces WHERE tenant_id=$1 AND owner_principal_id=$2 "
                "AND workspace_id=$3 FOR UPDATE",
                identity.tenant_id, identity.principal_id, workspace_id,
            )
            if row is None:
                raise HTTPException(404, "Workspace not found.")
            replay = await conn.fetchrow(
                "SELECT request_digest,receipt FROM atlas_explorer.workspace_mutations "
                "WHERE tenant_id=$1 AND workspace_id=$2 AND owner_principal_id=$3 AND mutation_id=$4",
                identity.tenant_id, workspace_id, identity.principal_id, request.mutation_id,
            )
            if replay:
                if replay["request_digest"] != digest:
                    raise HTTPException(409, {"code": "mutation_id_reused"})
                return replay["receipt"]
            if row["revision"] != request.expected_revision:
                raise HTTPException(409, {"code": "revision_conflict", "current_revision": row["revision"]})
            state = dict(row["state"])
            apply_mutation(state, request.kind, payload)
            encoded = json.dumps(state, separators=(",", ":"), ensure_ascii=False)
            if len(encoded.encode()) > 2 * 1024 * 1024:
                raise HTTPException(413, {"code": "workspace_too_large"})
            journal_count = await conn.fetchval(
                "SELECT count(*) FROM atlas_explorer.workspace_mutations "
                "WHERE tenant_id=$1 AND workspace_id=$2 AND owner_principal_id=$3",
                identity.tenant_id, workspace_id, identity.principal_id,
            )
            if (journal_count or 0) >= 10_000:
                raise HTTPException(429, {"code": "journal_quota"})
            revision = row["revision"] + 1
            timestamp: datetime = await conn.fetchval(
                "UPDATE atlas_explorer.workspaces SET state=$4::jsonb,revision=$5,updated_at=clock_timestamp() "
                "WHERE tenant_id=$1 AND workspace_id=$2 AND owner_principal_id=$3 RETURNING updated_at",
                identity.tenant_id, workspace_id, identity.principal_id, state, revision,
            )
            if timestamp is None:
                raise RuntimeError("Workspace mutation did not return a timestamp.")
            receipt = {
                "workspace_id": str(workspace_id),
                "mutation_id": request.mutation_id,
                "revision": revision,
                "saved_at": timestamp.isoformat(),
            }
            await conn.execute(
                "INSERT INTO atlas_explorer.workspace_mutations "
                "(tenant_id,workspace_id,owner_principal_id,mutation_id,request_digest,kind,payload,revision,receipt,saved_at) "
                "VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9::jsonb,$10)",
                identity.tenant_id, workspace_id, identity.principal_id, request.mutation_id,
                digest, request.kind, payload, revision, receipt, timestamp,
            )
            total_bytes = await conn.fetchval(
                "SELECT persisted_bytes FROM atlas_explorer.workspace_tenant_usage WHERE tenant_id=$1",
                identity.tenant_id,
            )
            if (total_bytes or 0) > 100 * 1024 * 1024:
                raise HTTPException(429, {"code": "tenant_storage_quota"})
            return receipt

    async def replay_mutation(
        self, identity: VerifiedIdentity, workspace_id: uuid.UUID, request: WorkspaceMutation
    ) -> dict[str, Any] | None:
        """Return the committed receipt before external verification on exact replay."""
        body = request.canonical()
        digest = _digest(body)
        async with self.transaction(identity) as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM atlas_explorer.workspaces "
                "WHERE tenant_id=$1 AND owner_principal_id=$2 AND workspace_id=$3",
                identity.tenant_id, identity.principal_id, workspace_id,
            )
            if not exists:
                raise HTTPException(404, "Workspace not found.")
            replay = await conn.fetchrow(
                "SELECT request_digest,receipt FROM atlas_explorer.workspace_mutations "
                "WHERE tenant_id=$1 AND workspace_id=$2 AND owner_principal_id=$3 AND mutation_id=$4",
                identity.tenant_id, workspace_id, identity.principal_id, request.mutation_id,
            )
            if replay is None:
                return None
            if replay["request_digest"] != digest:
                raise HTTPException(409, {"code": "mutation_id_reused"})
            return replay["receipt"]


def apply_mutation(state: dict[str, Any], kind: str, payload: dict[str, Any]) -> None:
    """Apply one validated event without trusting client-owned execution status."""
    if kind == "draft_checkpoint":
        state["draft"] = payload["draft"]
    elif kind == "query_submitted":
        state.setdefault("queries", []).append({"prompt": payload["prompt"]})
    elif kind == "selection_changed":
        state["selection"] = payload["selection"]
    elif kind == "branch_committed":
        state.setdefault("branches", []).append(payload["branch"])
    elif kind == "comparison_changed":
        state["comparison"] = payload["comparison"]
    elif kind == "action_draft_changed":
        state["action_draft"] = payload["action_draft"]
    elif kind == "operation_prepared":
        operations = state.setdefault("operations", {})
        operation = payload["operation_id"]
        unresolved = [
            item
            for item in operations.values()
            if item.get("state") in {"prepared", "submitting", "unknown"}
        ]
        if operation in operations or unresolved:
            raise HTTPException(409, {"code": "operation_exists"})
        intent = {"prompt": payload["prompt"], "selected_context": payload["selected_context"]}
        digest = hashlib.sha256(
            json.dumps(intent, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        operations[operation] = {**payload, "intent_digest": digest, "state": "prepared"}
    elif kind in {"operation_bound", "operation_outcome"}:
        operation = state.setdefault("operations", {}).get(payload["operation_id"])
        if operation is None:
            raise HTTPException(409, {"code": "operation_missing"})
        if kind == "operation_bound":
            if operation["state"] not in {"submitting", "unknown"}:
                raise HTTPException(409, {"code": "operation_transition"})
            known_run_id = operation.get("run_id")
            if known_run_id is not None and payload["run_id"] != known_run_id:
                raise HTTPException(409, {"code": "operation_run_id_immutable"})
            operation.update(
                {
                    "run_id": payload["run_id"],
                    "thread_id": payload.get("thread_id"),
                    "state": "bound",
                }
            )
        else:
            next_state = payload["state"]
            current_state = operation["state"]
            if current_state not in {"prepared", "submitting", "bound", "unknown"}:
                raise HTTPException(409, {"code": "operation_transition"})
            if next_state == "submitting" and current_state != "prepared":
                raise HTTPException(409, {"code": "operation_transition"})
            if next_state == "unknown" and current_state != "submitting":
                raise HTTPException(409, {"code": "operation_transition"})
            if next_state == "terminal" and current_state not in {"bound", "unknown"}:
                raise HTTPException(409, {"code": "operation_transition"})
            if next_state in {"submitting", "unknown"} and payload["status"] != next_state:
                raise HTTPException(409, {"code": "operation_status_mismatch"})
            if next_state == "terminal" and "run_id" not in operation:
                raise HTTPException(409, {"code": "operation_unbound"})
            if next_state == "unknown" and payload.get("run_id") is not None:
                known_run_id = operation.get("run_id")
                if known_run_id is not None and known_run_id != payload["run_id"]:
                    raise HTTPException(409, {"code": "operation_run_id_immutable"})
                operation["run_id"] = payload["run_id"]
            operation.update({"state": next_state, "status": payload["status"]})


async def _initialize_connection(conn: Any) -> None:
    await conn.set_type_codec("json", schema="pg_catalog", encoder=json.dumps, decoder=json.loads)
    await conn.set_type_codec("jsonb", schema="pg_catalog", encoder=json.dumps, decoder=json.loads)
