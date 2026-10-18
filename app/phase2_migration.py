"""Operator-only legacy mapping, bounded backfill and drain-to-terminal cutover."""

from __future__ import annotations

import argparse
import asyncio
import json
from uuid import UUID, uuid4

import asyncpg

from app.core.config import settings


async def report(conn: asyncpg.Connection) -> list[dict]:
    rows = await conn.fetch(
        "SELECT * FROM phase2_schema_backfill_status ORDER BY relation"
    )
    return [dict(row) for row in rows]


async def map_tenant(
    conn: asyncpg.Connection,
    legacy: str,
    tenant: UUID,
    actor: UUID,
    reason: str,
) -> None:
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(7300002)")
        valid = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM memberships m "
            "JOIN principals p ON p.principal_id = m.principal_id "
            "JOIN tenants t ON t.tenant_id = m.tenant_id "
            "WHERE m.tenant_id = $1 AND m.principal_id = $2 "
            "AND m.role = 'tenant_admin' AND m.status = 'active' "
            "AND p.status = 'active' AND t.status = 'active')",
            tenant,
            actor,
        )
        if not valid:
            raise ValueError(
                "Mapping approval must identify an active tenant administrator."
            )
        resolved = await conn.fetchval("SELECT resolve_haas_tenant_uuid($1)", legacy)
        if resolved is not None and resolved != tenant:
            raise ValueError("Mapping conflicts with an existing tenant selector.")
        await conn.execute(
            "INSERT INTO legacy_tenant_mappings "
            "(legacy_tenant_id, tenant_uuid, approved_by_principal_id, reason) "
            "VALUES ($1,$2,$3,$4)",
            legacy,
            tenant,
            actor,
            reason,
        )
        await conn.execute(
            "INSERT INTO audit_events (event_id, tenant_id, actor_principal_id, action, "
            "target_type, target_id, request_id, details) "
            "VALUES ($1,$2,$3,'migration.tenant_mapped','legacy_tenant',$4,$5,$6::jsonb)",
            uuid4(),
            tenant,
            actor,
            legacy,
            uuid4(),
            json.dumps({"reason": reason}),
        )


async def backfill(conn: asyncpg.Connection, batch_size: int) -> dict[str, int]:
    counts = {}
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(7300002)")
        for table, key, predicate in (
            ("threads", "thread_id", "thread_key IS NULL OR updated_at_ts IS NULL"),
            (
                "runs",
                "run_id",
                "run_key IS NULL OR created_at_ts IS NULL OR updated_at_ts IS NULL "
                "OR total_cost_usd_exact IS NULL OR max_cost_usd_exact IS NULL "
                "OR (thread_key IS NULL AND EXISTS (SELECT 1 FROM threads t "
                "WHERE t.thread_id = runs.thread_id AND t.tenant_uuid = runs.tenant_uuid))",
            ),
        ):
            query = f"""
                WITH batch AS (
                    SELECT {key} FROM {table}
                    WHERE ({predicate}
                       OR (tenant_uuid IS NULL AND resolve_haas_tenant_uuid(tenant_id) IS NOT NULL))
                    ORDER BY {key} LIMIT $1 FOR UPDATE SKIP LOCKED
                )
                UPDATE {table} t SET tenant_id = t.tenant_id
                FROM batch b WHERE t.{key} = b.{key}
            """
            result = await conn.execute(query, batch_size)
            counts[table] = int(result.split()[-1])
    return counts


async def cutover(conn: asyncpg.Connection) -> None:
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(7300002)")
        await conn.execute("LOCK TABLE threads, runs IN ACCESS EXCLUSIVE MODE")
        if await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM runs WHERE status IN ('queued','running','input_required','cancelling'))"
        ):
            raise ValueError("Drain active and paused runs before cutover.")
        counts = await report(conn)
        if any(
            row[name]
            for row in counts
            for name in (
                "unresolved_tenant_rows",
                "unresolved_thread_rows",
                "unresolved_state_rows",
                "unresolved_schema_rows",
            )
        ):
            raise ValueError(
                "Unresolved rows require an approved mapping or quarantine before cutover."
            )
        for table, constraints in (
            (
                "threads",
                ("threads_tenant_uuid_fk", "threads_history_version_nonnegative"),
            ),
            (
                "runs",
                (
                    "runs_tenant_uuid_fk",
                    "runs_thread_key_tenant_fk",
                    "runs_thread_alias_fk",
                    "runs_policy_tenant_fk",
                    "runs_status_phase2_check",
                    "runs_cost_phase2_check",
                ),
            ),
        ):
            for constraint in constraints:
                await conn.execute(
                    f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}"
                )
        view_definition = await conn.fetchval(
            "SELECT pg_get_viewdef('phase2_schema_backfill_status'::regclass, true)"
        )
        if not isinstance(view_definition, str):
            raise RuntimeError("Phase 2 backfill report view definition is missing.")
        await conn.execute("DROP VIEW phase2_schema_backfill_status")
        await conn.execute("""
            ALTER TABLE threads ALTER COLUMN tenant_uuid SET NOT NULL,
                ALTER COLUMN thread_key SET NOT NULL,
                ALTER COLUMN updated_at_ts SET NOT NULL;
            ALTER TABLE runs ALTER COLUMN tenant_uuid SET NOT NULL,
                ALTER COLUMN run_key SET NOT NULL,
                ALTER COLUMN thread_key SET NOT NULL,
                ALTER COLUMN created_at_ts SET NOT NULL,
                ALTER COLUMN updated_at_ts SET NOT NULL,
                ALTER COLUMN total_cost_usd_exact SET NOT NULL,
                ALTER COLUMN max_cost_usd_exact SET NOT NULL;
            ALTER TABLE runs
                ALTER COLUMN total_cost_usd TYPE NUMERIC USING total_cost_usd_exact,
                ALTER COLUMN max_cost_usd TYPE NUMERIC USING max_cost_usd_exact;
        """)
        await conn.execute(
            "CREATE VIEW phase2_schema_backfill_status AS " + view_definition
        )


async def quarantine(
    conn: asyncpg.Connection,
    table: str,
    source_id: str,
    actor: UUID,
    reason: str,
) -> None:
    if table not in {"runs", "threads"}:
        raise ValueError(
            "Only terminal runs and unreferenced threads can be quarantined."
        )
    key = "run_id" if table == "runs" else "thread_id"
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(7300002)")
        row = await conn.fetchrow(
            f"SELECT *, to_jsonb(t) AS payload FROM {table} t WHERE {key} = $1 FOR UPDATE",
            source_id,
        )
        if row is None:
            raise ValueError("Quarantine source not found.")
        if table == "runs" and row["status"] not in {
            "completed",
            "failed",
            "cancelled",
        }:
            raise ValueError("Only terminal runs may be quarantined.")
        if table == "threads" and await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM runs WHERE thread_id = $1)",
            source_id,
        ):
            raise ValueError("Quarantine or resolve referencing runs first.")
        await conn.execute(
            "INSERT INTO phase2_quarantine "
            "(source_table, source_id, payload, reason, operator_principal_id) "
            "VALUES ($1,$2,$3::jsonb,$4,$5)",
            table,
            source_id,
            row["payload"],
            reason,
            actor,
        )
        await conn.execute(f"DELETE FROM {table} WHERE {key} = $1", source_id)


async def execute(args: argparse.Namespace) -> None:
    if settings.haas_profile == "prod" and not settings.migration_postgres_dsn:
        raise ValueError("Production migration tools require MIGRATION_POSTGRES_DSN.")
    conn = await asyncpg.connect(
        settings.migration_postgres_dsn or settings.postgres_dsn
    )
    try:
        if args.operation == "map":
            await map_tenant(conn, args.legacy, args.tenant, args.actor, args.reason)
        elif args.operation == "backfill":
            print(json.dumps(await backfill(conn, args.batch_size)))
        elif args.operation == "cutover":
            if not args.admission_stopped:
                raise ValueError(
                    "Stop admission and pass --admission-stopped before cutover."
                )
            await cutover(conn)
        elif args.operation == "quarantine":
            await quarantine(conn, args.table, args.id, args.actor, args.reason)
        print(json.dumps(await report(conn)))
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("report")
    mapping = sub.add_parser("map")
    mapping.add_argument("--legacy", required=True)
    mapping.add_argument("--tenant", type=UUID, required=True)
    mapping.add_argument("--actor", type=UUID, required=True)
    mapping.add_argument("--reason", required=True)
    fill = sub.add_parser("backfill")
    fill.add_argument("--batch-size", type=int, choices=range(1, 10001), default=1000)
    final = sub.add_parser("cutover")
    final.add_argument("--admission-stopped", action="store_true")
    isolate = sub.add_parser("quarantine")
    isolate.add_argument("--table", choices=("runs", "threads"), required=True)
    isolate.add_argument("--id", required=True)
    isolate.add_argument("--actor", type=UUID, required=True)
    isolate.add_argument("--reason", required=True)
    asyncio.run(execute(parser.parse_args()))


if __name__ == "__main__":
    main()
