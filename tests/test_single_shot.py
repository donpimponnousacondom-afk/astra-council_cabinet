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
