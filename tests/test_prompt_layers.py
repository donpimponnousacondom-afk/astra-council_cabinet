from unittest.mock import AsyncMock

import pytest

from conftest import configured, ingest
from hortator.models import ControlError
from hortator.prompt_templates import DEFAULT_PROMPTS, LAYER_KEYS

CHANNEL = "222222222222222222"


async def assemble(k, bot, summary=""):
    return await k.engine.contexts.assemble(
        bot, k.store.get("profiles", "balanced"), CHANNEL, k.store.transcript(CHANNEL), summary
    )


async def test_all_generated_layers_editable_and_seed_never_overwrites(kernel, owner):
    bot = configured(kernel)
    for item in DEFAULT_PROMPTS:
        assert kernel.store.get("prompts", item["id"])["content"] == item["content"]
    original = kernel.store.get("prompts", "runtime-identity")
    await kernel.service.save(
        owner,
        "prompts",
        original["id"],
        {"content": "Identity for {bot_name}; literal {not_a_variable}", "role": "user"},
    )
    kernel.service.seed_prompt_templates()
    messages, meta = await assemble(kernel, bot)
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Identity for Ada; literal {not_a_variable}"
    assert meta["prompt_layers"][0]["template_id"] == "runtime-identity"
    assert meta["prompt_layers"][0]["revision"] == original["revision"] + 1


async def test_minimal_input_has_no_hidden_instructions_and_other_bot_unchanged(kernel, owner):
    bot = configured(kernel)
    ingest(kernel, content="old stuff", discord_id="old")
    ingest(kernel, content="just this {bot_name}")
    await kernel.service.save(
        owner,
        "prompts",
        "minimal-input",
        {
            "name": "Raw last message",
            "runtime_layer": "transcript",
            "role": "user",
            "content": "{latest_content}",
        },
        create=True,
    )
    bot = await kernel.service.save(
        owner,
        "bots",
        "ada",
        {
            "prompt_ids": [],
            "disabled_prompt_layers": [k for k in LAYER_KEYS if k != "transcript"],
            "prompt_layer_overrides": {"transcript": "minimal-input"},
        },
    )
    messages, meta = await assemble(kernel, bot, "old summary")
    assert messages == [{"role": "user", "content": "just this {bot_name}"}]
    assert meta["message_ids"] == ["555555555555555555"]
    assert meta["summary"] == "" and meta["stored_summary_present"]
    assert [p["id"] for p in meta["prompt_layers"]] == ["transcript"]
    other = kernel.store.get("bots", "socrates")
    assert other["disabled_prompt_layers"] == []
    assert "identity" in [p["id"] for p in kernel.engine.contexts.layers(other, CHANNEL)]


async def test_custom_prompt_role_and_disabled_all_fail_without_sending(kernel, owner):
    bot = configured(kernel, prompt_ids=[], disabled_prompt_layers=list(LAYER_KEYS))
    with pytest.raises(ControlError, match="All prompt layers"):
        await assemble(kernel, bot)
    await kernel.service.save(
        owner, "prompts", "extra", {"name": "Extra", "content": "run this", "role": "user"}, create=True
    )
    bot = await kernel.service.save(owner, "bots", "ada", {"prompt_ids": ["extra"]})
    assert (await assemble(kernel, bot))[0] == [{"role": "user", "content": "run this"}]


async def test_memory_switch_changes_injection_only_and_policy_variants(kernel, owner):
    bot = configured(kernel, enabled_plugins=["memory"], allow_silence=False)
    kernel.store.execute(
        "INSERT INTO memories VALUES(?,?,?,?,?)", ("ada", CHANNEL, "note", "private unique note", 1)
    )
    before = kernel.engine.contexts.layers(bot, CHANNEL)
    assert (
        next(p for p in before if p["id"] == "silence_policy")["template_id"]
        == "runtime-silence-policy-disabled"
    )
    assert "private unique note" in str(before)
    bot = await kernel.service.save(
        owner, "bots", "ada", {"disabled_prompt_layers": ["memory_budget", "memory", "silence_policy"]}
    )
    after = kernel.engine.contexts.layers(bot, CHANNEL)
    assert not {"memory", "memory_budget", "silence_policy"}.intersection(p["id"] for p in after)
    assert kernel.store.one("SELECT value FROM memories")["value"] == "private unique note"
    assert bot["memory_char_limit"] == 48000
    assert bot["enabled_plugins"] == ["memory"]
    assert bot["allow_silence"] is False


@pytest.mark.parametrize(
    "data",
    [
        {"disabled_prompt_layers": ["invented"]},
        {"prompt_layer_overrides": {"invented": "runtime-identity"}},
        {"prompt_layer_overrides": {"identity": "runtime-memory"}},
        {"prompt_layer_overrides": {"identity": "missing"}},
        {"prompt_ids": ["runtime-identity"]},
    ],
)
async def test_prompt_references_strictly_validated(kernel, owner, data):
    configured(kernel)
    with pytest.raises(ControlError):
        await kernel.service.save(owner, "bots", "ada", data)


async def test_runtime_prompt_deletion_placement_and_references(kernel, owner):
    with pytest.raises(ControlError):
        await kernel.service.delete(owner, "prompts", "runtime-identity")
    with pytest.raises(ControlError):
        await kernel.service.save(owner, "prompts", "runtime-identity", {"runtime_layer": "persona"})
    await kernel.service.save(
        owner,
        "prompts",
        "custom",
        {"name": "Custom", "runtime_layer": "identity", "content": "custom"},
        create=True,
    )
    await kernel.service.save(owner, "bots", "ada", {"prompt_layer_overrides": {"identity": "custom"}})
    with pytest.raises(ControlError):
        await kernel.service.delete(owner, "prompts", "custom")
    with pytest.raises(ControlError):
        await kernel.service.save(owner, "prompts", "custom", {"runtime_layer": "persona"})
    assert "ada" not in kernel.service.affected("prompts", "runtime-identity")
    assert kernel.service.affected("prompts", "custom") == ["ada"]


@pytest.mark.parametrize(
    "disabled,template",
    [(["compaction_transcript"], None), ([], "Empty input"), (["compaction_summary"], None)],
)
async def test_disabled_compaction_inputs_do_not_advance_checkpoint(
    kernel, owner, monkeypatch, disabled, template
):
    bot = configured(kernel, disabled_prompt_layers=disabled)
    ingest(kernel)
    ingest(kernel, discord_id="second", content="new message")
    kernel.store.execute("UPDATE contexts SET summary='keep old summary' WHERE bot_id='ada'")
    if template is not None:
        await kernel.service.save(owner, "prompts", "runtime-compaction-transcript", {"content": template})
    profile = kernel.store.get("profiles", "balanced")
    profile["recent_messages"] = 1
    call = AsyncMock()
    monkeypatch.setattr(kernel.pool, "complete", call)
    with pytest.raises(ControlError, match="Compaction .*prompt"):
        await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "test", [], force=True)
    assert not call.called
    assert kernel.store.context("ada", CHANNEL)["summary"] == "keep old summary"
    assert kernel.store.context("ada", CHANNEL)["checkpoint"] == 0


async def test_compaction_custom_roles_and_literal_values_are_used_on_wire(kernel, owner):
    import json
    from test_provider import install_client
    from test_runtime import completion

    bot = configured(kernel, persona="literal {transcript}")
    ingest(kernel, content="history to compact", discord_id="old")
    ingest(kernel, content="new question")
    profile = kernel.store.get("profiles", "balanced")
    await kernel.service.save(
        owner, "prompts", "runtime-compaction-instructions", {"content": "Summarize for {bot_name}"}
    )
    await kernel.service.save(
        owner,
        "prompts",
        "runtime-compaction-transcript",
        {"role": "system", "content": "{persona}\n{transcript}"},
    )
    bodies = []

    def response(request):
        bodies.append(json.loads(request.content))
        return completion("A complete summary")

    await install_client(kernel, response)
    await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "compact", [], force=True)
    assert bodies[0]["messages"][0] == {"role": "system", "content": "Summarize for Ada"}
    assert bodies[0]["messages"][-1]["role"] == "system"
    assert bodies[0]["messages"][-1]["content"].startswith("literal {transcript}\n[")
    assert "history to compact" in bodies[0]["messages"][-1]["content"]
