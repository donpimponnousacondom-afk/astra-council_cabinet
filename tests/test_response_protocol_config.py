"""Response protocol defaults, end_cycle settings and editable mode-aware guidance."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from conftest import configured
from hortator.models import Bot, ControlError
from hortator.prompt_templates import DEFAULT_PROMPTS, PREVIOUS_DEFAULT_CONTENT_SHA256, catalog
from hortator.slash_commands import SlashContexts

CHANNEL = "222222222222222222"
BOT = {"id": "protocol-test", "name": "Protocol test", "model_profile_id": "balanced"}


def test_response_protocol_defaults_and_end_cycle_settings():
    bot = Bot.model_validate(BOT)
    assert bot.response_protocol == "narrate"
    assert bot.max_progress_messages_per_turn == 1
    assert bot.narration_strikes == 2
    assert "publish_retained_draft" not in bot.model_dump()
    assert bot.notify_incomplete_cycle is False
    assert Bot.model_json_schema()["properties"]["response_protocol"]["enum"] == [
        "narrate",
        "progress",
        "end_cycle",
    ]
    assert Bot.model_validate({**BOT, "response_protocol": "end_cycle"}).response_protocol == "end_cycle"


@pytest.mark.parametrize("value", [True, 1.0, "2", None, 0, 6])
def test_narration_strikes_rejects_coercion_and_out_of_range(value):
    with pytest.raises(ValidationError, match="narration_strikes"):
        Bot.model_validate({**BOT, "narration_strikes": value})


@pytest.mark.parametrize("value", [1, 5])
def test_narration_strikes_accepts_strict_integer_bounds(value):
    assert Bot.model_validate({**BOT, "narration_strikes": value}).narration_strikes == value


@pytest.mark.parametrize("value", [True, False, 1.0, "1", None, -1, 21])
def test_progress_cap_rejects_coercion_and_out_of_range(value):
    with pytest.raises(ValidationError, match="max_progress_messages_per_turn"):
        Bot.model_validate({**BOT, "max_progress_messages_per_turn": value})


@pytest.mark.parametrize("value", [0, 1, 20])
def test_progress_cap_accepts_strict_integer_bounds(value):
    assert (
        Bot.model_validate({**BOT, "max_progress_messages_per_turn": value}).max_progress_messages_per_turn
        == value
    )


async def test_end_cycle_settings_save_and_unknown_protocol_is_rejected(kernel, owner):
    old = configured(kernel)
    with pytest.raises(ControlError):
        await kernel.service.save(owner, "bots", old["id"], {"response_protocol": "mystery"})
    assert kernel.store.get("bots", old["id"]) == old
    updated = await kernel.service.save(
        owner,
        "bots",
        old["id"],
        {
            "response_protocol": "end_cycle",
            "narration_strikes": 1,
            "notify_incomplete_cycle": True,
        },
    )
    assert updated["response_protocol"] == "end_cycle" and updated["narration_strikes"] == 1
    assert "publish_retained_draft" not in updated and updated["notify_incomplete_cycle"] is True
    updated = await kernel.service.save(
        owner, "bots", old["id"], {"response_protocol": "progress", "max_progress_messages_per_turn": 0}
    )
    assert updated["response_protocol"] == "progress" and updated["max_progress_messages_per_turn"] == 0


async def test_legacy_public_defaults_do_not_mutate_saved_bot(kernel):
    legacy = configured(kernel)
    for field in (
        "response_protocol",
        "max_progress_messages_per_turn",
        "narration_strikes",
        "notify_incomplete_cycle",
    ):
        legacy.pop(field, None)
    saved = kernel.store.put("bots", legacy)
    public = kernel.service.public("bots", saved)
    assert public["response_protocol"] == "narrate"
    assert public["max_progress_messages_per_turn"] == 1
    assert public["narration_strikes"] == 2
    assert "publish_retained_draft" not in public
    assert public["notify_incomplete_cycle"] is False
    assert kernel.store.get("bots", saved["id"]) == saved


@pytest.mark.parametrize("kind", ["ordinary", "slash", "panel"])
@pytest.mark.parametrize("protocol", ["narrate", "progress", "end_cycle"])
async def test_mode_template_runtime_facts_and_inspection_match_all_contexts(kernel, kind, protocol):
    bot = configured(
        kernel, response_protocol=protocol, max_progress_messages_per_turn=2, narration_strikes=3
    )
    contexts = (
        kernel.engine.contexts
        if kind == "ordinary"
        else SlashContexts(kernel.store, kernel.pool, {"kind": kind}, None)
    )
    profile = kernel.store.get("profiles", "balanced")
    messages, meta = await contexts.assemble(bot, profile, CHANNEL, [], "")
    selected = next(item for item in meta["prompt_layers"] if item["id"] == "response_protocol")
    expected_template = "runtime-response-protocol-" + protocol + ("-v3" if protocol == "end_cycle" else "")
    assert selected["template_id"] == expected_template
    content = "\n".join(message["content"] for message in messages)
    assert f"Current response protocol: {protocol}." in content
    assert '"response_protocol":"' + protocol + '"' in content
    assert '"max_progress_messages_per_turn":2' in content
    assert '"narration_strikes":3' in content
    if protocol == "end_cycle":
        assert "3 failed responses end this cycle" in content
        assert "do not repeat, regenerate or replace the answer" in content
    assert "{max_progress_messages_per_turn}" not in content
    if protocol == "progress":
        assert "At most 2 progress messages" in content
    elif protocol == "end_cycle":
        assert "Text without end_cycle is never posted" in content
    else:
        assert "NOT posted to Discord" in content
    layer = next(item for item in catalog() if item["id"] == "response_protocol")
    assert selected["template_id"] in {item["id"] for item in layer["templates"]}
    assert "response_protocol setting" in layer["inspection"]["when"]


async def test_existing_bot_and_stored_generic_prompt_default_to_new_narrate_guidance(kernel):
    bot = configured(kernel)
    bot.pop("response_protocol", None)
    bot.pop("max_progress_messages_per_turn", None)
    legacy = kernel.store.get("prompts", "runtime-response-protocol")
    saved = kernel.store.put("prompts", {**legacy, "content": "LEGACY_EDIT_DO_NOT_OVERWRITE"})
    identity = kernel.store.get("prompts", "runtime-identity")
    saved_identity = kernel.store.put("prompts", {**identity, "content": "OWNER_IDENTITY_EDIT"})
    kernel.service.seed_prompt_templates()
    layers = kernel.engine.contexts.layers(bot, CHANNEL)
    selected = next(item for item in layers if item["id"] == "response_protocol")
    assert selected["template_id"] == "runtime-response-protocol-narrate"
    assert "LEGACY_EDIT_DO_NOT_OVERWRITE" not in str(layers)
    assert kernel.store.get("prompts", "runtime-response-protocol") == saved
    assert kernel.store.get("prompts", "runtime-identity") == saved_identity
    assert next(item for item in layers if item["id"] == "identity")["content"] == "OWNER_IDENTITY_EDIT"


@pytest.mark.parametrize("protocol", ["narrate", "progress", "end_cycle"])
async def test_explicit_protocol_override_and_disabled_layer_are_preserved(kernel, owner, protocol):
    bot = configured(kernel, response_protocol=protocol, max_progress_messages_per_turn=3)
    custom = await kernel.service.save(
        owner,
        "prompts",
        "custom-protocol",
        {
            "name": "Owner protocol wording",
            "runtime_layer": "response_protocol",
            "role": "user",
            "content": "Owner text: {response_protocol}, cap {max_progress_messages_per_turn}",
        },
        create=True,
    )
    bot = await kernel.service.save(
        owner, "bots", bot["id"], {"prompt_layer_overrides": {"response_protocol": custom["id"]}}
    )
    kernel.service.seed_prompt_templates()
    selected = next(
        item for item in kernel.engine.contexts.layers(bot, CHANNEL) if item["id"] == "response_protocol"
    )
    assert selected["template_id"] == custom["id"] and selected["role"] == "user"
    assert selected["content"] == f"Owner text: {protocol}, cap 3"
    assert kernel.store.get("prompts", custom["id"]) == custom
    bot = await kernel.service.save(
        owner, "bots", bot["id"], {"disabled_prompt_layers": ["response_protocol"]}
    )
    assert "response_protocol" not in {item["id"] for item in kernel.engine.contexts.layers(bot, CHANNEL)}
    assert bot["response_protocol"] == protocol and bot["max_progress_messages_per_turn"] == 3


@pytest.mark.parametrize("old_value", [True, False])
@pytest.mark.parametrize("protocol", ["narrate", "progress", "end_cycle"])
async def test_draft_switch_migration_changes_only_retired_field_once(kernel, owner, old_value, protocol):
    old = configured(kernel, response_protocol=protocol, publish_retained_draft=old_value)
    untouched = kernel.store.get("bots", "hortator")
    kernel.service.migrate_response_protocol()
    migrated = kernel.store.get("bots", old["id"])
    expected = {key: value for key, value in old.items() if key != "publish_retained_draft"}
    expected["revision"] += 1
    assert migrated == expected
    assert kernel.store.get("bots", "hortator") == untouched
    events = kernel.store.rows(
        "SELECT data FROM events WHERE kind='config.migrated' AND bot_id=?", (old["id"],)
    )
    assert len(events) == 1
    assert json.loads(events[0]["data"])["previous_value"] is old_value
    kernel.service.migrate_response_protocol()
    assert kernel.store.get("bots", old["id"]) == migrated
    assert (
        len(kernel.store.rows("SELECT 1 FROM events WHERE kind='config.migrated' AND bot_id=?", (old["id"],)))
        == 1
    )
    # Partial saves after migration must validate, including bots outside end_cycle.
    saved = await kernel.service.save(owner, "bots", old["id"], {"name": "Still configurable"})
    assert saved["response_protocol"] == protocol
    assert "publish_retained_draft" not in saved


@pytest.mark.parametrize("value", [True, False])
async def test_retired_draft_switch_is_rejected_explicitly(kernel, owner, value):
    old = configured(kernel)
    with pytest.raises(ControlError, match="draft retention is mandatory"):
        await kernel.service.save(owner, "bots", old["id"], {"publish_retained_draft": value})
    assert kernel.store.get("bots", old["id"]) == old
    with pytest.raises(ValidationError, match="publish_retained_draft"):
        Bot.model_validate({**BOT, "publish_retained_draft": value})


async def test_new_default_does_not_overwrite_legacy_protocol_or_custom_guidance(kernel):
    bot = configured(kernel, response_protocol="end_cycle")
    legacy = kernel.store.get("prompts", "runtime-response-protocol-end_cycle")
    saved = kernel.store.put("prompts", {**legacy, "content": "OWNER LEGACY TEXT"})
    guidance = kernel.store.get("prompts", "runtime-tool-guidance")
    saved_guidance = kernel.store.put("prompts", {**guidance, "content": "OWNER TOOL GUIDANCE"})
    kernel.service.seed_prompt_templates()
    selected = kernel.engine.contexts.prompt(bot, "response_protocol")
    assert selected["template_id"] == "runtime-response-protocol-end_cycle-v3"
    assert "never a usage query" in selected["content"]
    assert kernel.store.get("prompts", legacy["id"]) == saved
    assert kernel.store.get("prompts", guidance["id"]) == saved_guidance
    overridden = {**bot, "prompt_layer_overrides": {"response_protocol": legacy["id"]}}
    assert kernel.engine.contexts.prompt(overridden, "response_protocol")["content"] == "OWNER LEGACY TEXT"


async def test_exact_previous_seed_guidance_is_upgraded_once(kernel):
    replacements = {
        "runtime-identity": (
            "Write your answer as ordinary assistant content and follow the current response protocol to publish it.",
            "To answer, write your contribution directly as ordinary assistant content. The runtime posts it to Discord.",
        ),
        "runtime-tool-guidance": (
            "Tool discovery and repair: call an available action tool with {} to receive its usage. end_cycle is NOT a discovery tool: {} closes tool work, then publishes the current or saved answer; if no answer exists, it permits exactly one tool-free first-answer request. Action-tool usage includes",
            "Tool discovery and repair: call any available tool with {} to receive its usage,",
        ),
    }
    for prompt_id, (new, old) in replacements.items():
        current = kernel.store.get("prompts", prompt_id)
        previous = current["content"].replace(new, old)
        if prompt_id == "runtime-tool-guidance":
            previous = previous.replace(
                "Read the remaining budget and finish\naccording to the current response protocol (or use the silence tool only when available).",
                "Read the remaining budget and finish\nwith an ordinary assistant text answer (or the silence tool only when available).",
            )
        assert hashlib.sha256(previous.encode()).hexdigest() == PREVIOUS_DEFAULT_CONTENT_SHA256[prompt_id]
        before = kernel.store.put("prompts", {**current, "content": previous})
        kernel.service.seed_prompt_templates()
        after = kernel.store.get("prompts", prompt_id)
        assert after["content"] == next(p["content"] for p in DEFAULT_PROMPTS if p["id"] == prompt_id)
        assert after["revision"] == before["revision"] + 1
        kernel.service.seed_prompt_templates()
        assert kernel.store.get("prompts", prompt_id) == after


async def test_draft_switch_migrates_during_kernel_construction(tmp_path):
    from hortator.app import Kernel

    original = Kernel(tmp_path / "restart")
    async with original.lifetime():
        bot = original.store.get("bots", "ada")
        before = original.store.put("bots", {**bot, "publish_retained_draft": False})
    restarted = Kernel(tmp_path / "restart")
    async with restarted.lifetime():
        expected = {key: value for key, value in before.items() if key != "publish_retained_draft"}
        expected["revision"] += 1
        assert restarted.store.get("bots", "ada") == expected
        assert (
            len(restarted.store.rows("SELECT 1 FROM events WHERE kind='config.migrated' AND bot_id='ada'"))
            == 1
        )


async def test_retired_prompt_placeholder_is_mandatory_constant(kernel):
    bot = configured(kernel, publish_retained_draft=False)
    values = kernel.engine.contexts.prompt_values(bot)
    assert values["publish_retained_draft"] is True
