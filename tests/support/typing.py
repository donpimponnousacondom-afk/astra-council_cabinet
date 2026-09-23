"""Shared typing test helpers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from conftest import configured, ingest

from hortator.models import OWNER_ID


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


def connect_channel(kernel, bot, channel_id, typing):
    channel = SimpleNamespace(id=int(channel_id), typing=typing)
    kernel.connector.clients[bot["id"]] = SimpleNamespace(
        is_ready=lambda: True,
        get_channel=lambda requested: channel if requested == int(channel_id) else None,
        fetch_channel=AsyncMock(return_value=channel),
        close=AsyncMock(),
    )
    return channel
