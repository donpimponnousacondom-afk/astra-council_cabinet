import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from conftest import configured, ingest
from hortator.models import ControlError
from hortator.security import Actor
from hortator.concurrency import cancel_and_wait
from support.provider import install_client
from support.runtime import completion, settle


def setup(k, *, enabled=True, interval=0):
    bot = configured(k, interval_seconds=interval, evaluate_when_idle=False)
    bot.pop("revision")
    bot["enabled"] = enabled
    bot = k.store.put("bots", bot)
    ingest(k)
    k.engine.transport = AsyncMock()
    k.engine.transport.send.return_value = "777777777777777777"
    return bot


async def trigger(k, actor, **data):
    return await k.service.control(actor, {"action": "trigger", "kind": "bots", "id": "ada", "data": data})


@pytest.mark.parametrize("enabled,interval", [(False, 60), (True, 0), (True, 600)])
@pytest.mark.parametrize("silent", [False, True])
async def test_trigger_one_turn_without_changing_configuration(kernel, owner, enabled, interval, silent):
    bot = setup(kernel, enabled=enabled, interval=interval)
    # No new input and a future cadence/cooldown must not swallow the explicit trigger.
    kernel.store.execute("UPDATE contexts SET last_seen=999 WHERE bot_id='ada'")
    kernel.store.execute(
        "UPDATE bot_runtime SET next_at=?,last_sent=? WHERE bot_id='ada'", (time.time() + 3600, time.time())
    )
    assert kernel.engine.pick_channel(bot) is None
    await install_client(kernel, lambda _: completion(silence=silent))
    start = time.time()
    result = await trigger(kernel, owner)
    assert result["single_shot"]
    assert kernel.connector.needed(bot)
    await settle(kernel)
    turn = kernel.store.one("SELECT * FROM turns")
    assert turn["id"] == result["turn_id"] and turn["trigger"] == "manual_trigger"
    assert turn["status"] == ("silent" if silent else "sent")
    assert kernel.store.get("bots", "ada") == bot
    assert not kernel.engine.single_shots
    assert kernel.connector.needed(bot) == enabled
    assert kernel.store.runtime("ada")["next_at"] >= start + interval
    assert not kernel.store.one("SELECT 1 FROM events WHERE kind='delivery.cooldown'")
    await kernel.engine.tick()
    assert len(kernel.store.rows("SELECT * FROM turns")) == 1


async def test_duplicate_trigger_is_rejected_and_pause_cancels_a_paused_bot_trial(kernel, owner):
    setup(kernel, enabled=False)
    started = asyncio.Event()

    async def respond(_):
        started.set()
        await asyncio.sleep(60)
        return completion()

    await install_client(kernel, respond)
    await trigger(kernel, owner)
    await started.wait()
    with pytest.raises(ControlError, match="already has an active turn"):
        await trigger(kernel, owner)
    await kernel.service.control(owner, {"action": "stop", "kind": "bots", "id": "ada"})
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "cancelled"
    assert not kernel.store.get("bots", "ada")["enabled"]
    assert not kernel.engine.single_shots and not kernel.engine.tasks
    assert not kernel.connector.needed(kernel.store.get("bots", "ada"))
    kernel.engine.transport.send.assert_not_awaited()


@pytest.mark.parametrize(
    "guard",
    ["council", "provider", "circuit", "retry", "slots", "budget", "scope", "empty", "setup", "owner"],
)
async def test_trigger_preserves_admission_and_scope_guards(kernel, owner, guard):
    setup(kernel)
    data = {}
    if guard in ("council", "provider"):
        kind, identity = ("settings", "global") if guard == "council" else ("providers", "openrouter")
        entity = kernel.store.get(kind, identity)
        entity.pop("revision")
        kernel.store.put(kind, {**entity, "enabled": False})
    elif guard == "circuit":
        kernel.store.health("openrouter")
        kernel.store.execute("UPDATE provider_health SET circuit_until=?", (time.time() + 60,))
    elif guard == "retry":
        kernel.store.execute("UPDATE bot_runtime SET retry_until=?", (time.time() + 60,))
    elif guard == "slots":
        maximum = kernel.store.get("settings", "global")["max_concurrent_turns"]
        kernel.engine.tasks.update({f"occupied-{n}": None for n in range(maximum)})
    elif guard == "budget":
        kernel.engine.budget_error = lambda _: "Hourly activation limit reached"
    elif guard == "scope":
        data["channel_id"] = "999999999999999999"
    elif guard == "empty":
        kernel.store.execute("DELETE FROM contexts WHERE bot_id='ada'")
    elif guard == "setup":
        kernel.vault.put("bot/ada/token", "")
    elif guard == "owner":
        owner = Actor("dashboard", "123456789012345678")
    try:
        with pytest.raises(ControlError):
            await trigger(kernel, owner, **data)
        assert not kernel.store.rows("SELECT * FROM turns")
        assert not kernel.engine.single_shots
    finally:
        if guard == "slots":
            kernel.engine.tasks.clear()


async def test_request_retries_stay_in_single_turn_and_failures_do_not_enable_bot(kernel, owner):
    bot = setup(kernel, enabled=False)
    provider = kernel.store.get("providers", "openrouter")
    provider.pop("revision")
    kernel.store.put("providers", {**provider, "retry_count": 1, "retry_delay_seconds": 0})
    calls = 0

    def respond(_):
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": {"message": "temporary"}})

    await install_client(kernel, respond)
    await trigger(kernel, owner)
    await settle(kernel)
    assert calls == 2
    assert len(kernel.store.rows("SELECT * FROM turns")) == 1
    assert kernel.store.one("SELECT status FROM turns")["status"] == "failed"
    assert kernel.store.get("bots", "ada") == bot
    assert not kernel.engine.single_shots
    await kernel.engine.tick()
    assert calls == 2


async def test_discord_preparation_failure_spends_no_provider_call(kernel, owner):
    bot = setup(kernel, enabled=False)
    kernel.engine.transport.prepare_single_shot.side_effect = ControlError("Discord not ready")
    await install_client(kernel, lambda _: pytest.fail("No inference before gateway readiness"))
    await trigger(kernel, owner)
    await settle(kernel)
    assert kernel.store.one("SELECT status,error FROM turns")["error"] == "ControlError: Discord not ready"
    assert not kernel.engine.single_shots
    assert kernel.store.get("bots", "ada") == bot


async def test_gateway_preparation_joins_owned_backfill(kernel):
    bot = setup(kernel, enabled=False)
    finished = asyncio.Event()

    async def backfill():
        await asyncio.sleep(0)
        finished.set()

    task = kernel.connector.background.spawn(backfill(), name="fixture-backfill")
    client = SimpleNamespace(is_ready=lambda: True, backfill_task=task)
    kernel.connector.clients[bot["id"]] = client
    try:
        await kernel.connector.prepare_single_shot(bot)
        assert finished.is_set() and task.done()
    finally:
        kernel.connector.clients.clear()


async def test_supervisor_connects_and_disconnects_only_the_paused_trial(kernel, monkeypatch):
    bot = setup(kernel, enabled=False)
    manager = kernel.connector
    started, stopped = asyncio.Event(), asyncio.Event()

    async def run_client(candidate, token):
        assert candidate["id"] == bot["id"]
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(manager, "run_client", run_client)
    kernel.engine.single_shots[bot["id"]] = "turn-fixture"
    supervisor = manager.background.spawn(manager.supervise(), name="fixture-supervisor")
    try:
        async with asyncio.timeout(5):
            await started.wait()
            kernel.engine.single_shots.clear()
            await stopped.wait()
        assert kernel.store.get("bots", bot["id"]) == bot
        assert bot["id"] not in manager.runners
    finally:
        await cancel_and_wait(supervisor)


async def test_connection_wait_is_bounded_and_cancellable(kernel, monkeypatch):
    bot = setup(kernel, enabled=False)
    original_timeout = asyncio.timeout
    requested = []

    def shortened(seconds):
        requested.append(seconds)
        return original_timeout(0.01)

    with monkeypatch.context() as patch:
        patch.setattr("hortator.discord_gateway.asyncio.timeout", shortened)
        with pytest.raises(ControlError, match="30 seconds.*no inference was started"):
            await kernel.connector.prepare_single_shot(bot)
    assert requested == [30]


async def test_cancelling_before_trial_starts_releases_temporary_permission(kernel, owner):
    bot = setup(kernel, enabled=False)
    await trigger(kernel, owner)
    await kernel.engine.cancel([bot["id"]], "operator stop")
    await asyncio.sleep(0)
    assert not kernel.engine.tasks and not kernel.engine.single_shots
    assert not kernel.connector.needed(bot)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "cancelled"


def conversation(k, channel, at, *, kind="human", deleted=False, targets=None, author=None):
    discord_id = str(
        600000000000000000 + k.store.one("SELECT coalesce(max(seq),0) AS seq FROM messages")["seq"]
    )
    k.store.ingest(
        discord_id=discord_id,
        channel_id=channel,
        room_id="council",
        guild_id="111111111111111111",
        parent_id="222222222222222222" if channel != "222222222222222222" else None,
        author_id=author or "1482143139828596916",
        author_name="Test participant",
        content="Routing fixture",
        at=at,
        addressing={"author_kind": kind, "live": True, "targets": targets or []},
    )
    seq = k.store.one("SELECT seq FROM messages WHERE discord_id=?", (discord_id,))["seq"]
    k.store.context("ada", channel)
    if deleted:
        k.store.execute("UPDATE messages SET deleted=1 WHERE seq=?", (seq,))
    return seq


async def test_manual_trigger_uses_human_time_not_old_pending_or_bot_activity(kernel, owner):
    bot = setup(kernel)
    root, old = "222222222222222222", "666666666666666666"
    kernel.store.execute("UPDATE messages SET at=200")
    kernel.store.execute("UPDATE contexts SET last_seen=999,updated_at=200")
    # History arrives later, but the human conversation happened earlier.
    conversation(kernel, old, 100)
    kernel.store.execute("UPDATE contexts SET updated_at=1 WHERE channel_id=?", (old,))
    conversation(kernel, old, 300, kind="bot")  # external bot has no council bot_id
    conversation(kernel, old, 400, kind="webhook")
    conversation(kernel, old, 500, deleted=True)
    assert kernel.engine.pick_channel(bot) == old  # ordinary fairness unchanged
    assert kernel.engine.manual_channel(bot) == root
    await install_client(kernel, lambda _: completion())
    await trigger(kernel, owner)
    await settle(kernel)
    assert kernel.store.one("SELECT channel_id FROM turns")["channel_id"] == root
    # An explicit target still wins, even when it is the older conversation.
    await trigger(kernel, owner, channel_id=old)
    await settle(kernel)
    assert kernel.store.rows("SELECT channel_id FROM turns ORDER BY started_at")[-1]["channel_id"] == old


async def test_manual_recency_excludes_other_targets_unknown_authors_and_disallowed_rooms(kernel):
    bot = setup(kernel)
    root, thread = "222222222222222222", "666666666666666666"
    kernel.store.execute("UPDATE messages SET at=100")
    conversation(kernel, thread, 200, targets=[{"bot_id": "curie", "user_id": "444444444444444444"}])
    conversation(kernel, thread, 300, kind="unknown")
    conversation(kernel, "777777777777777777", 400)
    kernel.store.execute("UPDATE channels SET parent_id=NULL WHERE id='777777777777777777'")
    assert kernel.engine.manual_channel(bot) == root
    conversation(kernel, thread, 500)
    assert kernel.engine.manual_channel(bot) == thread


async def test_manual_recency_respects_reset_and_has_no_bot_only_fallback(kernel, owner):
    bot = setup(kernel)
    kernel.store.execute("DELETE FROM messages")
    conversation(kernel, "222222222222222222", 200, kind="bot")
    assert kernel.engine.manual_channel(bot) is None
    with pytest.raises(ControlError, match="human activity"):
        await trigger(kernel, owner)
    seq = conversation(kernel, "222222222222222222", 300)
    kernel.store.execute(
        "INSERT INTO context_resets(bot_id,channel_id,after_seq,after_at) VALUES(?,?,?,?)",
        ("ada", "*", seq, 350),
    )
    conversation(kernel, "222222222222222222", 320)  # backfilled across cutoff
    assert kernel.engine.manual_channel(bot) is None
    conversation(kernel, "222222222222222222", 400)
    assert kernel.engine.manual_channel(bot) == "222222222222222222"


async def test_manual_recency_hortator_only_counts_owner(kernel):
    bot = setup(kernel)
    # Keep channel admission separately controlled to exercise the owner-activity guard.
    bot = {**bot, "role": "hortator"}
    kernel.engine.channel_allowed = lambda *_: True
    kernel.store.execute("UPDATE messages SET at=100")
    conversation(kernel, "666666666666666666", 200, author="999999999999999999")
    assert kernel.engine.manual_channel(bot) == "222222222222222222"


async def test_manual_recency_recognizes_authenticated_dm_command(kernel):
    bot = configured(kernel, bot_id="hortator")
    channel = SimpleNamespace(id=888888888888888888)
    message = SimpleNamespace(
        id=777777777777777777,
        content="!dm Please continue the private discussion",
        author=SimpleNamespace(id=1482143139828596916, bot=False, create_dm=AsyncMock(return_value=channel)),
        webhook_id=None,
        channel=SimpleNamespace(id=222222222222222222),
    )
    await kernel.connector.command(bot, message)
    assert kernel.engine.channel_allowed(bot, str(channel.id))
    assert kernel.engine.manual_channel(bot) == str(channel.id)
    # Recording the human author must not synthesize a live mention/reply activation.
    assert kernel.engine.attention(bot) == []
