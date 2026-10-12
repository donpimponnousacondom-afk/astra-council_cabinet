"""Progress remains optional across scheduling and owner-controlled turn boundaries."""

import asyncio
import json
import time

import httpx
import pytest

from hortator.models import OWNER_ID
from hortator.runtime import DeliveryError
from support.provider import install_client
from support.runtime import completion, settle
from support.runtime_feedback import enable_documents, ready, responses, tool

CHANNEL = "222222222222222222"


def mixed(content, *calls):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"content": content, "tool_calls": list(calls)},
                    "finish_reason": "tool_calls",
                }
            ]
        },
    )


def memory_write():
    return tool("memory", {"operation": "write", "key": "boundary", "value": "Action completed"})


async def test_task_extension_does_not_renew_progress_cap(kernel):
    _, transport = ready(
        kernel,
        response_protocol="progress",
        max_progress_messages_per_turn=1,
        enabled_plugins=["document_site", "memory"],
        max_tool_rounds=1,
        document_task_rounds=2,
        cooldown_seconds=0,
    )
    enable_documents(kernel)
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        if calls == 1:
            return mixed(
                "Creating a local draft.", tool("document_site", {"operation": "create", "site": "draft"})
            )
        if calls == 2:
            assert responses(body)[-1]["task_budget"]["rounds_remaining"] == 2
            return mixed("Saving its note.", memory_write())
        assert responses(body)[-1]["saved"] is True
        assert "progress attempts has been reached" in str(body["messages"])
        return completion("The local draft and note are ready.")

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    assert calls == 3
    assert [call.args[2] for call in transport.send.await_args_list] == [
        "Creating a local draft.",
        "The local draft and note are ready.",
    ]
    assert kernel.store.one("SELECT value FROM memories")["value"] == "Action completed"
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    assert len(kernel.store.rows("SELECT 1 FROM events WHERE kind='tool.task_started'")) == 1
    withheld = kernel.store.one("SELECT data FROM events WHERE kind='request.progress_withheld'")
    assert json.loads(withheld["data"]) == {
        "reason": "limit",
        "response_protocol": "progress",
        "attempts": 1,
        "limit": 1,
    }


@pytest.mark.parametrize("barrier", ["room_busy", "personal_cooldown", "send_gap"])
async def test_unavailable_progress_send_slot_does_not_delay_tools(kernel, barrier):
    _, transport = ready(
        kernel, response_protocol="progress", enabled_plugins=["memory"], cooldown_seconds=600
    )
    room_lock = kernel.engine.room_locks[CHANNEL]
    if barrier == "room_busy":
        await room_lock.acquire()
    elif barrier == "personal_cooldown":
        kernel.store.execute("UPDATE bot_runtime SET last_sent=? WHERE bot_id='ada'", (time.time(),))
    else:
        kernel.engine.room_last[CHANNEL] = time.time() + 600
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return mixed("Saving a note.", memory_write())
        assert responses(json.loads(request.content))[-1]["saved"] is True
        assert kernel.store.one("SELECT value FROM memories")["value"] == "Action completed"
        transport.send.assert_not_awaited()
        # The action and next provider request reached here before the unrelated
        # send barrier was released. Final delivery still honors normal barriers.
        if barrier == "room_busy":
            assert room_lock.locked()
            room_lock.release()
        elif barrier == "personal_cooldown":
            kernel.store.execute("UPDATE bot_runtime SET last_sent=0 WHERE bot_id='ada'")
        else:
            kernel.engine.room_last[CHANNEL] = 0
        return completion("Note saved.")

    await install_client(kernel, handle)
    try:
        await kernel.engine.tick()
        await asyncio.wait_for(settle(kernel), 5)
    finally:
        if barrier == "room_busy" and room_lock.locked():
            room_lock.release()
    assert calls == 2
    transport.send.assert_awaited_once()
    assert transport.send.await_args.args[2] == "Note saved."
    rows = kernel.store.rows("SELECT status FROM outbox ORDER BY created_at")
    assert [row["status"] for row in rows] == ["suppressed", "sent"]
    withheld = kernel.store.one("SELECT data FROM events WHERE kind='request.progress_withheld'")
    assert json.loads(withheld["data"])["reason"] == "delivery_unavailable"
    assert not kernel.store.one("SELECT 1 FROM events WHERE kind='delivery.cooldown'")


@pytest.mark.parametrize("interrupt", ["bot_revoked", "channel_revoked", "superseded"])
@pytest.mark.parametrize("send_fails", [False, True])
async def test_owner_boundary_during_progress_send_stops_pending_actions(kernel, interrupt, send_fails):
    bot, transport = ready(
        kernel, response_protocol="progress", enabled_plugins=["memory"], cooldown_seconds=0
    )

    async def send(*args, **kwargs):
        assert not kernel.store.rows("SELECT * FROM memories")
        if interrupt == "bot_revoked":
            kernel.store.put("bots", {**kernel.store.get("bots", bot["id"]), "enabled": False})
        elif interrupt == "channel_revoked":
            room = kernel.store.get("rooms", "council")
            kernel.store.put("rooms", {**room, "channel_id": "999999999999999999"})
        else:
            kernel.store.ingest(
                discord_id="555555555555555556",
                channel_id=CHANNEL,
                room_id="council",
                author_id=OWNER_ID,
                author_name="The Boss",
                content="Stop that request; answer the new question instead.",
                addressing={"author_kind": "human", "live": True, "targets": [{"bot_id": bot["id"]}]},
            )
        if send_fails:
            raise DeliveryError("Synthetic optional delivery failure")
        return "888888888888888888"

    transport.send.side_effect = send
    await install_client(kernel, lambda _: mixed("Saving a note.", memory_write()))
    await kernel.engine.tick()
    await settle(kernel)
    transport.send.assert_awaited_once()
    assert not kernel.store.rows("SELECT * FROM memories")
    assert not kernel.store.one("SELECT 1 FROM events WHERE kind='tool.started'")
    assert kernel.store.one("SELECT status FROM turns")["status"] == "cancelled"
    assert kernel.store.one("SELECT status FROM outbox")["status"] == ("failed" if send_fails else "sent")
    assert not kernel.store.one("SELECT 1 FROM events WHERE kind='request.progress_withheld'")


@pytest.mark.parametrize("send_fails", [False, True])
async def test_task_deadline_during_progress_send_stops_pending_actions(kernel, send_fails):
    _, transport = ready(
        kernel,
        response_protocol="progress",
        enabled_plugins=["document_site", "memory"],
        # Short internal fixture deadline; production validation retains its
        # existing 30-second minimum. The send runs after the task starts.
        document_task_seconds=0.2,
        cooldown_seconds=0,
    )
    enable_documents(kernel)
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return mixed("", tool("document_site", {"operation": "create", "site": "draft"}))
        return mixed("Saving a note.", memory_write())

    async def send(*args, **kwargs):
        await asyncio.sleep(0.3)
        if send_fails:
            raise DeliveryError("Synthetic optional delivery failure")
        return "888888888888888888"

    transport.send.side_effect = send
    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    assert calls == 2
    transport.send.assert_awaited_once()
    assert not kernel.store.rows("SELECT * FROM memories")
    turn = kernel.store.one("SELECT status,error FROM turns")
    assert turn["status"] == "failed" and "time budget exhausted" in turn["error"]
    assert kernel.store.one("SELECT status FROM outbox")["status"] == ("failed" if send_fails else "sent")
    assert not kernel.store.one("SELECT 1 FROM events WHERE kind='request.progress_withheld'")
