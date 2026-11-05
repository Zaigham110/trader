from enum import StrEnum
from typing import Literal

import pytest
from pydantic import BaseModel, Field

from goldtrader.llm.schema import SchemaError, pydantic_to_schema, validate

FLAT = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["wait", "enter", "exit"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "thesis": {"type": "string", "minLength": 20, "maxLength": 400},
        "side": {"type": ["string", "null"], "enum": ["long", "short", None]},
        "count": {"type": "integer"},
        "tags": {"type": "array", "items": {"type": "string", "maxLength": 5}, "maxItems": 2},
        "kind": {"const": "proposal"},
    },
    "required": ["action", "confidence", "thesis", "kind"],
    "additionalProperties": False,
}

GOOD = {
    "action": "wait",
    "confidence": 0.5,
    "thesis": "x" * 25,
    "side": None,
    "count": 3,
    "tags": ["a", "b"],
    "kind": "proposal",
}


def test_valid_instance_has_no_errors():
    assert validate(GOOD, FLAT) == []


@pytest.mark.parametrize(
    "patch, needle",
    [
        ({"action": "hold"}, "not one of"),
        ({"confidence": 1.5}, "above maximum"),
        ({"confidence": -0.1}, "below minimum"),
        ({"confidence": "0.5"}, "expected type number"),
        ({"thesis": "short"}, "minLength"),
        ({"thesis": "y" * 401}, "maxLength"),
        ({"count": 2.5}, "expected type integer"),
        ({"count": True}, "expected type integer"),
        ({"tags": ["toolong"]}, "$.tags[0]"),
        ({"tags": ["a", "b", "c"]}, "maxItems"),
        ({"kind": "other"}, "must equal"),
        ({"extra": 1}, "unexpected property 'extra'"),
        ({"side": 7}, "expected type string|null"),
    ],
)
def test_each_violation_is_reported_with_a_path(patch, needle):
    bad = {**GOOD, **patch}
    errors = validate(bad, FLAT)
    assert errors, f"{patch} should fail"
    assert any(needle in e for e in errors), errors


def test_missing_required_and_nested_paths():
    inst = {k: v for k, v in GOOD.items() if k != "thesis"}
    errors = validate(inst, FLAT)
    assert errors == ["$: missing required property 'thesis'"]
    nested = {"type": "object", "properties": {"a": {"type": "object", "properties": {"b": FLAT}}}}
    errors = validate({"a": {"b": {**GOOD, "action": "nope"}}}, nested)
    assert errors[0].startswith("$.a.b.action:")


def test_type_arrays_accept_null():
    assert validate(None, {"type": ["number", "null"]}) == []
    assert validate(1.5, {"type": ["number", "null"]}) == []
    assert validate("x", {"type": ["number", "null"]})


# --- pydantic flattening -----------------------------------------------------------------------
class EventType(StrEnum):
    fed = "fed"
    other = "other"


class Lesson(BaseModel):
    tag: str
    text: str = Field(max_length=200)


class Proposal(BaseModel):
    action: Literal["wait", "enter", "exit", "move_stop"]
    side: Literal["long", "short"] | None = None
    stop_atr: float | None = Field(default=None, ge=0.3, le=1.5)
    confidence: float = Field(ge=0, le=1, description="0-1")
    thesis: str = Field(min_length=20)
    event_type: EventType = EventType.other
    lessons: list[Lesson] = Field(default_factory=list, max_length=3)
    nested: Lesson | None = None


def test_pydantic_to_schema_flattens_to_the_supported_subset():
    schema = pydantic_to_schema(Proposal)
    text = str(schema)
    for banned in ("$ref", "$defs", "anyOf", "title", "default"):
        assert banned not in text, banned
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["action", "confidence", "thesis"]  # fields without defaults
    props = schema["properties"]
    assert props["action"]["enum"] == ["wait", "enter", "exit", "move_stop"]
    assert props["side"]["type"] == ["string", "null"]
    assert props["side"]["enum"] == ["long", "short", None]
    assert props["stop_atr"]["type"] == ["number", "null"]
    assert props["stop_atr"]["minimum"] == 0.3
    assert props["confidence"]["description"] == "0-1"
    assert props["thesis"]["minLength"] == 20
    assert props["event_type"]["enum"] == ["fed", "other"]
    assert props["lessons"]["items"]["additionalProperties"] is False
    assert props["lessons"]["items"]["properties"]["text"]["maxLength"] == 200
    assert props["lessons"]["maxItems"] == 3
    assert props["nested"]["type"] == ["object", "null"]
    assert props["nested"]["properties"]["tag"] == {"type": "string"}


def test_flattened_schema_validates_model_instances():
    schema = pydantic_to_schema(Proposal)
    good = Proposal(action="wait", confidence=0.7, thesis="a" * 25).model_dump(mode="json")
    assert validate(good, schema) == []
    bad = {**good, "action": "buy", "stop_atr": 9}
    errors = validate(bad, schema)
    assert any("action" in e for e in errors) and any("stop_atr" in e for e in errors)


def test_unions_of_two_real_types_are_rejected():
    class Bad(BaseModel):
        x: int | str

    with pytest.raises(SchemaError):
        pydantic_to_schema(Bad)
