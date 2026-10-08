import asyncio
import base64
import io
import json
import math
import struct
import wave
from unittest.mock import AsyncMock

import httpx
import pytest

from conftest import configured
from hortator import frenchy_tts
from hortator.audio_cache import probe
from hortator.models import ControlError
from hortator.plugins import ToolContext
from test_tts import mock_http


def wav():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(b"".join(struct.pack("<h", int(10000 * math.sin(i / 8))) for i in range(16000)))
    return stream.getvalue()


def setup(kernel, **config):
    bot = configured(kernel, enabled_plugins=["frenchy_tts"])
    plugin = kernel.store.get("plugins", "frenchy_tts")
    plugin.pop("revision")
    plugin.update(enabled=True, config={**plugin["config"], **config})
    kernel.store.put("plugins", plugin)
    kernel.vault.put("plugin/frenchy_tts/api_key", "frenchy-test-global-key")
    kernel.vault.put("bot/ada/plugin:frenchy_tts", "frenchy-test-bot-key")
    return ToolContext(bot, "222222222222222222", "frenchy-turn")


async def test_separate_plugin_default_and_help(kernel):
    assert not kernel.store.get("plugins", "frenchy_tts")["enabled"]
    assert all("frenchy_tts" not in b["enabled_plugins"] for b in kernel.store.list("bots"))
    context = setup(kernel)
    result = await kernel.registry.call("frenchy_tts", {}, context, "help")
    assert result["usage_only"]
    assert "clone_voice" in str(result)
    assert not kernel.registry.allowed("tts", context)


@pytest.mark.parametrize(
    "args",
    [
        {"operation": "clone_voice", "name": "Test"},
        {"operation": "clone_voice", "reference_artifact_id": "art_x"},
        {"text": "Hi", "message_id": "123"},
        {"text": "Hi", "reference_artifact_id": "art_x", "voice": "test"},
        {"text": "Hi", "reference_artifact_id": "art_x", "message_id": "123", "attachment_id": "456"},
        {"operation": "voices", "reference_artifact_id": "art_x"},
        {"text": "Hi", "name": "Test"},
        {"text": "Hi", "page_token": "token"},
    ],
)
async def test_argument_combinations_fail_before_http(kernel, monkeypatch, args):
    context = setup(kernel)
    request = AsyncMock(side_effect=AssertionError("No paid request for invalid arguments"))
    monkeypatch.setattr(kernel.registry, "media_request", request)
    result = await kernel.registry.call("frenchy_tts", args, context, "invalid")
    assert result["ok"] is False and result["usage"]
    request.assert_not_called()


async def test_real_conversion_file_override_and_catalog(kernel, monkeypatch):
    context = setup(kernel)
    bodies = []

    def handle(request):
        assert request.headers["authorization"] == "Bearer frenchy-test-bot-key"
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "marie-id", "name": "Marie", "tags": ["excited"]}],
                    "next_page_token": "next",
                },
            )
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"audio_data": base64.b64encode(wav()).decode()})

    mock_http(monkeypatch, handle)
    catalog = await kernel.registry.call("frenchy_tts", {"operation": "voices"}, context, "voices")
    assert catalog["next_tool"] == "frenchy_tts"
    result = await kernel.registry.call(
        "frenchy_tts", {"text": "Bonjour!", "voice": "marie-id"}, context, "speech"
    )
    assert result["delivery"] == "voice_note"
    path = kernel.registry.resolve_artifact(result["artifact_id"], context)
    assert path.read_bytes().startswith(b"OggS")
    assert 0.9 < probe(path)["duration_seconds"] < 1.1
    meta = kernel.store.one(
        "SELECT * FROM artifact_voice_notes WHERE artifact_id=?", (result["artifact_id"],)
    )
    waveform = base64.b64decode(json.loads(meta["metadata"])["waveform"])
    assert 1 <= len(waveform) <= 256 and any(waveform)
    assert meta["transcript"] == "Bonjour!" and bodies[-1]["voice_id"] == "marie-id"
    result = await kernel.registry.call("frenchy_tts", {"text": "Hi", "delivery": "file"}, context, "file")
    assert result["delivery"] == "file"
    assert not kernel.store.one(
        "SELECT * FROM artifact_voice_notes WHERE artifact_id=?", (result["artifact_id"],)
    )
    assert not list(kernel.registry.directory.glob(".voice-*"))


async def test_clone_and_one_shot_use_owned_audio_and_separate_key(kernel, monkeypatch):
    context = setup(kernel)
    audio = wav()
    artifact = kernel.registry.artifact(audio, "audio/wav", ".wav", context)
    calls = []

    def handle(request):
        calls.append((str(request.url), json.loads(request.content)))
        assert request.headers["authorization"] == "Bearer frenchy-test-bot-key"
        if request.url.path.endswith("/voices"):
            return httpx.Response(200, json={"id": "saved-voice", "name": "Experiment"})
        return httpx.Response(200, content=audio, headers={"content-type": "audio/wav"})

    mock_http(monkeypatch, handle)
    ref = {"reference_artifact_id": artifact["artifact_id"]}
    clone = await kernel.registry.call(
        "frenchy_tts", {"operation": "clone_voice", "name": "Experiment", **ref}, context, "clone"
    )
    assert clone["voice"]["id"] == "saved-voice" and clone["saved_upstream"]
    assert calls[0][0] == "https://api.mistral.ai/v1/audio/voices"
    assert base64.b64decode(calls[0][1]["sample_audio"]) == audio
    assert calls[0][1]["sample_filename"].endswith(".wav")
    result = await kernel.registry.call(
        "frenchy_tts", {"text": "Hi", "delivery": "file", **ref}, context, "reference"
    )
    assert result["artifact_id"] and len(calls) == 2
    body = calls[-1][1]
    assert base64.b64decode(body["ref_audio"]) == audio
    assert "voice_id" not in body and "voice" not in body
    for other in [
        ToolContext(context.bot, context.channel_id, "other-turn"),
        ToolContext({**context.bot, "id": "socrates"}, context.channel_id, context.turn_id),
    ]:
        with pytest.raises(ControlError):
            await kernel.registry.frenchy.reference(ref, other)


async def test_observed_attachment_scope_and_revocation(kernel, monkeypatch):
    context = setup(kernel)
    attachment = {
        "id": "456",
        "filename": "clip.wav",
        "content_type": "audio/wav",
        "url": "https://cdn.discordapp.com/attachments/1/2/a.wav",
        "size": 10,
    }
    kernel.store.ingest(
        discord_id="123",
        channel_id=context.channel_id,
        room_id="council",
        author_id="human",
        author_name="Human",
        content="Audio",
        attachments=[attachment],
    )
    reference = {"message_id": "123", "attachment_id": "456"}
    with pytest.raises(ControlError, match="not observed"):
        await kernel.registry.frenchy.reference(
            reference, ToolContext(context.bot, "another-channel", context.turn_id)
        )
    path = kernel.registry.directory / "reference.wav"
    path.write_bytes(wav())
    monkeypatch.setattr(kernel.registry.frenchy.cache, "path", lambda _: path)

    async def capture(_):
        kernel.store.execute("UPDATE messages SET deleted=1 WHERE discord_id='123'")
        return {"audio": {"status": "ready", "sha256": "fake"}}

    monkeypatch.setattr(kernel.registry.frenchy.cache, "capture", capture)
    with pytest.raises(ControlError, match="not observed"):
        await kernel.registry.frenchy.reference(reference, context)
    artifact = kernel.registry.artifact(wav(), "audio/wav", ".wav", context)

    async def revoke(*_):
        bot = kernel.store.get("bots", "ada")
        bot.pop("revision")
        kernel.store.put("bots", {**bot, "enabled_plugins": []})
        return "encoded", "clip.wav"

    monkeypatch.setattr(frenchy_tts, "joined_worker", revoke)
    with pytest.raises(ControlError, match="revoked"):
        await kernel.registry.frenchy.reference({"reference_artifact_id": artifact["artifact_id"]}, context)


async def test_clone_uncertainty_and_preparation_fallback(kernel, monkeypatch):
    context = setup(kernel)
    artifact = kernel.registry.artifact(wav(), "audio/wav", ".wav", context)
    request = AsyncMock(return_value=(b"{}", "application/json"))
    monkeypatch.setattr(kernel.registry, "media_request", request)
    result = await kernel.registry.call(
        "frenchy_tts",
        {"operation": "clone_voice", "name": "Test", "reference_artifact_id": artifact["artifact_id"]},
        context,
        "bad-clone",
    )
    assert result["ok"] is False and "may have succeeded" in result["error"]
    assert request.await_count == 1
    request.return_value = (b"audio", "audio/mpeg")
    monkeypatch.setattr(
        kernel.registry.voice_notes, "prepare", AsyncMock(side_effect=FileNotFoundError("ffmpeg"))
    )
    result = await kernel.registry.call("frenchy_tts", {"text": "Hello"}, context, "fallback")
    assert result["delivery"] == "file" and "ffmpeg" in result["warning"]
    assert kernel.registry.resolve_artifact(result["artifact_id"], context).read_bytes() == b"audio"
    monkeypatch.setattr(
        kernel.registry.voice_notes, "prepare", AsyncMock(side_effect=asyncio.CancelledError())
    )
    with pytest.raises(asyncio.CancelledError):
        await kernel.registry.call("frenchy_tts", {"text": "Hello"}, context, "cancel")
