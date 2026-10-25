"""Incident triage tools: log analysis and deterministic severity classification.

Both tools are pure computation over data passed in the arguments (no network, no secrets), so they
run on the read-only sandbox queue.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field

from app.core.context import ToolRemediationError

TS_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:[.,]\d+)?\s*(Z|[+-]\d{2}:?\d{2})?"
)
LEVEL_RE = re.compile(
    r"\b(fatal|critical|error|err|warn(?:ing)?|info|debug|trace)\b", re.IGNORECASE
)
EXC_RE = re.compile(
    r"\b((?:[A-Za-z_][\w]*\.)*[A-Z][A-Za-z0-9_]*(?:Exception|Error|Timeout|Panic))\b"
)
LEVEL_NORMAL = {
    "fatal": "FATAL",
    "critical": "FATAL",
    "error": "ERROR",
    "err": "ERROR",
    "warn": "WARN",
    "warning": "WARN",
    "info": "INFO",
    "debug": "DEBUG",
    "trace": "DEBUG",
}
ERROR_LEVELS = ("FATAL", "ERROR")
NORMALIZERS = [
    (
        re.compile(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
        ),
        "<uuid>",
    ),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-fA-F]{12,}\b"), "<hex>"),
    (re.compile(r'"[^"]*"|\'[^\']*\''), "<str>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|m|h|kb|mb|gb|%)?\b", re.IGNORECASE), "<n>"),
]


def parse_ts(line: str) -> datetime | None:
    m = TS_RE.search(line)
    if not m:
        return None
    try:
        dt = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}")
    except ValueError:
        return None
    tz = m.group(3)
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        dt -= sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return dt  # naive, expressed in UTC when the log carried an offset


def signature(message: str) -> str:
    for pattern, repl in NORMALIZERS:
        message = pattern.sub(repl, message)
    return " ".join(message.split())[:160]


class AnalyzeLogArgs(BaseModel):
    log_text: str = Field(
        ...,
        min_length=1,
        max_length=200_000,
        description="Raw log lines pasted by the user (plain text, JSON-lines or logfmt with ISO timestamps).",
    )
    top_n: int = Field(
        8, ge=1, le=20, description="How many error signatures to return."
    )
    bucket_minutes: int = Field(
        5, ge=1, le=60, description="Width of the timeline buckets in minutes."
    )


class SeverityArgs(BaseModel):
    users_affected_pct: float = Field(
        ...,
        ge=0,
        le=100,
        description="Estimated % of active users affected right now (0-100).",
    )
    core_function_down: bool = Field(
        ...,
        description="True if a core user journey (login, checkout, API availability…) is unavailable.",
    )
    data_loss_or_security: bool = Field(
        False,
        description="True if data loss/corruption or a confirmed security breach is involved.",
    )
    revenue_impact: Literal["none", "minor", "moderate", "major"] = Field(
        "none", description="Business/revenue impact estimate."
    )
    workaround_available: bool = Field(
        False, description="True if users have a reasonable workaround."
    )
    duration_minutes: int = Field(
        0, ge=0, description="How long the impact has lasted so far, in minutes."
    )


RESPONSE = {
    "SEV1": {
        "response": "Page on-call immediately; assign incident commander, comms lead and scribe; open a bridge.",
        "update_every_minutes": 15,
        "postmortem": "required",
    },
    "SEV2": {
        "response": "Page on-call; assign incident commander; notify stakeholders.",
        "update_every_minutes": 30,
        "postmortem": "required",
    },
    "SEV3": {
        "response": "Handle in business hours by the owning team; track in the ticket queue.",
        "update_every_minutes": 240,
        "postmortem": "optional",
    },
    "SEV4": {
        "response": "Log as a bug/improvement; no incident process.",
        "update_every_minutes": None,
        "postmortem": "no",
    },
}


def classify(a: SeverityArgs) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if a.data_loss_or_security:
        reasons.append("data loss/corruption or security breach")
    if a.core_function_down and a.users_affected_pct >= 50:
        reasons.append(f"core function down for {a.users_affected_pct:g}% of users")
    if a.revenue_impact == "major":
        reasons.append("major revenue impact")
    if reasons:
        return "SEV1", reasons

    if a.core_function_down:
        reasons.append("core function down")
    if a.users_affected_pct >= 25:
        reasons.append(f"{a.users_affected_pct:g}% of users affected")
    if a.revenue_impact == "moderate":
        reasons.append("moderate revenue impact")
    if (
        a.duration_minutes >= 60
        and not a.workaround_available
        and a.users_affected_pct >= 10
    ):
        reasons.append(
            "prolonged (≥60 min) impact on ≥10% of users without a workaround"
        )
    if reasons:
        if (
            a.workaround_available
            and not a.core_function_down
            and a.revenue_impact != "moderate"
        ):
            return "SEV3", reasons + ["downgraded one level: workaround available"]
        return "SEV2", reasons

    if a.users_affected_pct >= 1 or a.revenue_impact == "minor":
        return "SEV3", [
            f"{a.users_affected_pct:g}% of users affected"
            if a.users_affected_pct >= 1
            else "minor revenue impact"
        ]
    return "SEV4", ["negligible user impact"]


def analyze(text: str, top_n: int, bucket_minutes: int) -> dict:
    levels: Counter[str] = Counter()
    sigs: dict[str, dict] = {}
    exceptions: Counter[str] = Counter()
    buckets: dict[datetime, Counter[str]] = defaultdict(Counter)
    first_ts = last_ts = first_error = None
    records = continuation = 0
    last_ts_seen: datetime | None = None

    for line in text.splitlines():
        if not line.strip():
            continue
        ts = parse_ts(line)
        m = LEVEL_RE.search(line)
        if ts is None and m is None:
            continuation += 1  # stack frames / wrapped output
            for exc in EXC_RE.findall(line):
                exceptions[exc] += 1
            continue
        records += 1
        level = LEVEL_NORMAL[m.group(1).lower()] if m else "INFO"
        levels[level] += 1
        ts = ts or last_ts_seen
        if ts:
            last_ts_seen = ts
            first_ts = min(first_ts or ts, ts)
            last_ts = max(last_ts or ts, ts)
            slot = ts - timedelta(minutes=ts.minute % bucket_minutes, seconds=ts.second)
            buckets[slot][level] += 1
        for exc in EXC_RE.findall(line):
            exceptions[exc] += 1
        if level in ERROR_LEVELS or level == "WARN":
            msg = line[m.end() :] if m else line
            sig = signature(TS_RE.sub("", msg).strip(" :-[]"))
            entry = sigs.setdefault(
                sig,
                {
                    "count": 0,
                    "level": level,
                    "first_seen": None,
                    "last_seen": None,
                    "example": line.strip()[:240],
                },
            )
            entry["count"] += 1
            if level in ERROR_LEVELS:
                entry["level"] = "ERROR"
            if ts:
                entry["first_seen"] = entry["first_seen"] or ts.isoformat()
                entry["last_seen"] = ts.isoformat()
            if (
                level in ERROR_LEVELS
                and ts
                and (first_error is None or ts < first_error)
            ):
                first_error = ts

    timeline = [
        {
            "bucket_start": t.replace(tzinfo=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "errors": sum(c[lv] for lv in ERROR_LEVELS),
            "warnings": c["WARN"],
            "total": sum(c.values()),
        }
        for t, c in sorted(buckets.items())
    ]
    spike = None
    if len(timeline) >= 3:
        baseline = statistics.median(b["errors"] for b in timeline)
        peak = max(timeline, key=lambda b: b["errors"])
        if peak["errors"] >= 5 and peak["errors"] >= 3 * max(baseline, 1):
            spike = {
                "bucket_start": peak["bucket_start"],
                "errors": peak["errors"],
                "median_errors_per_bucket": baseline,
            }

    ranked = sorted(
        sigs.items(), key=lambda kv: (kv[1]["level"] != "ERROR", -kv[1]["count"])
    )[:top_n]
    return {
        "records": records,
        "continuation_lines": continuation,
        "levels": dict(levels),
        "error_ratio": round(sum(levels[lv] for lv in ERROR_LEVELS) / records, 4)
        if records
        else 0,
        "time_range": {
            "start": first_ts.isoformat() if first_ts else None,
            "end": last_ts.isoformat() if last_ts else None,
        },
        "first_error_at": first_error.isoformat() if first_error else None,
        "top_signatures": [{"signature": s, **v} for s, v in ranked],
        "top_exceptions": exceptions.most_common(top_n),
        "spike": spike,
        "timeline": timeline[-24:],
        "timeline_truncated": len(timeline) > 24,
    }


def register(registry) -> None:
    @registry.tool(
        name="analyze_log_text",
        description=(
            "Analyses pasted application logs: level counts, time range, first error, error spikes per time bucket, "
            "and the most frequent error/warning signatures (numbers, ids and IPs normalised) with exception types. "
            "Pure computation, read-only."
        ),
        args_schema=AnalyzeLogArgs,
        sandbox=False,
    )
    def analyze_log_text(args: AnalyzeLogArgs) -> str:
        result = analyze(args.log_text, args.top_n, args.bucket_minutes)
        if result["records"] == 0:
            raise ToolRemediationError(
                "No log records recognised. Logs need a timestamp (ISO-8601) or a level (ERROR/WARN/INFO…) per line."
            )
        return json.dumps(result)

    @registry.tool(
        name="classify_incident_severity",
        description=(
            "Maps impact facts to an incident severity (SEV1-SEV4) with reasons, response expectations and update cadence "
            "using the deterministic severity matrix. Only call with facts the user provided or tools confirmed."
        ),
        args_schema=SeverityArgs,
        sandbox=False,
    )
    def classify_incident_severity(args: SeverityArgs) -> str:
        sev, reasons = classify(args)
        return json.dumps({"severity": sev, "reasons": reasons, **RESPONSE[sev]})
