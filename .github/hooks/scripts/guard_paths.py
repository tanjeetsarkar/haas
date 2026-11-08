#!/usr/bin/env python3
"""PreToolUse guard for file-editing tools.

- Governance files (AGENTS.md, .github/agents|skills|instructions|hooks) always require user approval.
- With --role plugin, edits outside plugin-owned paths are denied.

Reads the hook payload (JSON) on stdin. Fails open on malformed input so it never wedges a session.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EDIT_TOOL = re.compile(r"edit|create|write|patch|replace|insert|delete|rename|move", re.I)
PATH_KEYS = {"path", "filepath", "file_path", "files", "paths", "target", "new_path", "old_path"}
PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)
GOVERNANCE_DIRS = (".github/agents/", ".github/skills/", ".github/instructions/", ".github/hooks/")
ROLE_ALLOWED = {
    "plugin": ("plugins/", "services/", "tests/plugins/"),
}


def collect_paths(node, out: list[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k.lower() in PATH_KEYS:
                if isinstance(v, str):
                    out.append(v)
                elif isinstance(v, list):
                    out.extend(x for x in v if isinstance(x, str))
            elif isinstance(v, str) and "*** " in v:
                out.extend(m.strip() for m in PATCH_FILE.findall(v))
            else:
                collect_paths(v, out)
    elif isinstance(node, list):
        for x in node:
            collect_paths(x, out)


def rel(p: str) -> str:
    path = Path(p)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(ROOT).as_posix()
        except ValueError:
            return path.as_posix()
    return path.as_posix().removeprefix("./")


def decide(decision: str, reason: str) -> int:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=sorted(ROLE_ALLOWED))
    args = ap.parse_args()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    tool = str(payload.get("tool_name") or payload.get("toolName") or "")
    if not EDIT_TOOL.search(tool):
        return 0
    raw: list[str] = []
    collect_paths(payload.get("tool_input") or payload.get("toolInput") or payload.get("toolArgs") or {}, raw)
    paths = [rel(p) for p in raw]
    if not paths:
        return 0

    if args.role:
        allowed = ROLE_ALLOWED[args.role]
        outside = [p for p in paths if not p.startswith(allowed)]
        if outside:
            return decide("deny", (
                f"{args.role}-engineer may only edit {', '.join(allowed)}; blocked: {', '.join(outside)}. "
                "If core must change, return NEEDS_DECISION describing the generic capability (AGENTS.md §1, §7)."
            ))

    gov = [p for p in paths if p == "AGENTS.md" or p.startswith(GOVERNANCE_DIRS)]
    if gov:
        return decide("ask", f"Agent governance file edit requires user approval (AGENTS.md §2): {', '.join(gov)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
