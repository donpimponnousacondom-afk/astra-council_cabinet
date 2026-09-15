"""Shared discoverability and complete argument feedback for every model tool."""

import copy
import json
import math

from jsonschema import Draft202012Validator


def with_usage(parameters):
    """Keep properties discoverable while explicitly allowing the empty help call."""
    return {
        "type": "object",
        "properties": copy.deepcopy(parameters.get("properties", {})),
        "anyOf": [{"type": "object", "maxProperties": 0}, copy.deepcopy(parameters)],
    }


def full_schema(parameters):
    branches = parameters.get("anyOf", [])
    if len(branches) == 2 and branches[0] == {"type": "object", "maxProperties": 0}:
        return branches[1]
    return parameters


def example_value(name, field):
    if "const" in field:
        return field["const"]
    if field.get("examples"):
        return field["examples"][0]
    if "default" in field:
        return field["default"]
    if field.get("enum"):
        return field["enum"][0]
    kind = field.get("type", "string")
    if kind == "integer":
        return max(1, field.get("minimum", 1))
    if kind == "number":
        return float(max(1, field.get("minimum", 1)))
    if kind == "boolean":
        return True
    if kind == "array":
        return [example_value(name, field.get("items", {})) for _ in range(field.get("minItems", 0))]
    if kind == "object":
        return example_arguments(field)
    if name == "url" or field.get("format") == "uri":
        return "https://example.com"
    if name == "path":
        return "index.html"
    if "pattern" in field and ("[0-9]" in field["pattern"] or "\\d" in field["pattern"]):
        return "123456789012345678"
    return "example"[: field.get("maxLength", 7)]


def example_arguments(parameters, arguments=None):
    properties = parameters.get("properties", {})
    # Prefer explicit, schema-valid examples and match a valid operation when
    # supplied. This avoids showing a read example to a bot trying to save a note.
    operation = arguments.get("operation") if isinstance(arguments, dict) else None
    for example in parameters.get("examples", [])[:5]:
        if (
            isinstance(example, dict)
            and Draft202012Validator(parameters).is_valid(example)
            and (operation is None or example.get("operation") == operation)
        ):
            return copy.deepcopy(example)
    required = set(parameters.get("required", []))
    example = {key: example_value(key, properties.get(key, {})) for key in sorted(required)}
    # Preserve a valid discriminant so conditional tool packs show the requested operation's usage.
    if isinstance(arguments, dict):
        for key in ("operation", "resource"):
            if (
                key in arguments
                and key in properties
                and Draft202012Validator(properties[key]).is_valid(arguments[key])
            ):
                example[key] = arguments[key]
    for branch in parameters.get("allOf", []):
        if "if" in branch and Draft202012Validator(branch["if"]).is_valid(example):
            required.update(branch.get("then", {}).get("required", []))
    for key in sorted(required):
        example.setdefault(key, example_value(key, properties.get(key, {})))
    return example


def usage(name, parameters, description="", arguments=None):
    parameters = full_schema(parameters)
    return {
        "tool": name,
        "description": description,
        "calling_convention": "JSON object with named fields; field order does not matter. {} returns usage only.",
        "parameters": parameters,
        "example": example_arguments(parameters, arguments),
        "example_note": "Replace example values with the real identifiers/content for this task; all schema constraints still apply.",
    }


def errors_for(parameters, arguments):
    def leaves(error):
        if error.context:
            for child in error.context:
                yield from leaves(child)
        else:
            yield error

    errors = []
    for error in Draft202012Validator(full_schema(parameters)).iter_errors(arguments):
        for item in leaves(error):
            errors.append(
                {
                    "path": "$"
                    + "".join(
                        f"[{part}]" if isinstance(part, int) else f".{part}" for part in item.absolute_path
                    ),
                    "rule": item.validator,
                    "message": item.message,
                }
            )
    return errors


def feedback(name, arguments, parameters, description=""):
    details = usage(name, parameters, description, arguments)
    if arguments == {}:
        return {"ok": True, "usage_only": True, "executed": False, "usage": details}
    errors = errors_for(parameters, arguments)
    if errors:
        return {
            "ok": False,
            "executed": False,
            "error": "Invalid tool arguments. Fix every error below; no action was executed.",
            "errors": errors,
            "error_count": len(errors),
            "usage": details,
        }
    return None


def parse_arguments(raw):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON field {key!r}; use each named field once")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"{value} is not a valid JSON number")

    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("JSON numbers must be finite; exponent exceeds supported numeric range")
        return parsed

    value = json.loads(
        raw, object_pairs_hook=object_pairs, parse_constant=invalid_constant, parse_float=finite_float
    )
    invalid = []

    def unicode_fields(item, path):
        if isinstance(item, str):
            if any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                invalid.append(path)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                unicode_fields(child, f"{path}[{index}]")
        elif isinstance(item, dict):
            for key, child in item.items():
                unicode_fields(key, path + ".key")
                unicode_fields(child, f"{path}[{key!a}]")

    unicode_fields(value, "arguments")
    if invalid:
        raise ValueError(
            "Unpaired UTF-16 surrogate in "
            + ", ".join(invalid)
            + "; use complete Unicode characters, not isolated emoji halves"
        )
    return value


def syntax_feedback(name, error, parameters, description=""):
    return {
        "ok": False,
        "executed": False,
        "error": "Tool arguments must be valid JSON: " + str(error),
        "errors": [{"path": "$", "rule": "json", "message": str(error)}],
        "error_count": 1,
        "usage": usage(name, parameters, description),
        "note": "Field validation needs parseable JSON. The full usage is included so you can fix syntax and fields together.",
    }
