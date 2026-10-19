from __future__ import annotations

import json
import difflib
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from haas_cli.client import HaasAPIError, HaasClient, SSEEvent
from haas_cli.config import ChatSettings


@dataclass
class PendingCall:
    id: str
    name: str
    arguments: Any

    @property
    def pretty_args(self) -> str:
        raw = self.arguments
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return raw
        return json.dumps(raw, indent=2, ensure_ascii=False)


@dataclass
class ApprovalBinding:
    """The server-issued identity of a pending approval; decisions are bound to it."""

    batch_id: str
    batch_digest: str
    expires_at: str | None = None

    @classmethod
    def parse(cls, data: Any) -> ApprovalBinding | None:
        if not isinstance(data, dict):
            return None
        batch_id, digest = data.get("batch_id"), data.get("batch_digest")
        if not isinstance(batch_id, str) or not isinstance(digest, str):
            return None
        expires = data.get("expires_at")
        return cls(batch_id, digest, expires if isinstance(expires, str) else None)


@dataclass
class Decision:
    approved: bool
    reason: str | None = None


@dataclass
class TurnResult:
    status: str  # completed | failed | paused | cancelled
    output: str = ""
    cost_usd: float = 0.0
    run_id: str | None = None


EventHandler = Callable[[SSEEvent], Awaitable[None] | None]
ApprovalHandler = Callable[[list[PendingCall]], Awaitable[Decision | None]]


@dataclass
class ChatSession:
    client: HaasClient
    settings: ChatSettings
    total_cost_usd: float = 0.0
    last_run_id: str | None = None
    turns: int = 0
    active_skills: list[str] = field(default_factory=list)
    skill_names: list[str] = field(default_factory=list)  # cached for completion
    _costed_runs: set[str] = field(default_factory=set, repr=False)
    thread_cursor: str | None = None
    thread_search: str = ""
    agent_cursor: str | None = None
    pending_approval: ApprovalBinding | None = None
    # Request advisory live output deltas; only enabled when the server advertises them.
    output_deltas: bool = False
    live_output: bool = True

    def __post_init__(self) -> None:
        self.client.set_credential(self.settings.credential)

    async def plugins(self) -> dict[str, Any]:
        data = await self.client.plugins()
        self.skill_names = [s["name"] for s in data["skills"]]
        return data

    def new_conversation(self) -> None:
        self.settings.thread_id = None
        self.settings.history_version = None
        self.settings.agent_revision_id = None
        self.thread_cursor = None
        self.thread_search = ""
        self.agent_cursor = None
        self.total_cost_usd = 0.0
        self.turns = 0
        self.last_run_id = None
        self.active_skills = []
        self._costed_runs.clear()

    async def run_turn(
        self,
        prompt: str,
        on_event: EventHandler,
        on_approval: ApprovalHandler,
    ) -> TurnResult:
        """
        Starts a run and drives it to completion, answering every human-approval gate
        through `on_approval`. Every server event is forwarded to `on_event`.
        """
        s = self.settings
        revision_options: dict[str, Any] = {}
        if s.agent_revision_id:
            revision_options["agent_revision_id"] = s.agent_revision_id
        if s.history_version is not None:
            revision_options["expected_history_version"] = s.history_version
        started = await self.client.create_run(
            prompt,
            thread_id=s.thread_id,
            preload_skills=s.preload_skills,
            primary_model=s.primary_model,
            planner_model=s.planner_model,
            max_steps=s.max_steps,
            max_cost_usd=s.max_cost_usd,
            secrets=s.secrets,
            **revision_options,
        )
        run_id = started["run_id"]
        s.thread_id = started["thread_id"]
        self.last_run_id = run_id
        self.turns += 1
        s.history_version = None
        return await self._follow_run(run_id, on_event, on_approval)

    async def attach_run(
        self,
        run_id: str,
        on_event: EventHandler,
        on_approval: ApprovalHandler,
    ) -> TurnResult:
        run = await self.client.get_run(run_id)
        self.settings.thread_id = run["thread_id"]
        self.last_run_id = run_id
        self.active_skills = list(run.get("active_skills") or [])
        return await self._follow_run(run_id, on_event, on_approval)

    def _record_cost(self, run_id: str, cost: float) -> None:
        if run_id not in self._costed_runs:
            self.total_cost_usd += cost
            self._costed_runs.add(run_id)

    async def _follow_run(
        self,
        run_id: str,
        on_event: EventHandler,
        on_approval: ApprovalHandler,
    ) -> TurnResult:
        async def emit(ev: SSEEvent) -> None:
            res = on_event(ev)
            if res is not None:
                await res

        cursor = "0"
        while True:
            paused: SSEEvent | None = None
            self.pending_approval = None
            async for ev in self.client.stream_events(
                run_id, cursor, output_deltas=self.output_deltas and self.live_output
            ):
                if ev.id:
                    cursor = ev.id
                if ev.event == "step_started":
                    self.active_skills = list(ev.data.get("active_skills", []))
                await emit(ev)
                if ev.event == "run_completed":
                    cost = float(ev.data.get("total_cost_usd", 0.0))
                    self._record_cost(run_id, cost)
                    return TurnResult(
                        "completed", str(ev.data.get("output", "")), cost, run_id
                    )
                if ev.event == "run_error":
                    return TurnResult(
                        "failed", str(ev.data.get("error", "")), 0.0, run_id
                    )
                if ev.event == "run_cancelled":
                    run = await self.client.get_run(run_id)
                    cost = float(run.get("total_cost_usd") or 0.0)
                    self._record_cost(run_id, cost)
                    return TurnResult("cancelled", "Run cancelled.", cost, run_id)
                if ev.event == "input_required":
                    paused = ev
                    self.pending_approval = ApprovalBinding.parse(ev.data)
            if paused is None:
                run = await self.client.get_run(run_id)
                if run["status"] == "input_required":
                    pending_calls: list[dict[str, Any]] = []
                    for call in run.get("pending_tool_calls") or []:
                        function = call.get("function") or {}
                        pending_calls.append(
                            {
                                "id": call["id"],
                                "name": function.get("name", call.get("name", "tool")),
                                "arguments": function.get(
                                    "arguments", call.get("arguments")
                                ),
                            }
                        )
                    paused = SSEEvent(
                        "input_required", {"pending_tool_calls": pending_calls}
                    )
                    self.pending_approval = ApprovalBinding.parse(run.get("approval"))
                else:
                    cost = float(run.get("total_cost_usd") or 0.0)
                    self._record_cost(run_id, cost)
                    if run["status"] == "completed":
                        output = str(run.get("result") or "")
                        await emit(
                            SSEEvent(
                                "run_completed",
                                {
                                    "output": output,
                                    "steps_used": run.get("step_count", 0),
                                    "total_cost_usd": cost,
                                },
                            )
                        )
                        return TurnResult("completed", output, cost, run_id)
                    if run["status"] == "cancelled":
                        message = str(run.get("result") or "Run cancelled.")
                        await emit(SSEEvent("run_cancelled", {"run_id": run_id}))
                        return TurnResult("cancelled", message, cost, run_id)
                    error = str(
                        run.get("result") or f"Run ended with status '{run['status']}'."
                    )
                    await emit(SSEEvent("run_error", {"error": error}))
                    return TurnResult("failed", error, cost, run_id)

            calls = [
                PendingCall(c["id"], c["name"], c.get("arguments"))
                for c in paused.data.get("pending_tool_calls", [])
            ]
            decision = await on_approval(calls)
            if decision is None:
                return TurnResult("paused", run_id=run_id)
            binding = self.pending_approval
            try:
                await self.client.resume(
                    run_id,
                    decision.approved,
                    decision.reason,
                    secrets=self.settings.secrets,
                    batch_id=binding.batch_id if binding else None,
                    batch_digest=binding.batch_digest if binding else None,
                )
            except HaasAPIError as exc:
                # 409: already decided elsewhere, or the batch changed since review; the
                # durable event log shows which, so keep following from the cursor.
                if exc.status != 409:
                    raise
            finally:
                self.pending_approval = None


@dataclass
class CommandResult:
    lines: list[str] = field(default_factory=list)
    quit: bool = False
    clear: bool = False
    new_conversation: bool = False
    error: bool = False


COMMAND_HELP: list[tuple[str, str]] = [
    ("/help", "Show this help"),
    ("/agents [next]", "Browse revision pages"),
    (
        "/agent show|validate|publish|use ID",
        "Inspect, validate, publish or pin a revision; /agent clear unpins",
    ),
    ("/agent draft", "Open structured agent configuration form (TUI)"),
    ("/agent diff ID ID", "Compare revision configuration and permissions"),
    ("/threads [search|next]", "Browse persistent threads by ID"),
    ("/history [offset]", "Read a version-pinned page of the current thread"),
    ("/branch VERSION", "Branch committed history at an explicit version"),
    ("/login", "Sign in with an API key or OIDC bearer token (memory only)"),
    ("/logout", "Clear the in-memory credential"),
    ("/tenant [id]", "Verify and switch the authenticated tenant"),
    ("/profile [name]", "Switch to a saved environment profile"),
    ("/admin", "Open role-authorized tenant administration and audit"),
    ("/approve <run-id>", "Review and resolve a pending run approval as an approver"),
    (
        "/approvals [STATUS]",
        "Approval inbox: pending (default), approved, rejected, expired, all",
    ),
    (
        "/inspect [run-id]",
        "Run inspector: state, budget, steps, tool invocations and usage",
    ),
    (
        "/events [run-id] [after] [type=TEXT]",
        "Page or search the durable, replayable run event log",
    ),
    ("/status", "Connection, thread, models, budgets, secrets (names only)"),
    ("/live [on|off]", "Advisory live output preview (when the server offers it)"),
    ("/skills", "List installed skills"),
    ("/tools", "List registered tools (⚠ = needs approval)"),
    ("/preload [skills…|clear]", "Skills activated up-front on every run"),
    ("/secret set NAME VALUE", "Per-run secret, memory only (also: list, unset NAME)"),
    ("/model [name]", "Primary model override (blank = server default)"),
    ("/planner [name]", "Planner model override (blank = server default)"),
    ("/steps [N]", "Max steps per run (1-50)"),
    ("/budget [USD]", "Max cost per run in USD (0-10]"),
    ("/thread [id]", "Show or switch conversation thread"),
    ("/new", "Start a fresh conversation (new thread)"),
    ("/copy [N|all]", "Copy code block N (default: last) or the whole last answer"),
    ("/clear", "Clear the screen (keeps the conversation)"),
    ("/quit", "Exit"),
]
COMMAND_NAMES = sorted({c.split()[0] for c, _ in COMMAND_HELP} | {"/exit"})


def mask(value: str) -> str:
    return "•" * min(len(value), 8) if len(value) <= 8 else value[:2] + "…" + "•" * 4


async def handle_command(session: ChatSession, line: str) -> CommandResult:
    parts = line.strip().split()
    cmd, args = parts[0].lower(), parts[1:]
    s = session.settings
    out = CommandResult()

    def err(msg: str) -> CommandResult:
        return CommandResult([msg], error=True)

    if cmd in ("/quit", "/exit", "/q"):
        out.quit = True
    elif cmd in {"/agents", "/agent", "/threads", "/history", "/branch"}:
        try:
            if cmd == "/agents":
                if args and args != ["next"]:
                    return err("Usage: /agents [next]")
                data = await session.client.list_agent_revisions(
                    session.agent_cursor if args else None
                )
                session.agent_cursor = data["next_cursor"]
                out.lines = [json.dumps(item, default=str) for item in data["items"]]
                out.lines.append(
                    "More: /agents next"
                    if session.agent_cursor
                    else "End of revisions."
                )
            elif cmd == "/threads":
                if args != ["next"]:
                    session.thread_search = " ".join(args)
                data = await session.client.list_threads(
                    cursor=session.thread_cursor if args == ["next"] else None,
                    search=session.thread_search,
                )
                session.thread_cursor = data["next_cursor"]
                out.lines = [json.dumps(item, default=str) for item in data["items"]]
                out.lines.append(
                    "More: /threads next"
                    if session.thread_cursor
                    else "End of threads."
                )
            elif cmd == "/history":
                if not s.thread_id or (
                    args and (len(args) != 1 or not args[0].isdigit())
                ):
                    return err("Select a thread, then use /history [offset].")
                data = await session.client.thread_history(
                    s.thread_id,
                    offset=int(args[0]) if args else 0,
                    history_version=s.history_version if args else None,
                )
                s.history_version = data["history_version"]
                out.lines = [
                    f"History version {s.history_version}; active run: {data['active_run_id']}"
                ]
                out.lines.extend(
                    json.dumps(item, ensure_ascii=True) for item in data["items"]
                )
                if data["next_offset"] is not None:
                    out.lines.append(f"More: /history {data['next_offset']}")
            elif cmd == "/branch":
                if not s.thread_id or len(args) != 1 or not args[0].isdigit():
                    return err(
                        "Select a thread, inspect /history, then use /branch VERSION."
                    )
                data = await session.client.branch_thread(s.thread_id, int(args[0]))
                s.thread_id = data["thread_id"]
                s.history_version = data["history_version"]
                out.lines = [
                    f"Branched to {s.thread_id} at history version {s.history_version}."
                ]
            elif args == ["clear"]:
                s.agent_revision_id = None
                out.lines = ["Agent revision unpinned."]
            elif len(args) == 3 and args[0] == "diff":
                left = await session.client.agent_revision(args[1])
                right = await session.client.agent_revision(args[2])

                def compared(revision: dict[str, Any]) -> dict[str, Any]:
                    return {
                        "configuration": revision["configuration"],
                        "policy_rules": revision.get("policy_rules", {}),
                        "registry_fingerprint": revision.get("registry_fingerprint"),
                    }

                out.lines = list(
                    difflib.unified_diff(
                        json.dumps(
                            compared(left), indent=2, sort_keys=True
                        ).splitlines(),
                        json.dumps(
                            compared(right), indent=2, sort_keys=True
                        ).splitlines(),
                        fromfile=args[1],
                        tofile=args[2],
                        lineterm="",
                    )
                ) or ["No configuration or permission changes."]
                out.lines.append(
                    f"Policy: {left['policy_revision_id']} -> {right['policy_revision_id']}"
                )
            elif len(args) == 2 and args[0] in {"show", "use", "validate", "publish"}:
                if args[0] in {"validate", "publish"}:
                    data = await session.client.transition_agent(args[1], args[0])
                else:
                    data = await session.client.agent_revision(args[1])
                if args[0] == "use":
                    if data["state"] != "published":
                        return err("Only published revisions can be used.")
                    s.agent_revision_id = args[1]
                out.lines = [json.dumps(data, indent=2, default=str)]
            else:
                return err(
                    "Usage: /agent show|validate|publish|use ID | diff ID ID | clear; /agent draft in TUI."
                )
        except HaasAPIError as exc:
            recovery = (
                " Refresh /history, wait/cancel the active run, or explicitly /branch VERSION."
                if exc.status == 409
                else ""
            )
            return err(str(exc) + recovery)
    elif cmd in {"/approvals", "/inspect", "/events"}:
        try:
            out.lines = await _inspection_command(session, cmd, args)
        except HaasAPIError as exc:
            return err(str(exc))
        except ValueError as exc:
            return err(str(exc))
    elif cmd == "/help":
        width = max(len(c) for c, _ in COMMAND_HELP)
        out.lines = [f"{c.ljust(width)}  {d}" for c, d in COMMAND_HELP]
    elif cmd == "/live":
        if args and args[0] not in ("on", "off"):
            return err("Usage: /live [on|off]")
        if args:
            session.live_output = args[0] == "on"
        offered = "offered" if session.output_deltas else "not offered by this server"
        out.lines = [
            f"Live output preview: {'on' if session.live_output else 'off'} ({offered})."
        ]
    elif cmd == "/clear":
        out.clear = True
    elif cmd == "/new":
        session.new_conversation()
        out.new_conversation = True
        out.lines = ["Started a new conversation."]
    elif cmd == "/status":
        try:
            await session.client.health()
            health = "ok"
        except HaasAPIError as exc:
            health = f"unreachable ({exc})"
        out.lines = [
            f"api         {s.base_url} [{health}]",
            f"tenant      {s.tenant}",
            f"thread      {s.thread_id or '(new)'}",
            f"models      primary={s.primary_model or 'default'} planner={s.planner_model or 'default'}",
            f"limits      steps={s.max_steps or 'default'} budget=${s.max_cost_usd or 'default'}",
            f"preload     {', '.join(s.preload_skills) or '-'}",
            f"secrets     {', '.join(sorted(s.secrets)) or '-'}",
            f"cost        ${session.total_cost_usd:.4f} over {session.turns} run(s)",
        ]
    elif cmd in ("/skills", "/tools"):
        try:
            data = await session.plugins()
        except HaasAPIError as exc:
            return err(str(exc))
        if cmd == "/skills":
            for sk in data["skills"]:
                out.lines.append(f"{sk['name']} v{sk['version']}")
                out.lines.append(f"    {sk['description']}")
                if sk["required_tools"]:
                    out.lines.append(f"    tools: {', '.join(sk['required_tools'])}")
                if sk["allowed_secrets"]:
                    out.lines.append(f"    secrets: {', '.join(sk['allowed_secrets'])}")
            out.lines = out.lines or ["No skills installed."]
        else:
            for t in data["tools"]:
                flag = "⚠ " if t["requires_approval"] else "  "
                out.lines.append(f"{flag}{t['name']}  [{t['queue']}]")
    elif cmd == "/preload":
        if not args:
            out.lines = [f"Preloaded: {', '.join(s.preload_skills) or '-'}"]
        elif args == ["clear"]:
            s.preload_skills = []
            out.lines = ["Preload list cleared."]
        else:
            try:
                known = {x["name"] for x in (await session.plugins())["skills"]}
            except HaasAPIError as exc:
                return err(str(exc))
            unknown = [a for a in args if a not in known]
            if unknown:
                return err(f"Unknown skill(s): {', '.join(unknown)}. Try /skills.")
            s.preload_skills = list(dict.fromkeys(args))
            out.lines = [f"Preloaded: {', '.join(s.preload_skills)}"]
    elif cmd == "/secret":
        sub = args[0].lower() if args else "list"
        if sub == "list":
            out.lines = [f"{k} = {mask(v)}" for k, v in sorted(s.secrets.items())] or [
                "No secrets set."
            ]
        elif sub == "set" and len(args) >= 3:
            s.secrets[args[1]] = " ".join(args[2:])
            out.lines = [f"Secret {args[1]} set (memory only)."]
        elif sub == "unset" and len(args) == 2:
            removed = s.secrets.pop(args[1], None)
            out.lines = [f"Secret {args[1]} {'removed' if removed else 'was not set'}."]
        else:
            return err(
                "Usage: /secret set NAME VALUE | /secret unset NAME | /secret list"
            )
    elif cmd in ("/model", "/planner"):
        attr = "primary_model" if cmd == "/model" else "planner_model"
        if args:
            setattr(
                s, attr, None if args[0].lower() in ("default", "clear") else args[0]
            )
        out.lines = [
            f"{attr.replace('_', ' ')}: {getattr(s, attr) or 'server default'}"
        ]
    elif cmd == "/steps":
        if args:
            if not args[0].isdigit() or not 1 <= int(args[0]) <= 50:
                return err("Usage: /steps N  (1-50)")
            s.max_steps = int(args[0])
        out.lines = [f"max steps: {s.max_steps or 'server default'}"]
    elif cmd == "/budget":
        if args:
            try:
                val = float(args[0].lstrip("$"))
            except ValueError:
                return err("Usage: /budget USD  (0-10]")
            if not 0 < val <= 10:
                return err("Budget must be > 0 and <= 10 USD.")
            s.max_cost_usd = val
        out.lines = [f"max cost per run: ${s.max_cost_usd or 'server default'}"]
    elif cmd == "/thread":
        if args:
            s.thread_id = args[0]
            s.history_version = None
            out.lines = [f"Switched to thread {args[0]}."]
        else:
            out.lines = [f"thread: {s.thread_id or '(new — created on first message)'}"]
    else:
        return err(f"Unknown command {cmd}. Type /help.")
    return out


APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired", "cancelled", "all")
EVENT_SEARCH_PAGE = 500


def _money(value: Any) -> str:
    try:
        return f"${float(value or 0):.4f}"
    except (TypeError, ValueError):
        return "$?"


def format_budget(budget: dict[str, Any]) -> list[str]:
    """Settled, uncertain and reserved spend against each ceiling."""
    lines = [
        f"cost        settled {_money(budget.get('settled_usd'))} · "
        f"uncertain {_money(budget.get('uncertain_usd'))} · "
        f"reserved {_money(budget.get('reserved_usd'))} / "
        f"max {_money(budget.get('max_cost_usd'))} "
        f"(remaining {_money(budget.get('remaining_usd'))})",
        f"steps       {budget.get('steps_used', 0)} / {budget.get('max_steps', '?')}",
    ]
    if budget.get("max_tool_invocations") is not None:
        lines.append(
            f"tools       {budget.get('tool_invocations_used', 0)} / "
            f"{budget['max_tool_invocations']}"
        )
    if budget.get("deadline_at"):
        lines.append(f"deadline    {budget['deadline_at']}")
    return lines


def format_approval(item: dict[str, Any]) -> list[str]:
    calls = item.get("tool_calls") or []
    names = (
        ", ".join(str(c.get("name", "?")) for c in calls if isinstance(c, dict)) or "-"
    )
    decision = item.get("decision") or {}
    state = str(item.get("status", "?"))
    if item.get("expired"):
        state += " (expired)"
    lines = [
        f"{item.get('batch_id')}  {state}  run {item.get('run_id')} "
        f"step {item.get('step_number')}",
        f"    tools: {names}",
        f"    digest {str(item.get('batch_digest', ''))[:16]}…  "
        f"expires {item.get('expires_at')}",
    ]
    if decision:
        lines.append(
            f"    decided {decision.get('decision')} by {decision.get('decided_by')} "
            f"at {decision.get('decided_at')}"
            + (f": {decision['reason']}" if decision.get("reason") else "")
        )
    return lines


async def _inspection_command(
    session: ChatSession, cmd: str, args: list[str]
) -> list[str]:
    client = session.client
    if cmd == "/approvals":
        status = args[0].lower() if args else "pending"
        if len(args) > 1 or status not in APPROVAL_STATUSES:
            raise ValueError(f"Usage: /approvals [{'|'.join(APPROVAL_STATUSES)}]")
        data = await client.list_approvals(status=status)
        lines: list[str] = []
        for item in data.get("items", []):
            lines.extend(format_approval(item))
        return lines or [f"No {status} approvals."]

    type_filter = None
    if cmd == "/events":
        filters = [a for a in args if a.startswith("type=")]
        if len(filters) > 1 or (filters and not filters[0][5:]):
            raise ValueError("Usage: /events [RUN_ID] [AFTER_SEQUENCE] [type=TEXT]")
        if filters:
            type_filter = filters[0][5:].lower()
            args = [a for a in args if a != filters[0]]
    run_id = args[0] if args and not args[0].isdigit() else session.last_run_id
    rest = args[1:] if args and not args[0].isdigit() else args
    if not run_id:
        raise ValueError(f"Usage: {cmd} RUN_ID (no current run in this session)")
    if cmd == "/events":
        if len(rest) > 1 or (rest and not rest[0].isdigit()):
            raise ValueError("Usage: /events [RUN_ID] [AFTER_SEQUENCE] [type=TEXT]")
        data = await client.run_events(
            run_id,
            after=int(rest[0]) if rest else 0,
            limit=EVENT_SEARCH_PAGE if type_filter else 100,
        )
        events = [
            e
            for e in data.get("items", [])
            if not type_filter or type_filter in str(e.get("type", "")).lower()
        ]
        lines = [
            f"{e.get('sequence'):>5}  {e.get('occurred_at')}  {e.get('type')}  "
            f"{json.dumps(e.get('data'), ensure_ascii=True, default=str)[:160]}"
            for e in events
        ] or [
            f"No {type_filter!r} events in this page."
            if type_filter
            else "No events after that sequence."
        ]
        if data.get("next_after", 0) < data.get("latest_sequence", 0):
            suffix = f" type={type_filter}" if type_filter else ""
            lines.append(f"More: /events {run_id} {data['next_after']}{suffix}")
        return lines

    if rest:
        raise ValueError("Usage: /inspect [RUN_ID]")
    run = await client.get_run(run_id)
    steps = await client.run_steps(run_id)
    usage = await client.run_usage(run_id)
    lines = [
        f"run         {run.get('run_id')}  [{run.get('status')}]",
        f"thread      {run.get('thread_id')}",
        f"state       version {run.get('state_version')} · "
        f"events {run.get('event_sequence')}"
        + (f" · parent {run['parent_run_id']}" if run.get("parent_run_id") else ""),
    ]
    if run.get("traceparent"):
        lines.append(f"trace       {run['traceparent']}")
    budget = usage.get("budget") or run.get("budget")
    if isinstance(budget, dict):
        lines.extend(format_budget(budget))
    approval = run.get("approval")
    if isinstance(approval, dict):
        lines.append(
            f"approval    batch {approval.get('batch_id')} "
            f"expires {approval.get('expires_at')}"
        )
    lines.append("steps:")
    for st in steps.get("steps", []):
        lines.append(
            f"  #{st.get('step_number')}.{st.get('attempt')}  {st.get('status'):<16} "
            f"{st.get('model') or '-'}  {_money(st.get('cost_usd'))}"
            + ("" if st.get("cost_reported", True) else " (unreported)")
            + (f"  error: {st['error']}" if st.get("error") else "")
        )
    lines.append("tool invocations:")
    for inv in steps.get("invocations", []):
        lines.append(
            f"  #{inv.get('step_number')}.{inv.get('ordinal')}  "
            f"{inv.get('tool_name')}  {inv.get('status')}  "
            f"effect {str(inv.get('effect_key') or '')[:12]}"
        )
        if inv.get("status") == "unknown":
            lines.append(
                "    outcome unknown: the effect may have happened and HaaS never re-executes "
                f"it. Reconcile with the destination using effect key "
                f"{inv.get('effect_key')}."
            )
    entries = usage.get("entries", [])
    lines.append(
        f"usage ledger: {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}"
    )
    for entry in entries:
        lines.append(
            f"  {entry.get('unit')}  {entry.get('quantity')}  "
            f"{_money(entry.get('cost_usd'))}  {entry.get('settlement_status')}"
        )
    return lines
