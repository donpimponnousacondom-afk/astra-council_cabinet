"""Owner slash reminders resolve a durable room or DM, never an interaction token."""

import json
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import discord
import httpx
import pytest

from hortator.app import Kernel
from hortator.models import ControlError, OWNER_ID
from hortator.plugins import ToolContext
from hortator.runtime import DeliveryError
from support.addressing import message
from support.provider import install_client
from support.runtime import completion, settle
from support.slash_commands import enable, interaction

ROOM = "222222222222222222"
DM = "666666666666666666"
ARGS = {"operation": "schedule", "key": "review", "message": "Review usage", "after_seconds": 3600}


def prepare(k):
    bot = enable(k, interval_seconds=0, cooldown_seconds=0)
    k.store.put("bots", {**bot, "enabled_plugins": [*bot["enabled_plugins"], "secretary"]})
    bot = k.store.get("bots", "ada")
    plugin = k.store.get("plugins", "secretary")
    k.store.put("plugins", {**plugin, "enabled": True})
    dm = Mock(spec=discord.DMChannel)
    dm.id = int(DM)
    dm.recipient = NS(id=int(OWNER_ID))
    dm.send = AsyncMock(return_value=NS(id=777777777777777777))
    user = NS(create_dm=AsyncMock(return_value=dm))
    client = Mock()
    client.is_ready.return_value = True
    client.get_user.return_value = user
    client.get_channel.return_value = dm
    k.connector.clients[bot["id"]] = client
    # Keep the real DM resolution/validation; no live Discord connections or sends.
    transport = AsyncMock()
    transport.owner_dm.side_effect = k.connector.owner_dm
    transport.send.return_value = "777777777777777777"
    k.engine.transport = transport
    return bot, dm, user


def tool_response(args):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "alarm",
                                "type": "function",
                                "function": {
                                    "name": "secretary",
                                    "arguments": json.dumps(args),
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40},
        },
    )


@pytest.mark.parametrize(
    "location,private",
    [("outside", False), ("outside", True), ("room", False), ("room", True), ("dm", False)],
)
async def test_slash_schedule_then_delayed_wake_and_private_snooze(kernel, location, private):
    bot, _, user = prepare(kernel)
    item = interaction(bot, private=private)
    if location == "room":
        item.channel_id = int(ROOM)
        item.guild_id = 111111111111111111
    elif location == "dm":
        item.channel_id, item.guild_id = int(DM), None
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return tool_response(ARGS) if len(requests) == 1 else completion("Saved reminder.")

    await install_client(kernel, handler)
    await kernel.connector.slash.receive(bot["id"], item)
    await settle(kernel)
    row = kernel.store.one("SELECT * FROM secretary_reminders")
    assert row is not None, kernel.store.rows("SELECT kind,data FROM events WHERE level='error'")
    destination = ROOM if location == "room" and not private else DM
    assert row["channel_id"] == destination
    receipt = kernel.registry.secretary.receipt(row)
    assert receipt["destination"]["kind"] == ("conversation" if destination == ROOM else "owner_dm")
    assert not kernel.engine.channel_allowed(bot, f"slash:{bot['id']}:{item.channel_id}")
    assert kernel.engine.pick_channel(bot) is None
    kernel.engine.transport.send.assert_not_awaited()
    assert user.create_dm.await_count == (destination == DM)
    assert len(requests) == 2
    item.edit_original_response.assert_awaited_once()

    kernel.store.execute("UPDATE secretary_reminders SET due_at=?", (time.time() - 1,))
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.engine.transport.send.call_args.args[1] == destination
    turn = kernel.store.one("SELECT * FROM turns WHERE trigger='scheduled_alarm'")
    assert turn["status"] == "sent"
    assert turn["channel_id"] == destination
    item.edit_original_response.assert_awaited_once()  # Delayed delivery never edits slash acknowledgement.
    assert "Review usage" in json.dumps(requests[-1])
    assert item.token not in json.dumps(kernel.store.rows("SELECT * FROM secretary_reminders"))
    if destination == DM:
        # The owner can postpone that same alarm from another server's private /prompt.
        item2 = interaction(bot, interaction_id="987654321012345679", private=True)
        await install_client(
            kernel,
            lambda _: (
                tool_response({"operation": "snooze", "reminder_id": row["id"], "after_seconds": 7200})
                if not kernel.store.one("SELECT 1 FROM secretary_reminders WHERE state='scheduled'")
                else completion("Snoozed.")
            ),
        )
        await kernel.connector.slash.receive(bot["id"], item2)
        await settle(kernel)
        assert kernel.registry.secretary.get(row["id"])["state"] == "scheduled"
        assert user.create_dm.await_count == 1  # Reuse the persisted, authenticated destination.


async def test_dm_resolution_failure_does_not_save_or_fall_back(kernel):
    bot, _, user = prepare(kernel)
    user.create_dm.side_effect = discord.Forbidden(
        NS(status=403, reason="Forbidden"), {"code": 50007, "message": "Cannot send messages to this user"}
    )
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return tool_response(ARGS) if len(seen) == 1 else completion("I could not save that reminder.")

    await install_client(kernel, handler)
    await kernel.connector.slash.receive(bot["id"], interaction(bot))
    await settle(kernel)
    assert not kernel.store.rows("SELECT * FROM secretary_reminders")
    assert not kernel.store.rows("SELECT * FROM owner_dm_channels")
    kernel.engine.transport.send.assert_not_awaited()
    assert "Cannot send messages" in json.dumps(seen[-1])


async def test_owner_dm_intake_is_directed_private_and_requires_plugin(kernel):
    bot, _, _ = prepare(kernel)
    msg = message(111111111111111123, content="Snooze that reminder for one hour")
    msg.guild = None
    msg.channel = NS(id=int(DM), type=discord.ChannelType.private)
    await kernel.connector.receive(bot["id"], msg)
    assert kernel.store.is_owner_dm(bot, DM)
    attention = kernel.engine.attention(bot)
    assert len(attention) == 1 and attention[0]["channel_id"] == DM
    await install_client(kernel, lambda _: completion("There are no alarms yet."))
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.store.one("SELECT trigger FROM turns")["trigger"] == "owner_message"
    assert kernel.engine.transport.send.call_args.args[1] == DM
    kernel.store.execute("UPDATE contexts SET last_seen=999")
    bot["evaluate_when_idle"] = True
    assert kernel.engine.pick_channel(bot) is None  # No unsolicited private cadence chatter.
    kernel.store.put("bots", {**bot, "enabled_plugins": []})
    assert not kernel.engine.channel_allowed(kernel.store.get("bots", bot["id"]), DM)
    msg.id += 1
    await kernel.connector.receive(bot["id"], msg)
    assert not kernel.store.one("SELECT 1 FROM messages WHERE discord_id=?", (str(msg.id),))


@pytest.mark.parametrize("invalid", ["other_human", "bot", "webhook", "group", "guild", "disabled"])
async def test_private_intake_rejects_untrusted_or_ungranted_sources(kernel, invalid):
    bot, _, _ = prepare(kernel)
    msg = message(111111111111111124)
    msg.guild = None
    msg.channel = NS(id=int(DM), type=discord.ChannelType.private)
    if invalid == "other_human":
        msg.author.id = 123456789012345678
    elif invalid == "bot":
        msg.author.bot = True
    elif invalid == "webhook":
        msg.webhook_id = 123456789012345678
    elif invalid == "group":
        msg.channel.type = discord.ChannelType.group
    elif invalid == "guild":
        msg.guild = NS(id=987654321098765432)
    else:
        plugin = kernel.store.get("plugins", "secretary")
        kernel.store.put("plugins", {**plugin, "enabled": False})
    await kernel.connector.receive(bot["id"], msg)
    assert not kernel.store.rows("SELECT * FROM owner_dm_channels")
    assert not kernel.store.rows("SELECT * FROM messages")


@pytest.mark.parametrize("guard", ["recipient", "grant", "identity"])
async def test_dm_send_rechecks_recipient_and_current_grant(kernel, guard):
    bot, dm, _ = prepare(kernel)
    await kernel.connector.owner_dm(bot)
    if guard == "recipient":
        dm.recipient.id = 123456789012345678
    elif guard == "grant":
        kernel.store.put("bots", {**bot, "enabled_plugins": []})
    else:
        kernel.store.put("bots", {**bot, "application_id": "555555555555555555"})
    with pytest.raises(DeliveryError, match="Owner DM"):
        await kernel.connector.send(bot, DM, "Reminder", None, [])
    dm.send.assert_not_awaited()


async def test_dm_alarm_restart_grant_deferral_and_clean_slate(kernel):
    bot, _, _ = prepare(kernel)
    await kernel.connector.owner_dm(bot)
    row = await kernel.registry.secretary.call(ARGS, ToolContext(bot, DM, "origin"), {}, "")
    kernel.store.execute("UPDATE secretary_reminders SET due_at=?", (time.time() - 1,))
    # New plugin/store construction preserves the mapping and pending alarm.
    fresh = Kernel(kernel.store.path.parent)
    async with fresh.lifetime():
        assert fresh.store.is_owner_dm(bot, DM)
        assert fresh.registry.secretary.pending(bot)["id"] == row["id"]
        fresh.store.put("bots", {**bot, "enabled_plugins": []})
        assert fresh.registry.secretary.pending(bot) is None
        fresh.store.put("bots", bot)
        fresh.registry.reset_wakes(bot["id"], DM)
        assert fresh.registry.secretary.get(row["id"])["state"] == "cancelled"


async def test_unverified_synthetic_slash_cannot_route_private_reminder(kernel):
    bot, _, user = prepare(kernel)
    context = ToolContext({**bot, "invocation": {"kind": "slash"}}, "slash:ada:madeup", "missing")
    with pytest.raises(ControlError, match="verified owner"):
        await kernel.registry.secretary.call(ARGS, context, {}, "")
    user.create_dm.assert_not_awaited()
