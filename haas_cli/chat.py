"""`haas-chat`: interactive TUI (default) or one-shot plain-text mode (`--once`)."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from haas_cli.client import HaasAPIError, HaasClient, SSEEvent
from haas_cli.config import ChatSettings, parse_environment
from haas_cli.profiles import ProfileStore
from haas_cli.session import ChatSession, Decision, PendingCall


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="haas-chat", description="Chat with a running HaaS (docker) deployment."
    )
    p.add_argument(
        "--url", help="API base URL (env HAAS_URL, default http://localhost:8000)"
    )
    p.add_argument("--tenant", help="X-Tenant-ID (env HAAS_TENANT)")
    p.add_argument("--profile", help="Named endpoint profile (env HAAS_CLIENT_PROFILE)")
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
    selected_profile = getattr(args, "profile", None) or os.environ.get(
        "HAAS_CLIENT_PROFILE"
    )
    profiles = ProfileStore()
    profile = profiles.get(selected_profile) if selected_profile else profiles.active()
    if selected_profile and profile is None:
        raise ValueError(f"HaaS profile {selected_profile!r} does not exist.")
    if profile:
        s.profile_name = profile.name
        s.base_url = profile.base_url
        s.tenant = profile.tenant
        s.environment = profile.environment
    if os.environ.get("HAAS_URL"):
        s.base_url = os.environ["HAAS_URL"].rstrip("/")
    if os.environ.get("HAAS_TENANT"):
        s.tenant = os.environ["HAAS_TENANT"]
    if os.environ.get("HAAS_ENVIRONMENT"):
        s.environment = parse_environment(os.environ["HAAS_ENVIRONMENT"])
    if getattr(args, "url", None):
        s.base_url = args.url.rstrip("/")
    if getattr(args, "tenant", None):
        s.tenant = args.tenant
    if getattr(args, "thread", None):
        s.thread_id = args.thread
    if getattr(args, "preload", None) is not None:
        s.preload_skills = args.preload
    if getattr(args, "model", None):
        s.primary_model = args.model
    if getattr(args, "planner", None):
        s.planner_model = args.planner
    if getattr(args, "max_steps", None):
        s.max_steps = args.max_steps
    if getattr(args, "budget", None):
        s.max_cost_usd = args.budget
    if getattr(args, "theme", None):
        s.theme = args.theme
    if getattr(args, "no_color", None) is not None:
        s.no_color = args.no_color
    if getattr(args, "ascii", None) is not None:
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
    try:
        settings = settings_from_args(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
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
