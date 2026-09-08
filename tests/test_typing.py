import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from conftest import configured, ingest
from test_provider import install_client
from test_runtime import completion, settle
from hortator import discord_gateway
from hortator.models import ControlError, OWNER_ID


def prepare_bot(kernel, bot_id="ada"):
    if not kernel.store.get("bots", bot_id):
        draft = kernel.store.get("bots", "ada")
        draft.pop("revision")
        kernel.store.put("bots", {**draft, "id": bot_id, "name": bot_id})
    bot = configured(kernel, bot_id)
    channel_id = "222222222222222222"
    if bot["role"] == "hortator":
        channel_id = "888888888888888888"
        kernel.store.ingest(
            discord_id="555555555555555555",
            channel_id=channel_id,
            room_id="owner:hortator",
            author_id=OWNER_ID,
            author_name="The Boss",
            content="What is happening?",
        )
        kernel.store.context(bot_id, channel_id)
    else:
        ingest(kernel, bot_id=bot_id)
    return bot, channel_id


class BusyTransport:
    def __init__(self):
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.identity = None
        self.sent = False

    async def typing(self, bot, channel_id, *, turn_id):
        self.identity = (bot["id"], channel_id, turn_id)
        self.started.set()
        try:
            await asyncio.Future()
        finally:
            self.stopped.set()

    async def send(self, bot, channel_id, content, reply_to, paths, *, nonce=None, footer=""):
        assert self.started.is_set() and not self.stopped.is_set()
        assert self.identity[:2] == (bot["id"], channel_id)
        self.sent = True
        return "777777777777777777"


@pytest.mark.parametrize("bot_id", ["ada", "socrates", "dirac", "curie", "hortator"])
async def test_typing_covers_preparation_generation_and_delivery_for_every_bot(kernel, bot_id):
    bot, channel_id = prepare_bot(kernel, bot_id)
    transport = BusyTransport()
    kernel.engine.transport = transport
    prepare = kernel.engine.contexts.prepare

    async def observed_prepare(*args, **kwargs):
        assert transport.started.is_set() and not transport.stopped.is_set()
        return await prepare(*args, **kwargs)

    kernel.engine.contexts.prepare = observed_prepare

    async def respond(request):
        assert transport.started.is_set() and not transport.stopped.is_set()
        return completion()

    await install_client(kernel, respond)
    turn_id = kernel.engine.launch(bot, channel_id)
    await settle(kernel)
    assert transport.identity == (bot_id, channel_id, turn_id)
    assert transport.sent and transport.stopped.is_set()
    assert kernel.store.one("SELECT status FROM turns WHERE id=?", (turn_id,))["status"] == "sent"
    assert not any(task.get_name().startswith("typing:") for task in asyncio.all_tasks())


@pytest.mark.parametrize("outcome", ["silence", "provider_failure", "cancelled", "compaction_failure"])
async def test_typing_stops_on_every_non_delivery_exit(kernel, owner, outcome):
    bot, channel_id = prepare_bot(kernel)
    transport = BusyTransport()
    kernel.engine.transport = transport
    request_started = asyncio.Event()

    async def respond(request):
        request_started.set()
        if outcome == "cancelled":
            await asyncio.Future()
        if outcome == "provider_failure":
            return httpx.Response(500, json={"error": "Provider unavailable"})
        return completion(silence=True)

    await install_client(kernel, respond)
    if outcome == "compaction_failure":

        async def failed_prepare(*args, **kwargs):
            assert transport.started.is_set()
            raise ControlError("Summary could not fit")

        kernel.engine.contexts.prepare = failed_prepare
    kernel.engine.launch(bot, channel_id, compact_only=outcome == "compaction_failure")
    if outcome == "cancelled":
        await asyncio.wait_for(request_started.wait(), 2)
        await kernel.service.control(owner, {"action": "stop", "id": "ada"})
    await settle(kernel)
    expected = {"silence": "silent", "cancelled": "cancelled"}.get(outcome, "failed")
    assert kernel.store.one("SELECT status FROM turns")["status"] == expected
    assert transport.started.is_set() and transport.stopped.is_set()
    assert not transport.sent


def connect_channel(kernel, bot, channel_id, typing):
    channel = SimpleNamespace(id=int(channel_id), typing=typing)
    kernel.connector.clients[bot["id"]] = SimpleNamespace(
        is_ready=lambda: True,
        get_channel=lambda requested: channel if requested == int(channel_id) else None,
        fetch_channel=AsyncMock(return_value=channel),
        close=AsyncMock(),
    )
    return channel


async def test_gateway_renews_typing_and_stops_when_channel_scope_changes(kernel, monkeypatch):
    bot, channel_id = prepare_bot(kernel)
    monkeypatch.setattr(discord_gateway, "TYPING_INTERVAL_SECONDS", 0.01)
    second_pulse = asyncio.Event()
    count = 0

    async def pulse():
        nonlocal count
        count += 1
        if count == 2:
            room = kernel.store.get("rooms", "council")
            room.pop("revision")
            kernel.store.put("rooms", {**room, "channel_id": "999999999999999999"})
            second_pulse.set()

    connect_channel(kernel, bot, channel_id, pulse)
    task = asyncio.create_task(kernel.connector.typing(bot, channel_id, turn_id="turn-test"))
    await asyncio.wait_for(second_pulse.wait(), 2)
    await asyncio.wait_for(task, 2)
    assert count == 2


async def test_typing_failures_are_redacted_throttled_and_do_not_fail_the_turn(kernel, monkeypatch):
    bot, channel_id = prepare_bot(kernel)
    monkeypatch.setattr(discord_gateway, "TYPING_INTERVAL_SECONDS", 0.01)
    second_pulse = asyncio.Event()
    calls = 0
    kernel.vault.put("plugin/typing-test/api_key", "typing-test-secret")

    async def failed_pulse():
        nonlocal calls
        calls += 1
        if calls == 2:
            second_pulse.set()
        raise RuntimeError("Permission denied typing-test-secret")

    connect_channel(kernel, bot, channel_id, failed_pulse)
    kernel.connector.send = AsyncMock(return_value="777777777777777777")

    async def respond(request):
        await asyncio.wait_for(second_pulse.wait(), 2)
        return completion()

    await install_client(kernel, respond)
    kernel.engine.launch(bot, channel_id)
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    events = [e for e in kernel.store.events() if e["kind"] == "discord.typing_failed"]
    assert len(events) == 1
    assert "typing-test-secret" not in str(events)
    kernel.connector.send.assert_awaited_once()


async def test_slow_typing_request_does_not_delay_generation_or_outlive_delivery(kernel):
    bot, channel_id = prepare_bot(kernel)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def slow_pulse():
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    connect_channel(kernel, bot, channel_id, slow_pulse)
    kernel.connector.send = AsyncMock(return_value="777777777777777777")

    async def respond(request):
        assert started.is_set() and not stopped.is_set()
        return completion()

    await install_client(kernel, respond)
    kernel.engine.launch(bot, channel_id)
    await asyncio.wait_for(settle(kernel), 2)
    assert stopped.is_set()
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"


async def test_simultaneous_bots_use_their_own_discord_clients(kernel):
    first, channel_id = prepare_bot(kernel)
    second, _ = prepare_bot(kernel, "socrates")
    pulses = {bot["id"]: asyncio.Event() for bot in (first, second)}
    for bot in (first, second):
        event = pulses[bot["id"]]
        connect_channel(kernel, bot, channel_id, AsyncMock(side_effect=event.set))
    tasks = [
        asyncio.create_task(kernel.connector.typing(bot, channel_id, turn_id=bot["id"]))
        for bot in (first, second)
    ]
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in pulses.values())), 2)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert all(event.is_set() for event in pulses.values())


async def test_paused_bot_does_not_publish_typing(kernel):
    bot, channel_id = prepare_bot(kernel)
    typing = AsyncMock()
    connect_channel(kernel, bot, channel_id, typing)
    bot.pop("revision")
    kernel.store.put("bots", {**bot, "enabled": False})
    await kernel.engine.tick()
    assert not kernel.engine.tasks
    typing.assert_not_awaited()
