"""Slash-command completion: pure functions, no UI dependencies."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable

from haas_cli.session import COMMAND_HELP

# Commands that take arguments: completing them appends a space so you can keep typing.
TAKES_ARGS = {
    "/preload",
    "/secret",
    "/model",
    "/planner",
    "/steps",
    "/budget",
    "/thread",
}
SECRET_SUBCOMMANDS = [
    ("set", "NAME VALUE: set a secret (memory only)"),
    ("list", "show secret names (values masked)"),
    ("unset", "NAME: remove a secret"),
]
_DESCRIPTIONS = {c.split()[0]: d for c, d in COMMAND_HELP}
_DESCRIPTIONS["/exit"] = "Exit"
_COMMAND_ORDER = [c.split()[0] for c, _ in COMMAND_HELP] + ["/exit"]


@dataclass
class Suggestion:
    value: str  # replaces the token being typed
    description: str = ""


@dataclass
class Completion:
    start: int = 0  # index in the text where the token being completed begins
    items: list[Suggestion] = field(default_factory=list)


def common_prefix(values: Iterable[str]) -> str:
    return os.path.commonprefix(list(values))


def suggest(
    text: str, *, skills: Iterable[str] = (), secrets: Iterable[str] = ()
) -> Completion:
    """Suggestions for a single-line `/command arg…` being typed; empty for anything else."""
    if not text.startswith("/") or "\n" in text:
        return Completion()

    if " " not in text:
        low = text.lower()
        return Completion(
            0,
            [
                Suggestion(c + (" " if c in TAKES_ARGS else ""), _DESCRIPTIONS[c])
                for c in _COMMAND_ORDER
                if c.startswith(low)
            ],
        )

    head, _, _ = text.partition(" ")
    cmd = head.lower()
    start = text.rfind(" ") + 1
    done = text[:start].split()[1:]  # arguments already completed
    partial = text[start:].lower()

    def pick(options: Iterable[tuple[str, str]]) -> Completion:
        return Completion(
            start,
            [Suggestion(v, d) for v, d in options if v.lower().startswith(partial)],
        )

    if cmd == "/preload":
        taken = set(done)
        opts = [(s, "skill") for s in skills if s not in taken]
        if not done:
            opts.append(("clear", "empty the preload list"))
        return pick(opts)
    if cmd == "/secret":
        if not done:
            return pick(SECRET_SUBCOMMANDS)
        if done == ["unset"]:
            return pick((n, "secret") for n in secrets)
    return Completion()
