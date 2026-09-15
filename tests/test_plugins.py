import base64
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from conftest import configured
from hortator.models import ControlError
from hortator.plugins import ToolContext


async def test_image_and_tts_produce_owned_downloadable_artifacts(kernel, monkeypatch):
    bot = configured(kernel, enabled_plugins=["image_generation", "tts"])
    for name in bot["enabled_plugins"]:
        plugin = kernel.store.get("plugins", name)
        plugin.pop("revision")
        kernel.store.put("plugins", {**plugin, "enabled": True})
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jX1kAAAAASUVORK5CYII="
    )
    responses = [
        (json.dumps({"data": [{"b64_json": base64.b64encode(png).decode()}]}).encode(), "application/json"),
        (b"ID3test-audio", "audio/mpeg"),
    ]
    monkeypatch.setattr(kernel.registry, "media_request", AsyncMock(side_effect=responses))
    context = ToolContext(bot, "channel", "turn-media")
    image = await kernel.registry.call(
        "image_generation", {"prompt": "A small test image"}, context, "image-call"
    )
    audio = await kernel.registry.call("tts", {"text": "Test audio"}, context, "tts-call")
    assert image["mime"] == "image/png" and audio["mime"] == "audio/mpeg"
    assert kernel.registry.resolve_artifact(image["artifact_id"], context).read_bytes() == png
    assert kernel.registry.resolve_artifact(audio["artifact_id"], context).read_bytes() == b"ID3test-audio"
    with pytest.raises(ControlError, match="does not belong"):
        kernel.registry.resolve_artifact(
            image["artifact_id"], ToolContext({**bot, "id": "socrates"}, "channel", "turn-media")
        )
    with pytest.raises(ControlError):
        kernel.registry.resolve_artifact(image["artifact_id"], ToolContext(bot, "channel", "another-turn"))


async def test_web_search_uses_bot_override_without_leaking_key(kernel, monkeypatch):
    import hortator.plugins as module

    bot = configured(kernel, enabled_plugins=["web_search"])
    plugin = kernel.store.get("plugins", "web_search")
    plugin.pop("revision")
    kernel.store.put("plugins", {**plugin, "enabled": True})
    kernel.vault.put("plugin/web_search/api_key", "global-test-search-key")
    kernel.vault.put("bot/ada/plugin:web_search", "ada-private-search-key")
    original = httpx.AsyncClient

    async def handle(request):
        assert request.headers["X-Subscription-Token"] == "ada-private-search-key"
        assert request.url.params["q"] == "latest research"
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {"title": "Primary source", "url": "https://example.com", "description": "A result"}
                    ]
                }
            },
        )

    monkeypatch.setattr(
        module.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle))
    )
    result = await kernel.registry.call(
        "web_search", {"query": "latest research"}, ToolContext(bot, "channel", "turn-search"), "search-call"
    )
    assert result["results"][0]["title"] == "Primary source"
    assert "ada-private-search-key" not in str(kernel.store.events())
