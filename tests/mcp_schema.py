"""A minimal JSON Schema validator for the MCP schema subset; tests only."""

import re


ALLOWED = frozenset(("type", "properties", "required", "additionalProperties", "items", "enum", "minimum", "maximum",
                     "minLength", "maxLength", "minItems", "maxItems", "pattern", "default", "description"))
FORBIDDEN = frozenset(("$ref", "format", "oneOf", "anyOf", "allOf", "minProperties", "uniqueItems", "const"))


def keywords(schema, path="$"):
    """Yield (path, keyword) for every keyword used, recursively."""
    for key, value in schema.items():
        yield path, key
        if key == "properties":
            for name, child in value.items():
                yield from keywords(child, f"{path}.{name}")
        elif key == "items":
            yield from keywords(value, path + "[]")


def _type_ok(kind, value):
    if kind == "null":
        return value is None
    if kind == "boolean":
        return type(value) is bool
    if kind == "integer":
        return type(value) is int
    if kind == "number":
        return type(value) in (int, float)
    if kind == "string":
        return isinstance(value, str)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    raise AssertionError("Unknown schema type " + kind)


def validate(value, schema, path="$"):
    """Raise AssertionError with a path when value does not conform."""
    kinds = schema.get("type")
    if kinds is not None:
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if not any(_type_ok(kind, value) for kind in kinds):
            raise AssertionError(f"{path}: {value!r} is not {kinds}")
    if "enum" in schema and value not in schema["enum"]:
        raise AssertionError(f"{path}: {value!r} not in enum")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 1 << 60):
            raise AssertionError(f"{path}: string length out of range")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise AssertionError(f"{path}: pattern mismatch")
    if type(value) in (int, float):
        if "minimum" in schema and value < schema["minimum"]:
            raise AssertionError(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise AssertionError(f"{path}: above maximum")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 1 << 60):
            raise AssertionError(f"{path}: item count out of range")
        if "items" in schema:
            for index, item in enumerate(value):
                validate(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", ()):
            if name not in value:
                raise AssertionError(f"{path}: missing {name}")
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            raise AssertionError(f"{path}: unexpected {sorted(value.keys() - properties.keys())}")
        for name, child in properties.items():
            if name in value:
                validate(value[name], child, f"{path}.{name}")
