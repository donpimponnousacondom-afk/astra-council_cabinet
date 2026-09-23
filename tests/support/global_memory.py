"""Shared global memory test helpers."""

from conftest import configured


def enabled(kernel, *, bot_id="ada", limit=48_000):
    plugin = kernel.store.get("plugins", "global_memory")
    plugin.pop("revision")
    kernel.store.put("plugins", {**plugin, "enabled": True})
    return configured(
        kernel, bot_id=bot_id, enabled_plugins=["global_memory"], global_memory_char_limit=limit
    )


async def write(kernel, context, key, value):
    return await kernel.registry.call(
        "global_memory", {"operation": "write", "key": key, "value": value}, context, f"write-{key}"
    )
