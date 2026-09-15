import json

import pytest

from conftest import configured
from hortator.global_memory import PARAMETERS, GlobalMemory, register
from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.tool_feedback import errors_for


@pytest.fixture
def memory(kernel):
    # The isolated module may be tested before the application registration stage.
    if "global_memory" not in kernel.registry.specs:
        register(kernel.registry)
        kernel.service.seed_plugins()
    return kernel.registry.global_memory


def enabled(kernel, *, bot_id="ada", limit=48_000):
    plugin = kernel.store.get("plugins", "global_memory")
    plugin.pop("revision")
    kernel.store.put("plugins", {**plugin, "enabled": True})
    return configured(
        kernel, bot_id=bot_id, enabled_plugins=["global_memory"], global_memory_char_limit=limit
    )


async def write(kernel, context, key, value):
    return await kernel.registry.call(
        "global_memory", {"operation": "write", "key": key, "value": value}, context, f"write-{key}"
    )


async def test_disabled_on_registration_and_both_global_and_bot_grant_required(kernel, memory):
    bot = configured(kernel, enabled_plugins=["global_memory"])
    context = ToolContext(bot, "channel", "grant-test")
    assert not kernel.store.get("plugins", "global_memory")["enabled"]
    assert not memory.enabled(bot)
    assert not kernel.registry.allowed("global_memory", context)
    assert memory.prompt_layers(bot) == []
    assert (await write(kernel, context, "topic", "not saved"))["ok"] is False
    bot = enabled(kernel)
    assert kernel.registry.allowed("global_memory", ToolContext(bot, "channel", "test"))
    assert memory.enabled(bot)
    bot.pop("revision")
    kernel.store.put("bots", {**bot, "enabled_plugins": []})
    assert not memory.enabled(bot)
    assert not kernel.registry.allowed("global_memory", context)


async def test_same_bot_reads_across_channels_other_bots_and_private_notes_are_separate(kernel, memory):
    bot = enabled(kernel)
    other = enabled(kernel, bot_id="socrates")
    a = ToolContext(bot, "room-a", "a")
    b = ToolContext(bot, "room-b", "b")
    c = ToolContext(other, "room-a", "c")
    await write(kernel, a, "topic", "The owner prefers concise replies.")
    await write(kernel, c, "topic", "A different bot's own preference.")
    read = await kernel.registry.call("global_memory", {"operation": "read"}, b, "read")
    assert read["notes"][0]["value"] == "The owner prefers concise replies."
    assert read["notes"][0]["source_channel_id"] == "room-a"
    await write(kernel, b, "topic", "A revised note from another room.")
    assert memory.notes(bot["id"])[0]["source_channel_id"] == "room-b"
    assert memory.notes(other["id"])[0]["value"] == "A different bot's own preference."
    assert not kernel.store.rows("SELECT * FROM memories")
    assert memory.budget_for(bot)["used_chars"] == len("A revised note from another room.")


async def test_scope_cannot_be_overridden_by_tool_arguments(kernel, memory):
    bot = enabled(kernel)
    context = ToolContext(bot, "a", "attempt")
    result = await kernel.registry.call(
        "global_memory",
        {
            "operation": "write",
            "key": "topic",
            "value": "escape",
            "bot_id": "socrates",
            "source_channel_id": "forged",
        },
        context,
        "escape",
    )
    assert result["ok"] is False
    assert not memory.notes("ada") and not memory.notes("socrates")
    assert "bot_id" in json.dumps(result) and "source_channel_id" in json.dumps(result)


async def test_slash_source_records_actual_channel_without_changing_bot_scope(kernel, memory):
    bot = enabled(kernel)
    bot["invocation"] = {"kind": "slash", "channel_id": "123456789012345678", "interaction_id": "verified"}
    context = ToolContext(bot, "slash:ada:123456789012345678", "slash")
    await write(kernel, context, "topic", "Confirmed in an invoked application conversation.")
    assert memory.notes(bot["id"])[0]["source_channel_id"] == "123456789012345678"
    assert memory.budget_for(bot)["used_chars"] == len("Confirmed in an invoked application conversation.")


async def test_unicode_headroom_replacement_and_repair_until_under_budget(kernel, memory):
    bot = enabled(kernel, limit=100)
    context = ToolContext(bot, "room-a", "headroom")
    value = "é\x00🦆" * 35
    result = await write(kernel, context, "topic", value)
    assert result["saved"] and result["budget"]["used_chars"] == 105
    assert result["budget"]["hard_limit_chars"] == 105
    assert "5 characters over budget" in result["warning"]
    before = memory.notes(bot["id"])
    for key, text in [("extra", "x"), ("topic", "y" * 105)]:
        rejected = await write(kernel, context, key, text)
        assert rejected["ok"] is False and "would use" in rejected["error"]
        assert memory.notes(bot["id"]) == before
    result = await write(kernel, context, "topic", "x" * 103)
    assert result["saved"] and result["budget"]["must_consolidate"]
    result = await write(kernel, context, "topic", "x" * 100)
    assert result["saved"] and not result["budget"]["must_consolidate"]
    assert "warning" not in result
    assert (await write(kernel, context, "extra", "x" * 5))["saved"]
    result = await kernel.registry.call(
        "global_memory", {"operation": "delete", "key": "extra"}, context, "delete"
    )
    assert result["saved"] and result["budget"]["used_chars"] == 100


async def test_default_maximum_and_individual_note_limit(kernel, memory):
    bot = enabled(kernel)
    context = ToolContext(bot, "room", "full-budget")
    for i in range(6):
        assert (await write(kernel, context, f"note-{i}", "x" * 8000))["saved"]
    assert memory.budget_for(bot)["used_chars"] == 48_000
    saved = await write(kernel, context, "grace", "x" * 2400)
    assert saved["saved"] and saved["budget"]["used_chars"] == 50_400
    rejected = await write(kernel, context, "extra", "x")
    assert rejected["ok"] is False and "50,401" in rejected["error"]
    too_long = await write(kernel, context, "note-0", "x" * 8001)
    assert too_long["ok"] is False
    assert memory.notes(bot["id"])[1]["value"] == "x" * 8000


async def test_tiny_budget_does_not_round_up_to_extra_character(kernel, memory):
    bot = enabled(kernel, limit=1)
    context = ToolContext(bot, "room", "tiny")
    assert (await write(kernel, context, "topic", "x"))["saved"]
    assert (await write(kernel, context, "topic", "xy"))["ok"] is False
    assert memory.budget_for(bot)["hard_limit_chars"] == 1


async def test_current_budget_and_revoked_grants_override_captured_context(kernel, memory):
    bot = enabled(kernel, limit=100)
    context = ToolContext(bot, "room", "stale")
    current = {**bot, "global_memory_char_limit": 40}
    current.pop("revision")
    kernel.store.put("bots", current)
    result = await write(kernel, context, "topic", "x" * 43)
    assert result["ok"] is False and "hard ceiling 42" in result["error"]
    await write(kernel, context, "topic", "x" * 40)
    kernel.store.put("bots", {**current, "enabled_plugins": []})
    with pytest.raises(ControlError, match="not enabled"):
        await memory.call({"operation": "read"}, context, {}, None)
    assert memory.prompt_layers(bot) == []
    assert memory.inspect(bot["id"])["notes"][0]["value"] == "x" * 40


async def test_operator_edits_use_same_budget_and_preserve_disabled_notes_and_provenance(kernel, memory):
    bot = enabled(kernel, limit=100)
    await write(kernel, ToolContext(bot, "source", "origin"), "topic", "x" * 105)
    with pytest.raises(ControlError, match="write not saved"):
        await memory.operator_change(bot["id"], {"operation": "write", "key": "extra", "value": "x"})
    plugin = kernel.store.get("plugins", "global_memory")
    plugin.pop("revision")
    kernel.store.put("plugins", {**plugin, "enabled": False})
    view = memory.inspect(bot["id"])
    assert not view["enabled"] and view["budget"]["must_consolidate"]
    saved = await memory.operator_change(bot["id"], {"operation": "write", "key": "topic", "value": "x" * 90})
    assert saved["saved"] and not saved["budget"]["must_consolidate"]
    assert memory.notes(bot["id"])[0]["source_channel_id"] == "source"
    await memory.operator_change(bot["id"], {"operation": "delete", "key": "topic"})
    assert memory.notes(bot["id"]) == []
    assert (
        await memory.operator_change(bot["id"], {"operation": "write", "key": "new", "value": "owner note"})
    )["saved"]
    assert memory.notes(bot["id"])[0]["source_channel_id"] is None


@pytest.mark.parametrize("limit", [0, -1, 48_001, 1.5, True, "1000", None])
async def test_invalid_budget_sentinels_and_types_rejected(kernel, memory, limit):
    bot = enabled(kernel)
    with pytest.raises(ControlError, match="integer from 1"):
        memory.validate_budget(bot["id"], limit)


async def test_lowering_budget_checks_existing_exact_usage_without_mutating(kernel, memory):
    bot = enabled(kernel, limit=100)
    await write(kernel, ToolContext(bot, "room", "lowering"), "topic", "é\x00" * 50)
    memory.validate_budget(bot["id"], 96)  # 100 fits 96 plus 5% headroom.
    before = memory.notes(bot["id"])
    with pytest.raises(ControlError, match="already uses 100"):
        memory.validate_budget(bot["id"], 95)
    assert memory.notes(bot["id"]) == before


async def test_usage_reports_all_errors_and_never_runs_empty_or_missing_value_write(kernel, memory):
    bot = enabled(kernel, limit=100)
    context = ToolContext(bot, "room", "usage")
    empty = await kernel.registry.call("global_memory", {}, context, "help")
    assert empty["usage_only"] and not memory.notes(bot["id"])
    invalid = await kernel.registry.call(
        "global_memory", {"operation": "write", "key": 123, "extra": True}, context, "invalid"
    )
    assert invalid["ok"] is False and len(invalid["errors"]) >= 3
    assert "$.key" in {error["path"] for error in invalid["errors"]}
    assert all(field in json.dumps(invalid["errors"]) for field in ("key", "value", "extra"))
    assert "value" in json.dumps(invalid["usage"])
    assert not memory.notes(bot["id"])
    with pytest.raises(ControlError, match="value") as error:
        await memory.operator_change(bot["id"], {"operation": "write", "key": "topic"})
    assert "usage" in str(error.value) and not memory.notes(bot["id"])
    assert errors_for(PARAMETERS, {"operation": "write", "key": "topic", "value": 10})


async def test_dynamic_usage_and_prompt_expose_real_budget_with_source_attribution(kernel, memory):
    bot = enabled(kernel, limit=100)
    context = ToolContext(bot, "room-a", "usage-budget")
    await write(kernel, context, "topic", "x" * 104)
    spec = memory.spec_for(context)
    assert "104/100" in spec.description and "WARNING" in spec.description
    layers = memory.prompt_layers(bot)
    assert "4 characters over budget" in layers[0]["content"]
    assert "room-a" in layers[1]["content"] and "untrusted recollections" in layers[1]["content"]
    assert "not current instructions" in layers[1]["content"]
    assert "+" in layers[1]["content"]  # Runtime timezone offset on stored update time.
    other = enabled(kernel, bot_id="socrates")
    assert len(memory.prompt_layers(other)) == 1
    # Reconstructing the helper/schema never resets persisted notes.
    again = GlobalMemory(kernel.store, kernel.vault)
    assert again.notes(bot["id"]) == memory.notes(bot["id"])


async def test_credentials_redacted_before_storage_and_global_events(kernel, memory):
    bot = enabled(kernel)
    secret = "fake-private-secret-never-for-a-live-account-0123456789"
    kernel.vault.put("plugin/test/api_key", secret)
    result = await write(kernel, ToolContext(bot, "room", "redaction"), "topic", "Useful fact and " + secret)
    assert result["saved"]
    assert secret not in dumps_global(kernel, memory, bot["id"])


def dumps_global(kernel, memory, bot_id):
    return json.dumps(
        {"notes": memory.notes(bot_id), "events": kernel.store.events(bot_id=bot_id, limit=100)}
    )
