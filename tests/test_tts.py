import asyncio
import base64
import json

import httpx
import pytest

from conftest import configured
from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator import tts

HTTP_CLIENT = httpx.AsyncClient


def setup_tts(kernel, **config):
    bot = configured(kernel, enabled_plugins=["tts"])
    plugin = kernel.store.get("plugins", "tts")
    plugin.pop("revision")
    plugin.update(
        enabled=True,
        config={
            "endpoint": "https://api.mistral.ai/v1/audio/speech",
            "request_json": {
                "model": "voxtral-mini-tts-2603",
                "voice_id": "configured-id",
                "response_format": "mp3",
            },
            **config,
        },
    )
    kernel.store.put("plugins", plugin)
    kernel.vault.put("plugin/tts/api_key", "global-tts-test-key")
    kernel.vault.put("bot/ada/plugin:tts", "private-tts-test-key")
    return ToolContext(bot, "channel", "tts-turn")


def mock_http(monkeypatch, handler):

    def client(**kwargs):
        assert kwargs == {"trust_env": False, "follow_redirects": False}
        return HTTP_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)


async def test_voice_discovery_auth_pagination_and_generate(kernel, monkeypatch):
    context = setup_tts(kernel)
    calls = []

    async def handle(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer private-tts-test-key"
        if request.method == "GET":
            assert str(request.url).startswith("https://api.mistral.ai/v2/audio/voices?")
            assert request.url.params["page_size"] == "1"
            assert request.url.params["type"] == "preset"
            last = "page_token" in request.url.params
            if last:
                assert request.url.params["page_token"] == "next-token"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "excited" if last else "curious",
                            "name": "Marie",
                            "tags": ["curious"],
                            "description": None,
                        }
                    ],
                    "next_page_token": None if last else "next-token",
                },
            )
        body = json.loads(request.content)
        assert body == {
            "input": "Hello",
            "model": "voxtral-mini-tts-2603",
            "voice_id": "curious",
            "response_format": "mp3",
        }
        return httpx.Response(200, json={"audio_data": base64.b64encode(b"ID3test-speech").decode()})

    mock_http(monkeypatch, handle)
    first = await kernel.registry.call("tts", {"operation": "voices", "page_size": 1}, context, "voices-1")
    assert first["voices"][0]["tags"] == ["curious"]
    assert first["voices"][0]["description"] is None
    assert first["configured_voice"] == "configured-id"
    second = await kernel.registry.call("tts", first["next"], context, "voices-2")
    assert second["next"] is None and second["voices"][0]["id"] == "excited"
    generated = await kernel.registry.call(
        "tts", {"text": "Hello", "voice": first["voices"][0]["id"]}, context, "speech"
    )
    assert generated["mime"] == "audio/mpeg"
    assert (
        kernel.registry.resolve_artifact(generated["artifact_id"], context).read_bytes() == b"ID3test-speech"
    )
    assert len(calls) == 3
    assert kernel.store.get("plugins", "tts")["config"]["request_json"]["voice_id"] == "configured-id"
    assert "private-tts-test-key" not in str(kernel.store.events())


async def test_discovery_help_grants_and_validation_do_not_call_remote(kernel, monkeypatch):
    context = setup_tts(kernel)

    def unexpected(_):
        pytest.fail("No request should be made")

    mock_http(monkeypatch, unexpected)
    help_result = await kernel.registry.call("tts", {}, context, "help")
    assert help_result["usage_only"] and not help_result["executed"]
    for i, args in enumerate(
        [
            {"operation": "voices", "text": "Do not generate"},
            {"voice": "curious"},
            {"text": "Hi", "page_token": "wrong"},
            {"operation": "voices", "page_size": 101},
            {"operation": "voices", "endpoint": "https://another.test"},
        ]
    ):
        result = await kernel.registry.call("tts", args, context, f"invalid-{i}")
        assert result["ok"] is False and result["executed"] is False
    bot = kernel.store.get("bots", "ada")
    bot.pop("revision")
    kernel.store.put("bots", {**bot, "enabled_plugins": []})
    result = await kernel.registry.call("tts", {"operation": "voices"}, context, "revoked")
    assert result["ok"] is False and "not enabled" in result["error"]


async def test_other_provider_reports_unsupported_without_inventing_catalog(kernel, monkeypatch):
    context = setup_tts(
        kernel, endpoint="https://api.openai.com/v1/audio/speech", request_json={"voice": "alloy"}
    )

    def unexpected(_):
        pytest.fail("Do not send credentials to a guessed discovery route")

    mock_http(monkeypatch, unexpected)
    result = await kernel.registry.call("tts", {"operation": "voices"}, context, "unsupported")
    assert result["supported"] is False and result["configured_voice"] == "alloy"
    assert "voices" not in result


async def test_explicit_proxy_adapter_uses_bot_config_and_global_key(kernel, monkeypatch):
    context = setup_tts(kernel)
    kernel.vault.put("bot/ada/plugin:tts", "")
    config = {"api_format": "mistral", "endpoint": "http://localhost:9000/proxy/v1/audio/speech"}
    context.bot["plugin_config"] = {"tts": config}
    seen = []

    def handle(request):
        seen.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer global-tts-test-key"
        return httpx.Response(200, json={"data": [], "next_page_token": None})

    mock_http(monkeypatch, handle)
    result = await kernel.registry.call("tts", {"operation": "voices", "type": "custom"}, context, "proxy")
    assert result["voices"] == [] and result["next"] is None
    assert seen[0].startswith("http://localhost:9000/proxy/v2/audio/voices?")
    config["voices_endpoint"] = "http://localhost:9000/catalog"
    await kernel.registry.call("tts", {"operation": "voices"}, context, "custom-route")
    assert seen[-1].startswith("http://localhost:9000/catalog?")


@pytest.mark.parametrize("status", [401, 404, 429, 500])
async def test_upstream_failure_is_readable_redacted_and_not_an_artifact(kernel, monkeypatch, status):
    context = setup_tts(kernel)

    def handle(_):
        return httpx.Response(status, json={"message": "Voice 'marie' not found. private-tts-test-key"})

    mock_http(monkeypatch, handle)
    for i, args in enumerate([{"operation": "voices"}, {"text": "Hello"}]):
        result = await kernel.registry.call("tts", args, context, f"fail-{i}")
        assert result["ok"] is False
        assert str(status) in result["error"] and "Voice 'marie' not found" in result["error"]
        assert "private-tts-test-key" not in str(result)
    assert not kernel.store.rows("SELECT * FROM artifacts")


@pytest.mark.parametrize("payload", [{}, [], {"audio_data": None}, {"audio_data": ""}, {"audio_data": "??"}])
async def test_malformed_json_audio_creates_no_attachment(kernel, monkeypatch, payload):
    context = setup_tts(kernel)
    mock_http(monkeypatch, lambda _: httpx.Response(200, json=payload))
    result = await kernel.registry.call("tts", {"text": "Hi"}, context, "malformed")
    assert result["ok"] is False and "audio_data" in result["error"]
    assert not kernel.store.rows("SELECT * FROM artifacts")


@pytest.mark.parametrize(
    "payload", [{}, {"data": [None]}, {"data": [{"name": "no id"}]}, {"data": [], "next_page_token": 4}]
)
async def test_malformed_catalog_is_not_success(kernel, monkeypatch, payload):
    context = setup_tts(kernel)
    mock_http(monkeypatch, lambda _: httpx.Response(200, json=payload))
    result = await kernel.registry.call("tts", {"operation": "voices"}, context, "malformed")
    assert result["ok"] is False


async def test_redirect_is_not_followed_and_cancellation_propagates(kernel, monkeypatch):
    context = setup_tts(kernel)
    mock_http(monkeypatch, lambda _: httpx.Response(302, headers={"location": "https://other.test"}))
    result = await kernel.registry.call("tts", {"operation": "voices"}, context, "redirect")
    assert result["ok"] is False and "302" in result["error"]

    async def cancelled(_):
        raise asyncio.CancelledError()

    mock_http(monkeypatch, cancelled)
    with pytest.raises(asyncio.CancelledError):
        await kernel.registry.call("tts", {"operation": "voices"}, context, "cancel")


async def test_bounds_apply_to_catalog_and_decoded_audio(kernel, monkeypatch):
    context = setup_tts(kernel)
    mock_http(monkeypatch, lambda _: httpx.Response(200, content=b" " * 1_000_001))
    result = await kernel.registry.call("tts", {"operation": "voices"}, context, "big-list")
    assert result["ok"] is False and "exceeds" in result["error"]
    # The wire body fits 20 MB, while the decoded attachment exceeds its existing limit.
    monkeypatch.undo()
    mock_http(
        monkeypatch,
        lambda _: httpx.Response(200, json={"audio_data": base64.b64encode(b"x" * 8_000_001).decode()}),
    )
    result = await kernel.registry.call("tts", {"text": "Hi"}, context, "big-audio")
    assert result["ok"] is False and "8 MB" in result["error"]
    assert not kernel.store.rows("SELECT * FROM artifacts")


def test_request_compatibility_and_per_call_voice():
    config = {
        "endpoint": "https://other.test/v1/audio/speech",
        "request_json": {"model": "tts-1", "voice": "alloy", "speed": 1.1},
    }
    original = json.dumps(config)
    body, fmt = tts.speech_body({"text": "Hello", "voice": "echo"}, config)
    assert body == {"model": "tts-1", "voice": "echo", "speed": 1.1, "input": "Hello"}
    assert fmt == "mp3" and json.dumps(config) == original
    assert tts.speech_body({"text": "Hi"}, config)[0]["voice"] == "alloy"
    with pytest.raises(ControlError, match="complete response"):
        tts.speech_body({"text": "Hi"}, {**config, "request_json": {"stream": True}})
