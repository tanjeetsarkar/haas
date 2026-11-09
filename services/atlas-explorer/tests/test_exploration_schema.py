"""Shared structural fixtures only; semantic validation remains in the TS client."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator, FormatChecker

CLIENT = Path(__file__).resolve().parents[1] / "client-ts"
SCHEMA = json.loads((CLIENT / "src/explorationSchema.v2.json").read_text())
CASES = json.loads((CLIENT / "tests/fixtures/validationCases.json").read_text())
VALIDATOR = Draft7Validator(SCHEMA, format_checker=FormatChecker())


def test_authoritative_schema_is_valid() -> None:
    Draft7Validator.check_schema(SCHEMA)
    # jsonschema makes format dependencies optional: never silently ignore formats.
    assert VALIDATOR.format_checker is not None
    assert "date-time" in VALIDATOR.format_checker.checkers
    assert "uri" in VALIDATOR.format_checker.checkers


@pytest.mark.parametrize("snapshot", CASES["valid"])
def test_shared_valid_snapshots(snapshot: dict) -> None:
    VALIDATOR.validate(snapshot)


@pytest.mark.parametrize("patch", CASES["invalidPatches"])
def test_shared_invalid_snapshots(patch: dict) -> None:
    assert list(VALIDATOR.iter_errors({**CASES["valid"][0], **patch}))
