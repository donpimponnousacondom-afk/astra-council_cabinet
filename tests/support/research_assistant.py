"""Shared research assistant test helpers."""

from conftest import configured, ingest

from hortator.plugins import ToolContext
from hortator.research_assistant import DEFAULTS, ID


def setup(k, *, stream=True):
    bot = configured(k, interval_seconds=0, enabled_plugins=[ID])
    ingest(k)
    p = k.store.get("profiles", "balanced")
    p.update(
        id="research",
        model="mimo-v2.6-flash",
        stream=stream,
        request_json={"thinking": {"type": "disabled"}, "max_tokens": 17},
    )
    p.pop("revision")
    k.store.put("profiles", p)
    plugin = k.store.get("plugins", ID)
    plugin.update(enabled=True, config={**DEFAULTS, "profile_id": "research"})
    plugin.pop("revision")
    k.store.put("plugins", plugin)
    return ToolContext(bot, "222222222222222222", "research-origin")


async def call(k, context, **args):
    return await k.registry.call(ID, args, context, "research-call")


def start(**changes):
    return {
        "operation": "start",
        "submission_id": "research-test",
        "task": "Find official prices",
        "max_output_tokens": 2048,
        **changes,
    }
