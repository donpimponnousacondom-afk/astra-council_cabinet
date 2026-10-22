"""File-only attachment replies retain native voice transport and delivery evidence."""

import json
from types import SimpleNamespace

import pytest

from hortator.plugins import ToolContext
from hortator.runtime import DeliveryError
from support.provider import install_client
from support.runtime import settle
from support.runtime_feedback import ready, reply, tool
from test_voice_notes import META, note


CHANNEL = "222222222222222222"


async def voice_attachment_reply(kernel, bot, count):
    requests, artifact_ids = [], []

    def handler(request):
        requests.append(json.loads(request.content))
        assert len(requests) == 1, "Terminal attachments must not require another generation"
        turn = kernel.store.one("SELECT id FROM turns WHERE bot_id=? AND status='running'", (bot["id"],))
        context = ToolContext(bot, CHANNEL, turn["id"])
        artifact_ids.extend(note(kernel, context) for _ in range(count))
        return reply(tool("discord_attach", {"artifact_ids": artifact_ids}))

    await install_client(kernel, handler)
    return requests, artifact_ids


@pytest.mark.parametrize("mode", ["narrate", "progress", "end_cycle"])
@pytest.mark.parametrize("count", [1, 3])
async def test_file_only_native_notes_send_without_empty_placeholder(kernel, mode, count):
    bot, transport = ready(kernel, response_protocol=mode, enabled_plugins=[], cooldown_seconds=0)
    transport.send.side_effect = [str(888888888888888880 + index) for index in range(count)]
    requests, artifact_ids = await voice_attachment_reply(kernel, bot, count)
    await kernel.engine.tick()
    await settle(kernel)

    turn = kernel.store.one("SELECT status,error FROM turns")
    assert turn["status"] == "sent", turn
    assert len(requests) == 1 and transport.send.await_count == count
    rows = kernel.store.rows("SELECT * FROM outbox ORDER BY created_at")
    assert len(rows) == count and {row["status"] for row in rows} == {"sent"}
    for index, (call, row, artifact_id) in enumerate(
        zip(transport.send.call_args_list, rows, artifact_ids, strict=True)
    ):
        assert call.args[2] == "" and len(call.args[4]) == 1
        assert call.kwargs["voice_note"] == META and call.kwargs["footer"] == ""
        assert "reasoning" not in call.kwargs and "panel" not in call.kwargs
        assert call.kwargs["nonce"] == row["id"]
        assert json.loads(row["artifacts"]) == [artifact_id]
        routing = json.loads(row["routing"])
        assert routing["kind"] == "voice_note"
        if index:
            assert routing["parent_outbox_id"] == rows[0]["id"]
            assert call.args[3] == rows[0]["discord_id"]
        message = kernel.store.one("SELECT * FROM messages WHERE discord_id=?", (row["discord_id"],))
        assert row["content"] == message["content"] == "[Voice note transcript]\nSpoken words"
        assert kernel.connector.canonical_edit(message, "") == message["content"]
        echo = SimpleNamespace(
            id=int(row["discord_id"]),
            nonce=row["id"],
            channel=SimpleNamespace(id=int(CHANNEL)),
            author=SimpleNamespace(id=int(bot["application_id"]), bot=True),
            webhook_id=None,
        )
        assert kernel.connector.reconcile(echo) == message["content"]
    receipt = kernel.store.one(
        "SELECT data FROM events WHERE kind='tool.completed' AND json_extract(data,'$.name')='discord_attach'"
    )
    assert json.loads(receipt["data"])["posted"] is True


@pytest.mark.parametrize("failure", ["rejected", "uncertain"])
async def test_primary_native_failure_never_sends_companions_or_retries(kernel, failure):
    bot, transport = ready(kernel, enabled_plugins=[], cooldown_seconds=0)
    transport.send.side_effect = DeliveryError("Voice delivery fixture", uncertain=failure == "uncertain")
    requests, artifact_ids = await voice_attachment_reply(kernel, bot, 3)
    await kernel.engine.tick()
    await settle(kernel)

    assert len(requests) == 1
    transport.send.assert_awaited_once()
    assert transport.send.await_args.kwargs["voice_note"] == META
    rows = kernel.store.rows("SELECT * FROM outbox")
    assert len(rows) == 1
    assert rows[0]["status"] == ("failed" if failure == "rejected" else "unknown")
    assert json.loads(rows[0]["artifacts"]) == [artifact_ids[0]]
    assert not kernel.store.one(
        "SELECT 1 FROM events WHERE kind='tool.completed' AND json_extract(data,'$.name')='discord_attach'"
    )


@pytest.mark.parametrize("failure", ["rejected", "uncertain"])
async def test_partial_native_delivery_receipt_distinguishes_each_attachment(kernel, failure):
    bot, transport = ready(kernel, enabled_plugins=[], cooldown_seconds=0)
    transport.send.side_effect = [
        "888888888888888880",
        DeliveryError("Companion delivery fixture", uncertain=failure == "uncertain"),
    ]
    requests, artifact_ids = await voice_attachment_reply(kernel, bot, 2)
    await kernel.engine.tick()
    await settle(kernel)

    assert len(requests) == 1 and transport.send.await_count == 2
    rows = kernel.store.rows("SELECT * FROM outbox ORDER BY created_at")
    expected_status = "failed" if failure == "rejected" else "unknown"
    assert [row["status"] for row in rows] == ["sent", expected_status]
    event = kernel.store.one(
        "SELECT data FROM events WHERE kind='tool.failed' AND json_extract(data,'$.name')='discord_attach'"
    )
    assert event is not None
    receipt = json.loads(event["data"])
    assert receipt["ok"] is False and receipt["executed"] is True and receipt["partial"] is True
    assert receipt["posted"] is (False if failure == "rejected" else None)
    assert receipt["attachments"] == [
        {"artifact_id": artifact_ids[0], "status": "sent"},
        {"artifact_id": artifact_ids[1], "status": expected_status},
    ]
    assert receipt["discord_id"] == rows[0]["discord_id"]
    assert not kernel.store.one(
        "SELECT 1 FROM events WHERE kind='tool.completed' AND json_extract(data,'$.name')='discord_attach'"
    )
