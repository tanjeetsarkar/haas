from __future__ import annotations

import json
import socket
import sys

import httpx
import pytest

from app.core.config import settings
from app.core.context import SecurityViolationError, ToolRemediationError

CSV = """Order ID,Region,Order Date,Amount,Zip,Note
1,north,2026-01-05,100.5,02134,
2,south,2026-01-20,200,10001,rush
3,north,2026-02-03,50,02134,
4,north,2026-02-09,,02135,"quoted, comma"
4,north,2026-02-09,,02135,"quoted, comma"
"""


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "scratchpad_dir", tmp_path)


async def load(h, name="sales", csv_text=CSV, **kw):
    return json.loads(await h.call("dataset_load", name=name, csv_text=csv_text, **kw))


async def test_load_infers_types_and_normalises_columns(make_harness):
    h = make_harness("data_tools.py")
    out = await load(h)
    cols = {c["name"]: c["type"] for c in out["columns"]}
    assert cols == {
        "order_id": "INTEGER",
        "region": "TEXT",
        "order_date": "TEXT",
        "amount": "REAL",
        "zip": "TEXT",
        "note": "TEXT",
    }
    assert out["rows"] == 5 and out["delimiter"] == ","
    assert out["columns"][0]["original"] == "Order ID"
    q = json.loads(
        await h.call("dataset_query", sql="SELECT zip FROM sales WHERE order_id = 1")
    )
    assert q["rows"] == [["02134"]]  # leading zero preserved


async def test_semicolon_delimiter_and_duplicate_headers(make_harness):
    h = make_harness("data_tools.py")
    out = await load(h, "d", "a;a;b\n1;2;3\n4;5;6\n")
    assert [c["name"] for c in out["columns"]] == ["a", "a_2", "b"] and out[
        "delimiter"
    ] == ";"


async def test_profile_reports_quality(make_harness):
    h = make_harness("data_tools.py")
    await load(h)
    p = json.loads(await h.call("dataset_profile", name="sales"))
    amount = next(c for c in p["columns"] if c["name"] == "amount")
    assert p["rows"] == 5 and p["duplicate_row_groups"] == 1
    assert (
        amount["nulls"] == 2
        and amount["min"] == 50
        and amount["max"] == 200
        and amount["median"] == 100.5
    )
    region = next(c for c in p["columns"] if c["name"] == "region")
    assert region["top_values"][0] == ["north", 4]
    assert len(p["sample"]) == 5
    with pytest.raises(ToolRemediationError, match="Unknown dataset"):
        await h.call("dataset_profile", name="nope")


async def test_query_aggregates_and_joins_across_datasets(make_harness):
    h = make_harness("data_tools.py")
    await load(h)
    await load(h, "targets", "region,target\nnorth,150\nsouth,500\n")
    q = json.loads(
        await h.call(
            "dataset_query",
            sql="SELECT s.region, SUM(s.amount) AS rev, t.target FROM sales s JOIN targets t USING(region) GROUP BY s.region ORDER BY s.region",
        )
    )
    assert q["columns"] == ["region", "rev", "target"]
    assert q["rows"] == [["north", 150.5, 150], ["south", 200.0, 500]]
    listing = json.loads(await h.call("dataset_list"))
    assert listing["datasets"] == [
        {"name": "sales", "rows": 5},
        {"name": "targets", "rows": 2},
    ]


async def test_query_row_limit_flag(make_harness):
    h = make_harness("data_tools.py")
    await load(h)
    q = json.loads(await h.call("dataset_query", sql="SELECT * FROM sales", max_rows=2))
    assert q["row_count"] == 2 and q["truncated"] is True


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE sales",
        "DELETE FROM sales",
        "INSERT INTO sales (order_id) VALUES (9)",
        "UPDATE sales SET amount = 0",
        "ATTACH DATABASE '/tmp/x.db' AS x",
        "PRAGMA writable_schema = ON",
        "CREATE TABLE x (a)",
        "SELECT 1; DROP TABLE sales",
    ],
)
async def test_query_is_strictly_read_only(make_harness, sql):
    h = make_harness("data_tools.py")
    await load(h)
    with pytest.raises(ToolRemediationError):
        await h.call("dataset_query", sql=sql)
    still = json.loads(await h.call("dataset_query", sql="SELECT COUNT(*) FROM sales"))
    assert still["rows"] == [[5]]


async def test_runaway_query_is_interrupted(make_harness):
    h = make_harness("data_tools.py")
    await load(h)
    sys.modules["haas_plugin_data_tools"].QUERY_SECONDS = 0.2
    with pytest.raises(ToolRemediationError, match="exceeded"):
        await h.call(
            "dataset_query",
            sql="WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT COUNT(*) FROM c",
        )


async def test_datasets_are_isolated_per_tenant_and_thread(make_harness):
    a = make_harness("data_tools.py", tenant="acme", thread="thd_1")
    await load(a)
    for other in (
        make_harness("data_tools.py", tenant="globex", thread="thd_1"),
        make_harness("data_tools.py", tenant="acme", thread="thd_2"),
    ):
        with pytest.raises(ToolRemediationError, match="No datasets loaded"):
            await other.call("dataset_query", sql="SELECT * FROM sales")


async def test_name_collisions_and_validation(make_harness):
    h = make_harness("data_tools.py")
    await load(h)
    with pytest.raises(ToolRemediationError, match="already exists"):
        await load(h)
    assert (await load(h, replace=True))["rows"] == 5
    for bad in ("Sales", "1abc", "a-b", "x" * 40, 'a"; DROP'):
        with pytest.raises(ToolRemediationError, match="Invalid dataset name"):
            await load(h, bad)
    with pytest.raises(ToolRemediationError, match="exactly one"):
        await h.call("dataset_load", name="x")
    with pytest.raises(ToolRemediationError, match="No data rows"):
        await load(h, "empty", "a,b\n")


async def test_dataset_cap(make_harness):
    h = make_harness("data_tools.py")
    for i in range(10):
        await load(h, f"d{i}", "a\n1\n")
    with pytest.raises(ToolRemediationError, match="At most 10"):
        await load(h, "d10", "a\n1\n")


@pytest.fixture
def public_dns(monkeypatch):
    def fake(host, port, *a, **kw):
        ip = "10.0.0.5" if host.startswith("internal") else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


async def test_load_from_url_with_bom_and_redirect(make_harness, public_dns):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new.csv"})
        return httpx.Response(
            200,
            content="\ufeffa,b\n1,2\n".encode("utf-8"),
            headers={"content-type": "text/csv"},
        )

    h = make_harness("data_tools.py", handler)
    out = json.loads(
        await h.call("dataset_load", name="remote", url="https://data.example.com/old")
    )
    assert out["rows"] == 1 and out["columns"][0]["name"] == "a"


async def test_url_ssrf_size_and_http_errors(make_harness, public_dns):
    h = make_harness(
        "data_tools.py",
        lambda r: httpx.Response(
            301, headers={"location": "https://internal.example.com/x"}
        ),
    )
    with pytest.raises(SecurityViolationError):
        await h.call("dataset_load", name="x", url="https://data.example.com/a")
    with pytest.raises(SecurityViolationError):
        await h.call("dataset_load", name="x", url="http://127.0.0.1/a")
    h = make_harness(
        "data_tools.py",
        lambda r: httpx.Response(200, content=b"a\n" + b"1\n" * 3_000_000),
    )
    with pytest.raises(ToolRemediationError, match="larger than"):
        await h.call("dataset_load", name="x", url="https://data.example.com/big")
    h = make_harness("data_tools.py", lambda r: httpx.Response(404))
    with pytest.raises(ToolRemediationError, match="HTTP 404"):
        await h.call("dataset_load", name="x", url="https://data.example.com/missing")
