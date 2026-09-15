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
from hortator.concurrency import join_tasks


def completion(content="Hello", silence=False):
    fn = "council_silence"
    args = {"label": "Listening"}
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call1",
                                "type": "function",
                                "function": {"name": fn, "arguments": json.dumps(args)},
                            }
                        ]
                    }
                    if silence
                    else {"content": content},
                    "finish_reason": "tool_calls" if silence else "stop",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        },
    )


async def settle(k):
    # Superseded turns are intentionally cancelled; join their finalizers while
    # still propagating unexpected exceptions from every task.
    await join_tasks(*list(k.engine.tasks.values()))
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
        await asyncio.sleep(0.03)
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
    kernel.store.execute("UPDATE bot_runtime SET last_sent=? WHERE bot_id='ada'", (start - 0.8,))
    await kernel.engine.deliver(
        bot, profile, provider, ToolContext(bot, "222222222222222222", "turn1"), "A thought", None, []
    )
    assert time.time() - start >= 0.18
    assert kernel.store.one("SELECT status FROM outbox")["status"] == "sent"
    kernel.store.execute("UPDATE bot_runtime SET last_sent=0 WHERE bot_id='ada'")
    kernel.engine.transport.send.side_effect = DeliveryError("Network disconnected", uncertain=True)
    with pytest.raises(DeliveryError):
        await kernel.engine.deliver(
            bot,
            profile,
            provider,
            ToolContext(bot, "222222222222222222", "turn2"),
            "Another thought",
            None,
            [],
        )
    assert kernel.store.one("SELECT status FROM outbox WHERE turn_id='turn2'")["status"] == "unknown"


async def test_compaction_preserves_original_transcript_and_exact_context(kernel):
    configured(kernel)
    profile = kernel.store.get("profiles", "balanced")
    profile.pop("revision")
    profile.update(
        context_window=4096,
        response_tokens=512,
        summary_tokens=128,
        compact_threshold=0.5,
        keep_recent_messages=2,
        request_json={"max_tokens": 256},
    )
    kernel.store.put("profiles", profile)
    for i in range(35):
        ingest(kernel, content=f"Point {i}: " + "specific idea " * 70, discord_id=str(500000000000000000 + i))

    async def handle(request):
        body = json.loads(request.content)
        if body["messages"][0]["content"].startswith("Summarize"):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "Participants explored specific ideas and left an open question for The Boss."
                            },
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
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
    await install_client(
        kernel,
        lambda r: httpx.Response(
            200, json={"choices": [{"message": {"content": "Incomplete summary"}, "finish_reason": "length"}]}
        ),
    )
    kernel.engine.launch(bot, "222222222222222222", compact_only=True)
    await settle(kernel)
    context = kernel.store.context("ada", "222222222222222222")
    assert context["checkpoint"] == 0 and context["summary"] == ""
    assert kernel.store.one("SELECT status FROM turns")["status"] == "failed"


async def test_restart_does_not_resend_uncertain_messages(kernel):
    for state, turn in (("sending", "one"), ("pending", "two")):
        kernel.store.execute(
            "INSERT INTO outbox(id,turn_id,bot_id,channel_id,content,status,created_at) VALUES(?,?,?,?,?,?,?)",
            (turn, turn, "ada", "channel", "hello", state, time.time()),
        )
    kernel.store.recover()
    assert kernel.store.one("SELECT status FROM outbox WHERE id='one'")["status"] == "unknown"
    assert kernel.store.one("SELECT status FROM outbox WHERE id='two'")["status"] == "suppressed"


async def test_memory_is_scoped_and_profile_swap_preserves_personality(kernel, owner):
    bot = configured(kernel)
    ingest(kernel)
    await kernel.registry.memory(
        {"operation": "write", "key": "note", "value": "Only Ada in this room"},
        ToolContext(bot, "room1", "turn"),
        {},
        "",
    )
    assert not (
        await kernel.registry.memory({"operation": "read"}, ToolContext(bot, "room2", "turn"), {}, "")
    )["notes"]
    other = {**bot, "id": "socrates"}
    assert not (
        await kernel.registry.memory({"operation": "read"}, ToolContext(other, "room1", "turn"), {}, "")
    )["notes"]
    cloned = await kernel.service.control(
        owner, {"action": "clone", "id": "balanced", "data": {"name": "Reasoning low", "id": "low"}}
    )
    assert cloned["id"] == "low"
    await kernel.service.save(owner, "bots", "ada", {"model_profile_id": "low"})
    updated = kernel.store.get("bots", "ada")
    assert updated["persona"] == bot["persona"] and updated["enabled_plugins"] == bot["enabled_plugins"]
    with pytest.raises(ControlError, match="referenced"):
        await kernel.service.delete(owner, "profiles", "low")


async def test_tool_loop_dynamic_prompt_and_reasoning_replay(kernel):
    configured(kernel, enabled_plugins=["memory"])
    ingest(kernel)
    requests = []

    async def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "reasoning_content": "DO-NOT-PUBLISH-THIS-REASONING",
                                "tool_calls": [
                                    {
                                        "id": "memory1",
                                        "type": "function",
                                        "function": {
                                            "name": "memory",
                                            "arguments": '{"operation":"write","key":"idea","value":"A shared topic"}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            )
        return completion("A considered contribution")

    await install_client(kernel, handle)
    transport = AsyncMock()
    transport.send.return_value = "888888888888888888"
    kernel.engine.transport = transport
    await kernel.engine.tick()
    await settle(kernel)
    assert len(requests) == 2
    assert '"rounds_remaining":3' in requests[0]["messages"][-1]["content"]
    assert '"rounds_remaining":2' in requests[1]["messages"][-1]["content"]
    assert any(m.get("reasoning_content") == "DO-NOT-PUBLISH-THIS-REASONING" for m in requests[1]["messages"])
    assert "DO-NOT-PUBLISH-THIS-REASONING" not in str(kernel.store.rows("SELECT * FROM requests"))
    assert "DO-NOT-PUBLISH-THIS-REASONING" not in str(kernel.store.events(limit=500))
    assert transport.send.call_args.args[2] == "A considered contribution"
    assert transport.send.call_args.kwargs["nonce"].startswith("out_")
    assert kernel.store.context("ada", "222222222222222222")["last_seen"] == 1


async def test_global_plugin_switch_is_checked_at_execution(kernel):
    bot = configured(kernel, enabled_plugins=["memory"])
    context = ToolContext(bot, "channel", "turn")
    assert kernel.registry.allowed("memory", context)
    plugin = kernel.store.get("plugins", "memory")
    plugin.pop("revision")
    kernel.store.put("plugins", {**plugin, "enabled": False})
    result = await kernel.registry.call(
        "memory", {"operation": "write", "key": "bad", "value": "should not write"}, context, "denied"
    )
    assert "error" in result
    assert not kernel.store.rows("SELECT * FROM memories")


async def test_two_bots_share_room_delivery_lock_not_context(kernel):
    configured(kernel, interval_seconds=1, cooldown_seconds=1)
    configured(kernel, "socrates", interval_seconds=2, cooldown_seconds=2)
    ingest(kernel)
    kernel.store.context("socrates", "222222222222222222")
    peak = active = 0
    order = []

    class Transport:
        async def send(self, bot, channel_id, content, reply_to, paths, nonce=None, footer=""):
            nonlocal peak, active
            active += 1
            peak = max(peak, active)
            order.append((bot["id"], time.time()))
            await asyncio.sleep(0.03)
            active -= 1
            return str(800000000000000000 + len(order))

    kernel.engine.transport = Transport()
    await install_client(kernel, lambda r: completion())
    await kernel.engine.tick()
    await settle(kernel)
    assert len(order) == 2 and peak == 1
    assert order[1][1] - order[0][1] >= 0.04
    assert kernel.store.runtime("ada")["next_at"] < kernel.store.runtime("socrates")["next_at"]
    turns = kernel.store.rows("SELECT * FROM turns")
    assert {t["bot_id"] for t in turns} == {"ada", "socrates"}
    assert all(t["status"] == "sent" for t in turns)


async def test_profile_edit_cancels_old_generation(kernel, owner):
    configured(kernel)
    ingest(kernel)
    started = asyncio.Event()

    async def handle(request):
        started.set()
        await asyncio.sleep(10)
        return completion()

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await started.wait()
    await kernel.service.save(owner, "profiles", "balanced", {"request_json": {"temperature": 0.2}})
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "cancelled"
    assert not kernel.store.rows("SELECT * FROM outbox")


async def test_nonce_reconciles_uncertain_discord_delivery(kernel):
    from types import SimpleNamespace

    bot = configured(kernel)
    kernel.store.execute(
        "INSERT INTO outbox(id,turn_id,bot_id,channel_id,content,status,created_at) VALUES(?,?,?,?,?,?,?)",
        ("out_nonce", "turn", "ada", "channel", "Canonical full output", "unknown", time.time()),
    )
    msg = SimpleNamespace(
        nonce="out_nonce",
        author=SimpleNamespace(id=int(bot["application_id"]), bot=True),
        webhook_id=None,
        channel=SimpleNamespace(id="channel"),
        id=999,
    )
    assert kernel.connector.reconcile(msg) == "Canonical full output"
    row = kernel.store.one("SELECT * FROM outbox WHERE id='out_nonce'")
    assert row["status"] == "sent" and row["discord_id"] == "999"
    assert len([e for e in kernel.store.events() if e["kind"] == "delivery.reconciled"]) == 1
    kernel.connector.reconcile(msg)
    assert len([e for e in kernel.store.events() if e["kind"] == "delivery.reconciled"]) == 1


async def test_idle_disabled_does_not_reactivate_for_own_output(kernel):
    configured(kernel, evaluate_when_idle=False)
    ingest(kernel)
    await install_client(kernel, lambda r: completion())
    kernel.engine.transport = AsyncMock()
    kernel.engine.transport.send.return_value = "777777777777777777"
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.engine.pick_channel(kernel.store.get("bots", "ada")) is None
    ingest(kernel, discord_id="999999999999999999", content="Another human question")
    assert kernel.engine.pick_channel(kernel.store.get("bots", "ada")) == "222222222222222222"


async def test_room_reassignment_does_not_schedule_or_send_to_old_channel(kernel, owner):
    bot = configured(kernel)
    ingest(kernel)
    assert kernel.engine.channel_allowed(bot, "222222222222222222")
    await kernel.service.save(owner, "rooms", "council", {"channel_id": "999999999999999999"})
    assert not kernel.engine.channel_allowed(bot, "222222222222222222")
    assert kernel.engine.pick_channel(bot) is None
    kernel.engine.transport = AsyncMock()
    profile, provider = kernel.engine.configuration(bot)
    with pytest.raises(asyncio.CancelledError):
        await kernel.engine.deliver(
            bot,
            profile,
            provider,
            ToolContext(bot, "222222222222222222", "old-room"),
            "Stale output",
            None,
            [],
        )
    kernel.engine.transport.send.assert_not_awaited()
    assert kernel.store.one("SELECT status FROM outbox WHERE turn_id='old-room'")["status"] == "suppressed"


async def test_thread_scope_rechecks_live_parent_and_guild_configuration(kernel, owner):
    bot = configured(kernel)
    kernel.store.ingest(
        discord_id="555555555555555555",
        channel_id="thread",
        room_id="council",
        author_id="human",
        author_name="Human",
        content="Thread message",
        guild_id="111111111111111111",
        parent_id="222222222222222222",
    )
    kernel.store.context("ada", "thread")
    assert kernel.engine.channel_allowed(bot, "thread")
    await kernel.service.save(owner, "rooms", "council", {"include_threads": False})
    assert not kernel.engine.channel_allowed(bot, "thread")
