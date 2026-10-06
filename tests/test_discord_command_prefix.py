from unittest.mock import AsyncMock

import pytest

from conftest import configured
from hortator.models import OWNER_ID
from support.addressing import CHANNEL, message


def recipient(kernel, bot_id):
    bot = configured(kernel, bot_id)
    if bot_id == "hortator":
        settings = kernel.store.get("settings", "global")
        settings.update(control_guild_id="111111111111111111", control_channel_id=CHANNEL)
        kernel.store.put("settings", settings)
    return bot


@pytest.mark.parametrize(
    "content",
    ["!!!!! GENIUS", "  !!hello", "!hello!", "!hello! more text", "!hello!, how are you?", "!hello!."],
)
@pytest.mark.parametrize("bot_id", ["ada", "hortator"])
@pytest.mark.parametrize("historical", [False, True])
async def test_exclamations_are_stored_as_replies_without_command_dispatch(
    kernel, content, bot_id, historical
):
    bot = recipient(kernel, bot_id)
    parent_id = "555555555555555551"
    kernel.store.ingest(
        discord_id=parent_id,
        channel_id=CHANNEL,
        room_id="council",
        author_id=bot["application_id"],
        author_name=bot["name"],
        bot_id=bot_id,
        content="Parent answer",
    )
    msg = message(555555555555555552, reply_to=parent_id, content=content)
    kernel.connector.command = AsyncMock()
    await kernel.connector.receive(bot_id, msg, historical=historical)
    row = kernel.store.transcript(CHANNEL)[-1]
    assert row["discord_id"] == str(msg.id) and row["content"] == content
    assert row["reply_to"] == parent_id
    assert bool(kernel.engine.attention(bot)) is not historical
    kernel.connector.command.assert_not_awaited()


@pytest.mark.parametrize(
    "content", ["!", "!help", "  ! help", "!ver", "!VERSION", "!unknown", "!help argument!"]
)
@pytest.mark.parametrize("bot_id", ["ada", "hortator"])
@pytest.mark.parametrize("historical", [False, True])
async def test_single_prefix_stays_out_of_context_and_hortator_handles_commands(
    kernel, content, bot_id, historical
):
    bot = recipient(kernel, bot_id)
    msg = message(555555555555555552, content=content, mentions=[bot])
    kernel.connector.reply = AsyncMock()
    await kernel.connector.receive(bot_id, msg, historical=historical)
    assert kernel.store.transcript(CHANNEL) == []
    assert not kernel.engine.attention(bot)
    assert not kernel.store.rows("SELECT * FROM requests")
    assert kernel.connector.reply.await_count == (1 if bot_id == "hortator" and not historical else 0)
    if bot_id == "hortator" and not historical:
        failed = [e for e in kernel.store.events() if e["kind"] == "discord.command_failed"]
        assert bool(failed) == (content == "!unknown")


@pytest.mark.parametrize("blocked", ["non_owner", "bot", "webhook", "outside_channel"])
async def test_punctuation_does_not_bypass_hortator_scope(kernel, blocked):
    recipient(kernel, "hortator")
    msg = message(555555555555555552, content="!hello!")
    if blocked == "non_owner":
        msg.author.id = int(OWNER_ID) + 1
    elif blocked == "bot":
        msg.author.bot = True
    elif blocked == "webhook":
        msg.webhook_id = 123
    else:
        msg.channel.id = 999
    kernel.connector.command = AsyncMock()
    await kernel.connector.receive("hortator", msg)
    assert kernel.store.transcript(str(msg.channel.id)) == []
    kernel.connector.command.assert_not_awaited()
