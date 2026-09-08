import copy
import json
from unittest.mock import AsyncMock, Mock

import pytest
from jsonschema import Draft202012Validator

from conftest import configured
from hortator.documents import OPERATIONS, PARAMETERS as DOCUMENT_PARAMETERS
from hortator.models import ControlError
from hortator.plugins import PluginSpec, ToolContext, schema
from hortator.tool_feedback import feedback, full_schema, parse_arguments, usage, with_usage


FOUR_FIELDS = schema(
    {
        "title": {"type": "string", "minLength": 1},
        "body": {"type": "string", "minLength": 1},
        "weight": {"type": "number", "minimum": 0},
        "count": {"type": "integer", "minimum": 1},
    },
    ("title", "body", "weight", "count"),
)
VALID = {"title": "Summary", "body": "Document", "weight": 0.5, "count": 2}


async def test_missing_memory_operation_returns_write_example_without_mutating_notes(kernel):
    bot = configured(kernel, enabled_plugins=["memory"])
    context = ToolContext(bot, "channel", "memory-guidance")
    await kernel.registry.call(
        "memory", {"operation": "write", "key": "existing", "value": "keep"}, context, "seed"
    )
    before = await kernel.registry.call("memory", {"operation": "read"}, context, "before")
    result = await kernel.registry.call("memory", {"key": "topic", "value": "note"}, context, "missing")
    assert result["error_count"] == 1 and not result["executed"]
    assert "operation" in result["errors"][0]["message"]
    assert result["usage"]["example"] == {"operation": "write", "key": "topic", "value": "Concise note"}
    spec = kernel.registry.specs["memory"]
    assert '"operation":"write"' in spec.description
    assert "REQUIRED" in spec.parameters["properties"]["operation"]["description"]
    after = await kernel.registry.call("memory", {"operation": "read"}, context, "after")
    assert after["notes"] == before["notes"]


@pytest.mark.parametrize("operation", ["read", "write", "delete"])
def test_explicit_usage_examples_preserve_the_requested_memory_operation(kernel, operation):
    spec = kernel.registry.specs["memory"]
    example = usage("memory", spec.parameters, spec.description, {"operation": operation})["example"]
    assert example["operation"] == operation
    assert Draft202012Validator(spec.parameters).is_valid(example)
    example["operation"] = "invalid"
    assert all(value["operation"] != "invalid" for value in spec.parameters["examples"])


def probe(kernel, *, owner_only=False):
    name = "feedback_probe"
    handler = AsyncMock(return_value={"ok": True, "saved": True})
    kernel.registry.register(
        PluginSpec(name, "Feedback probe", "Save a test document.", FOUR_FIELDS, handler, {}, owner_only)
    )
    kernel.store.put("plugins", {"id": name, "name": "Feedback probe", "enabled": True, "config": {}})
    bot = configured(kernel, enabled_plugins=[name])
    return name, handler, ToolContext(bot, "channel", "turn-feedback")


def assert_full_usage(result, parameters=FOUR_FIELDS):
    details = result["usage"]
    assert details["parameters"] == parameters
    assert "named fields" in details["calling_convention"]
    assert "order does not matter" in details["calling_convention"]
    assert Draft202012Validator(parameters).is_valid(details["example"])


def test_advertised_schema_accepts_empty_help_without_weakening_real_arguments():
    original = copy.deepcopy(FOUR_FIELDS)
    advertised = with_usage(FOUR_FIELDS)
    validator = Draft202012Validator(advertised)
    assert validator.is_valid({})
    assert validator.is_valid(VALID)
    assert not validator.is_valid({"title": "Missing fields"})
    assert not validator.is_valid({**VALID, "surprise": 1})
    assert full_schema(advertised) == FOUR_FIELDS == original


async def test_empty_tool_call_returns_usage_before_credentials_handler_or_network(kernel, monkeypatch):
    name, handler, context = probe(kernel)
    key_read = Mock(wraps=kernel.vault.get)
    monkeypatch.setattr(kernel.vault, "get", key_read)
    result = await kernel.registry.call(name, {}, context, "usage-call")
    assert result["usage_only"] is True
    assert result["executed"] is False
    assert_full_usage(result)
    handler.assert_not_awaited()
    key_read.assert_not_called()


async def test_every_registered_plugin_returns_usage_without_executing_its_handler(kernel, monkeypatch):
    names = list(kernel.registry.specs)
    bot = configured(kernel, bot_id="hortator", enabled_plugins=names)
    context = ToolContext(bot, "channel", "turn-all-usage", owner_verified=True)
    for name in names:
        config = kernel.store.get("plugins", name)
        config.pop("revision")
        kernel.store.put("plugins", {**config, "enabled": True})
        handler = AsyncMock(side_effect=AssertionError("Help must not execute an operation"))
        monkeypatch.setattr(kernel.registry.specs[name], "handler", handler)
        result = await kernel.registry.call_raw(name, "{}", context, f"help-{name}")
        assert result["usage_only"] is True, name
        assert result["executed"] is False
        assert result["usage"]["tool"] == name
        assert result["usage"]["parameters"] == kernel.registry.specs[name].parameters
        handler.assert_not_awaited()


async def test_all_four_missing_fields_and_unknown_key_are_reported_together(kernel):
    name, handler, context = probe(kernel)
    result = await kernel.registry.call(name, {"unexpected": "value"}, context, "all-missing")
    assert result["executed"] is False
    errors = result["errors"]
    assert result["error_count"] == len(errors) == 5
    missing = [error["message"] for error in errors if error["rule"] == "required"]
    assert len(missing) == 4
    for key in FOUR_FIELDS["required"]:
        assert any(repr(key) in message for message in missing)
    assert any(error["rule"] == "additionalProperties" for error in errors)
    assert_full_usage(result)
    handler.assert_not_awaited()


async def test_all_type_errors_and_missing_field_are_repaired_in_one_response(kernel):
    name, handler, context = probe(kernel)
    result = await kernel.registry.call(
        name, {"title": 123, "weight": "0.5", "count": "2", "extra": False}, context, "wrong-types"
    )
    assert result["error_count"] == 5
    assert {error["path"] for error in result["errors"] if error["rule"] == "type"} == {
        "$.title",
        "$.weight",
        "$.count",
    }
    messages = "\n".join(error["message"] for error in result["errors"])
    assert "string" in messages and "number" in messages and "integer" in messages
    assert "'body' is a required property" in messages
    assert_full_usage(result)
    handler.assert_not_awaited()


async def test_repeated_invalid_calls_never_coerce_or_partially_execute(kernel):
    name, handler, context = probe(kernel)
    bad = {**VALID, "count": "2"}
    for index in range(3):
        result = await kernel.registry.call(name, bad, context, f"invalid-{index}")
        assert result["executed"] is False
        assert result["errors"][0]["path"] == "$.count"
    assert bad["count"] == "2"
    handler.assert_not_awaited()
    # Named JSON fields deliberately arrive in another order. This is not a positional API.
    reordered = {"count": 2, "weight": 0.5, "body": "Document", "title": "Summary"}
    result = await kernel.registry.call(name, reordered, context, "corrected")
    assert result["saved"] is True
    handler.assert_awaited_once()
    assert handler.await_args.args[0] == VALID


@pytest.mark.parametrize("value", [True, 1.2, "2", None])
def test_integer_fields_do_not_accept_boolean_fraction_string_or_null(value):
    result = feedback("probe", {**VALID, "count": value}, FOUR_FIELDS)
    assert result["executed"] is False
    assert any(error["path"] == "$.count" and error["rule"] == "type" for error in result["errors"])


@pytest.mark.parametrize(
    "raw",
    [
        '{"title":',
        '{"title":"one","title":"two"}',
        '{"count":NaN}',
        '{"count":Infinity}',
        '{"count":1e999}',
        '{"count":-1e999}',
    ],
)
async def test_malformed_json_duplicate_keys_and_non_json_numbers_return_complete_usage(kernel, raw):
    name, handler, context = probe(kernel)
    result = await kernel.registry.call_raw(name, raw, context, "syntax-error")
    assert result["executed"] is False
    assert result["errors"][0]["rule"] == "json"
    assert_full_usage(result)
    handler.assert_not_awaited()


@pytest.mark.parametrize("raw", ["[]", "null", '"text"', "12"])
async def test_valid_json_must_still_be_an_object(kernel, raw):
    name, handler, context = probe(kernel)
    result = await kernel.registry.call_raw(name, raw, context, "wrong-top-level")
    assert result["executed"] is False
    assert any(error["rule"] == "type" for error in result["errors"])
    assert_full_usage(result)
    handler.assert_not_awaited()


@pytest.mark.parametrize("denial", ["unknown", "plugin-disabled", "bot-disabled-tool", "owner-only"])
async def test_usage_discovery_cannot_bypass_tool_availability(kernel, denial):
    name, handler, context = probe(kernel, owner_only=denial == "owner-only")
    if denial == "unknown":
        name = "unknown_tool"
    elif denial == "plugin-disabled":
        config = kernel.store.get("plugins", name)
        config.pop("revision")
        kernel.store.put("plugins", {**config, "enabled": False})
    elif denial == "bot-disabled-tool":
        context.bot["enabled_plugins"] = []
    result = await kernel.registry.call(name, {}, context, "denied-help")
    assert result.get("error")
    assert not result.get("usage_only")
    handler.assert_not_awaited()
    raw_result = await kernel.registry.call_raw(name, "{invalid", context, "denied-raw")
    assert raw_result.get("error")
    assert not raw_result.get("usage_only")
    handler.assert_not_awaited()


async def test_handler_error_includes_full_usage_and_redacts_credentials(kernel):
    name, handler, context = probe(kernel)
    kernel.vault.put("plugin/feedback_probe/api_key", "test-secret-feedback-value")
    handler.side_effect = ControlError("Remote operation rejected test-secret-feedback-value")
    result = await kernel.registry.call(name, VALID, context, "handler-failed")
    assert "Remote operation rejected" in result["error"]
    assert "test-secret-feedback-value" not in json.dumps(result)
    assert "test-secret-feedback-value" not in json.dumps(kernel.store.events())
    assert_full_usage(result)
    handler.assert_awaited_once()


def test_conditional_document_operation_reports_all_its_missing_fields():
    result = feedback("document_site", {"operation": "write", "path": 7}, DOCUMENT_PARAMETERS)
    assert result["error_count"] == 3
    messages = "\n".join(error["message"] for error in result["errors"])
    assert "'site' is a required property" in messages
    assert "'content' is a required property" in messages
    assert "string" in messages
    assert_full_usage(result, DOCUMENT_PARAMETERS)
    assert result["usage"]["example"]["operation"] == "write"


@pytest.mark.parametrize("operation", OPERATIONS)
def test_every_document_operation_has_a_schema_valid_usage_example(operation):
    details = usage("document_site", DOCUMENT_PARAMETERS, arguments={"operation": operation})
    assert details["example"]["operation"] == operation
    assert list(Draft202012Validator(DOCUMENT_PARAMETERS).iter_errors(details["example"])) == []


def test_nested_errors_include_every_field_and_array_index():
    parameters = schema({"rows": {"type": "array", "items": FOUR_FIELDS}}, ("rows",))
    result = feedback("nested_probe", {"rows": [{"title": 9}, {**VALID, "count": "2"}]}, parameters)
    assert result["error_count"] == 5
    assert {error["path"] for error in result["errors"]} == {
        "$.rows[0].title",
        "$.rows[0]",
        "$.rows[1].count",
    }


def test_parser_rejects_nested_duplicate_fields_without_overwriting_first_value():
    with pytest.raises(ValueError, match="Duplicate JSON field"):
        parse_arguments('{"outer":{"count":1,"count":2}}')


@pytest.mark.parametrize("operation", ["write", "delete"])
async def test_memory_mutation_missing_key_is_reported_before_handler(kernel, monkeypatch, operation):
    bot = configured(kernel, enabled_plugins=["memory"])
    spec = kernel.registry.specs["memory"]
    handler = AsyncMock()
    monkeypatch.setattr(spec, "handler", handler)
    result = await kernel.registry.call(
        "memory", {"operation": operation}, ToolContext(bot, "channel", "turn-memory"), "memory-missing"
    )
    assert result["executed"] is False
    assert any(error["rule"] == "required" and "'key'" in error["message"] for error in result["errors"])
    assert result["usage"]["example"]["operation"] == operation
    assert result["usage"]["example"]["key"]
    assert_full_usage(result, spec.parameters)
    handler.assert_not_awaited()


@pytest.mark.parametrize("key", ["", "   ", "\n\t"])
def test_memory_key_must_have_non_whitespace_content(kernel, key):
    result = feedback(
        "memory", {"operation": "write", "key": key}, kernel.registry.specs["memory"].parameters
    )
    assert result["executed"] is False
    assert result["errors"]


async def test_memory_read_needs_no_key_and_write_preserves_optional_empty_value(kernel):
    bot = configured(kernel, enabled_plugins=["memory"])
    context = ToolContext(bot, "channel", "turn-memory")
    result = await kernel.registry.call(
        "memory", {"operation": "write", "key": "empty-note"}, context, "write"
    )
    assert result["saved"] is True
    result = await kernel.registry.call("memory", {"operation": "read"}, context, "read")
    assert result["notes"][0]["key"] == "empty-note"
    assert result["notes"][0]["value"] == ""


@pytest.mark.parametrize("resource", ["turn", "context"])
async def test_council_inspector_required_target_is_in_conditional_usage(kernel, monkeypatch, resource):
    bot = configured(kernel, bot_id="hortator", enabled_plugins=["council_inspect"])
    spec = kernel.registry.specs["council_inspect"]
    handler = AsyncMock()
    monkeypatch.setattr(spec, "handler", handler)
    context = ToolContext(bot, "channel", "turn-inspect", owner_verified=True)
    result = await kernel.registry.call("council_inspect", {"resource": resource}, context, "missing-target")
    assert result["executed"] is False
    assert any(error["rule"] == "required" and "'id'" in error["message"] for error in result["errors"])
    assert result["usage"]["example"]["resource"] == resource
    assert result["usage"]["example"]["id"]
    assert_full_usage(result, spec.parameters)
    handler.assert_not_awaited()
