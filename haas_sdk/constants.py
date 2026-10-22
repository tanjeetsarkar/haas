"""Single source of truth for statuses, event names, error codes and limits.

**SDK-CONST-01:** no other module may define status strings; import from here so
values cannot drift (for example ``awaiting_approval`` vs ``input_required``).
"""

from __future__ import annotations

RUN_STATUSES = frozenset(
    {
        "queued",
        "running",
        "input_required",
        "cancelling",
        "completed",
        "failed",
        "cancelled",
    }
)
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
# `input_required` is PAUSED, not active: the run waits for a human decision.
ACTIVE_STATUSES = frozenset({"queued", "running", "cancelling"})
TERMINAL_EVENTS = frozenset({"run_completed", "run_error", "run_cancelled"})
APPROVAL_STATUSES = frozenset(
    {"pending", "approved", "rejected", "expired", "cancelled"}
)
TOOL_OUTCOMES = frozenset({"succeeded", "failed", "unknown", "cancelled"})

RUN_ERROR_CODES = frozenset(
    {
        "budget.max_steps",
        "budget.max_cost",
        "budget.max_tool_invocations",
        "budget.deadline",
        "approval.expired",
        "recovery.exhausted",
        "execution.failed",
        "run.failed",
    }
)

ROLES = frozenset({"tenant_admin", "submitter", "approver", "auditor"})
PRINCIPAL_KINDS = frozenset({"human", "service"})
DECISIONS = frozenset({"approve", "reject"})

# Client-side mirrors of CreateRunRequest / AgentConfiguration bounds (SDK-RUN-04).
LIMITS: dict[str, tuple[float, float]] = {
    "max_cost_usd": (0.0, 10.0),
    "max_steps": (1, 50),
    "deadline_sec": (10, 86400),
    "max_tool_invocations": (1, 1000),
}

# Wire values for `settlement_status` on usage ledger entries.
SETTLEMENT_STATUSES = frozenset({"reserved", "settled", "released", "uncertain"})
