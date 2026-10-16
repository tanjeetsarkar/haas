"""Data-analyst tools: load a CSV into a per-conversation SQLite scratch database, profile it and query it.

Datasets live in `settings.scratchpad_dir/<tenant-hash>/<thread>.db` (the shared scratchpad volume), one
table per dataset, so later tool calls in the same conversation - and joins between datasets - work.
`dataset_query` is strictly read-only: single statement, SELECT-only authorizer, row and time limits.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urljoin

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.context import (
    RunContext,
    ToolRemediationError,
    validate_safe_outbound_url,
)

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_BYTES = 5_000_000
MAX_ROWS = 100_000
MAX_COLUMNS = 100
MAX_DATASETS = 10
QUERY_SECONDS = 5.0
SQLITE_SELECT, SQLITE_READ, SQLITE_FUNCTION, SQLITE_RECURSIVE = 21, 20, 31, 33
ALLOWED_ACTIONS = {SQLITE_SELECT, SQLITE_READ, SQLITE_FUNCTION, SQLITE_RECURSIVE}


def _db_path(ctx: RunContext) -> Path:
    tenant = hashlib.sha256(ctx.tenant_id.encode()).hexdigest()[:16]
    thread = re.sub(r"[^A-Za-z0-9_-]", "_", ctx.thread_id)[:64]
    folder = Path(settings.scratchpad_dir) / "datasets" / tenant
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{thread}.db"


def _check_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise ToolRemediationError(
            f"Invalid dataset name '{name}': lowercase letters, digits and underscores, starting with a letter (max 32), e.g. 'sales_q3'."
        )
    return name


def _column_names(header: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for i, raw in enumerate(header):
        col = (
            re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_") or f"col_{i + 1}"
        )
        if col[0].isdigit():
            col = f"c_{col}"
        n = seen.get(col, 0)
        seen[col] = n + 1
        out.append(col if n == 0 else f"{col}_{n + 1}")
    return out


def _as_int(v: str) -> int | None:
    if re.fullmatch(
        r"[+-]?(0|[1-9]\d{0,17})", v
    ):  # leading zeros (zip codes, ids) stay text
        return int(v)
    return None


def _as_float(v: str) -> float | None:
    if re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", v) and not re.fullmatch(
        r"[+-]?0\d+", v
    ):
        return float(v)
    return None


def _infer(values: list[str]) -> str:
    present = [v for v in values if v != ""]
    if present and all(_as_int(v) is not None for v in present):
        return "INTEGER"
    if present and all(_as_float(v) is not None for v in present):
        return "REAL"
    return "TEXT"


def _ingest(path: Path, name: str, text: str, replace: bool) -> dict:
    sample = text[:4096]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        raise ToolRemediationError(
            "The data is empty; it needs a header row followed by data rows."
        )
    if len(header) > MAX_COLUMNS:
        raise ToolRemediationError(f"Too many columns ({len(header)} > {MAX_COLUMNS}).")
    cols = _column_names(header)
    rows, truncated = [], False
    for row in reader:
        if not row or all(not c.strip() for c in row):
            continue
        if len(rows) >= MAX_ROWS:
            truncated = True
            break
        row = [c.strip() for c in row[: len(cols)]]
        rows.append(row + [""] * (len(cols) - len(row)))
    if not rows:
        raise ToolRemediationError("No data rows found after the header.")

    types = [_infer([r[i] for r in rows]) for i in range(len(cols))]
    conv = {"INTEGER": _as_int, "REAL": _as_float, "TEXT": lambda v: v}

    conn = sqlite3.connect(path)
    try:
        existing = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if name in existing and not replace:
            raise ToolRemediationError(
                f"Dataset '{name}' already exists. Use another name or set replace=true."
            )
        if name not in existing and len(existing) >= MAX_DATASETS:
            raise ToolRemediationError(
                f"At most {MAX_DATASETS} datasets per conversation; reuse or replace one."
            )
        conn.execute(f'DROP TABLE IF EXISTS "{name}"')
        conn.execute(
            f'CREATE TABLE "{name}" ({", ".join(f"{c} {t}" for c, t in zip(cols, types))})'
        )
        conn.executemany(
            f'INSERT INTO "{name}" VALUES ({",".join("?" * len(cols))})',
            (
                [(conv[t](v) if v != "" else None) for v, t in zip(r, types)]
                for r in rows
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "dataset": name,
        "rows": len(rows),
        "truncated_at_row_limit": truncated,
        "columns": [
            {"name": c, "type": t, "original": h}
            for c, t, h in zip(cols, types, header)
        ],
        "delimiter": delimiter,
    }


def _open_ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise ToolRemediationError(
            "No datasets loaded in this conversation yet. Call `dataset_load` first."
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _profile(path: Path, name: str) -> dict:
    conn = _open_ro(path)
    try:
        info = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
        if not info:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
            raise ToolRemediationError(
                f"Unknown dataset '{name}'. Loaded datasets: {tables}."
            )
        total = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        columns = []
        for _, col, ctype, *_rest in info[:40]:
            q = f'"{col}"'
            nulls, distinct, mn, mx = conn.execute(
                f'SELECT SUM({q} IS NULL), COUNT(DISTINCT {q}), MIN({q}), MAX({q}) FROM "{name}"'
            ).fetchone()
            entry: dict = {
                "name": col,
                "type": ctype,
                "nulls": nulls or 0,
                "distinct": distinct,
                "min": mn,
                "max": mx,
            }
            if ctype in ("INTEGER", "REAL") and total - (nulls or 0) > 0:
                mean = conn.execute(f'SELECT AVG({q}) FROM "{name}"').fetchone()[0]
                n = total - (nulls or 0)
                median = conn.execute(
                    f'SELECT {q} FROM "{name}" WHERE {q} IS NOT NULL ORDER BY {q} LIMIT 1 OFFSET ?',
                    (n // 2,),
                ).fetchone()[0]
                entry.update(mean=round(mean, 4), median=median)
            else:
                entry["top_values"] = [
                    [v, c]
                    for v, c in conn.execute(
                        f'SELECT {q}, COUNT(*) c FROM "{name}" WHERE {q} IS NOT NULL GROUP BY {q} ORDER BY c DESC, {q} LIMIT 5'
                    )
                ]
            if entry["min"] is not None and isinstance(entry["min"], str):
                entry["min"], entry["max"] = entry["min"][:60], str(entry["max"])[:60]
            columns.append(entry)
        cur = conn.execute(f'SELECT * FROM "{name}" LIMIT 5')
        sample = [dict(zip([d[0] for d in cur.description], row)) for row in cur]
        all_cols = ", ".join(f'"{c[1]}"' for c in info)
        dup = conn.execute(
            f'SELECT COUNT(*) FROM (SELECT 1 FROM "{name}" GROUP BY {all_cols} HAVING COUNT(*) > 1)'
        ).fetchone()[0]
        return {
            "dataset": name,
            "rows": total,
            "duplicate_row_groups": dup,
            "columns": columns,
            "sample": sample,
        }
    finally:
        conn.close()


def _authorizer(action, *_args):
    return sqlite3.SQLITE_OK if action in ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def _query(path: Path, sql: str, max_rows: int) -> dict:
    conn = _open_ro(path)
    deadline = time.monotonic() + QUERY_SECONDS
    conn.set_authorizer(_authorizer)
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(max_rows + 1)
    except sqlite3.OperationalError as exc:
        msg = str(exc)
        if "interrupted" in msg:
            raise ToolRemediationError(
                f"Query exceeded {QUERY_SECONDS:g}s; add filters, LIMIT or simplify the joins."
            )
        if "not authorized" in msg or "readonly" in msg:
            raise ToolRemediationError("Only read-only SELECT queries are allowed.")
        raise ToolRemediationError(
            f"SQL error: {msg}. Check table/column names with dataset_profile."
        )
    except (sqlite3.Warning, sqlite3.DatabaseError) as exc:
        raise ToolRemediationError(
            f"SQL error: {exc}. Send exactly one SELECT statement."
        )
    finally:
        conn.close()
    truncated = len(rows) > max_rows
    return {
        "columns": cols,
        "rows": [list(r) for r in rows[:max_rows]],
        "row_count": min(len(rows), max_rows),
        "truncated": truncated,
    }


async def _download(ctx: RunContext, url: str) -> str:
    assert ctx.http_client is not None
    current = url.strip()
    for _ in range(6):
        await asyncio.to_thread(validate_safe_outbound_url, current, resolve_dns=True)
        async with ctx.http_client.stream(
            "GET",
            current,
            headers={"Accept": "text/csv,text/plain,*/*;q=0.5"},
            timeout=20.0,
            follow_redirects=False,
        ) as resp:
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get(
                "location"
            ):
                current = urljoin(current, resp.headers["location"])
                continue
            if resp.status_code >= 400:
                raise ToolRemediationError(
                    f"Download failed with HTTP {resp.status_code} for {current}."
                )
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise ToolRemediationError(
                        f"File is larger than {MAX_BYTES // 1_000_000} MB; load a smaller extract."
                    )
            return bytes(body).decode(resp.encoding or "utf-8-sig", errors="replace")
    raise ToolRemediationError("Too many redirects.")


class LoadArgs(BaseModel):
    name: str = Field(
        ...,
        description="Dataset (table) name: lowercase letters/digits/underscores, e.g. 'sales_q3'.",
    )
    csv_text: str | None = Field(
        None,
        max_length=1_000_000,
        description="CSV content with a header row (use for small pasted data).",
    )
    url: str | None = Field(
        None,
        description="Public http(s) URL of a CSV file (up to 5 MB) instead of csv_text.",
    )
    replace: bool = Field(
        False, description="Overwrite an existing dataset with the same name."
    )


class NameArgs(BaseModel):
    name: str = Field(..., description="Name of a loaded dataset.")


class ListArgs(BaseModel):
    pass


class QueryArgs(BaseModel):
    sql: str = Field(
        ...,
        min_length=6,
        max_length=4000,
        description="One read-only SQLite SELECT statement; tables are the loaded dataset names (joins allowed).",
    )
    max_rows: int = Field(50, ge=1, le=200, description="Maximum rows to return.")


def register(registry) -> None:
    @registry.tool(
        name="dataset_load",
        description=(
            "Loads CSV data (pasted text or a public URL) into a SQLite table for this conversation, inferring "
            "column types. Creates scratch data only; does not modify any external system."
        ),
        args_schema=LoadArgs,
        sandbox=True,
    )
    async def dataset_load(ctx: RunContext, args: LoadArgs) -> str:
        name = _check_name(args.name)
        if bool(args.csv_text) == bool(args.url):
            raise ToolRemediationError("Provide exactly one of csv_text or url.")
        text = args.csv_text if args.csv_text else await _download(ctx, args.url or "")
        path = _db_path(ctx)
        return json.dumps(
            await asyncio.to_thread(
                _ingest, path, name, text.lstrip("\ufeff"), args.replace
            )
        )

    @registry.tool(
        name="dataset_list",
        description="Lists the datasets loaded in this conversation with row counts. Read-only.",
        args_schema=ListArgs,
        sandbox=True,
    )
    def dataset_list(ctx: RunContext, args: ListArgs) -> str:
        conn = _open_ro(_db_path(ctx))
        try:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            return json.dumps(
                {
                    "datasets": [
                        {
                            "name": t,
                            "rows": conn.execute(
                                f'SELECT COUNT(*) FROM "{t}"'
                            ).fetchone()[0],
                        }
                        for t in tables
                    ]
                }
            )
        finally:
            conn.close()

    @registry.tool(
        name="dataset_profile",
        description=(
            "Profiles a loaded dataset: row count, duplicate rows, and per column type, nulls, distinct values, "
            "min/max, mean/median (numeric) or top values (text), plus 5 sample rows. Read-only."
        ),
        args_schema=NameArgs,
        sandbox=True,
    )
    def dataset_profile(ctx: RunContext, args: NameArgs) -> str:
        return json.dumps(_profile(_db_path(ctx), _check_name(args.name)))

    @registry.tool(
        name="dataset_query",
        description=(
            "Runs ONE read-only SQLite SELECT over the loaded datasets (aggregations, filters, joins, window functions) "
            "and returns columns and rows (limited). Writes, PRAGMA and ATTACH are rejected."
        ),
        args_schema=QueryArgs,
        sandbox=True,
    )
    def dataset_query(ctx: RunContext, args: QueryArgs) -> str:
        return json.dumps(_query(_db_path(ctx), args.sql, args.max_rows), default=str)
