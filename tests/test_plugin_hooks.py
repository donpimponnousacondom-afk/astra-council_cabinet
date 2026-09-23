from dataclasses import replace
from types import SimpleNamespace

import pytest

from hortator.app import Kernel
from hortator.models import ControlError
from hortator.plugins import PluginSpec, ToolContext, schema


async def handler(args, context, config, key):
    return {"ok": True}


def spec(**changes):
    return PluginSpec("hook_test", "Hook test", "Installed contract", schema({}), handler, {}, **changes)


def test_legacy_plugin_defaults_preserve_catalog_and_contract(kernel):
    plugin = spec()
    kernel.registry.register(plugin)
    kernel.service.seed_plugins()
    saved = kernel.store.get("plugins", plugin.id)
    saved["description"] = "Saved description"
    public = kernel.service.public("plugins", saved)
    assert public["description"] == "Saved description"
    assert public["keyless"] is False
    assert public["capability_type"] == "tool"
    assert kernel.registry.spec_for(plugin.id, None) is plugin
    assert not saved["enabled"]


async def test_hook_validation_and_references_run_through_service(kernel, owner):
    seen = []

    def validate(kind, entity, store):
        assert store is kernel.store
        seen.append((kind, entity["id"]))
        if kind == "plugins" and entity["id"] == "hook_test" and entity["config"].get("reject"):
            raise ControlError("Hook rejected configuration")

    plugin = spec(
        validate=validate,
        references=lambda kind, entity_id: ["plugins/hook_test"] if kind == "profiles" else [],
        keyless=True,
        installed_description=True,
    )
    kernel.registry.register(plugin)
    kernel.service.seed_plugins()
    with pytest.raises(ControlError, match="Hook rejected configuration"):
        await kernel.service.save(owner, "plugins", plugin.id, {"config": {"reject": True}})
    assert ("plugins", plugin.id) in seen
    assert kernel.store.get("plugins", plugin.id)["config"] == {}
    with pytest.raises(ControlError, match="plugins/hook_test"):
        await kernel.service.delete(owner, "profiles", "balanced")
    saved = kernel.store.get("plugins", plugin.id)
    saved["description"] = "Old seeded text"
    public = kernel.service.public("plugins", saved)
    assert public["description"] == "Installed contract"
    assert public["keyless"] is True


def test_context_hook_cannot_bypass_grants(kernel):
    seen = []
    plugin = spec()

    def context_spec(context):
        seen.append(context)
        return replace(plugin, description="Dynamic contract")

    plugin.context_spec = context_spec
    kernel.registry.register(plugin)
    kernel.service.seed_plugins()
    bot = kernel.store.get("bots", "ada")
    context = ToolContext(bot, "123456", "test-turn")
    assert not any(row["function"]["name"] == plugin.id for row in kernel.registry.schemas(context))
    assert seen == []
    saved = kernel.store.get("plugins", plugin.id)
    saved["enabled"] = True
    kernel.store.put("plugins", saved)
    bot["enabled_plugins"].append(plugin.id)
    kernel.store.put("bots", bot)
    advertised = next(row for row in kernel.registry.schemas(context) if row["function"]["name"] == plugin.id)
    assert advertised["function"]["description"].startswith("Dynamic contract")
    assert "Call with {} for usage only" in advertised["function"]["description"]
    assert seen == [context]
    assert plugin.description == "Installed contract"


async def test_entrypoint_binding_sees_complete_graph_without_starting_tasks(tmp_path, monkeypatch):
    seen = []

    def bind(kernel):
        assert kernel.service.registry is kernel.registry
        assert kernel.service.engine is kernel.engine
        assert kernel.engine.jobs is kernel.jobs
        assert kernel.jobs.engine is kernel.engine
        assert kernel.engine.transport is kernel.connector
        assert kernel.registry.discord_dispatch is not None
        assert kernel.jobs.background is kernel.background
        assert kernel.background.group is None
        assert not kernel.started
        seen.append(kernel)

    def install(registry):
        registry.register(spec(bind=bind))

    monkeypatch.setattr(
        "hortator.plugins.importlib.metadata.entry_points",
        lambda **kwargs: [SimpleNamespace(load=lambda: install)],
    )
    kernel = Kernel(tmp_path)
    async with kernel.lifetime():
        assert seen == [kernel]
        with pytest.raises(RuntimeError, match="already bound"):
            kernel._bind_services()
        assert not kernel.store.get("plugins", "hook_test")["enabled"]


async def test_background_service_survives_without_research_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr("hortator.research_assistant.ResearchAssistant", lambda registry: None)
    kernel = Kernel(tmp_path)
    async with kernel.lifetime():
        assert kernel.jobs is kernel.engine.jobs
        assert kernel.jobs is kernel.registry.jobs
        assert kernel.jobs.handlers == {}
        assert "research_assistant" not in kernel.registry.specs
        assert "research_assistant" not in [row["id"] for row in kernel.store.list("plugins")]
