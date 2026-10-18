"""Minimal Semantic Versioning 2.0 parsing, ordering and npm-style ranges."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


@dataclass(frozen=True, slots=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: str | None = None

    def __str__(self) -> str:
        core = f"{self.major}.{self.minor}.{self.patch}"
        return f"{core}-{self.prerelease}" if self.prerelease else core

    def _pre_key(self) -> tuple[int, tuple[tuple[int, int | str], ...]]:
        if self.prerelease is None:
            return (1, ())
        parts: list[tuple[int, int | str]] = []
        for item in self.prerelease.split("."):
            parts.append((0, int(item)) if item.isdigit() else (1, item))
        return (0, tuple(parts))

    def key(self) -> tuple[int, int, int, tuple[int, tuple[tuple[int, int | str], ...]]]:
        return (self.major, self.minor, self.patch, self._pre_key())

    def __lt__(self, other: Version) -> bool:
        return self.key() < other.key()

    def __le__(self, other: Version) -> bool:
        return self.key() <= other.key()

    def __gt__(self, other: Version) -> bool:
        return self.key() > other.key()

    def __ge__(self, other: Version) -> bool:
        return self.key() >= other.key()


def parse_version(value: str) -> Version:
    match = _SEMVER_RE.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        raise ValueError(f"'{value}' is not a valid semantic version (MAJOR.MINOR.PATCH)")
    return Version(int(match[1]), int(match[2]), int(match[3]), match[4])


def coerce_version(value: str) -> Version:
    """Accepts legacy `1` / `1.2` versions (manifest v1) by padding with zeros."""
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        text = ".".join((text.split(".") + ["0", "0"])[:3])
    return parse_version(text)


def _comparator(op: str, version: Version):
    def check(candidate: Version) -> bool:
        return {
            ">": candidate > version,
            ">=": candidate >= version,
            "<": candidate < version,
            "<=": candidate <= version,
            "=": candidate.key() == version.key(),
        }[op]

    return check


def _partial(text: str) -> tuple[int, int | None, int | None]:
    parts = text.split(".")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"invalid version range component '{text}'")
    nums = [int(p) for p in parts]
    return nums[0], nums[1] if len(nums) > 1 else None, nums[2] if len(nums) > 2 else None


def range_matches(spec: str, candidate: Version) -> bool:
    """`*`, exact, `^1.2`, `~1.2.3`, `1.x`, and space-separated comparators (`>=1 <2`)."""
    spec = spec.strip()
    if spec in ("", "*", "latest"):
        return candidate.prerelease is None
    checks = []
    for token in spec.split():
        token = token.replace(".x", "").replace(".*", "")
        if token.startswith("^") or token.startswith("~"):
            major, minor, patch = _partial(token[1:])
            low = Version(major, minor or 0, patch or 0)
            if token[0] == "^":
                if major > 0 or minor is None:
                    high = Version(major + 1, 0, 0)
                elif minor > 0 or patch is None:
                    high = Version(0, minor + 1, 0)
                else:
                    high = Version(0, 0, (patch or 0) + 1)
            else:
                high = (
                    Version(major + 1, 0, 0)
                    if minor is None
                    else Version(major, minor + 1, 0)
                )
            checks += [_comparator(">=", low), _comparator("<", high)]
            continue
        match = re.fullmatch(r"(>=|<=|>|<|=)?(.+)", token)
        assert match is not None
        op, rest = match[1], match[2]
        try:
            exact = parse_version(rest)
        except ValueError:
            major, minor, patch = _partial(rest)
            if op:
                checks.append(
                    _comparator(op, Version(major, minor or 0, patch or 0))
                )
            else:
                low = Version(major, minor or 0, patch or 0)
                high = (
                    Version(major + 1, 0, 0)
                    if minor is None
                    else Version(major, minor + 1, 0)
                    if patch is None
                    else Version(major, minor, patch + 1)
                )
                checks += [_comparator(">=", low), _comparator("<", high)]
            continue
        checks.append(_comparator(op or "=", exact))
    if candidate.prerelease is not None and "-" not in spec:
        return False
    return all(check(candidate) for check in checks)


def validate_range(spec: str) -> str:
    range_matches(spec, Version(0, 0, 0))
    return spec


def best_match(spec: str, versions: list[str]) -> str | None:
    matching = []
    for raw in versions:
        try:
            parsed = parse_version(raw)
        except ValueError:
            continue
        if range_matches(spec, parsed):
            matching.append(parsed)
    return str(max(matching)) if matching else None
