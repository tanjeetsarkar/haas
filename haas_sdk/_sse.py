"""Incremental ``text/event-stream`` parser."""

from __future__ import annotations

import json

from haas_sdk.models import RunEvent


class SSEParser:
    """Feed one line at a time (without the trailing newline); yields complete events."""

    def __init__(self) -> None:
        self._id: str | None = None
        self._event = "message"
        self._data: list[str] = []

    def feed(self, line: str) -> RunEvent | None:
        if line == "":
            if not self._data and self._id is None:
                self._event = "message"
                return None
            raw = "\n".join(self._data)
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"raw": raw}
            if not isinstance(data, dict):
                data = {"value": data}
            event = RunEvent(type=self._event, data=data, id=self._id)
            self._id, self._event, self._data = None, "message", []
            return event
        if line.startswith(":"):
            return None
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "id":
            self._id = value
        elif name == "event":
            self._event = value
        elif name == "data":
            self._data.append(value)
        return None
