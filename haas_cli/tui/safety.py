from __future__ import annotations

import re

_ANSI_SEQUENCE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-_])"
)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize_terminal_text(value: object, *, limit: int | None = None) -> str:
    """Remove terminal control sequences and optionally bound rendered content."""
    text = _ANSI_SEQUENCE.sub("", str(value))
    text = _CONTROL_CHARS.sub("", text)
    if limit is not None and len(text) > limit:
        suffix = "\n[display truncated; full content remains on the server]"
        if limit <= len(suffix):
            return text[:limit]
        return text[: limit - len(suffix)] + suffix
    return text
