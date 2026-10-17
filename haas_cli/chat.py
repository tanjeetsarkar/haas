"""`haas-chat`: interactive TUI (default) or one-shot plain-text mode (`--once`)."""

from __future__ import annotations

import argparse
import asyncio
import sys

from haas_cli.client import HaasAPIError, HaasClient, SSEEvent
from haas_cli.config import ChatSettings
from haas_cli.session import ChatSession, Decision, PendingCall


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="haas-chat", description="Chat with a running HaaS (docker) deployment."
    )
    p.add_argument(
        "--url", help="API base URL (env HAAS_URL, default http://localhost:8000)"
    )
    p.add_argument("--tenant", help="X-Tenant-ID (env HAAS_TENANT)")
    p.add_argument("--thread", help="Continue an existing thread id")
    p.add_argument(
        "--preload", nargs="*", metavar="SKILL", help="Skills to activate up-front"
    )
    p.add_argument("--model", help="Primary model override")
    p.add_argument("--planner", help="Planner model override")
    p.add_argument("--max-steps", type=int)
    p.add_argument("--budget", type=float, help="Max USD per run")
    p.add_argument(
        "--once",
        metavar="PROMPT",
        help="Non-interactive: run one prompt, print events as plain text, exit",
    )
    p.add_argument(
        "--auto-approve",
        action="store_true",
        help="With --once: approve every human-approval gate automatically (use with care)",
    )
    p.add_argument(
        "--theme",
        choices=("auto", "dark", "light"),
        help="TUI theme (env HAAS_TUI_THEME, default auto)",
    )
    p.add_argument(
        "--no-color",
        action="store_true",
        default=None,
        help="Disable terminal colors (also honored from NO_COLOR)",
    )
    p.add_argument(
        "--ascii",
        action="store_true",
        default=None,
        help="Use ASCII-friendly symbols (env HAAS_TUI_ASCII)",
    )
    return p


def settings_from_args(args: argparse.Namespace) -> ChatSettings:
    s = ChatSettings.from_env()
    if args.url:
        s.base_url = args.url.rstrip("/")
    if args.tenant:
        s.tenant = args.tenant
    if args.thread:
        s.thread_id = args.thread
    if args.preload is not None:
        s.preload_skills = args.preload
    if args.model:
        s.primary_model = args.model
    if args.planner:
        s.planner_model = args.planner
    if args.max_steps:
        s.max_steps = args.max_steps
    if args.budget:
        s.max_cost_usd = args.budget
    if args.theme:
        s.theme = args.theme
    if args.no_color is not None:
        s.no_color = args.no_color
    if args.ascii is not None:
        s.ascii = args.ascii
    return s


async def run_once(session: ChatSession, prompt: str, auto_approve: bool) -> int:
    def on_event(ev: SSEEvent) -> None:
        d = ev.data
        if ev.event == "step_started":
            print(
                f"[step {d.get('step')}] {d.get('model')} skills={d.get('active_skills')}"
            )
        elif ev.event == "tool_dispatched":
            print(f"  -> {d.get('tool')}")
        elif ev.event == "tool_result":
            mark = "ERR" if d.get("is_error") else "ok "
            print(f"  <- {mark} {d.get('tool')}: {str(d.get('preview', ''))[:120]}")

    async def on_approval(calls: list[PendingCall]) -> Decision:
        for c in calls:
            print(f"  ?? approval needed: {c.name} {c.arguments}")
        if auto_approve:
            print("  auto-approved")
            return Decision(True)
        answer = await asyncio.to_thread(input, "  approve? [y/N] ")
        return Decision(answer.strip().lower() in ("y", "yes"))

    result = await session.run_turn(prompt, on_event, on_approval)
    print(
        f"\n[{result.status}] cost=${result.cost_usd:.4f} thread={session.settings.thread_id}"
    )
    print(result.output)
    return 0 if result.status == "completed" else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = settings_from_args(args)
    client = HaasClient(settings.base_url, settings.tenant)
    session = ChatSession(client, settings)

    if args.once:

        async def go() -> int:
            try:
                return await run_once(session, args.once, args.auto_approve)
            finally:
                await client.aclose()

        try:
            return asyncio.run(go())
        except HaasAPIError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    try:
        from haas_cli.tui.app import ChatApp
    except ImportError:
        print(
            "The TUI needs Textual: pip install 'haas-distributed[tui]'  (or: uv pip install textual)",
            file=sys.stderr,
        )
        return 2
    ChatApp(session).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
