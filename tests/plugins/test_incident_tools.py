from __future__ import annotations

import json

import pytest

from app.core.context import ToolRemediationError

LOG = """\
2026-10-16T10:00:01Z INFO  api request ok id=1 took 20ms
2026-10-16T10:00:30Z INFO  api request ok id=2 took 22ms
2026-10-16T10:05:10Z ERROR db connection refused host=10.0.0.5:5432 attempt 1
2026-10-16T10:05:11Z ERROR db connection refused host=10.0.0.6:5432 attempt 2
2026-10-16T10:10:01Z WARN  slow query took 3500ms for user 'alice'
2026-10-16T10:10:02Z ERROR db connection refused host=10.0.0.5:5432 attempt 3
Traceback (most recent call last):
  File "app.py", line 3, in handler
psycopg2.OperationalError: could not connect
2026-10-16T10:10:03Z ERROR db connection refused host=10.0.0.5:5432 attempt 4
2026-10-16T10:10:04Z ERROR db connection refused host=10.0.0.5:5432 attempt 5
2026-10-16T10:10:05Z ERROR db connection refused host=10.0.0.9:5432 attempt 6
2026-10-16T10:10:06Z ERROR db connection refused host=10.0.0.5:5432 attempt 7
2026-10-16T10:10:07Z ERROR db connection refused host=10.0.0.5:5432 attempt 8
"""


def test_tools_are_sandboxed_and_unsecreted(make_harness):
    h = make_harness("incident_tools.py")
    assert {t.queue for t in h.reg.tools.values()} == {"q_tools_sandbox"}
    assert not any(t.requires_approval for t in h.reg.tools.values())


async def test_analyze_log_text_finds_signature_spike_and_first_error(make_harness):
    h = make_harness("incident_tools.py")
    out = json.loads(await h.call("analyze_log_text", log_text=LOG))
    assert out["records"] == 11 and out["continuation_lines"] == 3
    assert out["levels"] == {"INFO": 2, "ERROR": 8, "WARN": 1}
    assert out["first_error_at"] == "2026-10-16T10:05:10"
    top = out["top_signatures"][0]
    assert (
        top["signature"] == "db connection refused host=<ip> attempt <n>"
        and top["count"] == 8
    )
    assert top["first_seen"] == "2026-10-16T10:05:10" and top["level"] == "ERROR"
    assert (
        out["spike"]["bucket_start"] == "2026-10-16T10:10:00Z"
        and out["spike"]["errors"] == 6
    )
    assert ["psycopg2.OperationalError", 1] in out["top_exceptions"]
    assert out["top_signatures"][-1]["level"] == "WARN"
    assert [b["bucket_start"] for b in out["timeline"]] == [
        "2026-10-16T10:00:00Z",
        "2026-10-16T10:05:00Z",
        "2026-10-16T10:10:00Z",
    ]


async def test_analyze_handles_offsets_and_bucket_width(make_harness):
    log = "2026-10-16 12:30:00+02:00 ERROR boom\n2026-10-16 10:31:00Z ERROR boom\n"
    h = make_harness("incident_tools.py")
    out = json.loads(await h.call("analyze_log_text", log_text=log, bucket_minutes=1))
    assert [b["bucket_start"] for b in out["timeline"]] == [
        "2026-10-16T10:30:00Z",
        "2026-10-16T10:31:00Z",
    ]


async def test_analyze_rejects_unrecognised_text(make_harness):
    h = make_harness("incident_tools.py")
    with pytest.raises(ToolRemediationError, match="No log records"):
        await h.call(
            "analyze_log_text", log_text="just some prose\nwithout structure\n"
        )


@pytest.mark.parametrize(
    "kwargs,sev",
    [
        (dict(users_affected_pct=80, core_function_down=True), "SEV1"),
        (
            dict(
                users_affected_pct=1,
                core_function_down=False,
                data_loss_or_security=True,
            ),
            "SEV1",
        ),
        (
            dict(
                users_affected_pct=5, core_function_down=False, revenue_impact="major"
            ),
            "SEV1",
        ),
        (dict(users_affected_pct=30, core_function_down=False), "SEV2"),
        (dict(users_affected_pct=10, core_function_down=True), "SEV2"),
        (
            dict(users_affected_pct=12, core_function_down=False, duration_minutes=90),
            "SEV2",
        ),
        (
            dict(
                users_affected_pct=30,
                core_function_down=False,
                workaround_available=True,
            ),
            "SEV3",
        ),
        (
            dict(
                users_affected_pct=30,
                core_function_down=True,
                workaround_available=True,
            ),
            "SEV2",
        ),
        (dict(users_affected_pct=2, core_function_down=False), "SEV3"),
        (
            dict(
                users_affected_pct=0, core_function_down=False, revenue_impact="minor"
            ),
            "SEV3",
        ),
        (dict(users_affected_pct=0.2, core_function_down=False), "SEV4"),
    ],
)
async def test_severity_matrix(make_harness, kwargs, sev):
    h = make_harness("incident_tools.py")
    out = json.loads(await h.call("classify_incident_severity", **kwargs))
    assert out["severity"] == sev and out["reasons"]
    assert (
        out["postmortem"]
        == {"SEV1": "required", "SEV2": "required", "SEV3": "optional", "SEV4": "no"}[
            sev
        ]
    )
    if sev == "SEV1":
        assert out["update_every_minutes"] == 15
