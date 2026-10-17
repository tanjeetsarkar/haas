"""End-to-end driver. Requires the stack from docker-compose.e2e.yml:

docker compose -f docker-compose.yml -f tests/e2e/docker-compose.e2e.yml up --build -d
python tests/e2e/run_e2e.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx

API = "http://localhost:8000"
MOCK = "http://localhost:9000"
SECRETS = {
    "ANALYTICS_DB_PATH": f"/tmp/haas-e2e-{uuid4().hex}.db",
    "BILLING_WEBHOOK_SECRET": "whsec_live_123",
}
TENANT = {"X-Tenant-ID": "fin-ops"}
FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILED.append(label)


def start(prompt: str, *, traceparent: str | None = None, **extra) -> str:
    headers = dict(TENANT)
    if traceparent:
        headers["traceparent"] = traceparent
    r = httpx.post(
        f"{API}/v1/runs",
        headers=headers,
        json={"prompt": prompt, "secrets": SECRETS, **extra},
    )
    assert r.status_code == 202, r.text
    return r.json()["run_id"]


def stream(run_id: str, last_id: str = "0-0", tenant: dict | None = None) -> list[dict]:
    events: list[dict] = []
    with httpx.stream(
        "GET",
        f"{API}/v1/runs/{run_id}/stream",
        headers={**(tenant or TENANT), "Last-Event-ID": last_id},
        timeout=90,
    ) as r:
        cur: dict = {}
        for line in r.iter_lines():
            if line.startswith("id:"):
                cur["id"] = line[3:].strip()
            elif line.startswith("event:"):
                cur["event"] = line[6:].strip()
            elif line.startswith("data:"):
                cur["data"] = json.loads(line[5:])
            elif not line and cur:
                events.append(cur)
                cur = {}
    return events


def resume(run_id: str, approved: bool, **kw) -> httpx.Response:
    return httpx.post(
        f"{API}/v1/runs/{run_id}/resume",
        headers=TENANT,
        json={"approved": approved, **kw},
    )


def run_status(run_id: str) -> dict:
    return httpx.get(f"{API}/v1/runs/{run_id}", headers=TENANT).json()


def wait_status(run_id: str, want: str, timeout: float = 60) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        s = run_status(run_id)
        if s["status"] == want:
            return s
        time.sleep(0.5)
    return run_status(run_id)


def kinds(events: list[dict]) -> list[str]:
    return [e["event"] for e in events]


def scenario_happy_path() -> None:
    print("\n[1] Happy path: parallel tools, two HITL gates, SSE resume")
    run_id = start(
        "Investigate Q3 revenue anomalies and prepare a refund batch if needed."
    )
    ev1 = stream(run_id)
    check(kinds(ev1)[-1] == "input_required", "stream ends at first input_required")
    check(
        kinds(ev1).count("tool_dispatched") == 3,
        "activate + 2 parallel tools dispatched",
    )
    queues = {
        e["data"]["tool"]: e["data"]["queue"]
        for e in ev1
        if e["event"] == "tool_dispatched"
    }
    check(
        queues.get("query_revenue_metrics") == "q_tools_sandbox"
        and queues.get("lookup_customer_ledger") == "q_tools_fast",
        f"queue routing per tool {queues}",
    )
    pending = ev1[-1]["data"]["pending_tool_calls"]
    check(
        [p["name"] for p in pending] == ["prepare_refund_batch"],
        "pending approval is prepare_refund_batch",
    )
    st = run_status(run_id)
    check(
        st["status"] == "input_required" and st["pending_tool_calls"],
        "status input_required with pending calls",
    )

    # Tenant isolation
    other = {"X-Tenant-ID": "someone-else"}
    check(
        httpx.get(f"{API}/v1/runs/{run_id}", headers=other).status_code == 404,
        "other tenant cannot read run",
    )
    check(
        httpx.post(
            f"{API}/v1/runs/{run_id}/resume", headers=other, json={"approved": True}
        ).status_code
        == 404,
        "other tenant cannot resume run",
    )

    # Concurrent double-approval: exactly one wins
    with ThreadPoolExecutor(2) as pool:
        codes = sorted(
            f.result().status_code
            for f in [pool.submit(resume, run_id, True) for _ in range(2)]
        )
    check(codes == [202, 409], f"double approval -> exactly one winner {codes}")

    ev2 = stream(run_id, last_id=ev1[-1]["id"])
    check(
        kinds(ev2)[0] == "hitl_approved",
        "SSE resumes after Last-Event-ID without replay",
    )
    check(
        kinds(ev2)[-1] == "input_required",
        "second approval gate (notify_billing_system)",
    )
    check(resume(run_id, True).status_code == 202, "second approval accepted")
    ev3 = stream(run_id, last_id=ev2[-1]["id"])
    check(kinds(ev3)[-1] == "run_completed", "run completes")
    done = ev3[-1]["data"]
    check(
        "billing notified" in done["output"] and '"accepted":true' in done["output"],
        "final output includes signed webhook acknowledgement",
    )
    check(
        abs(done["total_cost_usd"] - 0.002 * done["steps_used"]) < 1e-9,
        f"cost ledger sums per step {done['total_cost_usd']}",
    )
    final = run_status(run_id)
    check(
        final["status"] == "completed" and final["active_skills"] == ["revenue-ops"],
        "final status + active skill",
    )
    check(resume(run_id, True).status_code == 409, "resume on completed run -> 409")

    full = stream(run_id)  # full replay from 0-0 on a finished run must terminate
    check(kinds(full)[-1] == "run_completed", "full replay of finished run terminates")


def scenario_reject() -> None:
    print("\n[2] Operator rejection")
    run_id = start(
        "Investigate Q3 revenue anomalies and prepare a refund batch if needed."
    )
    ev = stream(run_id)
    check(kinds(ev)[-1] == "input_required", "paused for approval")
    r = resume(run_id, False, rejection_reason="Finance freeze")
    check(
        r.status_code == 202 and r.json()["action"] == "rejected_and_resumed",
        "rejection accepted",
    )
    st = wait_status(run_id, "completed")
    check(
        st["status"] == "completed" and "Finance freeze" in (st["result"] or ""),
        "agent saw rejection reason",
    )


def scenario_tool_error_and_planner_routing() -> None:
    print("\n[3] Locked tool -> error -> planner model re-engaged")
    run_id = start("BADTOOL please")
    ev = stream(run_id)
    res = [e for e in ev if e["event"] == "tool_result"]
    check(
        res
        and res[0]["data"]["is_error"]
        and "not unlocked" in res[0]["data"]["preview"],
        "locked tool call rejected by worker",
    )
    check(kinds(ev)[-1] == "run_completed", "run recovers and completes")
    models = [e["data"]["model"] for e in ev if e["event"] == "step_started"]
    check(
        models[0].endswith("deepseek-r1")
        and models[1].endswith("deepseek-r1")
        and len(models) == 2,
        f"planner used on step 1 and right after the error {models}",
    )


def scenario_loop_guard() -> None:
    print("\n[4] Identical-call circuit breaker + max_steps")
    run_id = start("LOOP forever", max_steps=5)
    ev = stream(run_id)
    check(
        kinds(ev)[-1] == "run_error" and "max_steps" in ev[-1]["data"]["error"],
        "run fails on max_steps budget",
    )
    previews = [e["data"]["preview"] for e in ev if e["event"] == "tool_result"]
    check(any("Circuit Breaker" in p for p in previews), "circuit breaker triggered")
    check(run_status(run_id)["status"] == "failed", "status failed")


def scenario_budget() -> None:
    print("\n[5] USD budget")
    run_id = start("Investigate Q3 revenue anomalies", max_cost_usd=0.003)
    ev = stream(run_id)
    check(
        kinds(ev)[-1] == "run_error" and "max_cost_usd" in ev[-1]["data"]["error"],
        "run fails on cost budget",
    )


def scenario_validation_and_webhook() -> None:
    print("\n[6] API validation + completion webhook")
    r = httpx.post(
        f"{API}/v1/runs",
        headers=TENANT,
        json={"prompt": "x", "preload_skills": ["nope"]},
    )
    check(r.status_code == 422, "unknown preload skill -> 422")
    r = httpx.post(
        f"{API}/v1/runs",
        headers=TENANT,
        json={"prompt": "x", "webhook_url": "http://127.0.0.1:1/x"},
    )
    check(r.status_code == 422, "SSRF-blocked webhook url -> 422")
    run_id = start("BADTOOL webhook", webhook_url="http://mock:9000/run-webhook")
    stream(run_id)
    time.sleep(2)
    hits = httpx.get(f"{MOCK}/_calls").json()["endpoints"]
    check(
        any(
            h["kind"] == "run-webhook" and h["payload"]["run_id"] == run_id
            for h in hits
        ),
        "completion webhook delivered",
    )


def scenario_cancel_and_cursor_listing() -> None:
    print("\n[7] Cancellation and tenant-scoped run listing")
    expected_trace_id = "a" * 32
    run_id = start(
        "CANCEL this run",
        traceparent=f"00-{expected_trace_id}-{'b' * 16}-01",
    )
    cancelled = httpx.post(f"{API}/v1/runs/{run_id}/cancel", headers=TENANT)
    check(
        cancelled.status_code == 202 and cancelled.json()["status"] == "cancelled",
        "active run cancellation accepted",
    )
    check(
        run_status(run_id)["status"] == "cancelled",
        "in-flight model completion cannot overwrite cancellation",
    )
    time.sleep(1.2)
    calls = httpx.get(f"{MOCK}/_calls").json()["llm"]
    traceparents = [call["traceparent"] for call in calls if call["traceparent"]]
    check(
        any(
            traceparent.split("-")[1] == expected_trace_id
            for traceparent in traceparents
        ),
        "trace context crosses API, Celery and the worker HTTP call",
    )
    check(
        httpx.post(f"{API}/v1/runs/{run_id}/cancel", headers=TENANT).status_code == 409,
        "terminal run cannot be cancelled twice",
    )
    first = httpx.get(f"{API}/v1/runs?limit=1", headers=TENANT)
    check(
        first.status_code == 200 and len(first.json()["items"]) == 1,
        "run list page",
    )
    cursor = first.json()["next_cursor"]
    check(cursor is not None, "run list provides a continuation cursor")
    second = httpx.get(f"{API}/v1/runs?limit=1&cursor={cursor}", headers=TENANT)
    check(
        second.status_code == 200
        and second.json()["items"][0]["run_id"] != first.json()["items"][0]["run_id"],
        "cursor advances without repeating a run",
    )
    other_tenant = httpx.get(
        f"{API}/v1/runs?limit=1", headers={"X-Tenant-ID": "someone-else"}
    )
    check(
        other_tenant.status_code == 200 and other_tenant.json()["items"] == [],
        "run list is tenant-scoped",
    )


def scenario_concurrency() -> None:
    print("\n[8] 6 concurrent runs reach their approval gate")
    ids = [start("Investigate Q3 revenue anomalies") for _ in range(6)]
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(stream, ids))
    check(
        all(kinds(ev)[-1] == "input_required" for ev in results),
        "all runs paused at HITL gate",
    )
    for rid in ids:
        resume(rid, False, rejection_reason="cleanup")
    check(
        all(wait_status(rid, "completed")["status"] == "completed" for rid in ids),
        "all runs finish after rejection",
    )


def scenario_infra() -> None:
    print("\n[9] Isolation & wire-level checks")
    env = subprocess.run(
        ["docker", "compose", "exec", "-T", "worker-tools", "env"],
        capture_output=True,
        text=True,
    ).stdout
    check(
        "OPENROUTER_API_KEY" not in env and "POSTGRES_DSN" not in env,
        "worker-tools has no LLM key / DB DSN",
    )
    env_o = subprocess.run(
        ["docker", "compose", "exec", "-T", "worker-orchestrator", "env"],
        capture_output=True,
        text=True,
    ).stdout
    check(
        "OPENROUTER_API_KEY=sk-or-v1-e2e-mock-key" in env_o,
        "orchestrator holds the LLM key",
    )
    write = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "worker-tools",
            "sh",
            "-c",
            "touch /app/x 2>&1; id -u",
        ],
        capture_output=True,
        text=True,
    ).stdout
    check(
        "Read-only" in write and "10001" in write,
        "worker-tools rootfs read-only, non-root",
    )
    keys = (
        subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "redis",
                "redis-cli",
                "--scan",
                "--pattern",
                "haas:secrets:*",
            ],
            capture_output=True,
            text=True,
        )
        .stdout.strip()
        .splitlines()
    )
    print(
        f"        (secret keys still in redis: {len(keys)} — only paused/active runs should remain)"
    )

    data = httpx.get(f"{MOCK}/_calls").json()
    llm, hits = data["llm"], data["endpoints"]
    check(
        all(c["auth"] == "Bearer sk-or-v1-e2e-mock-key" for c in llm),
        "OpenRouter bearer auth sent",
    )
    check(
        all(c["last_role"] in ("user", "tool") for c in llm),
        "message history well-formed",
    )
    mcp = [h for h in hits if h["kind"] == "mcp"]
    check(
        mcp
        and all(
            h["protocol"] == "2026-07-28"
            and h["method"] == "tools/call"
            and h["name"] == "lookup_customer_ledger"
            and "text/event-stream" in h["accept"]
            and h["meta_version"] == "2026-07-28"
            for h in mcp
        ),
        "MCP 2026-07-28 headers/_meta on the wire",
    )
    check(
        all(h["traceparent"] and h["traceparent"].startswith("00-") for h in mcp),
        "traceparent propagated to MCP",
    )
    wh = [h for h in hits if h["kind"] == "webhook"]
    check(
        wh and all(h["signature_valid"] for h in wh),
        "billing webhook HMAC + bearer verified",
    )
    ledger = subprocess.run(
        ["docker", "compose", "exec", "-T", "worker-tools", "sh", "-c", "ls /tmp"],
        capture_output=True,
        text=True,
    ).stdout
    check(
        any(
            name.startswith("haas-e2e-") and name.endswith(".db")
            for name in ledger.splitlines()
        ),
        "sandbox tool created analytics DB on tool worker tmpfs",
    )


def main() -> int:
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            response = httpx.get(f"{API}/readyz", timeout=2)
            if response.status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(1)
    else:
        raise RuntimeError("API did not become ready within 90 seconds")

    check(httpx.get(f"{API}/livez").json()["status"] == "ok", "liveness endpoint")
    check(
        httpx.get(f"{API}/readyz").json()["status"] == "ok",
        "readiness checks PostgreSQL and Redis",
    )
    for fn in (
        scenario_happy_path,
        scenario_reject,
        scenario_tool_error_and_planner_routing,
        scenario_loop_guard,
        scenario_budget,
        scenario_validation_and_webhook,
        scenario_cancel_and_cursor_listing,
        scenario_concurrency,
        scenario_infra,
    ):
        fn()
    print(f"\n{'ALL CHECKS PASSED' if not FAILED else 'FAILED: ' + '; '.join(FAILED)}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
