"""Shell resource state changes only the runtime tail, not the reusable prefix."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from conftest import ingest
from hortator.agentic import SAVED_PACKAGE_GUIDANCE, SHELL_ENVIRONMENT_GUIDANCE
from support.agentic_runtime import grant


@pytest.mark.parametrize("storage", ["disposable", "workspace", "filesystem"])
async def test_shell_storage_modes_receive_the_same_installation_and_environment_guidance(kernel, storage):
    context = grant(kernel, "shell", *([] if storage == "disposable" else [storage]))
    spec = kernel.registry.spec_for("shell", context)
    assert spec.description.startswith(SHELL_ENVIRONMENT_GUIDANCE)
    common, storage_guidance = spec.description.split("Storage mode:", 1)
    assert common == SHELL_ENVIRONMENT_GUIDANCE
    assert common.index("uv pip install PACKAGE") < common.index(SAVED_PACKAGE_GUIDANCE)
    assert common.index("pkg install PACKAGE") < common.index(SAVED_PACKAGE_GUIDANCE)
    assert "(conda-forge); installed commands enter PATH" in common
    assert "/tmp is the same storage as /packages/tmp" in common
    assert "no process or shell environment change survives the call" in common
    assert ("without a task or start step" in storage_guidance) == (storage == "filesystem")


async def test_effective_shell_settings_and_usage_are_only_in_the_late_prompt_facts(kernel, monkeypatch):
    context = grant(kernel, "shell", "filesystem", plugin_config={"shell": {"timeout_seconds": 600}})
    bot = context.bot
    ingest(kernel, content="Keep this conversation prefix reusable")
    profile = kernel.store.get("profiles", bot["model_profile_id"])
    scratch = {"active": False, "used_bytes": 0}
    calls = []

    def runtime_facts(bot_id, channel_id, configuration):
        calls.append((bot_id, channel_id, deepcopy(configuration)))
        return {"scratch": deepcopy(scratch), "limits": deepcopy(configuration)}

    monkeypatch.setattr(kernel.registry.agentic.runner, "runtime_facts", runtime_facts)
    kernel.engine.contexts.shell_facts = kernel.registry.agentic.runtime_facts
    extras = [{"role": "assistant", "content": "UNCHANGED_SHELL_EXCHANGE"}]

    async def assemble(selected):
        return await kernel.engine.contexts.assemble(
            selected,
            profile,
            context.channel_id,
            kernel.store.transcript(context.channel_id),
            "saved summary",
            extras=extras,
        )

    bot = {**bot, "attachment_state": {"exported": 2, "prepared": 0, "posted": False}}
    schema_before = kernel.registry.schemas(context)
    before, meta_before = await assemble(bot)
    scratch.update(active=True, used_bytes=123456789)
    plugin = kernel.store.get("plugins", "shell")
    kernel.store.put("plugins", {**plugin, "config": {**plugin["config"], "package_bytes": 1073741824}})
    bot = {**bot, "attachment_state": {"exported": 2, "prepared": 2, "posted": False}}
    after, meta_after = await assemble(bot)

    tail = next(i for i, message in enumerate(before) if '"scratch"' in (message.get("content") or ""))
    assert before[:tail] == after[:tail]
    assert before[tail - 1] == extras[-1]
    assert before[tail] != after[tail]
    assert "123456789" in after[tail]["content"]
    assert "1073741824" in after[tail]["content"]
    assert '"posted":false' in after[tail]["content"]
    assert schema_before == kernel.registry.schemas(context)
    for meta in (meta_before, meta_after):
        layers = [item["id"] for item in meta["prompt_layers"]]
        assert layers.index("transcript") < layers.index("runtime_facts")
    assert calls[-1][:2] == (bot["id"], context.channel_id)
    assert calls[-1][2]["timeout_seconds"] == 600
    assert calls[-1][2]["package_bytes"] == 1073741824
    # The opposite mutation direction must leave the stable instruction prefix
    # intact as well: more conversation/tools, with unchanged memory/settings.
    ingest(kernel, discord_id="555555555555555556", content="A later human follow-up")
    grown, _ = await assemble(bot)
    transcript = next(
        i
        for i, message in enumerate(after)
        if "Keep this conversation prefix reusable" in str(message["content"])
    )
    assert after[:transcript] == grown[:transcript]
    assert after[transcript] != grown[transcript]
    assert schema_before == kernel.registry.schemas(context)


@pytest.mark.parametrize("gate", ["captured_grant", "current_grant", "global_switch"])
async def test_shell_runtime_facts_obey_captured_and_current_grants(kernel, monkeypatch, gate):
    context = grant(kernel, "shell")
    facts = Mock(return_value={"scratch": {"active": False}})
    monkeypatch.setattr(kernel.registry.agentic.runner, "runtime_facts", facts)
    assert kernel.registry.agentic.runtime_facts(context.bot, context.channel_id) == facts.return_value
    facts.reset_mock()
    bot = context.bot
    if gate == "captured_grant":
        bot = {**bot, "enabled_plugins": []}
    elif gate == "current_grant":
        kernel.store.put("bots", {**bot, "enabled_plugins": []})
    else:
        plugin = kernel.store.get("plugins", "shell")
        kernel.store.put("plugins", {**plugin, "enabled": False})
    assert kernel.registry.agentic.runtime_facts(bot, context.channel_id) is None
    facts.assert_not_called()


async def test_timeout_schema_keeps_enforcement_without_rewriting_common_description(kernel):
    context = grant(kernel, "shell")
    initial = kernel.registry.spec_for("shell", context)
    context.bot = {**context.bot, "plugin_config": {"shell": {"timeout_seconds": 600}}}
    changed = kernel.registry.spec_for("shell", context)
    assert initial.description == changed.description
    assert initial.parameters["properties"]["timeout_seconds"]["maximum"] == 90
    assert changed.parameters["properties"]["timeout_seconds"]["maximum"] == 600
