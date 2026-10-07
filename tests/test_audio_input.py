import asyncio
import base64
import copy
import io
import json
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace as NS
import wave
from unittest.mock import AsyncMock

import httpx
import discord
import pytest

from conftest import ingest
from hortator.audio_cache import AudioCache, MAX_AUDIO_BYTES, joined_worker, reuse_audio
from hortator.audio_input import DEFAULTS, audio_parts, audio_reserve, select_audio, validate_config
from hortator.concurrency import task_group
from hortator.models import ControlError
from hortator.provider import Completion
from hortator.transcript import render_transcript
from support.provider import install_client
from support.runtime import completion, settle
from support.typing import prepare_bot
from support.addressing import message


def wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x01\x00" * 16000)
    return output.getvalue()


def attachment(**changes):
    return {
        "id": "123",
        "filename": "voice-message.ogg",
        "size": len(wav()),
        "content_type": "audio/ogg",
        "duration_secs": 9999,
        "url": "https://cdn.discordapp.com/attachments/1/2/voice.ogg?ex=original",
        **changes,
    }


def enable(kernel, bot_id="ada"):
    bot, channel = prepare_bot(kernel, bot_id)
    bot["enabled_plugins"] = [*bot["enabled_plugins"], "audio_input"]
    kernel.store.put("bots", bot)
    plugin = kernel.store.get("plugins", "audio_input")
    kernel.store.put("plugins", {**plugin, "enabled": True})
    return kernel.store.get("bots", bot_id), channel


def cached(kernel, **changes):
    if not shutil.which("ffprobe"):
        pytest.skip("ffprobe required for actual audio container tests")
    cache = kernel.engine.contexts.audio.cache
    return attachment(audio=cache.prepare(wav()), **changes)


def store_attachment(kernel, channel, item):
    kernel.store.execute(
        "UPDATE messages SET attachments=? WHERE channel_id=?", (json.dumps([item]), channel)
    )


@pytest.mark.parametrize("format_", ["wav", "ogg", "mp3", "flac", "aac", "aiff", "m4a", "webm"])
async def test_original_bytes_format_and_duration_probe(kernel, tmp_path, format_):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required for actual format integration")
    source, output = tmp_path / "input.wav", tmp_path / ("output." + format_)
    source.write_bytes(wav())
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), str(output)], check=True, timeout=10)
    raw = output.read_bytes()
    item = attachment(filename=output.name, size=len(raw))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=raw))
    ) as client:
        cache = AudioCache(kernel.store, client)
        captured = await cache.capture(item)
    assert captured["audio"]["status"] == "ready", captured
    assert captured["audio"]["format"] == format_
    assert 0.9 <= captured["audio"]["duration_seconds"] <= 1.2  # actual bytes, not Discord's 9999
    assert cache.read(captured["audio"]) == raw
    assert cache.path(captured["audio"]["sha256"]).stat().st_mode & 0o777 == 0o600
    assert not list(cache.root.glob(".capture-*"))


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "https://elsewhere.test/file"}),
        httpx.Response(200, content=b"not audio"),
        httpx.Response(200, content=b"x", headers={"content-length": str(MAX_AUDIO_BYTES + 1)}),
        httpx.Response(200, content=b""),
    ],
)
async def test_bad_audio_is_explicit_metadata_not_fake_transcription(kernel, response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        result = await AudioCache(kernel.store, client).capture(attachment())
    assert result["audio"]["status"] == "unavailable"
    assert result["audio"]["error"]
    assert "https://" not in result["audio"]["error"]


async def test_untrusted_url_never_downloaded_and_missing_probe_is_clear(kernel, monkeypatch):
    client = AsyncMock()
    cache = AudioCache(kernel.store, client)
    result = await cache.capture(attachment(url="http://127.0.0.1/private"))
    assert "trusted Discord" in result["audio"]["error"]
    client.stream.assert_not_called()
    monkeypatch.setattr(
        "hortator.audio_cache.subprocess.run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=wav()))
    ) as client:
        result = await AudioCache(kernel.store, client).capture(attachment())
    assert "ffprobe installed" in result["audio"]["error"]


def test_reconnect_reuses_outcome_but_replacement_and_refreshed_transport_retry_do_not():
    previous = attachment(audio={"status": "ready", "sha256": "x"})
    assert reuse_audio(attachment(url=previous["url"] + "new"), previous)["audio"] == previous["audio"]
    assert "audio" not in reuse_audio(attachment(size=1), previous)
    previous["audio"] = {"status": "unavailable", "retry_on_refreshed_url": True}
    assert "audio" not in reuse_audio(attachment(url=previous["url"] + "new"), previous)


@pytest.mark.parametrize("stream", [False, True])
async def test_provider_expands_native_audio_and_ledger_keeps_references(kernel, stream):
    bot, channel = enable(kernel)
    item = cached(kernel)
    store_attachment(kernel, channel, item)
    ctx = kernel.engine.contexts
    profile = kernel.store.get("profiles", bot["model_profile_id"])
    profile["stream"] = stream
    messages, meta = await ctx.assemble(bot, profile, channel, kernel.store.transcript(channel), "")
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        if stream:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text='data: {"choices":[{"delta":{"content":"I heard it"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "I heard it"}, "finish_reason": "stop"}]}
        )

    await install_client(kernel, handler)
    result = await kernel.pool.complete(
        bot=bot, profile=profile, messages=messages, tools=[], turn_id="audio-test", context=meta
    )
    assert result.content == "I heard it"
    wire = [
        p
        for m in captured[0]["messages"]
        if isinstance(m["content"], list)
        for p in m["content"]
        if p["type"] == "input_audio"
    ]
    assert wire == [
        {"type": "input_audio", "input_audio": {"data": base64.b64encode(wav()).decode(), "format": "wav"}}
    ]
    stored = kernel.store.one("SELECT body,context FROM requests WHERE id=?", (result.request_id,))
    assert "hortator_audio" in stored["body"] and base64.b64encode(wav()).decode() not in stored["body"]
    assert json.loads(stored["context"])["audio_token_reserve"] == 32
    assert json.loads(stored["context"])["inline_audio_count"] == 1
    assert json.loads(stored["context"])["audio_bytes"] == len(wav())
    assert json.loads(stored["context"])["audio_base64_bytes"] == len(base64.b64encode(wav()))
    assert len(audio_parts(messages)) == 1  # wire expansion does not mutate prompt/ledger


async def test_disabled_bot_gets_metadata_even_with_shared_cached_audio(kernel):
    bot, channel = prepare_bot(kernel)
    item = cached(kernel)
    store_attachment(kernel, channel, item)
    ctx = kernel.engine.contexts
    assert not kernel.store.get("plugins", "audio_input")["enabled"]
    ctx.audio.cache.capture = AsyncMock(side_effect=AssertionError("Disabled means no download"))
    profile = kernel.store.get("profiles", bot["model_profile_id"])
    rows, summary, _, meta = await ctx.prepare(bot, profile, channel, "disabled", [])
    messages, _ = await ctx.assemble(bot, profile, channel, rows, summary, audio_plan=meta["audio_plan"])
    assert not audio_parts(messages)
    assert '"audio_in_this_request":false' in json.dumps(messages).replace('\\"', '"')
    assert "audio metadata only; no sound supplied" in render_transcript(
        ctx.conversation(rows, bot), "conversation", bot["id"]
    )
    ctx.audio.cache.capture.assert_not_awaited()


async def test_selection_survives_compaction_deletion_revokes_and_other_bot_independent(kernel):
    bot, channel = enable(kernel)
    item = cached(kernel)
    store_attachment(kernel, channel, item)
    ctx = kernel.engine.contexts
    profile = kernel.store.get("profiles", bot["model_profile_id"])
    profile.update(compact_threshold=0.01, keep_recent_messages=0)
    calls = []

    async def compact(**kwargs):
        calls.append(kwargs)
        return Completion(
            request_id="compact",
            content="The owner attached audio; it has not been heard yet.",
            finish_reason="stop",
        )

    kernel.pool.complete = compact
    rows, summary, _, meta = await ctx.prepare(bot, profile, channel, "compact-audio", [])
    assert calls and all(not audio_parts(call["messages"]) for call in calls)
    assert not rows
    messages, _ = await ctx.assemble(bot, profile, channel, rows, summary, audio_plan=meta["audio_plan"])
    assert len(audio_parts(messages)) == 1
    for deletion in ("UPDATE messages SET attachments='[]'", "UPDATE messages SET deleted=1"):
        kernel.store.execute(deletion)
        current, _ = await ctx.assemble(bot, profile, channel, rows, summary, audio_plan=meta["audio_plan"])
        assert not audio_parts(current)
        with pytest.raises(ControlError, match="removed or replaced"):
            await ctx.audio.wire_messages(messages, bot, channel)


async def test_audio_tool_rounds_then_metadata_only_after_handled_turn(kernel):
    bot, channel = enable(kernel)
    store_attachment(kernel, channel, cached(kernel))
    captured = []

    def response(request):
        captured.append(json.loads(request.content))
        if len(captured) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "help",
                                        "type": "function",
                                        "function": {"name": "council_silence", "arguments": "{}"},
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            )
        return completion(silence=True)

    await install_client(kernel, response)
    kernel.engine.launch(bot, channel)
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "silent"
    ingest(kernel, content="New topic", discord_id="777777777777777770")
    kernel.engine.launch(bot, channel)
    await settle(kernel)
    assert [
        sum(
            p.get("type") == "input_audio"
            for m in b["messages"]
            if isinstance(m.get("content"), list)
            for p in m["content"]
        )
        for b in captured
    ] == [1, 1, 0]
    # Shared cache is not shared consumption.
    other, _ = enable(kernel, "socrates")
    profile = kernel.store.get("profiles", bot["model_profile_id"])
    messages, _ = await kernel.engine.contexts.assemble(
        other, profile, channel, kernel.store.transcript(channel), ""
    )
    assert len(audio_parts(messages)) == 1


async def test_grant_revocation_and_corrupt_cache_fail_locally(kernel):
    bot, channel = enable(kernel)
    item = cached(kernel)
    store_attachment(kernel, channel, item)
    ctx = kernel.engine.contexts
    profile = kernel.store.get("profiles", bot["model_profile_id"])
    messages, _ = await ctx.assemble(bot, profile, channel, kernel.store.transcript(channel), "")
    ctx.audio.cache.path(item["audio"]["sha256"]).write_bytes(b"broken")
    with pytest.raises(ControlError, match="integrity"):
        await ctx.audio.wire_messages(messages, bot, channel)
    kernel.store.put("bots", {**bot, "enabled_plugins": []})
    with pytest.raises(ControlError, match="revoked"):
        await ctx.audio.wire_messages(messages, bot, channel)


@pytest.mark.parametrize(
    "config,reason",
    [
        ({"max_clips": 1}, "clip_count"),
        ({"max_request_mib": 1}, "audio_bytes"),
        ({"max_duration_seconds": 1}, "audio_duration"),
    ],
)
def test_newest_selection_obeys_all_budgets(config, reason):
    rows = [
        {
            "seq": i,
            "discord_id": str(i),
            "attachments": [
                attachment(audio={"status": "ready", "size": 800000, "duration_seconds": 1, "sha256": str(i)})
            ],
        }
        for i in range(1, 4)
    ]
    before = copy.deepcopy(rows)
    plan = select_audio(rows, 0, {**DEFAULTS, **config}, True)
    assert [p["message_id"] for p in plan["inputs"]] == ["3"]
    assert all(reason in p["reasons"] for p in plan["omitted"])
    assert rows == before
    assert audio_reserve([{"content": plan["inputs"]}]) == 32


@pytest.mark.parametrize("value", [0, -1, True, "3", 1.5, float("nan")])
def test_invalid_operator_budget(value):
    with pytest.raises(ControlError, match="positive integer"):
        validate_config({"max_clips": value})


async def test_download_edit_race_does_not_resurrect_removed_attachment(kernel):
    bot, channel = enable(kernel)
    item = cached(kernel)
    store_attachment(kernel, channel, item)
    ctx = kernel.engine.contexts

    async def capture(_):
        kernel.store.execute("UPDATE messages SET attachments='[]'")
        return item

    ctx.audio.cache.capture = capture
    plan = await ctx.audio.prepare(kernel.store.transcript(channel), 0, bot)
    assert not plan["inputs"]
    assert kernel.store.transcript(channel)[0]["attachments"] == []


async def test_cancellation_joins_file_worker_before_returning():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def work():
        entered.set()
        release.wait(5)
        finished.set()

    async with task_group() as group:
        task = group.create_task(joined_worker(work))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set()


async def test_voice_note_intake_and_raw_edit_preserve_cache_metadata(kernel):
    from hortator.discord_gateway import CouncilClient

    bot, channel = enable(kernel)
    msg = message(666666666666666660, content="", mentions=[bot])
    source = attachment()
    msg.attachments = [
        discord.Attachment(
            data={**source, "proxy_url": source["url"], "waveform": "AA=="}, state=NS(http=None)
        )
    ]
    await kernel.connector.receive(bot["id"], msg)
    row = kernel.store.transcript(channel)[-1]
    assert row["content"] == "" and row["attachments"][0]["duration_secs"] == 9999
    assert kernel.engine.attention(bot)  # no text is required to register directed human input
    item = cached(kernel)
    kernel.store.execute(
        "UPDATE messages SET attachments=? WHERE discord_id=?", (json.dumps([item]), str(msg.id))
    )
    client = CouncilClient(kernel.connector, bot["id"])
    try:
        await client.on_raw_message_edit(
            NS(message_id=msg.id, data={"attachments": [source]}, channel_id=int(channel))
        )
        assert kernel.store.transcript(channel)[-1]["attachments"][0]["audio"] == item["audio"]
        await client.on_raw_message_edit(
            NS(message_id=msg.id, data={"attachments": []}, channel_id=int(channel))
        )
        assert kernel.store.transcript(channel)[-1]["attachments"] == []
    finally:
        await client.close()


async def test_provider_rejection_does_not_consume_audio_and_not_a_model_tool(kernel):
    bot, channel = enable(kernel)
    store_attachment(kernel, channel, cached(kernel))
    await install_client(
        kernel, lambda _: httpx.Response(400, json={"error": {"message": "Model does not support audio"}})
    )
    kernel.engine.launch(bot, channel)
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "failed"
    assert kernel.store.context(bot["id"], channel)["last_seen"] == 0
    row = kernel.store.one("SELECT body,error FROM requests")
    assert "does not support audio" in row["error"]
    assert "hortator_audio" in row["body"]
    assert "audio_input" not in [
        tool["function"]["name"] for tool in json.loads(row["body"]).get("tools", [])
    ]


async def test_cli_backup_carries_original_audio_and_hash_reference(kernel, tmp_path):
    item = cached(kernel)
    bot, channel = enable(kernel)
    store_attachment(kernel, channel, item)
    destination = tmp_path / "backup"
    await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-m",
            "hortator.cli",
            "--data-dir",
            str(kernel.store.path.parent),
            "backup",
            str(destination),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert (destination / "audio" / item["audio"]["sha256"]).read_bytes() == wav()


async def test_preparation_bounds_captures_and_historical_audio_never_downloads(kernel):
    bot, channel = enable(kernel)
    item = cached(kernel)
    kernel.store.execute(
        "UPDATE messages SET attachments=? WHERE channel_id=?",
        (json.dumps([{**item, "id": str(i)} for i in range(7)]), channel),
    )
    ctx = kernel.engine.contexts
    original = ctx.audio.cache.capture
    ctx.audio.cache.capture = AsyncMock(side_effect=original)
    plan = await ctx.audio.prepare(kernel.store.transcript(channel), 0, bot)
    assert ctx.audio.cache.capture.await_count == DEFAULTS["max_clips"]
    assert len(plan["inputs"]) == DEFAULTS["max_clips"]
    sequence = kernel.store.transcript(channel)[0]["seq"]
    ctx.audio.cache.capture.reset_mock()
    plan = await ctx.audio.prepare(kernel.store.transcript(channel), sequence, bot)
    assert plan["historical_clips"] == 7 and not plan["inputs"]
    ctx.audio.cache.capture.assert_not_awaited()
