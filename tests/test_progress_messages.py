import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import discord
import aiohttp
import httpx
import pytest

from hortator.runtime import DeliveryError
from hortator.plugins import ToolContext
from support.provider import Fragments, install_client
from support.runtime import completion, settle
from support.runtime_feedback import ready, responses, tool
from support.slash_commands import enable, interaction


def mixed(content, *calls, finish="tool_calls"):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"content": content, "tool_calls": list(calls)},
                    "finish_reason": finish,
                }
            ]
        },
    )


def write_call(**changes):
    return tool("memory", {"operation": "write", "key": "topic", "value": "Saved fact", **changes})


async def test_progress_is_sent_before_tools_and_final_does_not_wait_personal_cooldown(kernel):
    _, transport = ready(
        kernel,
        response_protocol="progress",
        enabled_plugins=["memory"],
        cooldown_seconds=120,
        footer_enabled=True,
        footer_template="Final metrics",
    )
    seen = []

    async def send(*args, **kwargs):
        if args[2] == "Saving that now.":
            assert not kernel.store.rows("SELECT * FROM memories")
            assert kwargs["footer"] == ""
        else:
            assert args[2] == "Saved."
            assert kernel.store.one("SELECT value FROM memories")["value"] == "Saved fact"
            assert "Final metrics" in kwargs["footer"]
        return str(888888888888888880 + len(transport.send.await_args_list))

    transport.send.side_effect = send

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            assert "Current response protocol: progress." in str(body["messages"])
            return mixed("Saving that now.", write_call())
        assert transport.send.await_count == 1
        assert responses(body)[-1]["saved"] is True
        assistant = [m for m in body["messages"] if m["role"] == "assistant"][-1]
        assert assistant["content"] == "Saving that now."
        assert assistant["tool_calls"][0]["function"]["name"] == "memory"
        return completion("Saved.")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(seen) == 2 and transport.send.await_count == 2
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    boxes = kernel.store.rows("SELECT content,status,routing FROM outbox ORDER BY created_at")
    assert [row["content"] for row in boxes] == ["Saving that now.", "Saved."]
    assert json.loads(boxes[0]["routing"]) == {"kind": "progress"}
    assert json.loads(boxes[1]["routing"]) == {}
    assert kernel.store.context("ada", "222222222222222222")["last_seen"] == 1
    assert not kernel.store.one(
        "SELECT 1 FROM events WHERE kind='delivery.cooldown' AND json_extract(data,'$.scope')='bot'"
    )


async def test_progress_does_not_hide_argument_errors_from_next_round(kernel):
    _, transport = ready(kernel, response_protocol="progress", enabled_plugins=["memory"])
    transport.send.side_effect = ["888888888888888881", "888888888888888882"]
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return mixed("I will save that.", write_call(value=42))
        assert not responses(json.loads(request.content))[-1]["ok"]
        return completion("The write failed; nothing was saved.")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await settle(kernel)
    assert transport.send.await_count == 2
    assert not kernel.store.rows("SELECT * FROM memories")


@pytest.mark.parametrize("finish", ["length", "content_filter"])
async def test_incomplete_mixed_response_neither_posts_nor_executes(kernel, finish):
    _, transport = ready(kernel, response_protocol="progress", enabled_plugins=["memory"])
    await install_client(kernel, lambda _: mixed("Partial progress", write_call(), finish=finish))
    await kernel.engine.tick()
    await settle(kernel)
    transport.send.assert_not_awaited()
    assert not kernel.store.rows("SELECT * FROM memories")
    assert kernel.store.one("SELECT status FROM turns")["status"] == "failed"


@pytest.mark.parametrize("failure", ["definite", "uncertain", "timeout", "os_error", "payload", "disconnect"])
async def test_progress_delivery_failure_keeps_tools_and_final_answer(kernel, monkeypatch, failure):
    _, transport = ready(kernel, response_protocol="progress", enabled_plugins=["memory"], cooldown_seconds=0)
    monkeypatch.setattr("hortator.runtime.PROGRESS_SEND_TIMEOUT", 0.01)
    requests = []
    stopped_send = asyncio.Event()

    async def send(*args, **kwargs):
        if args[2] == "Saving":
            assert not kernel.store.rows("SELECT * FROM memories")
            if failure == "timeout":
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped_send.set()
            if failure == "os_error":
                raise OSError("Synthetic transport failure")
            if failure == "payload":
                raise aiohttp.ClientPayloadError("Synthetic truncated Discord body")
            if failure == "disconnect":
                raise aiohttp.ServerDisconnectedError("Synthetic Discord disconnect")
            raise DeliveryError("Synthetic send failure", uncertain=failure == "uncertain")
        assert args[2] == "Both saved"
        return "888888888888888888"

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return mixed("Saving", write_call(key="first"))
        assert responses(body)[-1]["saved"] is True
        notices = private_notices(body)
        assert notices
        if len(requests) == 2:
            expected = "not delivered" if failure == "definite" else "unknown"
            assert expected in notices[0]["content"]
            return mixed("Still working", write_call(key="second"))
        assert "limit of 1" in notices[-1]["content"]
        return completion("Both saved")

    transport.send.side_effect = send
    await install_client(kernel, handler)
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 5)
    assert len(requests) == 3 and transport.send.await_count == 2
    assert len(kernel.store.rows("SELECT * FROM memories")) == 2
    boxes = kernel.store.rows("SELECT status FROM outbox ORDER BY created_at")
    assert [row["status"] for row in boxes] == ["failed" if failure == "definite" else "unknown", "sent"]
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    if failure == "timeout":
        assert stopped_send.is_set()


async def test_cancellation_after_progress_is_not_a_completed_answer(kernel):
    _, transport = ready(kernel, response_protocol="progress", enabled_plugins=["memory"])
    following = asyncio.Event()

    async def handler(request):
        if not responses(json.loads(request.content)):
            return mixed("Saving", write_call())
        following.set()
        await asyncio.Event().wait()

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await asyncio.wait_for(following.wait(), 5)
    await kernel.engine.cancel(["ada"], "Owner stopped work")
    await settle(kernel)
    transport.send.assert_awaited_once()
    assert kernel.store.one("SELECT status FROM outbox")["status"] == "sent"
    assert kernel.store.one("SELECT status FROM turns")["status"] == "cancelled"
    assert kernel.store.context("ada", "222222222222222222")["last_seen"] == 0
    assert kernel.store.one("SELECT value FROM memories")["value"] == "Saved fact"


async def test_silence_does_not_publish_accompanying_text(kernel):
    _, transport = ready(kernel, response_protocol="progress")
    await install_client(
        kernel, lambda _: mixed("Must stay silent", tool("council_silence", {"label": "Nothing to add"}))
    )
    await kernel.engine.tick()
    await settle(kernel)
    transport.send.assert_not_awaited()
    assert kernel.store.one("SELECT status FROM turns")["status"] == "silent"


async def test_prepared_attachments_and_panels_wait_for_final_answer(kernel, monkeypatch):
    bot, transport = ready(
        kernel, response_protocol="progress", enabled_plugins=["memory"], cooldown_seconds=0
    )
    transport.send.side_effect = ["888888888888888881", "888888888888888882"]
    panel_lookup = Mock(return_value=None)
    monkeypatch.setattr(kernel.registry.panels, "prepared", panel_lookup)
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            turn_id = kernel.store.one("SELECT id FROM turns")["id"]
            ctx = ToolContext(bot, "222222222222222222", turn_id)
            kernel.registry.artifact(b"Useful output", "text/plain", ".txt", ctx)
            return mixed("", tool("memory", {"operation": "read"}))
        if count == 2:
            artifact_id = kernel.store.one("SELECT id FROM artifacts")["id"]
            return mixed("", tool("discord_attach", {"artifact_ids": [artifact_id]}))
        if count == 3:
            return mixed("Finishing the note.", write_call())
        assert not panel_lookup.called
        assert transport.send.await_args_list[0].args[4] == []
        return completion("Finished, with the file.")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await settle(kernel)
    assert transport.send.await_count == 2
    assert len(transport.send.await_args_list[1].args[4]) == 1
    panel_lookup.assert_called_once()


@pytest.mark.parametrize("invalid", ["duplicate", "over_budget", "mixed_terminal"])
async def test_invalid_batches_do_not_publish_progress_or_partially_write(kernel, invalid):
    _, transport = ready(
        kernel, response_protocol="progress", enabled_plugins=["memory"], max_calls_per_round=1
    )
    calls = [write_call(), write_call()]
    if invalid == "over_budget":
        calls[1]["id"] = "second"
    if invalid == "mixed_terminal":
        calls[1] = tool("council_silence", {"label": "Done"}, "second")
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return mixed("Should not be posted", *calls) if count == 1 else completion(silence=True)

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await settle(kernel)
    transport.send.assert_not_awaited()
    assert not kernel.store.rows("SELECT * FROM memories")


@pytest.mark.parametrize("private", [False, True])
async def test_slash_progress_edits_response_then_final_replaces_it(kernel, private):
    bot = enable(kernel, response_protocol="progress")
    item = interaction(bot, private=private)
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return mixed("Saving", write_call())
        assert item.edit_original_response.await_count == 1
        assert responses(json.loads(request.content))[-1]["saved"] is True
        return completion("Saved")

    await install_client(kernel, handler)
    await kernel.connector.slash.receive("ada", item)
    await settle(kernel)
    assert [call.kwargs["content"] for call in item.edit_original_response.await_args_list] == [
        "Saving",
        "Saved",
    ]
    boxes = kernel.store.rows("SELECT routing FROM outbox ORDER BY created_at")
    assert json.loads(boxes[0]["routing"])["progress"] is True
    assert all(json.loads(row["routing"])["private"] is private for row in boxes)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"


def private_notices(body):
    return [
        message
        for message in body["messages"]
        if message["role"] == "user"
        and message["content"].startswith("Automatic harness notice (private, not sent to Discord):")
    ]


@pytest.mark.parametrize("cap", [0, 1])
async def test_progress_cap_preserves_action_batches_and_adds_one_private_notice(kernel, cap):
    _, transport = ready(
        kernel,
        response_protocol="progress",
        max_progress_messages_per_turn=cap,
        enabled_plugins=["memory"],
        cooldown_seconds=0,
    )
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        notices = private_notices(body)
        assert len(notices) <= 1
        for notice in notices:
            assert f"limit of {cap}" in notice["content"]
            assert body["messages"][body["messages"].index(notice) - 1]["role"] == "tool"
        if len(bodies) <= 3:
            return mixed(f"Progress {len(bodies)}", write_call(key=f"saved-{len(bodies)}"))
        assert len(notices) == 1
        assert len(responses(body)) == 3
        return completion("All three saved")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await settle(kernel)
    expected = (["Progress 1"] if cap else []) + ["All three saved"]
    assert [call.args[2] for call in transport.send.await_args_list] == expected
    assert len(kernel.store.rows("SELECT * FROM memories")) == 3
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    assert not kernel.store.one("SELECT 1 FROM messages WHERE content LIKE 'Automatic harness notice%'")
    events = kernel.store.rows(
        "SELECT data FROM events WHERE kind='request.progress_withheld' AND json_extract(data,'$.reason')='limit'"
    )
    assert len(events) == 3 - cap
    assert all(json.loads(event["data"])["attempts"] == cap for event in events)


async def test_legacy_missing_protocol_keeps_narration_private_and_retains_provider_evidence(kernel):
    bot, transport = ready(kernel, enabled_plugins=["memory"])
    bot.pop("revision")
    bot.pop("response_protocol", None)
    bot.pop("max_progress_messages_per_turn", None)
    kernel.store.put("bots", bot)
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            assert "Current response protocol: narrate." in str(body["messages"])
            return mixed("Unpublished narration", write_call())
        assert responses(body)[-1]["saved"] is True
        assert "narrate mode" in private_notices(body)[0]["content"]
        return completion("Saved")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await settle(kernel)
    assert [call.args[2] for call in transport.send.await_args_list] == ["Saved"]
    row = kernel.store.one("SELECT response FROM requests ORDER BY started_at LIMIT 1")
    recorded = json.loads(row["response"])
    assert recorded["content"] == "Unpublished narration"
    assert len(recorded["tool_calls"]) == 1
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"


async def test_owner_cancellation_during_progress_send_stops_before_actions(kernel):
    _, transport = ready(kernel, response_protocol="progress", enabled_plugins=["memory"])
    sending, stopped = asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        sending.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    transport.send.side_effect = send
    await install_client(kernel, lambda _: mixed("Saving", write_call()))
    await kernel.engine.tick()
    await asyncio.wait_for(sending.wait(), 5)
    await kernel.engine.cancel(["ada"], "Owner stopped work")
    await settle(kernel)
    assert stopped.is_set()
    transport.send.assert_awaited_once()
    assert not kernel.store.rows("SELECT * FROM memories")
    assert kernel.store.one("SELECT status FROM turns")["status"] == "cancelled"
    assert kernel.store.one("SELECT status FROM outbox")["status"] == "unknown"


async def test_busy_progress_channel_skips_notice_without_blocking_tool_execution(kernel):
    _, transport = ready(kernel, response_protocol="progress", enabled_plugins=["memory"], cooldown_seconds=0)
    lock = kernel.engine.room_locks["222222222222222222"]
    await lock.acquire()
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return mixed("Saving", write_call())
        assert responses(body)[-1]["saved"] is True
        assert "not delivered" in private_notices(body)[0]["content"]
        lock.release()
        return completion("Saved")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    try:
        await asyncio.wait_for(settle(kernel), 5)
    finally:
        if lock.locked():
            lock.release()
    assert [call.args[2] for call in transport.send.await_args_list] == ["Saved"]
    assert [row["status"] for row in kernel.store.rows("SELECT status FROM outbox ORDER BY created_at")] == [
        "suppressed",
        "sent",
    ]


@pytest.mark.parametrize("protocol,cap", [("narrate", 1), ("progress", 0), ("progress", 1)])
async def test_slash_protocol_and_progress_cap_keep_tool_results_available(kernel, protocol, cap):
    bot = enable(kernel, response_protocol=protocol, max_progress_messages_per_turn=cap)
    item = interaction(bot)
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) <= 2:
            return mixed(f"Saving {len(bodies)}", write_call(key=f"saved-{len(bodies)}"))
        assert len(responses(body)) == 2
        assert len(private_notices(body)) == 1
        return completion("Saved both")

    await install_client(kernel, handler)
    await kernel.connector.slash.receive("ada", item)
    await settle(kernel)
    published = [call.kwargs["content"] for call in item.edit_original_response.await_args_list]
    assert published == (["Saving 1"] if protocol == "progress" and cap else []) + ["Saved both"]
    assert len(kernel.store.rows("SELECT * FROM memories")) == 2
    sent = kernel.store.rows(
        "SELECT data FROM events WHERE kind='delivery.sent' AND json_extract(data,'$.kind')='progress'"
    )
    assert len(sent) == (1 if protocol == "progress" and cap else 0)


@pytest.mark.parametrize("failure", ["rejected", "timeout"])
async def test_slash_progress_failure_is_best_effort_and_final_replaces_response(
    kernel, monkeypatch, failure
):
    bot = enable(kernel, response_protocol="progress")
    item = interaction(bot)
    monkeypatch.setattr("hortator.runtime.PROGRESS_SEND_TIMEOUT", 0.01)
    bodies = []

    async def edit(**kwargs):
        if kwargs["content"] == "Saving":
            if failure == "timeout":
                await asyncio.Event().wait()
            raise discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"), "Rejected")
        assert kwargs["content"] == "Saved"
        return SimpleNamespace(id=888888888888888888)

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return mixed("Saving", write_call())
        assert responses(body)[-1]["saved"] is True
        expected = "not delivered" if failure == "rejected" else "unknown"
        assert expected in private_notices(body)[0]["content"]
        return completion("Saved")

    item.edit_original_response.side_effect = edit
    await install_client(kernel, handler)
    await kernel.connector.slash.receive("ada", item)
    await settle(kernel)
    assert item.edit_original_response.await_count == 2
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    boxes = kernel.store.rows("SELECT status FROM outbox ORDER BY created_at")
    assert [row["status"] for row in boxes] == ["failed" if failure == "rejected" else "unknown", "sent"]


async def test_fragmented_sse_keeps_mixed_visible_text_and_calls_before_final_answer(kernel):
    _, transport = ready(kernel, response_protocol="progress", enabled_plugins=["memory"])
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) != 1:
            assert responses(body)[-1]["saved"] is True
            return completion("Saved")
        call = write_call()
        packets = [
            {"choices": [{"delta": {"role": "assistant", "content": "Saving "}}]},
            {"choices": [{"delta": {"content": "now", "tool_calls": [{"index": 0, **call}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        payload = (
            "".join("data: " + json.dumps(packet) + "\n\n" for packet in packets) + "data: [DONE]\n\n"
        ).encode()
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Fragments(payload))

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await settle(kernel)
    assert [call.args[2] for call in transport.send.await_args_list] == ["Saving now", "Saved"]
    recorded = json.loads(
        kernel.store.one("SELECT response FROM requests ORDER BY started_at LIMIT 1")["response"]
    )
    assert recorded["content"] == "Saving now" and len(recorded["tool_calls"]) == 1
