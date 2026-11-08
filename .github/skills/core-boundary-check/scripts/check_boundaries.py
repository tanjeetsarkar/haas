#!/usr/bin/env python3
"""Detect plugin-specific code leaking into HaaS core, and plugins reaching into core internals.

ERROR  core path references a specific plugin (id, skill, tool name or configured token).
WARN   plugin code imports `app.*` instead of the public `haas_plugin_sdk` surface (legacy debt).

Usage:
  python .github/skills/core-boundary-check/scripts/check_boundaries.py [--plugin ID] [--json] [--strict]

Exit code: 1 when any ERROR exists (or any WARN with --strict), else 0. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
CONFIG = Path(__file__).with_name("boundary.json")
APP_IMPORT = re.compile(r"^\s*(from\s+app(\.|\s)|import\s+app(\.|\s|$))")
MAX_FILE_BYTES = 2_000_000


@dataclass
class Finding:
    level: str
    plugin: str
    path: str
    line: int
    token: str
    text: str


def load_plugins(only: str | None) -> dict[str, set[str]]:
    cfg = json.loads(CONFIG.read_text())
    generic = set(cfg.get("generic_ids", []))
    tokens: dict[str, set[str]] = {}
    for manifest in sorted((ROOT / "plugins").glob("*/plugin.json")):
        if manifest.parent.name.startswith("_"):
            continue
        data = json.loads(manifest.read_text())
        pid = data["id"]
        if only and pid != only:
            continue
        provides = data.get("provides") or {}
        found = set(provides.get("tools", [])) | set(cfg.get("plugin_tokens", {}).get(pid, []))
        if pid not in generic:
            found |= {pid, pid.replace("-", "_")}
            found |= set(provides.get("skills", []))
        tokens[pid] = {t for t in found if len(t) >= 4}
    return tokens


def iter_files(paths: list[str], skip_dirs: set[str], exts: set[str]):
    for rel in paths:
        base = ROOT / rel
        if base.is_file():
            yield base
            continue
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.is_file() and not (skip_dirs & set(p.relative_to(ROOT).parts)) and p.suffix in exts:
                yield p


def is_allowed(allow: list[dict], rel: str, token: str) -> bool:
    return any(a["path"] == rel and a["token"] == token for a in allow)


def scan(only: str | None) -> list[Finding]:
    cfg = json.loads(CONFIG.read_text())
    plugin_tokens = load_plugins(only)
    patterns = {
        pid: re.compile(r"(?<![A-Za-z0-9])(" + "|".join(map(re.escape, sorted(toks, key=len, reverse=True))) + r")", re.I)
        for pid, toks in plugin_tokens.items()
        if toks
    }
    findings: list[Finding] = []
    skip, exts, allow = set(cfg["skip_dirs"]), set(cfg["extensions"]), cfg.get("allow", [])

    for f in iter_files(cfg["core_paths"], skip, exts):
        if f.stat().st_size > MAX_FILE_BYTES:
            continue
        rel = f.relative_to(ROOT).as_posix()
        try:
            lines = f.read_text(errors="ignore").splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, 1):
            if "boundary: allow" in line:
                continue
            for pid, pat in patterns.items():
                m = pat.search(line)
                if m and not is_allowed(allow, rel, m.group(1).lower()):
                    findings.append(Finding("ERROR", pid, rel, n, m.group(1), line.strip()[:160]))

    for pid in plugin_tokens:
        for f in sorted((ROOT / "plugins" / pid).rglob("*.py")):
            if "__pycache__" in f.parts:
                continue
            rel = f.relative_to(ROOT).as_posix()
            for n, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                if APP_IMPORT.match(line):
                    findings.append(Finding("WARN", pid, rel, n, "app.*", line.strip()[:160]))
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plugin", help="limit to one plugin id")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--strict", action="store_true", help="treat WARN as failure")
    args = ap.parse_args()

    findings = scan(args.plugin)
    errors = [f for f in findings if f.level == "ERROR"]
    warns = [f for f in findings if f.level == "WARN"]

    if args.json:
        print(json.dumps([asdict(f) for f in findings], indent=2))
    else:
        by_file: dict[tuple[str, str, str], list[Finding]] = {}
        for f in findings:
            by_file.setdefault((f.level, f.plugin, f.path), []).append(f)
        for (level, pid, path), items in sorted(by_file.items()):
            lines = ",".join(str(i.line) for i in items[:12]) + (" ..." if len(items) > 12 else "")
            print(f"{level:5} [{pid}] {path}:{lines}  e.g. {items[0].text}")
        print(f"\n{len(errors)} core-boundary error(s), {len(warns)} plugin->core-internal warning(s)")

    return 1 if errors or (args.strict and warns) else 0


if __name__ == "__main__":
    sys.exit(main())
