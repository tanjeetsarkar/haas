import json
from dataclasses import dataclass

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.run_restrictions import (
    ExactIntent, IntentContext, RunAuthority, canonical_arguments, narrow_tools,
    validate_exact_arguments,
)


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient: str
    count: int = 1


def intent(plugin="synthetic-a", **changes):
    context = dict(
        tenant_id="tenant-a", submitter_id="person-a", run_id="run-a",
        policy_revision="policy-a", registry_fingerprint="registry-a",
        plugin_id=plugin, plugin_digest="a" * 64, destination="https://a.invalid",
        idempotency_key="request-a",
    )
    context.update(changes)
    return ExactIntent(
        tool_name="deliver", arguments_json='{"count":1,"recipient":"a"}',
        context=IntentContext(**context),
    )


def test_null_empty_and_parent_narrowing():
    assert RunAuthority().tools is RunAuthority(tools=None).tools is None
    assert RunAuthority().permits("control")
    assert not RunAuthority(tools=()).permits("control")
    assert narrow_tools(None, available={"a", "b"}, parent=("a",)) == ("a",)
    assert narrow_tools((), available={"a"}, parent=("a",)) == ()
    assert narrow_tools(("a",), available={"a", "b"}) == ("a",)
    with pytest.raises(ValueError):
        narrow_tools(("b",), available={"a", "b"}, parent=("a",))
    with pytest.raises(ValueError):
        narrow_tools(("unknown",), available={"a"})


@pytest.mark.parametrize("tools", [("",), ("a", "a"), tuple(map(str, range(129)))])
def test_invalid_tools(tools):
    with pytest.raises(ValidationError):
        RunAuthority(tools=tools)


@pytest.mark.parametrize("arguments", [
    [], {1: "not-string"}, {"x": (1,)}, {"x": b"bytes"},
    {"x": float("nan")}, {"x": float("inf")}, {"x": "\ud800"}, {"x": "é" * 32768},
])
def test_non_json_or_oversized(arguments):
    with pytest.raises(ValueError):
        canonical_arguments(arguments)


def test_strict_effective_arguments():
    assert validate_exact_arguments({"recipient": "a", "count": 1}, Args)
    for args in ({"recipient": "a"}, {"recipient": "a", "count": "1"},
                 {"recipient": "a", "count": True}, {"recipient": "a", "extra": 1}):
        with pytest.raises(ValueError):
            validate_exact_arguments(args, Args)

    class Dropping(BaseModel):
        recipient: str

    class Transforming(BaseModel):
        recipient: str

        @field_validator("recipient")
        @classmethod
        def transform(cls, value):
            return value.strip()

    with pytest.raises(ValueError):
        validate_exact_arguments({"recipient": "a", "extra": 1}, Dropping)
    with pytest.raises(ValueError):
        validate_exact_arguments({"recipient": " a "}, Transforming)

    class Aliased(BaseModel):
        value: str = Field(alias="wire")

    assert validate_exact_arguments({"wire": "a"}, Aliased) == '{"wire":"a"}'


def test_canonical_equality_and_detached_immutable_storage():
    source = {"b": [1, 2], "a": {"recipient": "a"}}
    encoded = canonical_arguments(source)
    assert encoded == canonical_arguments({"a": {"recipient": "a"}, "b": [1, 2]})
    for different in ({"b": [2, 1], "a": {"recipient": "a"}},
                      {"b": [1, 2], "a": {"recipient": " a"}},
                      {"b": [1, 2], "a": {"recipient": "b"}},
                      {"b": [1, 2], "a": {"recipient": "a"}, "c": None}):
        assert encoded != canonical_arguments(different)
    source["b"].append(3)
    assert json.loads(encoded)["b"] == [1, 2]
    bound = intent()
    assert bound.matches("deliver", {"recipient": "a", "count": 1})
    assert not bound.matches("other", {"recipient": "a", "count": 1})
    assert not bound.matches("deliver", {"recipient": "b", "count": 1})
    authority = RunAuthority(tools=("deliver",), intent=bound)
    assert RunAuthority.model_validate_json(authority.model_dump_json()) == authority
    with pytest.raises(ValidationError):
        bound.arguments_json = "{}"
    with pytest.raises(ValidationError):
        bound.context.tenant_id = "other"
    with pytest.raises(ValidationError):
        RunAuthority(intent=bound)
    with pytest.raises(ValidationError):
        RunAuthority.model_validate({"unknown": 1})


@pytest.mark.parametrize("field", list(IntentContext.model_fields))
def test_binding_provenance_isolation(field):
    value = "b" * 64 if field == "plugin_digest" else "different"
    assert intent().binding != intent(**{field: value}).binding


@pytest.mark.parametrize("changes", [
    {"plugin_id": ""}, {"plugin_digest": ""}, {"plugin_digest": "a" * 63},
    {"plugin_digest": "a" * 65}, {"plugin_digest": "A" * 64},
    {"plugin_digest": "g" * 64}, {"plugin_digest": "sha256:" + "a" * 64},
])
def test_missing_or_noncanonical_plugin_provenance_rejected(changes):
    with pytest.raises(ValidationError):
        intent(**changes)


def test_binding_tool_arguments_and_synthetic_plugin_isolation():
    first = intent()
    assert first.binding != intent(plugin="synthetic-b").binding
    for field, value in (("tool_name", "other"), ("arguments_json", '{"recipient":"b"}')):
        assert first.binding != ExactIntent.model_validate(
            {**first.model_dump(), field: value}
        ).binding
    with pytest.raises(ValidationError):
        ExactIntent.model_validate({**first.model_dump(), "arguments_json": '{ "x":1}'})

    @dataclass
    class NotJson:
        value: int

    with pytest.raises(ValueError):
        canonical_arguments({"x": NotJson(1)})
