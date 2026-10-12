"""Response protocol defaults, end_cycle settings and editable mode-aware guidance."""

import pytest
from pydantic import ValidationError

from conftest import configured
from hortator.models import Bot, ControlError
from hortator.prompt_templates import catalog
from hortator.slash_commands import SlashContexts

CHANNEL = "222222222222222222"
BOT = {"id": "protocol-test", "name": "Protocol test", "model_profile_id": "balanced"}


def test_response_protocol_defaults_and_end_cycle_settings():
    bot = Bot.model_validate(BOT)
    assert bot.response_protocol == "narrate"
    assert bot.max_progress_messages_per_turn == 1
    assert bot.narration_strikes == 2
    assert bot.publish_retained_draft is True
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
            "publish_retained_draft": False,
            "notify_incomplete_cycle": True,
        },
    )
    assert updated["response_protocol"] == "end_cycle" and updated["narration_strikes"] == 1
    assert updated["publish_retained_draft"] is False and updated["notify_incomplete_cycle"] is True
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
        "publish_retained_draft",
        "notify_incomplete_cycle",
    ):
        legacy.pop(field, None)
    saved = kernel.store.put("bots", legacy)
    public = kernel.service.public("bots", saved)
    assert public["response_protocol"] == "narrate"
    assert public["max_progress_messages_per_turn"] == 1
    assert public["narration_strikes"] == 2
    assert public["publish_retained_draft"] is True
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
    assert selected["template_id"] == "runtime-response-protocol-" + protocol
    content = "\n".join(message["content"] for message in messages)
    assert f"Current response protocol: {protocol}." in content
    assert '"response_protocol":"' + protocol + '"' in content
    assert '"max_progress_messages_per_turn":2' in content
    assert '"narration_strikes":3' in content
    if protocol == "end_cycle":
        assert "After 3 strikes" in content and "currently True" in content
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


@pytest.mark.parametrize("protocol", ["narrate", "progress"])
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
