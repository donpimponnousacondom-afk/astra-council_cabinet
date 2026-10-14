"""Character-only Engram budgets, existing-data migration, and prompt compatibility."""

import json

import pytest

from hortator.app import Kernel
from hortator.engrams import DEFAULTS, LIMITS, PLUGIN_ID, parse_response, validate_config
from hortator.models import ControlError
from hortator.prompt_templates import DEFAULT_PROMPTS, PLACEHOLDERS, layer
from test_engrams import NONCE, delivered, enabled, generation, wire


PREVIOUS_INSTRUCTIONS = (
    "Maintain a concise factual memory for this conversation. When useful information changes, "
    "supply a complete replacement MEM and FACTS in your final answer using the private suffix "
    "protocol below. If nothing useful has changed, you may retain the existing memory without "
    "a replacement. MEM records the current situation, open work, commitments and relevant "
    "preferences; FACTS records durable facts with clear attribution, useful dates and uncertainty. "
    "Refer to people by name and user ID; never store temporary P labels, which are reassigned "
    "in each request. Apply corrections and remove obsolete material. Preserve information "
    "needed beyond the recent conversation without copying the transcript. Record confirmed "
    "tool outcomes, not imagined actions or an undelivered answer as an accomplished action. "
    "Do not record private reasoning or chain-of-thought. The combined state must fit "
    "{state_char_limit} characters and {state_token_limit} locally counted tokens. The recent-message "
    "target is {recent_messages}; history reduction is {reduce_history}. Update only with the final "
    "ordinary answer, not intermediate tool calls. Your visible answer remains normal conversational "
    "text; the suffix is stored privately by the harness."
)


def migration_events(kernel):
    return [
        json.loads(row["data"])
        for row in kernel.store.rows("SELECT data FROM events WHERE kind='config.migrated' ORDER BY seq")
    ]


def seed_legacy_settings(kernel):
    plugin = kernel.store.get("plugins", PLUGIN_ID)
    plugin = kernel.store.put(
        "plugins",
        {**plugin, "config": {**DEFAULTS, "state_char_limit": 16384, "state_token_limit": 7}},
    )
    bot = kernel.store.get("bots", "ada")
    bot = kernel.store.put(
        "bots",
        {
            **bot,
            "plugin_config": {
                **bot["plugin_config"],
                "engram": {"state_char_limit": 12345, "state_token_limit": 11, "reduce_history": True},
                "unrelated_plugin": {"state_token_limit": 13},
            },
        },
    )
    other = kernel.store.get("bots", "socrates")
    other = kernel.store.put(
        "bots", {**other, "plugin_config": {"unrelated_plugin": {"state_token_limit": 17}}}
    )
    return plugin, bot, other


async def test_token_dense_state_under_character_limit_commits_only_after_delivery(kernel):
    memory = kernel.registry.engrams
    state = {"MEM": "🦆" * 2100, "FACTS": "界" * 100}
    bot = enabled(kernel, config={"state_char_limit": 2200})
    snapshot = memory.capture(bot, "room", "dense-turn")
    parsed = await memory.parse(snapshot, wire(state, nonce=snapshot["nonce"]))
    assert parsed["state_chars"] == 2200
    assert parsed["state_tokens"] > 2048
    assert parsed["state"] == state and parsed["invalid"] is None

    generation(kernel, snapshot, "dense-request")
    candidate = memory.stage(snapshot, parsed, "dense-request", covered_through=42)
    assert memory.finalize(candidate) is False
    assert memory.inspect(bot["id"])["states"] == []

    delivered(kernel, snapshot)
    assert memory.finalize(candidate)
    saved = memory.inspect(bot["id"])["states"][0]
    assert saved["MEM"] == state["MEM"] and saved["FACTS"] == state["FACTS"]
    assert saved["state_chars"] == 2200 and saved["state_tokens"] == parsed["state_tokens"]
    assert saved["covered_through"] == 42 and saved["source_request_id"] == "dense-request"


@pytest.mark.parametrize("limit,accepted", [(6, False), (7, True), (8, True)])
def test_character_boundary_counts_combined_unicode_values_not_bytes_or_json(limit, accepted):
    state = {"MEM": 'é🦆\n"', "FACTS": "界\x00x"}
    parsed = parse_response(wire(state), NONCE, {**DEFAULTS, "state_char_limit": limit})
    assert parsed["content"] == "Public answer."
    if accepted:
        assert parsed["state"] == state and parsed["state_chars"] == 7
        assert parsed["invalid"] is None
    else:
        assert parsed["state"] is None and parsed["invalid"] == "state_char_limit"


def test_legacy_parser_config_cannot_reintroduce_a_token_budget():
    state = {"MEM": "🦆" * 100, "FACTS": "A fact."}
    parsed = parse_response(wire(state), NONCE, {**DEFAULTS, "state_token_limit": 1})
    assert parsed["state"] == state and parsed["state_tokens"] > 1
    assert parsed["invalid"] is None
    assert "state_token_limit" not in DEFAULTS and "state_token_limit" not in LIMITS


def test_runtime_tolerates_legacy_settings_without_mutating_them(kernel):
    plugin, bot, _ = seed_legacy_settings(kernel)
    resolved = kernel.registry.engrams.config(bot)
    assert resolved == {**DEFAULTS, "state_char_limit": 12345, "reduce_history": True}
    assert kernel.store.get("plugins", PLUGIN_ID) == plugin
    assert kernel.store.get("bots", "ada") == bot


@pytest.mark.parametrize("value", [1, 2048, None])
def test_retired_token_setting_is_explicitly_rejected_in_new_configuration(value):
    with pytest.raises(ControlError, match="state_token_limit"):
        validate_config({"state_token_limit": value})


@pytest.mark.parametrize("resource", ["plugins", "bots"])
async def test_api_save_rejects_retired_key_without_changing_record(kernel, owner, resource):
    identifier = PLUGIN_ID if resource == "plugins" else "ada"
    before = kernel.store.get(resource, identifier)
    changes = (
        {"config": {**DEFAULTS, "state_token_limit": 2048}}
        if resource == "plugins"
        else {"plugin_config": {"engram": {"state_token_limit": 2048}}}
    )
    with pytest.raises(ControlError, match="state_token_limit"):
        await kernel.service.save(owner, resource, identifier, changes)
    assert kernel.store.get(resource, identifier) == before


async def test_migration_preserves_state_and_other_settings_and_is_idempotent(kernel, owner):
    memory = kernel.registry.engrams
    bot = enabled(kernel)
    snapshot = memory.capture(bot, "room", "kept-turn")
    parsed = await memory.parse(snapshot, wire(nonce=snapshot["nonce"]))
    generation(kernel, snapshot, "kept-request")
    candidate = memory.stage(snapshot, parsed, "kept-request", covered_through=77)
    delivered(kernel, snapshot)
    assert memory.finalize(candidate)
    saved_state = kernel.store.rows("SELECT * FROM engram_states")
    candidates = kernel.store.rows("SELECT * FROM engram_candidates")
    contexts = kernel.store.rows("SELECT * FROM contexts")
    plugin, bot, other = seed_legacy_settings(kernel)
    before_receipts = len(migration_events(kernel))

    kernel.service.migrate_engram_token_limit()
    current_plugin = kernel.store.get("plugins", PLUGIN_ID)
    current_bot = kernel.store.get("bots", "ada")
    assert current_plugin["config"] == {k: v for k, v in plugin["config"].items() if k != "state_token_limit"}
    expected_bot_config = {
        **bot["plugin_config"],
        "engram": {k: v for k, v in bot["plugin_config"]["engram"].items() if k != "state_token_limit"},
    }
    assert current_bot["plugin_config"] == expected_bot_config
    for previous, current in ((plugin, current_plugin), (bot, current_bot)):
        assert current["revision"] == previous["revision"] + 1
        assert {k: v for k, v in current.items() if k not in ("revision", "config", "plugin_config")} == {
            k: v for k, v in previous.items() if k not in ("revision", "config", "plugin_config")
        }
    assert kernel.store.get("bots", "socrates") == other
    assert kernel.store.rows("SELECT * FROM engram_states") == saved_state
    assert kernel.store.rows("SELECT * FROM engram_candidates") == candidates
    assert kernel.store.rows("SELECT * FROM contexts") == contexts
    receipts = migration_events(kernel)[before_receipts:]
    assert {(event["resource"], event["id"]) for event in receipts} == {
        ("plugins", PLUGIN_ID),
        ("bots", "ada"),
    }

    kernel.service.migrate_engram_token_limit()
    assert kernel.store.get("plugins", PLUGIN_ID) == current_plugin
    assert kernel.store.get("bots", "ada") == current_bot
    assert migration_events(kernel)[before_receipts:] == receipts
    # Normal saves work after migration without requiring the owner to clean raw JSON.
    saved = await kernel.service.save(owner, "plugins", PLUGIN_ID, {"name": "Character memory"})
    assert saved["name"] == "Character memory" and "state_token_limit" not in saved["config"]


def test_migration_rolls_back_all_records_and_receipts_on_mid_transaction_failure(kernel, monkeypatch):
    seed_legacy_settings(kernel)
    before_entities = kernel.store.rows("SELECT * FROM entities ORDER BY kind,id")
    before_events = kernel.store.rows("SELECT * FROM events ORDER BY seq")
    emit = kernel.store.emit
    migrated = 0

    def failing_emit(kind, *args, **kwargs):
        nonlocal migrated
        if kind == "config.migrated":
            migrated += 1
            if migrated == 2:
                raise RuntimeError("synthetic audit-write failure")
        return emit(kind, *args, **kwargs)

    monkeypatch.setattr(kernel.store, "emit", failing_emit)
    with pytest.raises(RuntimeError, match="synthetic audit-write failure"):
        kernel.service.migrate_engram_token_limit()
    assert migrated == 2
    assert kernel.store.rows("SELECT * FROM entities ORDER BY kind,id") == before_entities
    assert kernel.store.rows("SELECT * FROM events ORDER BY seq") == before_events
    monkeypatch.setattr(kernel.store, "emit", emit)
    kernel.service.migrate_engram_token_limit()
    assert "state_token_limit" not in kernel.store.get("plugins", PLUGIN_ID)["config"]


async def test_kernel_startup_migrates_old_settings_before_runtime_and_only_once(tmp_path):
    first = Kernel(tmp_path)
    async with first.lifetime():
        seed_legacy_settings(first)

    second = Kernel(tmp_path)
    async with second.lifetime():
        assert second.started is False
        plugin = second.store.get("plugins", PLUGIN_ID)
        bot = second.store.get("bots", "ada")
        assert "state_token_limit" not in plugin["config"]
        assert "state_token_limit" not in bot["plugin_config"]["engram"]
        receipts = migration_events(second)
        assert len(receipts) == 2

    third = Kernel(tmp_path)
    async with third.lifetime():
        assert third.store.get("plugins", PLUGIN_ID) == plugin
        assert third.store.get("bots", "ada") == bot
        assert migration_events(third) == receipts


@pytest.mark.parametrize(
    "previous_content",
    [
        PREVIOUS_INSTRUCTIONS,
        PREVIOUS_INSTRUCTIONS.replace(
            "When useful information changes, supply a complete replacement MEM and FACTS in your final answer "
            "using the private suffix protocol below. If nothing useful has changed, you may retain the existing "
            "memory without a replacement.",
            "In your final answer, supply a complete replacement MEM and FACTS using the private suffix protocol below.",
        ),
    ],
)
def test_exact_shipped_prompt_upgrades_once_but_custom_edits_are_preserved(kernel, previous_content):
    identifier = "runtime-engram-instructions"
    current = next(prompt for prompt in DEFAULT_PROMPTS if prompt["id"] == identifier)
    assert "state_token_limit" not in current["content"]
    assert "state_char_limit" in current["content"]
    prior = kernel.store.get("prompts", identifier)
    prior = kernel.store.put("prompts", {**prior, "content": previous_content})
    kernel.service.seed_prompt_templates()
    upgraded = kernel.store.get("prompts", identifier)
    assert upgraded["content"] == current["content"]
    assert upgraded["revision"] == prior["revision"] + 1
    receipts = migration_events(kernel)
    assert any(event.get("id") == identifier for event in receipts)
    kernel.service.seed_prompt_templates()
    assert kernel.store.get("prompts", identifier) == upgraded
    assert migration_events(kernel) == receipts

    custom = kernel.store.put(
        "prompts", {**upgraded, "content": previous_content + " Preserve my custom instruction."}
    )
    kernel.service.seed_prompt_templates()
    assert kernel.store.get("prompts", identifier) == custom
    assert migration_events(kernel) == receipts


@pytest.mark.parametrize("placeholder", ["{state_token_limit}", "{{ STATE_TOKEN_LIMIT }}"])
def test_custom_legacy_placeholder_renders_as_unlimited_and_is_not_advertised(kernel, placeholder):
    bot = enabled(kernel)
    memory = kernel.registry.engrams
    snapshot = memory.capture(bot, "room", "prompt-turn")
    default = kernel.store.get("prompts", "runtime-engram-instructions")
    text = f"Custom budget: {placeholder}; characters {{state_char_limit}}."
    kernel.store.put("prompts", {**default, "content": text})
    rendered = layer(kernel.store, bot, "engram_instructions", memory.prompt_values(snapshot))
    assert rendered["content"] == f"Custom budget: unlimited; characters {DEFAULTS['state_char_limit']}."
    assert kernel.store.get("prompts", default["id"])["content"] == text
    assert "state_token_limit" not in PLACEHOLDERS
    protocol = memory.protocol(snapshot)
    assert str(DEFAULTS["state_char_limit"]) in protocol
    assert "cl100k_base" not in protocol and "state_token_limit" not in protocol


def test_historical_token_miss_feedback_says_that_limit_was_removed(kernel):
    bot = enabled(kernel)
    memory = kernel.registry.engrams
    snapshot = memory.capture(bot, "room", "missed-turn")
    generation(kernel, snapshot)
    memory.record_miss(snapshot, "req", "out", "state_token_limit")
    delivered(kernel, snapshot)
    next_snapshot = memory.capture(bot, "room", "next-turn")
    reminder = memory.reminder(next_snapshot)
    assert next_snapshot["missed_updates"] == {"count": 1, "reason": "state_token_limit"}
    assert "token" in reminder and "removed" in reminder
    assert "previous saved memory is still intact" in reminder
