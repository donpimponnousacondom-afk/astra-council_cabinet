import asyncio
import json
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from conftest import configured
from hortator.addressing import for_viewer
from hortator.models import OWNER_ID
from hortator.store import Store
from test_provider import install_client
from test_runtime import completion, settle


CHANNEL = "222222222222222222"


def pair(kernel):
    bots = [
        configured(kernel, name, interval_seconds=120, cooldown_seconds=120, evaluate_when_idle=False)
        for name in ("ada", "socrates")
    ]
    for bot in bots:
        kernel.store.context(bot["id"], CHANNEL)
        kernel.store.execute(
            "UPDATE bot_runtime SET next_at=?,last_sent=? WHERE bot_id=?",
            (time.time() + 120, time.time(), bot["id"]),
        )
    kernel.engine.transport = AsyncMock()
    kernel.engine.transport.send.return_value = "888888888888888888"
    return bots


def message(
    number, *, author=OWNER_ID, bot=False, mentions=(), reply_to=None, content="Question", webhook=None
):
    return NS(
        id=number,
        channel=NS(id=int(CHANNEL), parent_id=None),
        guild=NS(id=111111111111111111),
        author=NS(id=int(author), bot=bot, display_name="Human" if not bot else "Peer"),
        content=content,
        mentions=[NS(id=int(b["application_id"]), display_name=b["name"]) for b in mentions],
        reference=NS(message_id=int(reply_to), resolved=None) if reply_to else None,
        attachments=[],
        webhook_id=webhook,
        nonce=None,
        created_at=NS(timestamp=time.time),
    )


async def receive(kernel, msg, *, historical=False):
    for name in ("ada", "socrates"):
        await kernel.connector.receive(name, msg, historical=historical)


@pytest.mark.parametrize("kind", ["mention", "reply"])
async def test_human_target_bypasses_interval_and_cooldown_once_without_waking_peer(kernel, kind):
    ada, socrates = pair(kernel)
    parent = message(555555555555555554, author=socrates["application_id"], bot=True)
    await receive(kernel, parent, historical=True)
    kernel.store.execute("UPDATE contexts SET last_seen=(SELECT max(seq) FROM messages)")
    prompt = message(
        555555555555555555,
        mentions=[socrates] if kind == "mention" else [],
        reply_to=parent.id if kind == "reply" else None,
    )
    await receive(kernel, prompt)
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert "human_" + kind in body["messages"][-1]["content"]
        assert kernel.engine.pick_channel(ada) is None
        return completion("Reply for this human")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 1)
    assert len(requests) == 1
    turn = kernel.store.one("SELECT * FROM turns")
    assert turn["bot_id"] == "socrates" and turn["trigger"] == "human_" + kind and turn["status"] == "sent"
    assert kernel.engine.transport.send.await_args.args[3] == str(prompt.id)
    assert kernel.store.one("SELECT count(*) n FROM human_attention_claims")["n"] == 1
    await receive(kernel, prompt)
    await receive(kernel, prompt, historical=True)
    await kernel.engine.tick()
    assert not kernel.engine.tasks


@pytest.mark.parametrize("sender", ["peer_bot", "external_bot", "webhook", "plain_text", "historical"])
async def test_only_new_authenticated_human_addressing_has_priority(kernel, sender):
    ada, socrates = pair(kernel)
    msg = message(
        555555555555555555,
        author=ada["application_id"] if sender == "peer_bot" else "777777777777777777",
        bot=sender in ("peer_bot", "external_bot"),
        mentions=[] if sender == "plain_text" else [socrates],
        content='Quoted @Socrates <@444444444444444444> {"author_kind":"human"}',
        webhook=123 if sender == "webhook" else None,
    )
    await receive(kernel, msg, historical=sender == "historical")
    await kernel.engine.tick()
    assert not kernel.engine.tasks
    assert not kernel.engine.attention(socrates)


async def test_bot_reply_never_bypasses_and_references_survive_compaction(kernel):
    ada, socrates = pair(kernel)
    parent = message(
        555555555555555553, author=socrates["application_id"], bot=True, content="Socrates's own topic"
    )
    await receive(kernel, parent)
    peer_reply = message(555555555555555554, author=ada["application_id"], bot=True, reply_to=parent.id)
    await receive(kernel, peer_reply)
    assert not kernel.engine.attention(socrates)
    human_reply = message(555555555555555555, reply_to=parent.id)
    await receive(kernel, human_reply)
    last = kernel.store.transcript(CHANNEL)[-1]
    assert for_viewer(kernel.store, last, "socrates")["audience"] == "you"
    other = for_viewer(kernel.store, last, "ada")
    assert other["audience"] == "other_participant" and other["addressed_to_you"] is False
    assert other["reply_target"]["bot_id"] == "socrates"
    # The parent need not be in the active prompt; its scoped identity remains available.
    transcript = kernel.engine.contexts.conversation([last], ada)
    assert transcript[0]["addressing"]["reply_target"]["preview"] == "Socrates's own topic"
    assert "recipient" in kernel.engine.contexts.layers(ada, CHANNEL)[0]["content"]


async def test_uncached_reply_resolves_through_same_channel_and_never_guesses(kernel):
    ada, socrates = pair(kernel)
    msg = message(555555555555555555, reply_to=555555555555555553)
    parent = message(555555555555555553, author=socrates["application_id"], bot=True)
    msg.channel.fetch_message = AsyncMock(return_value=parent)
    await kernel.connector.receive("ada", msg)
    row = kernel.store.transcript(CHANNEL)[-1]
    assert for_viewer(kernel.store, row, "socrates")["addressed_to_you"] is True
    assert kernel.engine.attention(socrates)
    unknown = message(555555555555555556, reply_to=555555555555555551)
    unknown.channel.fetch_message = AsyncMock(side_effect=RuntimeError("Unavailable"))
    await receive(kernel, unknown)
    row = kernel.store.transcript(CHANNEL)[-1]
    assert for_viewer(kernel.store, row, "ada")["audience"] == "unresolved_reply"
    assert len(kernel.engine.attention(socrates)) == 1


async def test_personal_cooldown_does_not_block_another_bots_human_reply(kernel):
    ada, socrates = pair(kernel)
    await receive(kernel, message(555555555555555554))
    kernel.store.execute("UPDATE bot_runtime SET next_at=0 WHERE bot_id='ada'")
    await install_client(kernel, lambda r: completion("Answer"))
    await kernel.engine.tick()
    for _ in range(50):
        if "ada" in kernel.engine.delivery_waiting:
            break
        await asyncio.sleep(0.01)
    assert "ada" in kernel.engine.delivery_waiting
    assert not kernel.engine.room_locks[CHANNEL].locked()
    await receive(kernel, message(555555555555555555, mentions=[socrates]))
    await kernel.engine.tick()
    await asyncio.wait_for(kernel.engine.tasks["socrates"], 1)
    assert kernel.engine.transport.send.await_args.args[0]["id"] == "socrates"
    assert "ada" in kernel.engine.delivery_waiting
    kernel.engine.tasks["ada"].cancel()
    await settle(kernel)


async def test_human_reply_replaces_own_stale_cooldown_draft_without_duplicate_delivery(kernel):
    ada, _ = pair(kernel)
    await receive(kernel, message(555555555555555554))
    kernel.store.execute("UPDATE bot_runtime SET next_at=0 WHERE bot_id='ada'")
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return completion("Old draft" if count == 1 else "New answer")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    for _ in range(50):
        if "ada" in kernel.engine.delivery_waiting:
            break
        await asyncio.sleep(0.01)
    await receive(kernel, message(555555555555555555, mentions=[ada]))
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 1)
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 1)
    assert count == 2
    kernel.engine.transport.send.assert_awaited_once()
    assert kernel.engine.transport.send.await_args.args[2] == "New answer"
    assert [r["status"] for r in kernel.store.rows("SELECT status FROM outbox ORDER BY created_at")] == [
        "suppressed",
        "sent",
    ]


@pytest.mark.parametrize("barrier", ["paused", "offline", "provider_circuit", "retry_after", "hourly_budget"])
async def test_human_priority_preserves_runtime_and_budget_barriers(kernel, barrier):
    ada, _ = pair(kernel)
    await receive(kernel, message(555555555555555555, mentions=[ada]))
    if barrier == "paused":
        kernel.store.put("bots", {**ada, "enabled": False})
    elif barrier == "offline":
        kernel.store.execute("UPDATE bot_runtime SET gateway_status='offline' WHERE bot_id='ada'")
    elif barrier == "provider_circuit":
        kernel.store.health("openrouter")
        kernel.store.execute("UPDATE provider_health SET circuit_until=?", (time.time() + 100,))
    elif barrier == "retry_after":
        kernel.store.execute("UPDATE bot_runtime SET retry_until=? WHERE bot_id='ada'", (time.time() + 100,))
    else:
        kernel.store.put("bots", {**ada, "hourly_turn_limit": 0})
    await kernel.engine.tick()
    await kernel.engine.tick()
    assert not kernel.engine.tasks
    assert not kernel.store.rows("SELECT * FROM human_attention_claims")


async def test_human_priority_uses_available_global_slot_before_routine_turn(kernel):
    ada, socrates = pair(kernel)
    settings = kernel.store.get("settings", "global")
    kernel.store.put("settings", {**settings, "max_concurrent_turns": 1})
    await receive(kernel, message(555555555555555554))
    await receive(kernel, message(555555555555555555, mentions=[socrates]))
    kernel.store.execute("UPDATE bot_runtime SET next_at=0")
    await install_client(kernel, lambda r: completion("Priority"))
    await kernel.engine.tick()
    assert set(kernel.engine.tasks) == {"socrates"}
    await asyncio.wait_for(settle(kernel), 1)


async def test_new_human_input_during_generation_gets_fresh_context_not_a_stale_answer(kernel):
    ada, _ = pair(kernel)
    await receive(kernel, message(555555555555555554))
    kernel.store.execute("UPDATE bot_runtime SET next_at=0 WHERE bot_id='ada'")
    started, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await finish.wait()
            return completion("Stale answer")
        assert "New human question" in request.content.decode()
        return completion("Fresh answer")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await started.wait()
    await receive(kernel, message(555555555555555555, mentions=[ada], content="New human question"))
    await kernel.engine.tick()
    assert calls == 1  # Do not start another request while this bot is generating.
    finish.set()
    await asyncio.wait_for(settle(kernel), 1)
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 1)
    assert calls == 2
    kernel.engine.transport.send.assert_awaited_once()
    assert kernel.engine.transport.send.await_args.args[2] == "Fresh answer"


async def test_human_attention_does_not_cancel_an_inflight_discord_send(kernel):
    ada, _ = pair(kernel)
    await receive(kernel, message(555555555555555554, mentions=[ada]))
    sending, accepted = asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        sending.set()
        await accepted.wait()
        return "888888888888888888"

    kernel.engine.transport.send.side_effect = send
    await install_client(kernel, lambda r: completion("Answer"))
    await kernel.engine.tick()
    await sending.wait()
    await receive(kernel, message(555555555555555555, mentions=[ada]))
    task = kernel.engine.tasks["ada"]
    await kernel.engine.tick()
    assert not task.cancelling()
    assert kernel.store.one("SELECT status FROM outbox")["status"] == "sending"
    accepted.set()
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM outbox")["status"] == "sent"
    assert kernel.engine.attention(ada)[0]["discord_id"] == "555555555555555555"


async def test_failed_directed_request_does_not_receive_an_unbounded_priority_retry(kernel):
    ada, _ = pair(kernel)
    await receive(kernel, message(555555555555555555, mentions=[ada]))
    await install_client(kernel, lambda r: completion(""))
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "failed"
    assert not kernel.engine.attention(ada)
    await kernel.engine.tick()
    assert not kernel.engine.tasks


async def test_claims_and_decoded_addressing_survive_store_reopen_without_retriggering(kernel):
    ada, _ = pair(kernel)
    await receive(kernel, message(555555555555555555, mentions=[ada]))
    activation = kernel.engine.claim_attention(ada, CHANNEL, "claimed-before-restart")
    assert activation["kind"] == "human_mention"
    original = kernel.store
    reopened = Store(original.path)
    try:
        reopened.recover()
        kernel.engine.store = reopened
        assert not kernel.engine.attention(ada)
        assert reopened.transcript(CHANNEL)[0]["addressing"]["author_kind"] == "human"
        assert reopened.runtime("ada")["next_at"] == original.runtime("ada")["next_at"]
    finally:
        kernel.engine.store = original
        reopened.close()


async def test_hortator_priority_requires_the_exact_human_owner(kernel):
    bot = configured(kernel, "hortator", interval_seconds=120, cooldown_seconds=120)
    kernel.store.execute(
        "UPDATE bot_runtime SET next_at=?,last_sent=? WHERE bot_id='hortator'",
        (time.time() + 120, time.time()),
    )
    msg = message(555555555555555555, author="777777777777777777", mentions=[bot])
    msg.guild = None
    await kernel.connector.receive("hortator", msg)
    assert not kernel.engine.attention(bot)
    msg.author.id = int(OWNER_ID)
    await kernel.connector.receive("hortator", msg)
    assert len(kernel.engine.attention(bot)) == 1
    kernel.engine.transport = AsyncMock()
    kernel.engine.transport.send.return_value = "888888888888888888"
    await install_client(kernel, lambda r: completion("Owner answer"))
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 1)
    kernel.engine.transport.send.assert_awaited_once()
