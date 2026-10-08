import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import configured, ingest
from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.runtime import DeliveryError
from hortator.store import dumps
from hortator.voice_notes import convert
from test_frenchy_tts import wav


META = {"duration_secs": 1.2, "waveform": base64.b64encode(bytes([0, 20, 0])).decode()}


def note(kernel, context):
    artifact = kernel.registry.artifact(b"OggS-audio", "audio/ogg", ".ogg", context)
    kernel.store.execute(
        "INSERT INTO artifact_voice_notes VALUES(?,?,?)",
        (artifact["artifact_id"], dumps(META), "Spoken words"),
    )
    return artifact["artifact_id"]


async def deliver(kernel, side_effect=None, count=1):
    bot = configured(kernel, cooldown_seconds=0)
    ingest(kernel)
    profile = kernel.store.get("profiles", bot["model_profile_id"])
    provider = kernel.store.get("providers", profile["provider_id"])
    context = ToolContext(bot, "222222222222222222", "voice-turn")
    ids = [note(kernel, context) for _ in range(count)]
    regular = kernel.registry.artifact(b"text", "text/plain", ".txt", context)
    kernel.engine.transport = SimpleNamespace(
        send=AsyncMock(side_effect=side_effect or ["666666666666666666", "777777777777777777"])
    )

    async def run():
        return await kernel.engine.deliver(
            bot,
            profile,
            provider,
            context,
            "Written answer",
            "555555555555555555",
            [regular["artifact_id"], *ids],
        )

    return run, ids, regular["artifact_id"], context


async def test_separate_receipts_files_text_and_canonical_transcript(kernel):
    run, ids, regular, context = await deliver(kernel)
    parent = await run()
    rows = kernel.store.rows("SELECT * FROM outbox ORDER BY created_at")
    assert len(rows) == 2 and [r["status"] for r in rows] == ["sent", "sent"]
    assert rows[0]["id"] == parent and json.loads(rows[0]["artifacts"]) == [regular]
    assert json.loads(rows[1]["artifacts"]) == ids
    assert json.loads(rows[1]["routing"])["parent_outbox_id"] == parent
    calls = kernel.engine.transport.send.call_args_list
    assert calls[0].args[2] == "Written answer" and len(calls[0].args[4]) == 1
    assert calls[1].args[2] == "" and calls[1].args[3] == rows[0]["discord_id"]
    assert calls[1].kwargs["nonce"] == rows[1]["id"] and calls[1].kwargs["voice_note"] == META
    assert calls[1].kwargs["footer"] == "" and calls[1].kwargs["strict_reply"]
    message = kernel.store.one("SELECT * FROM messages WHERE discord_id=?", (rows[1]["discord_id"],))
    assert message["content"] == "[Voice note transcript]\nSpoken words"
    assert kernel.connector.canonical_edit(message, "") == message["content"]
    # A late echo or history record resolves back to the same canonical transcript.
    echo = SimpleNamespace(
        id=int(rows[1]["discord_id"]),
        nonce=rows[1]["id"],
        channel=SimpleNamespace(id=int(context.channel_id)),
        author=SimpleNamespace(id=int(context.bot["application_id"]), bot=True),
        webhook_id=None,
    )
    assert kernel.connector.reconcile(echo) == message["content"]


@pytest.mark.parametrize(
    "error,status",
    [
        (DeliveryError("rejected"), "failed"),
        (TimeoutError("uncertain"), "unknown"),
        (asyncio.CancelledError(), "unknown"),
    ],
)
async def test_voice_failure_preserves_confirmed_text_without_resend(kernel, error, status):
    run, _, _, _ = await deliver(kernel, ["666666666666666666", error])
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await run()
    else:
        await run()
    rows = kernel.store.rows("SELECT * FROM outbox ORDER BY created_at")
    assert [r["status"] for r in rows] == ["sent", status]
    assert kernel.engine.transport.send.await_count == 2
    assert rows[1]["error"]


async def test_cancel_suppresses_remaining_voice_notes(kernel):
    run, _, _, _ = await deliver(kernel, ["666666666666666666", asyncio.CancelledError()], count=2)
    with pytest.raises(asyncio.CancelledError):
        await run()
    assert [r["status"] for r in kernel.store.rows("SELECT status FROM outbox ORDER BY created_at")] == [
        "sent",
        "unknown",
        "suppressed",
    ]
    assert kernel.engine.transport.send.await_count == 2


async def test_revoked_configuration_after_text_prevents_voice(kernel):
    async def send(*args, **kwargs):
        bot = kernel.store.get("bots", "ada")
        bot.pop("revision")
        kernel.store.put("bots", {**bot, "enabled": False})
        return "666666666666666666"

    run, _, _, _ = await deliver(kernel, send)
    await run()
    assert [r["status"] for r in kernel.store.rows("SELECT status FROM outbox ORDER BY created_at")] == [
        "sent",
        "failed",
    ]
    assert kernel.engine.transport.send.await_count == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_native_gateway_payload_and_file_cleanup(kernel, tmp_path, cancel):
    bot = configured(kernel)
    channel_id = "222222222222222222"
    path = tmp_path / "audio.ogg"
    path.write_bytes(b"audio")
    channel = SimpleNamespace(id=int(channel_id))
    seen = []

    async def send(channel_id, *, params):
        seen.extend(params.files)
        payload = json.loads(params.multipart[0]["value"])
        assert payload["flags"] == 8192 and "content" not in payload and "components" not in payload
        assert payload["attachments"] == [{"id": 0, "filename": "voice-message.ogg", **META}]
        assert payload["enforce_nonce"] and payload["nonce"] == "out_test"
        assert payload["message_reference"]["message_id"] == 555555555555555555
        assert payload["allowed_mentions"]["parse"] == []
        assert params.multipart[1]["content_type"] == "audio/ogg"
        assert params.files[0].fp.read() == b"audio"
        if cancel:
            raise asyncio.CancelledError()
        return {"id": "666666666666666666"}

    client = SimpleNamespace(
        is_ready=lambda: True,
        get_channel=lambda _: channel,
        http=SimpleNamespace(send_message=send),
        close=AsyncMock(),
    )
    kernel.connector.clients[bot["id"]] = client
    call = kernel.connector.send(
        bot,
        channel_id,
        "",
        "555555555555555555",
        [path],
        voice_note=META,
        nonce="out_test",
        strict_reply=True,
    )
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await call
    else:
        assert await call == "666666666666666666"
    assert len(seen) == 1 and seen[0].fp.closed
    with pytest.raises(DeliveryError, match="no text"):
        await kernel.connector.send(bot, channel_id, "caption", None, [path], voice_note=META)


def test_conversion_rejects_overlong_input_before_encoding(tmp_path, monkeypatch):
    monkeypatch.setattr("hortator.voice_notes.probe", lambda _: {"duration_seconds": 1201})
    with pytest.raises(ControlError, match="20 minutes"):
        convert(wav(), tmp_path)
    assert not list(tmp_path.iterdir())
