"""Shared web search test helpers."""

import httpx
from conftest import configured

from hortator.plugins import ToolContext


def setup(kernel, monkeypatch, handler, *, key="", config=None):
    bot = configured(kernel, enabled_plugins=["web_search"])
    plugin = kernel.store.get("plugins", "web_search")
    plugin.update(enabled=True, config={**plugin["config"], **(config or {})})
    kernel.store.put("plugins", plugin)
    if key:
        kernel.vault.put("plugin/web_search/api_key", key)
    original = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs == {"trust_env": False, "follow_redirects": False}
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return ToolContext(bot, "channel", "turn-search")


async def call(kernel, context, **args):
    return await kernel.registry.call("web_search", {"query": "test search", **args}, context, "search-call")
