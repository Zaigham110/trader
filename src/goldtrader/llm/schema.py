"""Dependency-free JSON-schema validation and pydantic → flat schema conversion (R-021).

Schemas must stay **flat** (docs/LOCAL_LLM.md §2, §8): no `$ref`, `anyOf`, `oneOf` or `allOf`.
Nested objects and arrays of objects are fine as long as they are written inline. Optional fields
use a type array (`{"type": ["string", "null"]}`), which is how `pydantic_to_schema` rewrites
`X | None`. This is the subset Anthropic structured outputs accept with `strict: true` and the
subset local JSON-schema enforcers (llama.cpp / vLLM) grammar-compile reliably.

Supported keywords: type (incl. type arrays), enum, const, required, properties,
additionalProperties (false), minimum / maximum / exclusiveMinimum / exclusiveMaximum,
minLength / maxLength, minItems / maxItems, items. Anything else is ignored by `validate`.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

_UNSUPPORTED = ("$ref", "anyOf", "oneOf", "allOf", "not", "patternProperties", "if")


class SchemaError(ValueError):
    """A schema uses a keyword outside the flat subset."""


def _type_matches(value: Any, t: str) -> bool:
    if t == "null":
        return value is None
    if t == "boolean":
        return isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if t == "string":
        return isinstance(value, str)
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict)
    return False


def validate(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Return a list of human-readable problems; empty means the instance is valid.

    Error strings carry a JSON-path-like location so they can be fed back to the model verbatim.
    """
    errors: list[str] = []
    types = schema.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        if not any(_type_matches(instance, t) for t in allowed):
            errors.append(f"{path}: expected type {'|'.join(allowed)}, got {_name(instance)}")
            return errors  # further checks assume the right type
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']!r}")

    if isinstance(instance, int | float) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} is below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: {instance} is above maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: {instance} must be > {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: {instance} must be < {schema['exclusiveMaximum']}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: length {len(instance)} < minLength {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: length {len(instance)} > maxLength {schema['maxLength']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: {len(instance)} items < minItems {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: {len(instance)} items > maxItems {schema['maxItems']}")
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(instance):
                errors.extend(validate(item, items, f"{path}[{i}]"))

    if isinstance(instance, dict):
        props: dict[str, Any] = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required property '{key}'")
        for key, value in instance.items():
            if key in props:
                errors.extend(validate(value, props[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property '{key}'")
    return errors


def _name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    return type(value).__name__


# --- pydantic → flat schema --------------------------------------------------------------------
def pydantic_to_schema(model_cls: type[BaseModel]) -> dict[str, Any]:
    """Flatten a pydantic model's JSON schema into the supported subset (strict).

    * `$defs` / `$ref` are inlined (nested models, enums).
    * `anyOf: [{...}, {"type": "null"}]` becomes a type array with "null"; a nullable enum
      (`Literal[...] | None`) gets `null` appended to its `enum` list.
    * every object gets `additionalProperties: false`; `required` lists every field without a
      default (pydantic's own rule), so an optional field still needs an explicit default.
    * `title` and `default` are dropped (noise for the model; `default` is not allowed in strict
      mode). `description` is kept.

    Raises `SchemaError` on anything that cannot be flattened (unions of two non-null types, etc.).
    """
    raw = model_cls.model_json_schema()
    defs = raw.pop("$defs", {})
    return _flatten(raw, defs, model_cls.__name__)


def _resolve(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if ref is None:
        return node
    name = ref.rsplit("/", 1)[-1]
    if name not in defs:
        raise SchemaError(f"unresolvable $ref {ref}")
    merged = copy.deepcopy(defs[name])
    for k, v in node.items():  # sibling keywords (description…) win over the definition
        if k != "$ref":
            merged[k] = v
    return merged


def _flatten(node: dict[str, Any], defs: dict[str, Any], where: str) -> dict[str, Any]:
    node = _resolve(copy.deepcopy(node), defs)
    if "anyOf" in node:
        branches = [_resolve(b, defs) for b in node.pop("anyOf")]
        non_null = [b for b in branches if b.get("type") != "null"]
        nullable = len(non_null) < len(branches)
        if len(non_null) != 1:
            raise SchemaError(f"{where}: unions other than X | None are not flat")
        base = _flatten(non_null[0], defs, where)
        t = base.get("type")
        if nullable:
            if t is None:
                raise SchemaError(f"{where}: nullable branch without a type")
            base["type"] = [*(t if isinstance(t, list) else [t]), "null"]
            if "enum" in base and None not in base["enum"]:
                base["enum"] = [*base["enum"], None]
        for k, v in node.items():
            if k not in ("title", "default"):
                base.setdefault(k, v)
        node = base
    for key in _UNSUPPORTED:
        if key in node:
            raise SchemaError(f"{where}: keyword {key} is not in the flat subset")
    out: dict[str, Any] = {}
    for k, v in node.items():
        if k in ("title", "default"):
            continue
        out[k] = v
    if out.get("type") == "object" or "properties" in out:
        props = out.get("properties", {})
        out["properties"] = {k: _flatten(v, defs, f"{where}.{k}") for k, v in props.items()}
        out.setdefault("required", [])
        out["additionalProperties"] = False
    if isinstance(out.get("items"), dict):
        out["items"] = _flatten(out["items"], defs, f"{where}[]")
    return out
