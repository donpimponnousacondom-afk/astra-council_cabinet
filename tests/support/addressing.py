"""Shared addressing test helpers."""

import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from conftest import configured

from hortator.models import OWNER_ID

CHANNEL = "222222222222222222"


def pair(kernel, *, interval_seconds=120):
    bots = [
        configured(
            kernel, name, interval_seconds=interval_seconds, cooldown_seconds=120, evaluate_when_idle=False
        )
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
