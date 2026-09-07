import asyncio
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from conftest import configured, ingest
from test_provider import install_client
from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.runtime import DeliveryError


def completion(content="Hello", silence=False):
    fn = "council_silence" if silence else "council_speak"
    args = {"label": "Listening"} if silence else {"content": content}
    return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"id": "call1", "type": "function", "function": {"name": fn, "arguments": json.dumps(args)}}]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 100, "completion_tokens": 10}})


async def settle(k):
    await asyncio.gather(*list(k.engine.tasks.values()))
    await asyncio.sleep(0)


async def test_duplicate_events_one_turn_and_independent_silence(kernel):
    bot = configured(kernel)
    ingest(kernel)
    ingest(kernel)
    assert len(kernel.store.rows("SELECT * FROM messages")) == 1
    calls = 0
    async def handle(request):
        nonlocal calls
        calls += 1
        await asyncio.sleep(.03)
        return completion(silence=True)
    await install_client(kernel, handle)
    await asyncio.gather(kernel.engine.tick(), kernel.engine.tick(), kernel.engine.tick())
    await settle(kernel)
    assert calls == 1
    assert kernel.store.one("SELECT status FROM turns")["status"] == "silent"
    assert kernel.store.context(bot["id"], "222222222222222222")["last_seen"] == 1
    await kernel.engine.tick()
    assert not kernel.engine.tasks
    assert kernel.store.runtime(bot["id"])["next_at"] > time.time()
    assert not kernel.store.rows("SELECT * FROM outbox")


async def test_pause_cancels_active_http_and_suppresses_delivery(kernel, owner):
    configured(kernel)
    ingest(kernel)
    started = asyncio.Event()
    async def handle(request):
        started.set()
        await asyncio.sleep(30)
        return completion()
    await install_client(kernel, handle)
    kernel.engine.transport = AsyncMock()
    await kernel.engine.tick()
    await started.wait()
    await kernel.service.control(owner, {"action": "stop", "id": "all"})
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "cancelled"
    assert kernel.store.one("SELECT status FROM requests")["status"] == "cancelled"
    kernel.engine.transport.send.assert_not_awaited()
    assert kernel.store.health("openrouter")["consecutive_failures"] == 0


async def test_context_claim_does_not_consume_arrivals_during_generation(kernel):
    configured(kernel)
    ingest(kernel)
    async def handle(request):
        ingest(kernel, content="Arrived while model was running", discord_id="666666666666666666")
        return completion(silence=True)
    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.store.context("ada", "222222222222222222")["last_seen"] == 1
    assert kernel.engine.pick_channel(kernel.store.get("bots", "ada")) == "222222222222222222"
    body = json.loads(kernel.store.one("SELECT body FROM requests")["body"])
    assert "Arrived while model was running" not in json.dumps(body)


async def test_delivery_cooldown_and_ambiguous_outcome(kernel):
    bot = configured(kernel, cooldown_seconds=1)
    profile, provider = kernel.engine.configuration(bot)
    ingest(kernel)
    kernel.engine.transport = AsyncMock()
    kernel.engine.transport.send.return_value = "777777777777777777"
    start = time.time()
    kernel.store.execute("UPDATE bot_runtime SET last_sent=? WHERE bot_id='ada'", (start-.8,))
    await kernel.engine.deliver(bot, profile, provider, ToolContext(bot, "222222222222222222", "turn1"), "A thought", None, [])
    assert time.time() - start >= .18
    assert kernel.store.one("SELECT status FROM outbox")["status"] == "sent"
    kernel.store.execute("UPDATE bot_runtime SET last_sent=0 WHERE bot_id='ada'")
    kernel.engine.transport.send.side_effect = DeliveryError("Network disconnected", uncertain=True)
    with pytest.raises(DeliveryError):
        await kernel.engine.deliver(bot, profile, provider, ToolContext(bot, "222222222222222222", "turn2"), "Another thought", None, [])
    assert kernel.store.one("SELECT status FROM outbox WHERE turn_id='turn2'")["status"] == "unknown"


async def test_compaction_preserves_original_transcript_and_exact_context(kernel):
    bot = configured(kernel)
    profile = kernel.store.get("profiles", "balanced")
    profile.pop("revision")
    profile.update(context_window=4096, response_tokens=512, summary_tokens=128, compact_threshold=.5, keep_recent_messages=2, request_json={"max_tokens": 256})
    kernel.store.put("profiles", profile)
    for i in range(35):
        ingest(kernel, content=f"Point {i}: " + "specific idea " * 70, discord_id=str(500000000000000000+i))
    async def handle(request):
        body = json.loads(request.content)
        if body["messages"][0]["content"].startswith("Summarize"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "Participants explored specific ideas and left an open question for The Boss."}, "finish_reason": "stop"}]})
        return completion(silence=True)
    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    context = kernel.store.context("ada", "222222222222222222")
    assert context["compactions"] == 1
    assert context["checkpoint"] > 0
    assert len(kernel.store.rows("SELECT * FROM messages")) == 35
    events = kernel.store.events(limit=500)
    compacted = next(e for e in events if e["kind"] == "compaction.completed")
    assert compacted["data"]["after_tokens"] < compacted["data"]["before_tokens"]
    request = kernel.store.one("SELECT body,context FROM requests WHERE purpose='generation'")
    assert context["summary"] in request["body"]
    meta = json.loads(request["context"])
    assert meta["message_ids"] == compacted["data"]["retained_message_ids"]
    assert "1482143139828596916" in request["body"]


async def test_failed_compaction_keeps_checkpoint(kernel):
    bot = configured(kernel)
    ingest(kernel)
    await install_client(kernel, lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "Incomplete summary"}, "finish_reason": "length"}]}))
    kernel.engine.launch(bot, "222222222222222222", compact_only=True)
    await settle(kernel)
    context = kernel.store.context("ada", "222222222222222222")
    assert context["checkpoint"] == 0 and context["summary"] == ""
    assert kernel.store.one("SELECT status FROM turns")["status"] == "failed"


async def test_restart_does_not_resend_uncertain_messages(kernel):
    for state, turn in (("sending", "one"), ("pending", "two")):
        kernel.store.execute("INSERT INTO outbox(id,turn_id,bot_id,channel_id,content,status,created_at) VALUES(?,?,?,?,?,?,?)", (turn, turn, "ada", "channel", "hello", state, time.time()))
    kernel.store.recover()
    assert kernel.store.one("SELECT status FROM outbox WHERE id='one'")["status"] == "unknown"
    assert kernel.store.one("SELECT status FROM outbox WHERE id='two'")["status"] == "suppressed"


async def test_memory_is_scoped_and_profile_swap_preserves_personality(kernel, owner):
    bot = configured(kernel)
    ingest(kernel)
    await kernel.registry.memory({"operation": "write", "key": "note", "value": "Only Ada in this room"}, ToolContext(bot, "room1", "turn"), {}, "")
    assert not (await kernel.registry.memory({"operation": "read"}, ToolContext(bot, "room2", "turn"), {}, ""))["notes"]
    other = {**bot, "id": "socrates"}
    assert not (await kernel.registry.memory({"operation": "read"}, ToolContext(other, "room1", "turn"), {}, ""))["notes"]
    cloned = await kernel.service.control(owner, {"action": "clone", "id": "balanced", "data": {"name": "Reasoning low", "id": "low"}})
    assert cloned["id"] == "low"
    await kernel.service.save(owner, "bots", "ada", {"model_profile_id": "low"})
    updated = kernel.store.get("bots", "ada")
    assert updated["persona"] == bot["persona"] and updated["enabled_plugins"] == bot["enabled_plugins"]
    with pytest.raises(ControlError, match="referenced"):
        await kernel.service.delete(owner, "profiles", "low")
