"""Shared agentic runtime test helpers."""

from conftest import configured

from hortator.plugins import ToolContext


def grant(kernel, *names, **changes):
    bot = configured(kernel, enabled_plugins=list(names), **changes)
    for name in names:
        plugin = kernel.store.get("plugins", name)
        plugin.pop("revision")
        kernel.store.put("plugins", {**plugin, "enabled": True})
    return ToolContext(bot, "222222222222222222", "turn-agentic")
