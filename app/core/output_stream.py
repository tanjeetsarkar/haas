"""Optional live `run.output.delta` previews (`run-output-delta.v1`).

Deltas are batched model-content fragments published to a bounded Redis stream while a step
is in flight. They are advisory and live-only: they never carry an SSE `id`, are not part of
the durable `run-events.v1` log, and clients must replace them with the authoritative output
from `run_completed`. Only assistant `content` is forwarded; tool-call arguments never are.
"""

from __future__ import annotations

import logging
import time
from typing import Any

OUTPUT_DELTA_EVENT = "run.output.delta"
OUTPUT_DELTA_SCHEMA = "run-output-delta.v1"
OUTPUT_DELTA_FEATURE = "output-deltas.v1"
_TTL_SEC = 3_600

log = logging.getLogger("haas.output")


def delta_key(run_id: str) -> str:
    return f"haas:delta:{run_id}"


def decode_delta(fields: dict[str, Any]) -> dict[str, Any] | None:
    """Parses one Redis stream entry; returns None for malformed entries."""
    try:
        return {
            "schema": OUTPUT_DELTA_SCHEMA,
            "step": int(fields["step"]),
            "attempt": int(fields["attempt"]),
            "call": int(fields["call"]),
            "index": int(fields["index"]),
            "text": str(fields.get("text", "")),
            "final": fields.get("final") == "1",
            "truncated": fields.get("truncated") == "1",
        }
    except (KeyError, TypeError, ValueError):
        return None


class DeltaPublisher:
    """Batches content fragments for one model call (`step`, `attempt`, `call`)."""

    def __init__(
        self,
        redis: Any,
        run_id: str,
        *,
        step: int,
        attempt: int,
        call: int,
        flush_chars: int,
        flush_ms: int,
        max_chars: int,
        buffer: int,
    ) -> None:
        self.redis = redis
        self.key = delta_key(run_id)
        self.ident = {"step": str(step), "attempt": str(attempt), "call": str(call)}
        self.flush_chars = flush_chars
        self.flush_sec = flush_ms / 1000
        self.max_chars = max_chars
        self.buffer = buffer
        self.index = 0
        self.sent_chars = 0
        self.pending: list[str] = []
        self.pending_chars = 0
        self.truncated = False
        self.failed = False
        self.last_flush = time.monotonic()

    async def push(self, text: str) -> None:
        if self.truncated or self.failed or not text:
            return
        room = self.max_chars - self.sent_chars - self.pending_chars
        if len(text) > room:
            text, self.truncated = text[: max(room, 0)], True
        if text:
            self.pending.append(text)
            self.pending_chars += len(text)
        if (
            self.truncated
            or self.pending_chars >= self.flush_chars
            or time.monotonic() - self.last_flush >= self.flush_sec
        ):
            await self.flush()

    async def flush(self, *, final: bool = False) -> None:
        if self.failed or (not self.pending and not final and not self.truncated):
            return
        text = "".join(self.pending)
        self.pending, self.pending_chars = [], 0
        self.last_flush = time.monotonic()
        fields = {
            **self.ident,
            "index": str(self.index),
            "text": text,
            "final": "1" if final else "0",
            "truncated": "1" if self.truncated else "0",
        }
        try:
            await self.redis.xadd(
                self.key, fields, maxlen=self.buffer, approximate=True
            )
            await self.redis.expire(self.key, _TTL_SEC)
        except Exception:  # Advisory channel: never fail the model call over a preview.
            self.failed = True
            log.warning("output delta publish failed for %s", self.key, exc_info=True)
            return
        self.index += 1
        self.sent_chars += len(text)
