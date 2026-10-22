"""`Secret` wrapper and log redaction (P5: secrets live in memory only)."""

from __future__ import annotations

import hmac
import logging
import re
from collections.abc import Iterable

_MASK = "••••"

# Patterns mirror app.core.redaction (SDK-SEC-03).
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]+"),
    re.compile(r"\bhk_(?:live|test)_[A-Za-z0-9_-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bhaas_canary_[A-Za-z0-9_-]+"),
    re.compile(
        r"(?i)\b(authorization|api[_-]?key|token|password|secret|signature)"
        r"([\"']?\s*[:=]\s*[\"']?)[^,\s\"']+"
    ),
)


class Secret:
    """A credential value that masks itself in `repr`, `str` and logs.

    >>> s = Secret("hk_test_abc")
    >>> str(s), repr(s)
    ("Secret('••••')", "Secret('••••')")
    >>> s.reveal()
    'hk_test_abc'
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """Return the underlying value. Only the transport layer calls this."""
        return self._value

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            other = other._value
        if not isinstance(other, str):
            return NotImplemented
        return hmac.compare_digest(self._value, other)

    def __hash__(self) -> int:
        return hash(self.__class__)

    def __repr__(self) -> str:
        return f"Secret('{_MASK}')"

    def __str__(self) -> str:
        return f"Secret('{_MASK}')"


def redact(text: str) -> str:
    """Mask credential-shaped substrings in ``text``."""
    for pattern in _PATTERNS:
        text = pattern.sub(_mask_match, text)
    return text


def _mask_match(match: re.Match[str]) -> str:
    # Keep the key part of `key=value` pairs; mask everything else fully.
    groups = match.groups()
    if groups and groups[0]:
        return f"{groups[0]}{groups[1]}[REDACTED]"
    return "[REDACTED]"


class _RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            record.args = tuple(
                redact(a) if isinstance(a, str) else a for a in record.args
            )
        return True


def install_log_redaction(
    logger: str | logging.Logger = "haas_sdk",
) -> _RedactionFilter:
    """Attach the credential-masking filter to a logger (idempotent)."""
    target = logging.getLogger(logger) if isinstance(logger, str) else logger
    for existing in target.filters:
        if isinstance(existing, _RedactionFilter):
            return existing
    redaction_filter = _RedactionFilter()
    target.addFilter(redaction_filter)
    return redaction_filter


def secret_values(secrets: Iterable[tuple[str, str | Secret]] | None) -> list[str]:
    """Extract raw values from a secrets mapping for one-shot transport encoding."""
    if not secrets:
        return []
    return [v.reveal() if isinstance(v, Secret) else v for _, v in secrets]
