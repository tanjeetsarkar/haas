"""Compute SLO compliance from ``loadtest.haas_load`` results JSON.

Targets default to the initial engineering SLOs in
``docs/design/operations-migration.md`` §2 and can be overridden with ``--targets``
(a JSON file with the same shape as ``DEFAULT_TARGETS``). Exit status is ``0`` when
every SLO passes, ``1`` when any fails and ``2`` for unusable input.

Client-side measurements include network and gateway time, so they are a
conservative upper bound on the server-side SLI boundaries in the design doc.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

DEFAULT_TARGETS: dict[str, Any] = {
    "availability": {"objective": 0.999},
    "latency": {
        # Create/status/decision API latency: p95 < 300 ms, p99 < 1 s.
        "create_run": {"p95_ms": 300.0, "p99_ms": 1000.0},
        "get_run": {"p95_ms": 300.0, "p99_ms": 1000.0},
        "decision": {"p95_ms": 300.0, "p99_ms": 1000.0},
    },
    # Accepted work starts p95 < 5 s; first SSE event after admission is the
    # client-observable proxy (includes worker lease and first durable event).
    "first_event": {"p95_ms": 5000.0},
    "min_samples": 20,
}

# Authorized, non-quota-rejected requests form the availability denominator.
EXCLUDED_FROM_AVAILABILITY = frozenset({401, 403, 429})


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile; ``None`` for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return float(ordered[rank - 1])


def summarize_latency(values: Sequence[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "p50_ms": percentile(values, 50),
        "p95_ms": percentile(values, 95),
        "p99_ms": percentile(values, 99),
        "max_ms": max(values) if values else None,
    }


def _is_failure(sample: dict[str, Any]) -> bool:
    code = sample.get("status_code")
    return code is None or int(code) >= 500


def _counts_for_availability(sample: dict[str, Any]) -> bool:
    code = sample.get("status_code")
    return code is None or int(code) not in EXCLUDED_FROM_AVAILABILITY


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _merge_targets(overrides: dict[str, Any] | None) -> dict[str, Any]:
    return _deep_merge(json.loads(json.dumps(DEFAULT_TARGETS)), overrides or {})


def build_report(results: dict[str, Any], targets: dict[str, Any] | None = None) -> dict[str, Any]:
    targets = _merge_targets(targets)
    requests: list[dict[str, Any]] = list(results.get("requests", []))
    runs: list[dict[str, Any]] = list(results.get("runs", []))
    min_samples = int(targets.get("min_samples", 0))

    by_op: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in requests:
        by_op[str(sample.get("op", "unknown"))].append(sample)

    operations: dict[str, Any] = {}
    for op, samples in sorted(by_op.items()):
        ok = [float(s["latency_ms"]) for s in samples if not _is_failure(s)]
        status_counts: dict[str, int] = defaultdict(int)
        for s in samples:
            status_counts[str(s.get("status_code") or s.get("error") or "error")] += 1
        operations[op] = {
            **summarize_latency(ok),
            "requests": len(samples),
            "errors": sum(1 for s in samples if _is_failure(s)),
            "error_rate": _ratio(sum(1 for s in samples if _is_failure(s)), len(samples)),
            "status_counts": dict(sorted(status_counts.items())),
        }

    eligible = [s for s in requests if _counts_for_availability(s)]
    failures = sum(1 for s in eligible if _is_failure(s))
    availability = 1.0 - failures / len(eligible) if eligible else None

    checks: list[dict[str, Any]] = []
    objective = float(targets["availability"]["objective"])
    checks.append(
        _check(
            "availability",
            availability,
            objective,
            passed=None if availability is None else availability >= objective,
            samples=len(eligible),
            min_samples=min_samples,
            comparator=">=",
        )
    )
    for op, limits in sorted(targets.get("latency", {}).items()):
        stats = operations.get(op)
        if stats is None:
            continue
        for key, limit in sorted(limits.items()):
            value = stats.get(key)
            checks.append(
                _check(
                    f"{op}.{key}",
                    value,
                    float(limit),
                    passed=None if value is None else value < float(limit),
                    samples=stats["count"],
                    min_samples=min_samples,
                    comparator="<",
                )
            )
    first_events = [float(r["first_event_ms"]) for r in runs if r.get("first_event_ms") is not None]
    for key, limit in sorted(targets.get("first_event", {}).items()):
        if not first_events:
            break
        value = summarize_latency(first_events).get(key)
        checks.append(
            _check(
                f"first_event.{key}",
                value,
                float(limit),
                passed=None if value is None else value < float(limit),
                samples=len(first_events),
                min_samples=min_samples,
                comparator="<",
            )
        )

    run_status: dict[str, int] = defaultdict(int)
    for run in runs:
        run_status[str(run.get("final_status", "unknown"))] += 1
    tenants: dict[str, Any] = {}
    by_tenant: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in requests:
        by_tenant[str(sample.get("tenant", "unknown"))].append(sample)
    for tenant, samples in sorted(by_tenant.items()):
        ok = [float(s["latency_ms"]) for s in samples if not _is_failure(s)]
        tenants[tenant] = {
            **summarize_latency(ok),
            "requests": len(samples),
            "error_rate": _ratio(sum(1 for s in samples if _is_failure(s)), len(samples)),
            "throttled": sum(1 for s in samples if s.get("status_code") == 429),
        }

    verdicts = [c["status"] for c in checks]
    overall = "fail" if "fail" in verdicts else ("pass" if "pass" in verdicts else "insufficient_data")
    return {
        "schema": "haas.slo.report/1",
        "overall": overall,
        "checks": checks,
        "availability": {
            "eligible_requests": len(eligible),
            "failures": failures,
            "value": availability,
        },
        "operations": operations,
        "tenants": tenants,
        "runs": {
            "total": len(runs),
            "final_status": dict(sorted(run_status.items())),
            "end_to_end": summarize_latency(
                [float(r["end_to_end_ms"]) for r in runs if r.get("end_to_end_ms") is not None]
            ),
            "first_event": summarize_latency(first_events),
        },
        "meta": results.get("meta", {}),
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _check(
    name: str,
    value: float | None,
    target: float,
    *,
    passed: bool | None,
    samples: int,
    min_samples: int,
    comparator: str,
) -> dict[str, Any]:
    if passed is None or samples < min_samples:
        status = "insufficient_data"
    else:
        status = "pass" if passed else "fail"
    return {
        "name": name,
        "value": value,
        "target": target,
        "comparator": comparator,
        "samples": samples,
        "status": status,
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}" if value <= 1.0 else f"{value:.1f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    lines = [f"# SLO report — overall: **{report['overall'].upper()}**", ""]
    lines += ["| Check | Value | Target | Samples | Status |", "|---|---|---|---|---|"]
    for c in report["checks"]:
        lines.append(
            f"| {c['name']} | {_fmt(c['value'])} | {c['comparator']} {_fmt(c['target'])} "
            f"| {c['samples']} | {c['status']} |"
        )
    lines += ["", "## Operations", "", "| Op | Requests | Error rate | p50 ms | p95 ms | p99 ms |"]
    lines.append("|---|---|---|---|---|---|")
    for op, s in report["operations"].items():
        lines.append(
            f"| {op} | {s['requests']} | {_fmt(s['error_rate'])} | {_fmt(s['p50_ms'])} "
            f"| {_fmt(s['p95_ms'])} | {_fmt(s['p99_ms'])} |"
        )
    lines += ["", "## Tenants", "", "| Tenant | Requests | Error rate | 429s | p95 ms |"]
    lines.append("|---|---|---|---|---|")
    for tenant, s in report["tenants"].items():
        lines.append(
            f"| {tenant} | {s['requests']} | {_fmt(s['error_rate'])} | {s['throttled']} "
            f"| {_fmt(s['p95_ms'])} |"
        )
    runs = report["runs"]
    lines += ["", f"Runs: {runs['total']} — final status {json.dumps(runs['final_status'])}", ""]
    return "\n".join(lines)


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate load-test results against SLOs.")
    parser.add_argument("results", type=Path)
    parser.add_argument("--targets", type=Path, default=None)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--allow-insufficient",
        action="store_true",
        help="exit 0 when no check fails but some lack samples",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        results = _load_json(args.results)
        targets = _load_json(args.targets) if args.targets else None
    except (OSError, ValueError) as exc:
        print(f"slo_report: {exc}", file=sys.stderr)
        return 2
    report = build_report(results, targets)
    text = json.dumps(report, indent=2) if args.format == "json" else render_markdown(report)
    if args.out:
        args.out.write_text(text)
    else:
        print(text)
    if report["overall"] == "fail":
        return 1
    if report["overall"] == "insufficient_data" and not args.allow_insufficient:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
