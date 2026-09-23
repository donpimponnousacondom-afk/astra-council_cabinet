"""Shared discord panels test helpers."""

from conftest import configured

from hortator.plugins import ToolContext

CHANNEL = "222222222222222222"

BUTTONS = [{"label": "Brief me", "prompt": "Give me a factual briefing", "style": "primary"}]


def enable(k, **changes):
    bot = configured(k, enabled_plugins=["discord_panel", "document_site"], **changes)
    for name in bot["enabled_plugins"]:
        plugin = k.store.get("plugins", name)
        plugin.pop("revision")
        plugin["enabled"] = True
        k.store.put("plugins", plugin)
    return bot


async def prepare(k, bot, turn="panel-turn", **changes):
    context = ToolContext(bot, CHANNEL, turn)
    args = {"operation": "prepare", "title": "👑 Loki’s desk", "buttons": BUTTONS, **changes}
    result = await k.registry.call("discord_panel", args, context, "panel-call")
    assert result.get("prepared"), result
    row = k.registry.panels.prepared(context)
    assert row
    return context, row
