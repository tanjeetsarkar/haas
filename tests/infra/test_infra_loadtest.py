"""Load generator and SLO report tests (in-process fake API, no network)."""

from __future__ import annotations

import json
import random
from pathlib import Path

import httpx
import pytest

from loadtest.haas_load import (
    LoadConfig,
    LoadGenerator,
    Tenant,
    build_parser,
    config_from_args,
)
from scripts import slo_report


def _fake_api(state: dict) -> httpx.MockTransport:
    """Minimal emulation of POST /v1/runs, GET /v1/runs/{id} and its SSE stream."""

    def handler(request: httpx.Request) -> httpx.Response:
        tenant = request.headers.get("x-tenant-id", "")
        state.setdefault("tenants", []).append(tenant)
        state.setdefault("auth", []).append(request.headers.get("authorization"))
        path = request.url.path
        if request.method == "POST" and path == "/v1/runs":
            if tenant == "throttled":
                return httpx.Response(429, json={"detail": "quota"})
            body = json.loads(request.content)
            assert body["prompt"] and body["max_steps"] == 1
            run_id = f"run_{len(state.setdefault('runs', {}))}"
            state["runs"][run_id] = 0
            return httpx.Response(202, json={"run_id": run_id, "status": "queued"})
        if request.method == "GET" and path.endswith("/stream"):
            sse = (
                "id: 1\nevent: run_started\ndata: {}\n\n"
                "id: 2\nevent: output_delta\ndata: {\"text\": \"OK\"}\n\n"
                "id: 3\nevent: run_completed\ndata: {}\n\n"
            )
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        if request.method == "GET" and path.startswith("/v1/runs/"):
            run_id = path.rsplit("/", 1)[-1]
            state["runs"][run_id] += 1
            status = "completed" if state["runs"][run_id] >= 2 else "running"
            if state.get("fail_status_once") and state["runs"][run_id] == 1:
                return httpx.Response(503, json={"detail": "unavailable"})
            return httpx.Response(200, json={"run_id": run_id, "status": status})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_infra_load_generator_drives_runs_and_records_samples():
    state: dict = {}
    config = LoadConfig(
        base_url="http://haas.test",
        tenants=[Tenant("acme", 5.0, "tok-a"), Tenant("globex", 1.0)],
        duration_sec=30,
        concurrency=4,
        max_runs=24,
        stream_ratio=0.5,
        poll_interval_sec=0,
        seed=7,
    )
    results = await LoadGenerator(config, transport=_fake_api(state)).run()

    assert results["schema"] == "haas.loadtest.results/1"
    runs = results["runs"]
    assert len(runs) == 24
    assert {r["mode"] for r in runs} == {"stream", "poll"}
    assert all(r["final_status"] == "completed" for r in runs)
    assert all(r["end_to_end_ms"] is not None for r in runs)
    streamed = [r for r in runs if r["mode"] == "stream"]
    assert all(r["events"] == 3 and r["first_event_ms"] is not None for r in streamed)
    ops = {s["op"] for s in results["requests"]}
    assert ops == {"create_run", "get_run", "stream_open"}
    assert all(s["status_code"] == 200 or s["status_code"] == 202 for s in results["requests"])
    # Skew: acme (weight 5) should dominate globex (weight 1).
    tenants = [r["tenant"] for r in runs]
    assert tenants.count("acme") > tenants.count("globex")
    assert "Bearer tok-a" in state["auth"] and None in state["auth"]

    report = slo_report.build_report(results, {"min_samples": 1})
    assert report["overall"] == "pass"
    assert report["availability"]["value"] == 1.0


async def test_infra_load_generator_records_rejections_and_errors():
    state: dict = {"fail_status_once": True}
    config = LoadConfig(
        base_url="http://haas.test",
        tenants=[Tenant("throttled"), Tenant("acme")],
        duration_sec=30,
        concurrency=2,
        max_runs=10,
        stream_ratio=0.0,
        poll_interval_sec=0,
        seed=1,
    )
    results = await LoadGenerator(config, transport=_fake_api(state)).run()
    statuses = {r["final_status"] for r in results["runs"]}
    assert "rejected" in statuses and "completed" in statuses
    codes = {s["status_code"] for s in results["requests"]}
    assert {429, 503} <= codes

    report = slo_report.build_report(results, {"min_samples": 1})
    # 503s count against availability; 429s are excluded from the denominator.
    assert report["availability"]["failures"] > 0
    assert report["tenants"]["throttled"]["throttled"] > 0
    assert report["overall"] == "fail"


async def test_infra_load_generator_transport_errors_are_failures():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    config = LoadConfig(
        base_url="http://haas.test", tenants=[Tenant("a")], concurrency=1, max_runs=3
    )
    results = await LoadGenerator(config, transport=httpx.MockTransport(boom)).run()
    assert [s["error"] for s in results["requests"]] == ["ConnectError"] * 3
    assert all(s["status_code"] is None for s in results["requests"])
    report = slo_report.build_report(results, {"min_samples": 1})
    assert report["availability"]["value"] == 0.0


def test_infra_tenant_spec_parsing(monkeypatch):
    monkeypatch.setenv("LOAD_TOKEN", "secret-token")
    assert Tenant.parse("acme") == Tenant("acme", 1.0, None)
    assert Tenant.parse("acme:3") == Tenant("acme", 3.0, None)
    assert Tenant.parse("acme:2:env:LOAD_TOKEN") == Tenant("acme", 2.0, "secret-token")
    assert Tenant.parse("acme::raw") == Tenant("acme", 1.0, "raw")
    with pytest.raises(ValueError):
        Tenant.parse("acme:0")
    config = config_from_args(
        build_parser().parse_args(["--tenant", "a:4", "--tenant", "b", "--max-runs", "5"])
    )
    assert [t.tenant_id for t in config.tenants] == ["a", "b"] and config.max_runs == 5


def test_infra_percentiles_nearest_rank():
    values = list(range(1, 101))
    assert slo_report.percentile(values, 50) == 50
    assert slo_report.percentile(values, 95) == 95
    assert slo_report.percentile(values, 99) == 99
    assert slo_report.percentile([], 95) is None
    assert slo_report.percentile([7.0], 99) == 7.0


def _synthetic(latencies: dict[str, list[float]], errors: int = 0) -> dict:
    requests = [
        {"op": op, "tenant": "t", "latency_ms": v, "status_code": 200, "started_at": 0}
        for op, values in latencies.items()
        for v in values
    ]
    requests += [
        {"op": "get_run", "tenant": "t", "latency_ms": 5.0, "status_code": 500, "started_at": 0}
        for _ in range(errors)
    ]
    return {"requests": requests, "runs": []}


def test_infra_slo_report_pass_and_fail_thresholds():
    rng = random.Random(3)
    fast = [rng.uniform(10, 200) for _ in range(1000)]
    report = slo_report.build_report(_synthetic({"create_run": fast, "get_run": fast}))
    assert report["overall"] == "pass"
    names = {c["name"] for c in report["checks"]}
    assert {"availability", "create_run.p95_ms", "create_run.p99_ms", "get_run.p95_ms"} <= names

    slow_tail = fast[:900] + [rng.uniform(400, 900) for _ in range(100)]
    report = slo_report.build_report(_synthetic({"create_run": slow_tail}))
    failed = {c["name"] for c in report["checks"] if c["status"] == "fail"}
    assert failed == {"create_run.p95_ms"}

    # 2 failures in 1000 eligible requests breaks a 99.9% objective.
    report = slo_report.build_report(_synthetic({"get_run": fast[:998]}, errors=2))
    assert next(c for c in report["checks"] if c["name"] == "availability")["status"] == "fail"
    assert report["operations"]["get_run"]["error_rate"] == pytest.approx(0.002)


def test_infra_slo_report_insufficient_samples_and_overrides():
    report = slo_report.build_report(_synthetic({"create_run": [10.0] * 5}))
    assert report["overall"] == "insufficient_data"
    report = slo_report.build_report(
        _synthetic({"create_run": [50.0] * 50}),
        {"latency": {"create_run": {"p95_ms": 40.0}}},
    )
    check = next(c for c in report["checks"] if c["name"] == "create_run.p95_ms")
    assert check["status"] == "fail" and check["target"] == 40.0
    # Untouched defaults survive a partial override.
    assert next(c for c in report["checks"] if c["name"] == "create_run.p99_ms")["target"] == 1000.0


def test_infra_slo_report_cli(tmp_path: Path, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_synthetic({"get_run": [20.0] * 100})))
    assert slo_report.main([str(good)]) == 0
    assert "overall: **PASS**" in capsys.readouterr().out

    out = tmp_path / "report.json"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_synthetic({"get_run": [2000.0] * 100})))
    assert slo_report.main([str(bad), "--format", "json", "--out", str(out)]) == 1
    assert json.loads(out.read_text())["overall"] == "fail"

    sparse = tmp_path / "sparse.json"
    sparse.write_text(json.dumps(_synthetic({"get_run": [20.0]})))
    assert slo_report.main([str(sparse)]) == 1
    assert slo_report.main([str(sparse), "--allow-insufficient"]) == 0

    broken = tmp_path / "broken.json"
    broken.write_text("[1, 2]")
    assert slo_report.main([str(broken)]) == 2
