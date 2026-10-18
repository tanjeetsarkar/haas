"""Shared helpers for validating emitted payloads against docs/contracts schemas."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONTRACTS = Path(__file__).resolve().parent.parent / "docs" / "contracts"


def load_contract(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / f"{name}.schema.json").read_text())


def event_violations(kind: str, payload: dict[str, Any]) -> list[str]:
    """Schema errors plus undeclared payload keys (drift the schema must document)."""
    import jsonschema

    schema = load_contract("run-events.v1")
    data_schema = schema["$defs"].get(kind)
    if data_schema is None:
        return [f"{kind}: event type is not declared in run-events.v1"]
    errors = [
        f"{kind}: {e.message}"
        for e in jsonschema.Draft202012Validator(data_schema).iter_errors(payload)
    ]
    undeclared = sorted(set(payload) - set(data_schema["properties"]))
    if undeclared:
        errors.append(f"{kind}: undeclared fields {undeclared}")
    return errors
