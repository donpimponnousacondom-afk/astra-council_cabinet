import asyncio
import json
import re
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from conftest import configured
from hortator.addressing import for_viewer
from hortator.models import OWNER_ID
from hortator.store import Store
from support.provider import install_client
from support.runtime import completion, settle
from support.addressing import CHANNEL, message, pair, receive


@pytest.mark.parametrize("kind", ["mention", "reply"])
@pytest.mark.parametrize("interval", [0, 120])
async def test_human_target_bypasses_interval_and_cooldown_once_without_waking_peer(kernel, kind, interval):
    ada, socrates = pair(kernel, interval_seconds=interval)
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
@pytest.mark.parametrize("role_ping", [False, True])
async def test_human_priority_preserves_runtime_and_budget_barriers(kernel, barrier, role_ping):
    ada, _ = pair(kernel)
    msg = message(555555555555555555, mentions=[] if role_ping else [ada])
    if role_ping:
        msg.role_mentions = [NS(id=666666666666666666)]
        msg.guild.me = NS(id=int(ada["application_id"]), roles=msg.role_mentions)
    await receive(kernel, msg)
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


@pytest.mark.parametrize(
    "arrival", ["before_launch", "during_preparation", "during_generation", "during_send_gap"]
)
async def test_different_humans_queue_without_replacing_or_consuming_each_other(kernel, arrival):
    ada, _ = pair(kernel, interval_seconds=0)
    first = message(555555555555555551, mentions=[ada], content="First person's question")
    second = message(
        555555555555555552, author="777777777777777777", mentions=[ada], content="Second person's question"
    )
    third = message(
        555555555555555553, author="666666666666666666", mentions=[ada], content="Third person's question"
    )
    second_update = message(
        555555555555555554,
        author="777777777777777777",
        mentions=[ada],
        content="Second person's clarification",
    )
    await receive(kernel, first)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    original_prepare = kernel.engine.contexts.prepare

    async def prepare(*args, **kwargs):
        if arrival == "during_preparation" and not entered.is_set():
            entered.set()
            await release.wait()
        return await original_prepare(*args, **kwargs)

    kernel.engine.contexts.prepare = prepare

    async def handler(request):
        body = json.loads(request.content)
        tail = body["messages"][-1]["content"]
        calls.append(tail)
        if len(calls) == 1 and arrival == "during_generation":
            entered.set()
            await release.wait()
        return completion(f"Answer {len(calls)}")

    await install_client(kernel, handler)
    if arrival == "before_launch":
        for msg in (second, third, second_update):
            await receive(kernel, msg)
    if arrival == "during_send_gap":
        kernel.engine.room_last[CHANNEL] = time.time() + 0.15
    await kernel.engine.tick()
    if arrival in ("during_preparation", "during_generation"):
        await asyncio.wait_for(entered.wait(), 1)
    elif arrival == "during_send_gap":
        for _ in range(100):
            if kernel.store.one("SELECT 1 FROM events WHERE kind='delivery.cooldown'"):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Did not reach shared send gap")
    if arrival != "before_launch":
        for msg in (second, third, second_update):
            await receive(kernel, msg)
        await kernel.engine.tick()
        assert not kernel.engine.tasks["ada"].cancelling()
    release.set()
    await asyncio.wait_for(settle(kernel), 2)
    assert kernel.engine.transport.send.await_count == 1
    assert kernel.engine.transport.send.await_args.args[3] == str(first.id)
    assert len(kernel.engine.attention(ada)) == 3
    # All three waiting messages may already have appeared in the first prompt.
    # They must still survive the successful turn's last_seen update.
    for _ in range(2):
        await kernel.engine.tick()
        await asyncio.wait_for(settle(kernel), 2)
    assert [call.args[3] for call in kernel.engine.transport.send.await_args_list] == [
        str(first.id),
        str(second_update.id),
        str(third.id),
    ]
    assert all(row["status"] == "sent" for row in kernel.store.rows("SELECT status FROM turns"))
    assert not kernel.engine.attention(ada)
    assert len(calls) == 3
    assert str(first.id) in calls[0]
    assert str(second_update.id) in calls[1]
    assert str(third.id) in calls[2]
    claims = kernel.store.rows("SELECT turn_id,message_id FROM human_attention_claims ORDER BY message_id")
    assert claims[1]["turn_id"] == claims[3]["turn_id"]
    assert len({row["turn_id"] for row in claims}) == 3


async def test_same_human_can_still_supersede_their_own_directed_answer(kernel):
    ada, _ = pair(kernel, interval_seconds=0)
    await receive(kernel, message(555555555555555551, mentions=[ada]))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return completion(f"Answer {calls}")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await asyncio.wait_for(entered.wait(), 1)
    await receive(kernel, message(555555555555555552, mentions=[ada], content="Actually, new question"))
    release.set()
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM outbox")["status"] == "suppressed"
    await kernel.engine.tick()
    await settle(kernel)
    kernel.engine.transport.send.assert_awaited_once()
    assert kernel.engine.transport.send.await_args.args[3] == "555555555555555552"


async def test_waiting_human_survives_store_reopen_after_another_humans_answer(kernel):
    ada, _ = pair(kernel, interval_seconds=0)
    await receive(kernel, message(555555555555555551, mentions=[ada]))
    await receive(kernel, message(555555555555555552, author="777777777777777777", mentions=[ada]))
    await install_client(kernel, lambda r: completion("First answer"))
    await kernel.engine.tick()
    await settle(kernel)
    original = kernel.store
    reopened = Store(original.path)
    try:
        reopened.recover()
        kernel.engine.store = reopened
        assert [r["discord_id"] for r in kernel.engine.attention(ada)] == ["555555555555555552"]
    finally:
        kernel.engine.store = original
        reopened.close()
    # Recovery correctly marks gateways offline until their normal reconnect.
    kernel.store.execute("UPDATE bot_runtime SET gateway_status='online' WHERE bot_id='ada'")
    await kernel.engine.tick()
    await settle(kernel)
    assert [call.args[3] for call in kernel.engine.transport.send.await_args_list] == [
        "555555555555555551",
        "555555555555555552",
    ]


async def test_human_followup_in_another_thread_does_not_cancel_current_conversation(kernel):
    ada, _ = pair(kernel, interval_seconds=0)
    await receive(kernel, message(555555555555555551, mentions=[ada]))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return completion("Answer")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await asyncio.wait_for(entered.wait(), 1)
    followup = message(555555555555555552, mentions=[ada])
    followup.channel = NS(id=222222222222222223, parent_id=int(CHANNEL))
    await receive(kernel, followup)
    release.set()
    await settle(kernel)
    await kernel.engine.tick()
    await settle(kernel)
    assert [call.args[3] for call in kernel.engine.transport.send.await_args_list] == [
        "555555555555555551",
        "555555555555555552",
    ]
    assert [call.args[1] for call in kernel.engine.transport.send.await_args_list] == [
        CHANNEL,
        "222222222222222223",
    ]


async def test_queued_humans_keep_original_questions_through_engram_reduction(kernel):
    from hortator.engrams import DEFAULTS, PLUGIN_ID

    ada, _ = pair(kernel, interval_seconds=0)
    plugin = kernel.store.get("plugins", PLUGIN_ID)
    kernel.store.put(
        "plugins",
        {**plugin, "enabled": True, "config": {**DEFAULTS, "reduce_history": True, "recent_messages": 1}},
    )
    kernel.store.put("bots", {**ada, "enabled_plugins": [PLUGIN_ID]})
    ada = kernel.store.get("bots", "ada")
    questions = ["FIRST UNIQUE QUESTION", "SECOND UNIQUE QUESTION", "THIRD UNIQUE QUESTION"]
    for i, author in enumerate([OWNER_ID, "777777777777777777", "666666666666666666"]):
        await receive(
            kernel, message(555555555555555551 + i, author=author, mentions=[ada], content=questions[i])
        )
    prompts = []

    def handler(request):
        body = json.loads(request.content)
        full = json.dumps(body)
        prompts.append(full)
        nonce = re.search(r"\[\^ENGRAM:([a-f0-9]{32})\]", full).group(1)
        # A valid memory update is not a guarantee that it retained another
        # queued person's exact question. The harness must preserve that input.
        return completion(
            f'Answer\n[^ENGRAM:{nonce}]\n{{"MEM":"A person answered","FACTS":""}}\n[^END:{nonce}]'
        )

    await install_client(kernel, handler)
    for _ in range(3):
        await kernel.engine.tick()
        await settle(kernel)
    assert len(prompts) == 3
    for i, question in enumerate(questions):
        assert question in prompts[i]
        assert kernel.engine.transport.send.await_args_list[i].args[3] == str(555555555555555551 + i)
    assert not kernel.engine.attention(ada)


@pytest.mark.parametrize("prior_checkpoint", ["automatic", "existing", "manual"])
async def test_pending_human_text_and_pixels_survive_compaction(kernel, prior_checkpoint):
    from support.vision import cached

    ada, _ = pair(kernel, interval_seconds=0)
    profile = kernel.store.get("profiles", "balanced")
    kernel.store.put(
        "profiles", {**profile, "compact_threshold": 0.1, "keep_recent_messages": 1, "context_window": 16384}
    )
    # Include ordinary chatter after the questions: compaction must be able to
    # summarize it without consuming an earlier queued question's raw input.
    for i, author in enumerate([OWNER_ID, "777777777777777777", "666666666666666666"]):
        await receive(
            kernel,
            message(555555555555555551 + i, author=author, mentions=[ada], content=f"UNIQUE QUESTION {i}"),
        )
    await receive(kernel, message(555555555555555554, content="Unaddressed ordinary chatter"))
    _, item = await cached(kernel)
    kernel.store.execute(
        "UPDATE messages SET attachments=? WHERE discord_id=?", (json.dumps([item]), "555555555555555552")
    )
    if prior_checkpoint == "existing":
        # Even an earlier forced compaction must not hide a pending question.
        kernel.store.execute(
            "UPDATE contexts SET checkpoint=(SELECT max(seq) FROM messages),summary='Earlier summary without questions' WHERE bot_id='ada'"
        )
    prompts = []

    def handler(request):
        body = json.loads(request.content)
        prompts.append(body)
        return completion("An answer or a summary that does not repeat the questions")

    await install_client(kernel, handler)
    if prior_checkpoint == "manual":
        kernel.engine.launch(ada, CHANNEL, compact_only=True)
        await settle(kernel)
        assert kernel.store.context("ada", CHANNEL)["checkpoint"] > 0
        assert len(kernel.engine.attention(ada)) == 3
        kernel.engine.transport.send.assert_not_awaited()
    for _ in range(3):
        await kernel.engine.tick()
        await settle(kernel)
    requests = kernel.store.rows("SELECT purpose FROM requests ORDER BY started_at")
    generations = [
        body for body, record in zip(prompts, requests, strict=True) if record["purpose"] == "generation"
    ]
    assert len(generations) == 3
    assert "UNIQUE QUESTION 1" in json.dumps(generations[1])
    parts = [
        part
        for msg in generations[1]["messages"]
        if isinstance(msg["content"], list)
        for part in msg["content"]
    ]
    assert sum(part.get("type") == "image_url" for part in parts) == 1
    assert [call.args[3] for call in kernel.engine.transport.send.await_args_list] == [
        str(555555555555555551 + i) for i in range(3)
    ]
    if prior_checkpoint != "existing":
        assert any(record["purpose"] == "compaction" for record in requests)
    assert not kernel.engine.attention(ada)
