"""Changing memory allowances must not move notes behind growing conversation."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from conftest import configured, ingest
from hortator.slash_commands import SlashContexts


CHANNEL = "222222222222222222"
OTHER_CHANNEL = "222222222222222223"


def template(kernel, key, content, role="system"):
    saved = kernel.store.get("prompts", "runtime-" + key.replace("_", "-"))
    kernel.store.put("prompts", {**saved, "content": content, "role": role})


def setup(kernel, *, note_role="system", budget_role="system"):
    bot = configured(
        kernel,
        enabled_plugins=["memory", "global_memory"],
        memory_char_limit=1000,
        global_memory_char_limit=2000,
        allow_images=False,
    )
    for plugin in ("memory", "global_memory"):
        kernel.store.put("plugins", {**kernel.store.get("plugins", plugin), "enabled": True})
    template(kernel, "memory", "CHANNEL_NOTES:{notes}", note_role)
    template(kernel, "global_memory", "GLOBAL_NOTES:{notes}", note_role)
    template(
        kernel,
        "memory_budget",
        "CHANNEL_BUDGET:{used_chars}/{limit_chars}; remaining={remaining_chars}",
        budget_role,
    )
    template(
        kernel,
        "global_memory_budget",
        "GLOBAL_BUDGET:{used_chars}/{limit_chars}; remaining={remaining_chars}",
        budget_role,
    )
    template(kernel, "summary", "SUMMARY:{summary}")
    template(kernel, "transcript", "TRANSCRIPT:{transcript}", "user")
    kernel.store.execute(
        "INSERT INTO memories VALUES(?,?,?,?,?)", (bot["id"], CHANNEL, "topic", "channel note", 1)
    )
    kernel.store.execute(
        "INSERT INTO global_memories VALUES(?,?,?,?,?)",
        (bot["id"], "preference", "global note", CHANNEL, 1),
    )
    ingest(kernel, content="Original conversation message")
    return bot


def containing(messages, marker):
    return next(index for index, message in enumerate(messages) if marker in (message["content"] or ""))


async def assemble(kernel, bot, *, extras=None, channel_id=CHANNEL, contexts=None):
    return await (contexts or kernel.engine.contexts).assemble(
        bot,
        kernel.store.get("profiles", "balanced"),
        channel_id,
        kernel.store.transcript(channel_id),
        "retained continuity",
        extras=extras,
    )


@pytest.mark.parametrize("note_role", ["system", "user"])
@pytest.mark.parametrize("budget_role", ["system", "user"])
async def test_notes_stay_early_and_budget_roles_follow_unchanged_tool_exchanges(
    kernel, note_role, budget_role
):
    bot = setup(kernel, note_role=note_role, budget_role=budget_role)
    extras = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "read-note", "type": "function", "function": {"name": "memory", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "read-note", "content": "UNCHANGED_TOOL_RECEIPT"},
    ]
    original = deepcopy(extras)
    messages, meta = await assemble(kernel, bot, extras=extras)
    summary = containing(messages, "SUMMARY:")
    transcript = containing(messages, "TRANSCRIPT:")
    receipt = containing(messages, "UNCHANGED_TOOL_RECEIPT")
    for marker in ("CHANNEL_NOTES:", "GLOBAL_NOTES:"):
        index = containing(messages, marker)
        assert index < summary < transcript < receipt
        assert messages[index]["role"] == note_role
    for marker in ("CHANNEL_BUDGET:", "GLOBAL_BUDGET:"):
        index = containing(messages, marker)
        assert index > receipt
        assert messages[index]["role"] == budget_role
    assert messages[receipt - 1 : receipt + 1] == original
    assert extras == original
    layers = [layer["id"] for layer in meta["prompt_layers"]]
    assert layers.index("memory") < layers.index("summary") < layers.index("transcript")
    assert layers.index("global_memory") < layers.index("summary")
    assert layers.index("transcript") < layers.index("memory_budget") < layers.index("runtime_facts")
    assert layers.index("transcript") < layers.index("global_memory_budget") < layers.index("runtime_facts")
    assert not kernel.store.rows("SELECT id FROM requests")


async def test_only_budget_changes_leave_the_note_summary_history_and_exchange_prefix_identical(
    kernel, monkeypatch
):
    bot = setup(kernel)
    monkeypatch.setattr("hortator.context.time", SimpleNamespace(time=lambda: 1_800_000_000))
    extras = [{"role": "assistant", "content": "UNCHANGED_ASSISTANT_EXCHANGE"}]
    before, before_meta = await assemble(kernel, bot, extras=extras)
    bot = kernel.store.put("bots", {**bot, "memory_char_limit": 1500, "global_memory_char_limit": 2500})
    after, after_meta = await assemble(kernel, bot, extras=extras)
    first_budget = containing(before, "CHANNEL_BUDGET:")
    assert containing(after, "CHANNEL_BUDGET:") == first_budget
    assert before[:first_budget] == after[:first_budget]
    assert before[first_budget - 1] == extras[0]
    assert "12/1,000; remaining=988" in before[first_budget]["content"]
    assert "12/1,500; remaining=1,488" in after[first_budget]["content"]
    assert "11/2,000; remaining=1,989" in before[containing(before, "GLOBAL_BUDGET:")]["content"]
    assert "11/2,500; remaining=2,489" in after[containing(after, "GLOBAL_BUDGET:")]["content"]
    for key in ("memory", "global_memory", "summary", "transcript"):
        first = next(layer for layer in before_meta["prompt_layers"] if layer["id"] == key)
        second = next(layer for layer in after_meta["prompt_layers"] if layer["id"] == key)
        assert first == second


async def test_growing_history_keeps_unchanged_notes_in_the_reusable_prefix(kernel):
    bot = setup(kernel)
    before, _ = await assemble(kernel, bot)
    ingest(kernel, content="New conversation message", discord_id="555555555555555556")
    after, _ = await assemble(kernel, bot)
    first_transcript = containing(before, "TRANSCRIPT:")
    assert first_transcript == containing(after, "TRANSCRIPT:")
    assert before[:first_transcript] == after[:first_transcript]
    assert "Original conversation message" in after[first_transcript]["content"]
    assert "New conversation message" in after[first_transcript]["content"]
    assert containing(after, "CHANNEL_NOTES:") < first_transcript
    assert containing(after, "GLOBAL_NOTES:") < first_transcript
    assert containing(after, "CHANNEL_BUDGET:") > first_transcript


@pytest.mark.parametrize("layer", ["memory", "global_memory", "memory_budget", "global_memory_budget"])
async def test_disabling_one_memory_layer_preserves_its_independent_companion_and_saved_notes(kernel, layer):
    bot = setup(kernel)
    saved = kernel.store.rows("SELECT * FROM memories") + kernel.store.rows("SELECT * FROM global_memories")
    bot = kernel.store.put("bots", {**bot, "disabled_prompt_layers": [layer]})
    messages, meta = await assemble(kernel, bot)
    markers = {
        "memory": "CHANNEL_NOTES:",
        "global_memory": "GLOBAL_NOTES:",
        "memory_budget": "CHANNEL_BUDGET:",
        "global_memory_budget": "GLOBAL_BUDGET:",
    }
    content = "\n".join(message["content"] for message in messages)
    assert markers[layer] not in content
    assert layer not in {entry["id"] for entry in meta["prompt_layers"]}
    assert all(marker in content for key, marker in markers.items() if key != layer)
    assert saved == kernel.store.rows("SELECT * FROM memories") + kernel.store.rows(
        "SELECT * FROM global_memories"
    )


@pytest.mark.parametrize("plugin", ["memory", "global_memory"])
@pytest.mark.parametrize("gate", ["captured_grant", "current_grant", "global_switch"])
async def test_note_and_counter_injection_both_obey_current_and_captured_scope(kernel, plugin, gate):
    bot = setup(kernel)
    if gate == "captured_grant":
        bot = {**bot, "enabled_plugins": [name for name in bot["enabled_plugins"] if name != plugin]}
    elif gate == "current_grant":
        kernel.store.put(
            "bots", {**bot, "enabled_plugins": [name for name in bot["enabled_plugins"] if name != plugin]}
        )
    else:
        kernel.store.put("plugins", {**kernel.store.get("plugins", plugin), "enabled": False})
    messages, meta = await assemble(kernel, bot)
    keys = {layer["id"] for layer in meta["prompt_layers"]}
    assert not {plugin, plugin + "_budget"} & keys
    surviving = "memory" if plugin == "global_memory" else "global_memory"
    assert {surviving, surviving + "_budget"} <= keys
    content = "\n".join(message["content"] for message in messages)
    assert ('"value":"channel note"' if plugin == "memory" else '"value":"global note"') not in content


@pytest.mark.parametrize("kind", ["slash", "panel"])
async def test_invocation_memory_and_late_counters_keep_the_existing_isolated_channel_scope(kernel, kind):
    bot = setup(kernel)
    scope = f"{kind}:{bot['id']}:{CHANNEL}"
    kernel.store.execute(
        "INSERT INTO memories VALUES(?,?,?,?,?)", (bot["id"], scope, "topic", "invocation note", 1)
    )
    kernel.store.execute(
        "INSERT INTO memories VALUES(?,?,?,?,?)", (bot["id"], OTHER_CHANNEL, "topic", "other room note", 1)
    )
    kernel.store.execute(
        "INSERT INTO global_memories VALUES(?,?,?,?,?)", ("socrates", "topic", "other bot note", CHANNEL, 1)
    )
    contexts = SlashContexts(
        kernel.store, kernel.pool, {"kind": kind, "channel_id": CHANNEL}, kernel.registry.global_memory
    )
    messages, _ = await assemble(
        kernel,
        bot,
        channel_id=scope,
        contexts=contexts,
        extras=[{"role": "assistant", "content": "INVOCATION_EXCHANGE"}],
    )
    content = "\n".join(message["content"] for message in messages)
    assert "invocation note" in content and "global note" in content
    assert all(
        f'"value":"{value}"' not in content for value in ("channel note", "other room note", "other bot note")
    )
    assert "15/1,000; remaining=985" in content
    assert "11/2,000; remaining=1,989" in content
    assert containing(messages, "GLOBAL_NOTES:") < containing(messages, "TRANSCRIPT:")
    assert containing(messages, "CHANNEL_BUDGET:") > containing(messages, "INVOCATION_EXCHANGE")
