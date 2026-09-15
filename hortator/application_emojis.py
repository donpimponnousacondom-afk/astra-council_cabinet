"""Application-owned emoji discovery; public metadata, never another bot's catalog."""

import asyncio
import re
import time

from .concurrency import error_text
from .store import dumps


REFRESH_SECONDS = 300
RETRY_SECONDS = 60
DISCOVERY_TIMEOUT_SECONDS = 20
MAX_APPLICATION_EMOJIS = 2000
MAX_PROMPT_EMOJIS = 64
MAX_PROMPT_CHARS = 6000
GUIDANCE = (
    "These are this application's own custom emojis, verified through Discord. "
    "To display one, copy its complete markup verbatim into ordinary message text. "
    "A bare :name: is only text in a bot API message; it is not expanded automatically. "
    "Do not invent an emoji ID, borrow another application's private emoji, escape its angle brackets, "
    "or put it in backticks/code blocks when you want it rendered. "
    "Other available server emojis may still be used with their known complete markup."
)


class ApplicationEmojis:
    def __init__(self, store):
        self.store = store
        self.catalogs = {}
        self.next_sync = {}
        self.tasks = {}

    def current(self, manager, client, application_id):
        bot = self.store.get("bots", client.bot_id)
        return bool(
            not manager.closed
            and not client.stopping
            and manager.clients.get(client.bot_id) is client
            and bot
            and bot["application_id"] == application_id
            and str(client.application_id) == application_id
        )

    def schedule_sync(self, manager, client):
        application_id = str(client.application_id)
        if not client.is_ready() or not self.current(manager, client, application_id):
            return
        bot_id = client.bot_id
        previous, next_at = self.next_sync.get(bot_id, (None, 0))
        if previous is client and time.monotonic() < next_at:
            return
        task = self.tasks.get(bot_id)
        if task and not task.done():
            return
        self.next_sync[bot_id] = (client, time.monotonic() + REFRESH_SECONDS)
        self.tasks[bot_id] = manager.background.spawn(
            self.sync(manager, client), name=f"application-emojis:{bot_id}", bot_id=bot_id
        )

    async def sync(self, manager, client):
        bot_id, application_id = client.bot_id, str(client.application_id)
        if not self.current(manager, client, application_id):
            return
        self.next_sync[bot_id] = (client, time.monotonic() + REFRESH_SECONDS)
        deadline = asyncio.timeout(DISCOVERY_TIMEOUT_SECONDS)
        try:
            async with deadline:
                emojis = await client.fetch_application_emojis()
            if not self.current(manager, client, application_id):
                return  # Token/client/application changed while the read was in flight.
            if len(emojis) > MAX_APPLICATION_EMOJIS:
                raise ValueError(f"Application emoji inventory exceeds {MAX_APPLICATION_EMOJIS} entries")
            items = []
            for emoji in emojis:
                identifier, name = str(emoji.id), emoji.name
                if (
                    not identifier.isascii()
                    or not identifier.isdecimal()
                    or not 1 <= len(identifier) <= 20
                    or not isinstance(name, str)
                    or not re.fullmatch(r"[A-Za-z0-9_]{1,32}", name)
                    or type(emoji.animated) is not bool
                ):
                    raise ValueError(
                        "Discord returned an invalid application emoji ID, name or animation flag"
                    )
                if not emoji.available:
                    continue
                items.append(
                    {"name": name, "markup": f"<{'a' if emoji.animated else ''}:{name}:{identifier}>"}
                )
            items.sort(key=lambda item: (item["name"], item["markup"]))
            previous = self.catalogs.get(bot_id)
            self.catalogs[bot_id] = {
                "application_id": application_id,
                "items": items,
                "fetched_at": time.monotonic(),
            }
            if not previous or previous["application_id"] != application_id or previous["items"] != items:
                self.store.emit(
                    "discord.application_emojis_updated",
                    {
                        "application_id": application_id,
                        "count": len(items),
                        "operation": "list_application_emojis",
                    },
                    bot_id=bot_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.current(manager, client, application_id):
                return
            self.catalogs.pop(bot_id, None)
            self.next_sync[bot_id] = (client, time.monotonic() + RETRY_SECONDS)
            self.store.emit(
                "discord.application_emojis_failed",
                {
                    "application_id": application_id,
                    "operation": "list_application_emojis",
                    "error": (
                        f"Local application emoji discovery deadline exceeded: {DISCOVERY_TIMEOUT_SECONDS} seconds"
                        if deadline.expired()
                        else error_text(exc)
                    ),
                    "http_status": getattr(exc, "status", None),
                    "retry_in_seconds": RETRY_SECONDS,
                    "reason": "Emoji inventory is unavailable; ordinary chat continues. Inventory is omitted until a successful refresh.",
                },
                bot_id=bot_id,
                level="warning",
            )

    def prompt(self, bot):
        catalog = self.catalogs.get(bot["id"])
        if (
            not catalog
            or catalog["application_id"] != bot["application_id"]
            or time.monotonic() - catalog["fetched_at"] > REFRESH_SECONDS * 2
        ):
            return {
                "status": "unavailable",
                "note": "Application emoji inventory has not been verified recently. Do not invent IDs or assume :name: will render.",
            }
        items, size = [], 0
        for item in catalog["items"]:
            size += len(dumps(item))
            if len(items) >= MAX_PROMPT_EMOJIS or size > MAX_PROMPT_CHARS:
                break
            items.append(dict(item))
        return {
            "status": "ready",
            "application_id": catalog["application_id"],
            "total": len(catalog["items"]),
            "shown": len(items),
            "truncated": len(items) < len(catalog["items"]),
            "items": items,
            "usage": GUIDANCE,
        }
