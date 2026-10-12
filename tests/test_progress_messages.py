import asyncio
import json
from unittest.mock import Mock

import httpx
import pytest

from hortator.runtime import DeliveryError
from hortator.plugins import ToolContext
from support.provider import install_client
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
            assert "SAME response" in str(body["messages"])
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
    _, transport = ready(kernel, enabled_plugins=["memory"])
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
    _, transport = ready(kernel, enabled_plugins=["memory"])
    await install_client(kernel, lambda _: mixed("Partial progress", write_call(), finish=finish))
    await kernel.engine.tick()
    await settle(kernel)
    transport.send.assert_not_awaited()
    assert not kernel.store.rows("SELECT * FROM memories")
    assert kernel.store.one("SELECT status FROM turns")["status"] == "failed"


@pytest.mark.parametrize("uncertain", [False, True])
async def test_progress_delivery_failure_stops_before_tool_execution(kernel, uncertain):
    _, transport = ready(kernel, enabled_plugins=["memory"])
    transport.send.side_effect = DeliveryError("Synthetic send failure", uncertain=uncertain)
    await install_client(kernel, lambda _: mixed("Saving", write_call()))
    await kernel.engine.tick()
    await settle(kernel)
    assert not kernel.store.rows("SELECT * FROM memories")
    assert kernel.store.one("SELECT status FROM outbox")["status"] == ("unknown" if uncertain else "failed")
    assert kernel.store.context("ada", "222222222222222222")["last_seen"] == 0
    assert kernel.store.one("SELECT status FROM turns")["status"] == "failed"


async def test_cancellation_after_progress_is_not_a_completed_answer(kernel):
    _, transport = ready(kernel, enabled_plugins=["memory"])
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
    _, transport = ready(kernel)
    await install_client(
        kernel, lambda _: mixed("Must stay silent", tool("council_silence", {"label": "Nothing to add"}))
    )
    await kernel.engine.tick()
    await settle(kernel)
    transport.send.assert_not_awaited()
    assert kernel.store.one("SELECT status FROM turns")["status"] == "silent"


async def test_prepared_attachments_and_panels_wait_for_final_answer(kernel, monkeypatch):
    bot, transport = ready(kernel, enabled_plugins=["memory"], cooldown_seconds=0)
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
    _, transport = ready(kernel, enabled_plugins=["memory"], max_calls_per_round=1)
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
    bot = enable(kernel)
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
