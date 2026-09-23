"""Shared slash commands test helpers."""

import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from conftest import configured

from hortator.models import OWNER_ID
from hortator.slash_commands import COMMAND


def enable(k, bot_id="ada", **changes):
    bot = configured(k, bot_id, enabled_plugins=["slash_commands", "memory"], **changes)
    plugin = k.store.get("plugins", "slash_commands")
    plugin.pop("revision")
    plugin["enabled"] = True
    k.store.put("plugins", plugin)
    k.store.execute(
        "INSERT INTO slash_commands VALUES(?,?,?,?,?)",
        (bot_id, bot["application_id"], "123456789012345678", json.dumps(COMMAND), time.time()),
    )
    return bot


def interaction(
    bot, *, actor=OWNER_ID, private=True, prompt="Find something useful", interaction_id="987654321012345678"
):
    return SimpleNamespace(
        id=int(interaction_id),
        token="SECRET-INTERACTION-TOKEN",
        application_id=int(bot["application_id"]),
        type=discord.InteractionType.application_command,
        user=SimpleNamespace(id=int(actor), bot=False),
        channel_id=987654321098765432,
        guild_id=777777777777777777,
        created_at=datetime.now(timezone.utc),
        data={
            "name": "prompt",
            "type": 1,
            "id": "123456789012345678",
            "options": [
                {"name": "text", "type": 3, "value": prompt},
                {"name": "private", "type": 5, "value": private},
            ],
        },
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(return_value=SimpleNamespace(id=888888888888888888)),
    )
