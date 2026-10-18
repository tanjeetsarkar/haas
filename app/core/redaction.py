"""Secret redaction for persisted events, logs, webhook bodies and tool results (owner: SEC).

``redact`` masks, recursively over dicts/lists/tuples/strings:
* exact secret values passed in ``secrets`` or registered for a live run
  (``register_secret_values``), longest first so overlapping values mask fully;
* canary tokens ``haas_canary_*`` (planted in tests and red-team runs);
* bearer/basic credentials, ``sk-`` API keys and ``key=value`` credential pairs.
Dict keys are kept; only values are rewritten.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

MASK = "[REDACTED]"
MIN_SECRET_LENGTH = 4
_MAX_TRACKED_RUNS = 10_000

CANARY_PATTERN = re.compile(r"haas_canary_[A-Za-z0-9_-]+")
_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]+"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(
        r"(?i)\b(authorization|api[_-]?key|token|password|secret|signature)"
        r"([\"']?\s*[:=]\s*[\"']?)[^,\s\"']+"
    ),
)

_lock = threading.Lock()
_registry: OrderedDict[str, frozenset[str]] = OrderedDict()


def register_secret_values(run_id: str, values: Iterable[str]) -> None:
    """Remember a run's live secret values so every later ``redact`` call masks them."""
    usable = frozenset(v for v in values if isinstance(v, str) and len(v) >= MIN_SECRET_LENGTH)
    if not usable:
        return
    with _lock:
        merged = _registry.pop(run_id, frozenset()) | usable
        _registry[run_id] = merged
        while len(_registry) > _MAX_TRACKED_RUNS:
            _registry.popitem(last=False)


def forget_secret_values(run_id: str) -> None:
    with _lock:
        _registry.pop(run_id, None)


def registered_secret_values() -> frozenset[str]:
    with _lock:
        return frozenset().union(*_registry.values()) if _registry else frozenset()


def _sub_pattern(match: re.Match[str]) -> str:
    if match.lastindex and match.lastindex >= 2:
        return match.group(1) + match.group(2) + MASK
    return MASK


def _secret_values(secrets: Iterable[str]) -> tuple[str, ...]:
    values = {
        s for s in secrets if isinstance(s, str) and len(s) >= MIN_SECRET_LENGTH
    } | registered_secret_values()
    return tuple(sorted(values, key=len, reverse=True))


def _redact_str(text: str, values: tuple[str, ...]) -> str:
    for value in values:
        if value in text:
            text = text.replace(value, MASK)
    text = CANARY_PATTERN.sub(MASK, text)
    for pattern in _PATTERNS:
        text = pattern.sub(_sub_pattern, text)
    return text


def _walk(value: Any, values: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return _redact_str(value, values)
    if isinstance(value, dict):
        return {k: _walk(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, values) for v in value]
    if isinstance(value, tuple):
        return tuple(_walk(v, values) for v in value)
    return value


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    return _redact_str(text, _secret_values(secrets))


def redact(value: Any, *, secrets: Iterable[str] = ()) -> Any:
    return _walk(value, _secret_values(secrets))
