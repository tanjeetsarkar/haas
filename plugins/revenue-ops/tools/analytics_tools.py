"""Revenue-ops analytics tools. Loaded by `PluginRegistry.discover_all` via `register(registry)`."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from pydantic import BaseModel, Field

from app.core.context import RunContext, ToolRemediationError

# (invoice_id, customer, month, segment, amount_usd, status)
_SEED_INVOICES = [
    ("inv_1001", "acme", "2026-07", "enterprise", 12000.0, "paid"),
    ("inv_1002", "globex", "2026-07", "enterprise", 9000.0, "paid"),
    ("inv_1003", "initech", "2026-07", "smb", 1500.0, "paid"),
    ("inv_1004", "umbrella", "2026-07", "smb", 1700.0, "paid"),
    ("inv_2001", "acme", "2026-08", "enterprise", 12000.0, "paid"),
    ("inv_2002", "globex", "2026-08", "enterprise", 9000.0, "paid"),
    ("inv_2003", "initech", "2026-08", "smb", 1500.0, "paid"),
    ("inv_2004", "umbrella", "2026-08", "smb", 1700.0, "paid"),
    ("inv_3001", "acme", "2026-09", "enterprise", 12000.0, "paid"),
    ("inv_3002", "acme", "2026-09", "enterprise", 12000.0, "paid"),  # duplicate charge
    ("inv_3003", "globex", "2026-09", "enterprise", 9000.0, "paid"),
    ("inv_3004", "initech", "2026-09", "smb", 1500.0, "paid"),
    ("inv_3005", "umbrella", "2026-09", "smb", 1700.0, "paid"),
]

_QUARTER_MONTHS = {"Q1": (1, 3), "Q2": (4, 6), "Q3": (7, 9), "Q4": (10, 12)}


def _open_db(ctx: RunContext) -> sqlite3.Connection:
    raw = ctx.get_secret("ANALYTICS_DB_PATH")
    path = Path(raw).resolve()
    # The path comes from the caller; keep it inside /tmp and to *.db files.
    if not path.is_relative_to("/tmp") or path.suffix != ".db":
        raise ToolRemediationError("ANALYTICS_DB_PATH must be a *.db file under /tmp.")
    first_use = not path.exists()
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS invoices (invoice_id TEXT PRIMARY KEY, customer TEXT, "
        "month TEXT, segment TEXT, amount_usd REAL, status TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS refund_batches (batch_id TEXT PRIMARY KEY, reason TEXT, "
        "invoice_ids TEXT, total_usd REAL)"
    )
    if first_use:  # demo dataset so a fresh deployment is explorable
        conn.executemany(
            "INSERT OR IGNORE INTO invoices VALUES (?,?,?,?,?,?)", _SEED_INVOICES
        )
        conn.commit()
    return conn


def _months_for(quarter: str) -> list[str]:
    try:
        year, q = quarter.upper().split("-")
        lo, hi = _QUARTER_MONTHS[q]
        return [f"{int(year):04d}-{m:02d}" for m in range(lo, hi + 1)]
    except (ValueError, KeyError):
        raise ToolRemediationError(
            f"Invalid quarter '{quarter}'. Use the format YYYY-Qn, e.g. 2026-Q3."
        )


class QueryRevenueArgs(BaseModel):
    quarter: str = Field(..., description="Quarter in YYYY-Qn format, e.g. 2026-Q3.")
    segment: str | None = Field(
        None, description="Optional segment filter: enterprise or smb."
    )


class RefundBatchArgs(BaseModel):
    invoice_ids: list[str] = Field(
        ..., min_length=1, max_length=50, description="Exact invoice ids to refund."
    )
    reason: str = Field(
        ..., min_length=3, description="Why these invoices are refunded."
    )


def register(registry) -> None:
    @registry.tool(
        name="query_revenue_metrics",
        description="Returns monthly revenue per segment for a quarter, MoM change, and flagged anomalies/duplicate invoices.",
        args_schema=QueryRevenueArgs,
        sandbox=False,
    )
    def query_revenue_metrics(ctx: RunContext, args: QueryRevenueArgs) -> str:
        months = _months_for(args.quarter)
        conn = _open_db(ctx)
        try:
            sql = (
                "SELECT month, segment, SUM(amount_usd) FROM invoices "
                "WHERE status = 'paid' AND month IN ({}) {} GROUP BY month, segment ORDER BY segment, month"
            ).format(
                ",".join("?" * len(months)), "AND segment = ?" if args.segment else ""
            )
            rows = conn.execute(
                sql, [*months, *([args.segment] if args.segment else [])]
            ).fetchall()

            series: list[dict] = []
            prev: dict[str, float] = {}
            for month, segment, total in rows:
                change = (
                    (total - prev[segment]) / prev[segment] if segment in prev else None
                )
                series.append(
                    {
                        "month": month,
                        "segment": segment,
                        "gross_usd": total,
                        "mom_change": None if change is None else round(change, 4),
                        "anomaly": change is not None and abs(change) > 0.15,
                    }
                )
                prev[segment] = total

            dupes = conn.execute(
                "SELECT customer, month, amount_usd, GROUP_CONCAT(invoice_id) FROM invoices "
                "WHERE status = 'paid' AND month IN ({}) GROUP BY customer, month, amount_usd "
                "HAVING COUNT(*) > 1".format(",".join("?" * len(months))),
                months,
            ).fetchall()
            duplicates = [
                {
                    "customer": c,
                    "month": m,
                    "amount_usd": a,
                    "invoice_ids": ids.split(","),
                    "anomaly": True,
                }
                for c, m, a, ids in dupes
            ]
            return json.dumps(
                {
                    "quarter": args.quarter,
                    "series": series,
                    "duplicate_charges": duplicates,
                }
            )
        finally:
            conn.close()

    @registry.tool(
        name="prepare_refund_batch",
        description="Creates a refund batch for confirmed duplicate/erroneous invoices. Requires human approval.",
        args_schema=RefundBatchArgs,
        requires_approval=True,
        sandbox=False,
    )
    def prepare_refund_batch(ctx: RunContext, args: RefundBatchArgs) -> str:
        conn = _open_db(ctx)
        try:
            marks = ",".join("?" * len(args.invoice_ids))
            found = conn.execute(
                f"SELECT invoice_id, amount_usd FROM invoices WHERE status = 'paid' AND invoice_id IN ({marks})",
                args.invoice_ids,
            ).fetchall()
            missing = sorted(set(args.invoice_ids) - {r[0] for r in found})
            if missing:
                raise ToolRemediationError(
                    f"Unknown or non-refundable invoice ids: {missing}"
                )
            batch_id = f"rb_{uuid.uuid4().hex[:8]}"
            total = sum(r[1] for r in found)
            conn.execute(
                f"UPDATE invoices SET status = 'refunded' WHERE invoice_id IN ({marks})",
                args.invoice_ids,
            )
            conn.execute(
                "INSERT INTO refund_batches VALUES (?,?,?,?)",
                (batch_id, args.reason, json.dumps(args.invoice_ids), total),
            )
            conn.commit()
            return json.dumps(
                {"batch_id": batch_id, "invoice_count": len(found), "total_usd": total}
            )
        finally:
            conn.close()
