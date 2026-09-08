from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import time

import discord
import httpx

from .models import ControlError, OWNER_ID
from .runtime import DeliveryError
from .security import Actor
from .discord_text import CodeBlock, code_pages, model_message, preview, with_footer
from .footer import footer_settings, render_footer
from .version import version_text
from .vision import ImageCache, image_candidate, MAX_IMAGES
from .store import dumps


TYPING_INTERVAL_SECONDS = 5
TYPING_TIMEOUT_SECONDS = 5
RECONNECT_NOTICE_DELAY_SECONDS = 30


COMMANDS = [
    ("!version | !ver", "Running code: commit, ISO date/time, title and startup time"),
    ("!status", "Council, Discord gateway and provider status"),
    ("!stats [hours]", "Usage, timings, costs and failures; default 24 hours"),
    ("!footer [bot-id] [enable | disable]", "Inspect or toggle message footer; defaults to Hortator"),
    (
        "!footer [bot-id] template <text>",
        "Set footer template; {{TTFT}}, {{TPS}}, {{PROVIDER}}, {{CONTEXT}}, {{MODEL}}, {{BOT}}",
    ),
    ("!stop [bot-id | all | providers:id]", "Pause and cancel active work immediately"),
    ("!restart <bot-id>", "Reconnect a failed Discord gateway without changing configuration"),
    ("!start [bot-id | all | providers:id]", "Resume a configured bot, provider or council"),
    ("!bots / !providers / !models / !plugins / !prompts / !rooms", "List configuration"),
    ("!get <resource> <id>", "Inspect one configuration record"),
    ("!set <resource> <id> <JSON>", "Patch configuration; advanced model JSON is supported"),
    ("!create <resource> <JSON>", "Create a bot, profile, provider, prompt or room"),
    ("!delete <resource> <id>", "Delete an unreferenced configuration record; retain history"),
    ("!clone <profile-id> [new-name]", "Clone a model profile with independent parameters"),
    ("!use <bot-id> <profile-id>", "Switch the model without changing personality or tools"),
    ("!prompt <bot-id> <text>", "Replace that bot's persona prompt"),
    ("!interval <bot-id> <seconds>", "Set activation interval and minimum send cooldown"),
    ("!context <bot-id> [channel-id]", "Inspect scoped memory and compaction"),
    ("!compact <bot-id> [channel-id]", "Request model-driven compaction"),
    ("!memory <bot-id> <JSON>", "Edit notes: channel_id, key, value (empty deletes)"),
    ("!trajectory [turn-id]", "Export the full turn or list recent turns"),
    ("!events [bot-id]", "Recent operational events"),
    ("!check <provider-id>", "Discover model catalog; does not verify credentials or generation"),
    ("!reset-circuit <provider-id>", "Allow another completion probe immediately"),
    ("!thread <room-id> <name>", "Create a public thread or forum post"),
    ("!dm <command or question>", "Continue privately with Hortator"),
]


def owner_message(message):
    return str(message.author.id) == OWNER_ID and not message.author.bot and not message.webhook_id


class CouncilClient(discord.Client):
    def __init__(self, manager, bot_id):
        intents = discord.Intents.none()
        intents.guilds = intents.messages = intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none(), max_messages=500)
        self.manager, self.bot_id = manager, bot_id
        self.backfill_task = None
        self.stopping = False

    async def on_ready(self):
        self.manager.gateway(self.bot_id, "online", source="ready")
        if not self.backfill_task or self.backfill_task.done():
            self.backfill_task = asyncio.create_task(
                self.manager.backfill(self), name=f"backfill:{self.bot_id}"
            )

    async def on_disconnect(self):
        if self.stopping or self.manager.closed:
            return
        socket = getattr(getattr(self, "ws", None), "socket", None)
        self.manager.gateway(
            self.bot_id,
            "reconnecting",
            source="disconnect_callback",
            close_code=getattr(socket, "close_code", None),
        )

    async def on_resumed(self):
        self.manager.gateway(self.bot_id, "online", source="resumed")

    async def on_message(self, message):
        try:
            await self.manager.receive(self.bot_id, message)
        except Exception as exc:
            self.manager.store.emit(
                "discord.receive_failed", {"error": str(exc)}, bot_id=self.bot_id, level="error"
            )

    async def on_raw_message_delete(self, payload):
        existing = self.manager.store.one(
            "SELECT deleted FROM messages WHERE discord_id=?", (str(payload.message_id),)
        )
        if existing and not existing["deleted"]:
            self.manager.store.execute(
                "UPDATE messages SET deleted=1 WHERE discord_id=?", (str(payload.message_id),)
            )
            self.manager.store.emit(
                "message.deleted",
                {"message_id": str(payload.message_id), "channel_id": str(payload.channel_id)},
            )

    async def on_raw_message_edit(self, payload):
        existing = self.manager.store.one(
            "SELECT * FROM messages WHERE discord_id=?", (str(payload.message_id),)
        )
        content = payload.data.get("content")
        if existing and content is not None:
            content = self.manager.canonical_edit(existing, content)
        if existing and content is not None and content != existing["content"]:
            content = self.manager.vault.redact(content)
            self.manager.store.execute(
                "UPDATE messages SET content=? WHERE discord_id=?", (content, str(payload.message_id))
            )
            self.manager.store.emit(
                "message.edited",
                {"message_id": str(payload.message_id), "before": existing["content"], "after": content},
            )

        if existing and not existing["deleted"] and "attachments" in payload.data:
            # Embed-only edits omit attachments. Explicit attachment edits, including [],
            # must update the transcript so removed pixels do not remain model-visible.
            prior = {a["id"]: a for a in json.loads(existing["attachments"])}
            attachments, image_index = [], 0
            for value in payload.data["attachments"]:
                item = {key: value.get(key) for key in ("id", "filename", "url", "size", "content_type")}
                item["id"] = str(item["id"])
                if prior.get(item["id"], {}).get("vision", {}).get("status") == "ready":
                    item["vision"] = prior[item["id"]]["vision"]
                if image_candidate(item):
                    image_index += 1
                    if image_index <= MAX_IMAGES:
                        item = await self.manager.images.capture(item)
                    else:
                        item["vision"] = {
                            "status": "unavailable",
                            "error": "Message exceeds the 8-image capture limit",
                        }
                attachments.append(item)
            self.manager.store.execute(
                "UPDATE messages SET attachments=? WHERE discord_id=?",
                (dumps(self.manager.vault.redact(attachments)), str(payload.message_id)),
            )

    async def on_thread_join(self, thread):
        self.manager.store.emit(
            "discord.thread_joined",
            {"channel_id": str(thread.id), "parent_id": str(thread.parent_id)},
            bot_id=self.bot_id,
        )

    async def on_error(self, event_method, *args, **kwargs):
        self.manager.store.emit(
            "discord.handler_error", {"handler": event_method}, bot_id=self.bot_id, level="error"
        )

    async def close(self):
        self.stopping = True
        if self.backfill_task and self.backfill_task is not asyncio.current_task():
            self.backfill_task.cancel()
            await asyncio.gather(self.backfill_task, return_exceptions=True)
        await super().close()


class DiscordManager:
    def __init__(self, service):
        self.service, self.store, self.vault = service, service.store, service.vault
        self.images = ImageCache(self.store)
        self.clients: dict[str, CouncilClient] = {}
        self.runners: dict[str, tuple[str, asyncio.Task]] = {}
        self.task = None
        self.closed = False
        self.last_notification = {}
        self.notification_cursor = self.store.one("SELECT coalesce(max(seq),0) AS seq FROM events")["seq"]
        self.last_health_check = 0
        self.unhealthy = {}
        self.reconnect_started = {}
        self.pending_reconnects = {}

    async def validate_token(self, token):
        try:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=15) as client:
                headers = {"Authorization": "Bot " + token.strip()}
                application = await client.get(
                    "https://discord.com/api/v10/oauth2/applications/@me", headers=headers
                )
                user = await client.get("https://discord.com/api/v10/users/@me", headers=headers)
                if application.status_code != 200 or user.status_code != 200 or not user.json().get("bot"):
                    raise ControlError(
                        "Discord rejected the bot token. Copy the Bot token, not the OAuth client secret."
                    )
                return {
                    "application_id": application.json()["id"],
                    "user_id": user.json()["id"],
                    "username": user.json()["username"],
                }
        except httpx.HTTPError as exc:
            raise ControlError("Could not reach Discord to verify the token") from exc

    def gateway(self, bot_id, status, error=None, *, source="supervisor", close_code=None):
        previous = self.store.runtime(bot_id)
        now = time.time()
        data = {"source": source, "previous_status": previous["gateway_status"]}
        if error:
            data["error"] = self.vault.redact(error)
        if close_code is not None:
            data["close_code"] = close_code
        if status == "reconnecting":
            data["reconnect_started_at"] = self.reconnect_started.setdefault(bot_id, now)
            if not error:
                data["reason"] = "Discord did not supply a disconnect reason"
        elif status == "online" and bot_id in self.reconnect_started:
            data["reconnect_duration_ms"] = max(0, now - self.reconnect_started.pop(bot_id)) * 1000
        elif status == "offline":
            self.reconnect_started.pop(bot_id, None)
        self.store.execute(
            "UPDATE bot_runtime SET gateway_status=?,error=coalesce(?,error) WHERE bot_id=?",
            (status, self.vault.redact(error) if error else None, bot_id),
        )
        if previous["gateway_status"] != status or error and previous["error"] != error:
            self.store.emit(
                "discord." + status,
                data,
                bot_id=bot_id,
                level="error" if status == "failed" else "warning" if status == "reconnecting" else "info",
            )

    def start(self):
        self.task = asyncio.create_task(self.supervise(), name="discord-supervisor")

    async def run_client(self, bot, token):
        delay = 5
        while not self.closed:
            client = CouncilClient(self, bot["id"])
            self.clients[bot["id"]] = client
            try:
                self.gateway(bot["id"], "connecting")
                async with client:
                    await client.login(token)
                    info = await client.application_info()
                    if str(info.id) != bot["application_id"] or not client.user.bot:
                        self.gateway(bot["id"], "failed", "Stored token does not match this application")
                        return
                    self.vault.put(f"bot/{bot['id']}/user_id", str(client.user.id))
                    await client.connect(reconnect=True)
            except (discord.LoginFailure, discord.PrivilegedIntentsRequired) as exc:
                self.gateway(
                    bot["id"],
                    "failed",
                    "Invalid token"
                    if isinstance(exc, discord.LoginFailure)
                    else "Enable Message Content Intent in the Discord Developer Portal",
                )
                return
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.gateway(bot["id"], "failed", f"{type(exc).__name__}: {exc}")
            finally:
                await client.close()
                if self.clients.get(bot["id"]) is client:
                    self.clients.pop(bot["id"], None)
            if self.closed:
                break
            await asyncio.sleep(delay)
            delay = min(120, delay * 2)

    async def supervise(self):
        while not self.closed:
            try:
                desired = {}
                for bot in self.store.list("bots"):
                    token = self.vault.get(f"bot/{bot['id']}/token")
                    # The deterministic control plane stays online even when Hortator's model is paused.
                    if token and bot["application_id"] and (bot["enabled"] or bot["role"] == "hortator"):
                        fingerprint = hashlib.sha256((token + bot["application_id"]).encode()).hexdigest()
                        desired[bot["id"]] = fingerprint
                        existing = self.runners.get(bot["id"])
                        if existing and existing[0] != fingerprint:
                            existing[1].cancel()
                            await asyncio.gather(existing[1], return_exceptions=True)
                            self.runners.pop(bot["id"], None)
                        if bot["id"] not in self.runners:
                            self.runners[bot["id"]] = (
                                fingerprint,
                                asyncio.create_task(self.run_client(bot, token), name=f"discord:{bot['id']}"),
                            )
                for bot_id in list(self.runners):
                    if bot_id not in desired:
                        _, task = self.runners.pop(bot_id)
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        self.gateway(bot_id, "offline")
                if time.time() - self.last_health_check > 15:
                    self.last_health_check = time.time()
                    for bot_id, client in list(self.clients.items()):
                        healthy = (
                            client.is_ready()
                            and not client.is_closed()
                            and math.isfinite(client.latency)
                            and client.latency < 30
                        )
                        self.store.execute(
                            "UPDATE bot_runtime SET heartbeat_ms=? WHERE bot_id=?",
                            (client.latency * 1000 if math.isfinite(client.latency) else None, bot_id),
                        )
                        self.unhealthy[bot_id] = 0 if healthy else self.unhealthy.get(bot_id, 0) + 1
                        if self.unhealthy[bot_id] >= 3:
                            self.gateway(
                                bot_id,
                                "reconnecting",
                                "Gateway heartbeat is unhealthy; reconnecting",
                                source="heartbeat_watchdog",
                            )
                            # Close one client; its single supervisor recreates it. Never start competing reconnect loops.
                            await client.close()
                            self.unhealthy[bot_id] = 0
                await self.notifications()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.store.emit("discord.supervisor_error", {"error": str(exc)}, level="error")
            await asyncio.sleep(2)

    def scope(self, bot, message):
        channel_id = str(message.channel.id)
        guild_id = str(message.guild.id) if message.guild else None
        parent_id = str(getattr(message.channel, "parent_id", ""))
        if bot["role"] == "hortator":
            if not owner_message(message):
                return None
            settings = self.store.get("settings", "global")
            if (
                not message.guild
                or guild_id == settings["control_guild_id"]
                and (
                    channel_id == settings["control_channel_id"]
                    or parent_id == settings["control_channel_id"]
                )
            ):
                return f"owner:{bot['id']}"
            return None
        if not message.guild:
            return None
        for room in self.store.list("rooms"):
            if (
                room["id"] in bot["room_ids"]
                and guild_id == room["guild_id"]
                and (
                    channel_id == room["channel_id"]
                    or room["include_threads"]
                    and parent_id == room["channel_id"]
                )
            ):
                if message.webhook_id:
                    return None
                known = {
                    self.vault.get(f"bot/{b['id']}/user_id") or b["application_id"]
                    for b in self.store.list("bots")
                }
                if (
                    message.author.bot
                    and str(message.author.id) not in known
                    and not room["allow_external_bots"]
                ):
                    return None
                if not message.author.bot and not room["allow_humans"] and str(message.author.id) != OWNER_ID:
                    return None
                return room["id"]
        return None

    async def restart(self, bot_id):
        entry = self.runners.pop(bot_id, None)
        if entry:
            entry[1].cancel()
            await asyncio.gather(entry[1], return_exceptions=True)
        self.gateway(bot_id, "offline")

    def reconcile(self, message):
        nonce = getattr(message, "nonce", None)
        if not message.author.bot or message.webhook_id:
            return None
        message_id = str(getattr(message, "id", ""))
        pending = (
            self.store.one(
                "SELECT * FROM outbox WHERE id=? AND channel_id=?", (str(nonce), str(message.channel.id))
            )
            if nonce
            else None
        )
        if not pending:
            canonical = self.store.one(
                "SELECT content FROM messages WHERE discord_id=? AND channel_id=? AND author_id=? AND bot_id IS NOT NULL",
                (message_id, str(message.channel.id), str(message.author.id)),
            )
            if canonical:
                return canonical["content"]
            # History/backfill can omit nonces. A recorded Discord ID still proves
            # which canonical model answer belongs here, without its diagnostic footer.
            pending = self.store.one(
                "SELECT * FROM outbox WHERE discord_id=? AND channel_id=?",
                (message_id, str(message.channel.id)),
            )
        if not pending:
            return None
        bot = self.store.get("bots", pending["bot_id"])
        if not bot or str(message.author.id) != (
            self.vault.get(f"bot/{bot['id']}/user_id") or bot["application_id"]
        ):
            return None
        if pending["status"] == "unknown":
            self.store.execute(
                "UPDATE outbox SET status='sent',discord_id=?,sent_at=?,error=NULL WHERE id=?",
                (str(message.id), time.time(), pending["id"]),
            )
            self.store.emit(
                "delivery.reconciled",
                {
                    "outbox_id": pending["id"],
                    "discord_id": str(message.id),
                    "evidence": "Discord gateway nonce",
                },
                bot_id=bot["id"],
                turn_id=pending["turn_id"],
            )
        return pending["content"]

    async def receive(self, bot_id, message, *, historical=False):
        bot = self.store.get("bots", bot_id)
        if not bot:
            return
        canonical_content = self.reconcile(message)
        scope = self.scope(bot, message)
        if not scope:
            return
        if message.content.lstrip().startswith("!"):
            if bot["role"] == "hortator" and not historical:
                await self.command(bot, message)
            return
        # Commands never reach a model. Ordinary Hortator messages must pass the exact same owner gate.
        known = next(
            (
                b
                for b in self.store.list("bots")
                if str(message.author.id) == (self.vault.get(f"bot/{b['id']}/user_id") or b["application_id"])
            ),
            None,
        )
        from .addressing import capture

        addressing = await capture(self.store, self.vault, message, historical=historical)
        attachments = []
        previous = self.store.one("SELECT attachments FROM messages WHERE discord_id=?", (str(message.id),))
        prior = {a["id"]: a for a in json.loads(previous["attachments"])} if previous else {}
        image_index = 0
        for a in message.attachments:
            attachment = {
                "id": str(a.id),
                "filename": a.filename,
                "url": a.url,
                "size": a.size,
                "content_type": a.content_type,
            }
            if prior.get(str(a.id), {}).get("vision", {}).get("status") == "ready":
                attachment["vision"] = prior[str(a.id)]["vision"]
            if image_candidate(attachment):
                image_index += 1
                if image_index <= MAX_IMAGES:
                    attachment = await self.images.capture(attachment)
                else:
                    attachment["vision"] = {
                        "status": "unavailable",
                        "error": "Message exceeds the 8-image capture limit",
                    }
                if attachment.get("vision", {}).get("status") == "unavailable":
                    self.store.emit(
                        "attachment.image_unavailable",
                        {
                            "message_id": str(message.id),
                            "attachment_id": str(a.id),
                            "error": attachment["vision"]["error"],
                        },
                        bot_id=bot_id,
                        level="warning",
                    )
            attachments.append(attachment)
        self.store.ingest(
            discord_id=str(message.id),
            channel_id=str(message.channel.id),
            room_id=scope,
            author_id=str(message.author.id),
            author_name=message.author.display_name,
            bot_id=known["id"] if known else None,
            content=canonical_content or message.content,
            at=message.created_at.timestamp(),
            guild_id=str(message.guild.id) if message.guild else None,
            parent_id=str(message.channel.parent_id) if getattr(message.channel, "parent_id", None) else None,
            reply_to=str(message.reference.message_id)
            if message.reference and message.reference.message_id
            else None,
            attachments=attachments,
            addressing=addressing,
        )
        if previous:
            self.store.execute(
                "UPDATE messages SET attachments=? WHERE discord_id=?",
                (dumps(self.vault.redact(attachments)), str(message.id)),
            )
            # Enrich old records from authenticated history, without making history
            # a live priority trigger or replacing an already recorded live event.
            self.store.execute(
                "UPDATE messages SET addressing=? WHERE discord_id=? AND addressing='{}'",
                (dumps(self.vault.redact({**addressing, "live": False})), str(message.id)),
            )
        self.store.context(bot_id, str(message.channel.id))
        self.store.runtime(bot_id)

    def canonical_edit(self, existing, content):
        if not existing["bot_id"]:
            return content
        sent = self.store.one(
            "SELECT id,turn_id,content FROM outbox WHERE discord_id=? AND bot_id=? AND channel_id=?",
            (existing["discord_id"], existing["bot_id"], existing["channel_id"]),
        )
        if not sent:
            return content
        queued = self.store.one(
            "SELECT data FROM events WHERE turn_id=? AND kind='delivery.queued' AND json_extract(data,'$.outbox_id')=? ORDER BY seq DESC LIMIT 1",
            (sent["turn_id"], sent["id"]),
        )
        footer = json.loads(queued["data"]).get("footer", "") if queued else ""
        if footer:
            # Discord embed updates may repeat the unchanged wire text. Preserve
            # the full canonical answer, including when Discord only saw a preview.
            if content == model_message(sent["content"], footer)[0]:
                return sent["content"]
            return content.removesuffix("\n" + footer)
        return content

    async def backfill(self, client):
        bot = self.store.get("bots", client.bot_id)
        if not bot or bot["role"] == "hortator":
            return
        channel_ids = {
            r["channel_id"]
            for r in self.store.list("rooms")
            if r["id"] in bot["room_ids"] and r["channel_id"]
        }
        channel_ids.update(
            row["channel_id"]
            for row in self.store.rows("SELECT channel_id FROM contexts WHERE bot_id=?", (bot["id"],))
        )
        for channel_id in channel_ids:
            try:
                channel = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
                room = next(
                    (
                        r
                        for r in self.store.list("rooms")
                        if r["id"] in bot["room_ids"]
                        and (
                            r["channel_id"] == str(channel.id)
                            or r["include_threads"]
                            and r["channel_id"] == str(getattr(channel, "parent_id", ""))
                        )
                    ),
                    None,
                )
                if (
                    not room
                    or not getattr(channel, "guild", None)
                    or str(channel.guild.id) != room["guild_id"]
                ):
                    raise ControlError("Channel does not match an assigned room and configured guild")
                if isinstance(channel, discord.ForumChannel):
                    continue
                last = self.store.one(
                    "SELECT discord_id FROM messages WHERE channel_id=? ORDER BY at DESC LIMIT 1",
                    (channel_id,),
                )
                after = (
                    discord.Object(id=int(last["discord_id"]))
                    if last and last["discord_id"].isdigit()
                    else None
                )
                count = 0
                async for message in channel.history(
                    limit=5000 if after else 100, after=after, oldest_first=True if after else False
                ):
                    # First-time history is collected below and replayed chronologically.
                    if after:
                        await self.receive(bot["id"], message, historical=True)
                    else:
                        if count == 0:
                            collected = []
                        collected.append(message)
                    count += 1
                if not after and count:
                    for message in reversed(collected):
                        await self.receive(bot["id"], message, historical=True)
                self.store.emit(
                    "discord.history_imported",
                    {"channel_id": channel_id, "observed": count, "initial_tail": not bool(after)},
                    bot_id=bot["id"],
                )
                if count == 5000:
                    self.store.emit(
                        "discord.history_gap",
                        {
                            "channel_id": channel_id,
                            "error": "Backfill reached 5,000 messages; older downtime history may be incomplete",
                        },
                        bot_id=bot["id"],
                        level="warning",
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.execute(
                    "UPDATE bot_runtime SET error=? WHERE bot_id=?",
                    (self.vault.redact(f"Discord channel {channel_id}: {exc}"), bot["id"]),
                )
                self.store.emit(
                    "discord.history_failed",
                    {"channel_id": channel_id, "error": str(exc)},
                    bot_id=bot["id"],
                    level="warning",
                )

    async def reply(self, channel, result):
        bot = next((b for b in self.store.list("bots") if b["role"] == "hortator"), None)
        footer = self.footer(bot) if bot else ""
        limit = 2000 - (len(footer.encode("utf-16-le")) // 2 + 1 if footer else 0)
        if isinstance(result, CodeBlock):
            for page in code_pages(self.vault.redact(result.text), result.language, limit=limit):
                await channel.send(
                    content=with_footer(page, footer), allowed_mentions=discord.AllowedMentions.none()
                )
            return
        content = result if isinstance(result, str) else json.dumps(result, indent=2, ensure_ascii=False)
        content = self.vault.redact(content)
        message = with_footer(content if isinstance(result, str) else "```json\n" + content + "\n```", footer)
        if len(message.encode("utf-16-le")) <= 4000:
            await channel.send(
                content=message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await channel.send(
                content=with_footer("Council report attached.", footer),
                file=discord.File(
                    io.BytesIO(content.encode()),
                    filename="council-report.txt" if isinstance(result, str) else "council-report.json",
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def command(self, bot, message):
        actor = Actor(
            "discord",
            str(message.author.id),
            message.author.bot,
            str(message.webhook_id) if message.webhook_id else None,
        )
        actor.require_owner()
        text = message.content.lstrip()[1:].strip()
        channel = message.channel
        try:
            if text.startswith("dm "):
                text = text[3:].lstrip()
                user = message.author
                channel = await user.create_dm()
                if not text.startswith("!"):
                    # This is an owner-initiated private question, not a synthetic Discord message author.
                    self.store.ingest(
                        discord_id=f"private:{message.id}",
                        channel_id=str(channel.id),
                        room_id=f"owner:{bot['id']}",
                        author_id=OWNER_ID,
                        author_name="The Boss",
                        content=text,
                    )
                    self.store.context(bot["id"], str(channel.id))
                    return
                text = text[1:]
            if message.attachments:
                if len(message.attachments) != 1 or message.attachments[0].size > 200000:
                    raise ControlError("Attach one UTF-8 text/JSON file under 200 KB")
                text += " " + (await message.attachments[0].read()).decode("utf-8")
            parts = text.split(maxsplit=1)
            name, rest = parts[0].lower() if parts else "help", parts[1] if len(parts) > 1 else ""
            aliases = {"models": "profiles", "pause": "stop", "resume": "start", "ver": "version"}
            name = aliases.get(name, name)
            if name == "help":
                result = CodeBlock("\n".join(f"{cmd} — {desc}" for cmd, desc in COMMANDS))
            elif name == "version":
                result = CodeBlock(version_text(self.service.version()))
            elif name == "footer":
                result = await self.footer_command(actor, bot, rest)
            elif name == "status":
                status = self.service.status()
                result = {
                    "enabled": status["settings"]["enabled"],
                    "active_requests": status["active_requests"],
                    "bots": [
                        {
                            "id": b["id"],
                            "enabled": b["enabled"],
                            "profile": b["model_profile_id"],
                            **b["runtime"],
                            "readiness": b["readiness"],
                        }
                        for b in status["bots"]
                    ],
                    "providers": [
                        {"id": p["id"], "enabled": p["enabled"], **p["health"]} for p in status["providers"]
                    ],
                }
            elif name == "stats":
                hours = int(rest or "24")
                if not 1 <= hours <= 8760:
                    raise ControlError("Hours must be between 1 and 8760")
                result = self.service.stats(hours)
            elif name in ("bots", "providers", "profiles", "plugins", "prompts", "rooms", "settings"):
                result = self.service.inspect(name)
            elif name == "get":
                kind, entity_id = rest.split(maxsplit=1)
                result = self.service.inspect(aliases.get(kind, kind), entity_id.strip())
            elif name in ("set", "create", "delete"):
                if name == "set":
                    kind, entity_id, raw = rest.split(maxsplit=2)
                    data = self.parse_json(raw)
                elif name == "create":
                    kind, raw = rest.split(maxsplit=1)
                    data = self.parse_json(raw)
                    entity_id = data.get("id")
                else:
                    kind, entity_id = rest.split(maxsplit=1)
                    data = {}
                result = await self.service.control(
                    actor,
                    {
                        "action": "save" if name == "set" else name,
                        "kind": aliases.get(kind, kind),
                        "id": entity_id,
                        "data": data,
                    },
                )
            elif name in ("stop", "start"):
                target = rest.strip() or "all"
                kind, target = target.split(":", 1) if ":" in target else ("bots", target)
                result = await self.service.control(actor, {"action": name, "kind": kind, "id": target})
            elif name in ("use", "prompt", "interval"):
                bot_id, value = rest.split(maxsplit=1)
                data = (
                    {"model_profile_id": value.strip()}
                    if name == "use"
                    else {"persona": value}
                    if name == "prompt"
                    else {"interval_seconds": float(value), "cooldown_seconds": float(value)}
                )
                result = await self.service.control(
                    actor, {"action": "save", "kind": "bots", "id": bot_id, "data": data}
                )
            elif name == "clone":
                args = rest.split(maxsplit=1)
                result = await self.service.control(
                    actor,
                    {"action": "clone", "id": args[0], "data": {"name": args[1]} if len(args) > 1 else {}},
                )
            elif name in ("context", "compact"):
                args = rest.split()
                result = (
                    self.service.inspect("context", args[0], args[1] if len(args) > 1 else None)
                    if name == "context"
                    else await self.service.control(
                        actor,
                        {
                            "action": "compact",
                            "id": args[0],
                            "data": {"channel_id": args[1]} if len(args) > 1 else {},
                        },
                    )
                )
            elif name == "memory":
                bot_id, raw = rest.split(maxsplit=1)
                result = await self.service.control(
                    actor, {"action": "memory", "id": bot_id, "data": self.parse_json(raw)}
                )
            elif name == "trajectory":
                result = self.service.turn(rest.strip()) if rest.strip() else self.service.turns(limit=20)
            elif name == "events":
                result = self.store.events(bot_id=rest.strip() or None, limit=30)
            elif name == "restart":
                result = await self.service.control(actor, {"action": "restart", "id": rest.strip()})
            elif name in ("check", "reset-circuit"):
                result = await self.service.control(
                    actor, {"action": "probe" if name == "check" else "reset_circuit", "id": rest.strip()}
                )
            elif name == "thread":
                room_id, title = rest.split(maxsplit=1)
                result = await self.service.control(
                    actor, {"action": "thread", "id": room_id, "data": {"name": title}}
                )
            else:
                raise ControlError("Unknown command. Use !help.")
            self.store.emit(
                "discord.command", {"actor": actor.label, "command": name, "channel_id": str(channel.id)}
            )
            await self.reply(channel, result)
        except Exception as exc:
            error = self.vault.redact(str(exc)) or "Invalid command arguments. Use !help."
            self.store.emit(
                "discord.command_failed",
                {"actor": actor.label, "error": error},
                bot_id=bot["id"],
                level="warning",
            )
            await self.reply(channel, "Command failed: " + error[:1500])

    def footer(self, bot):
        profile = self.store.get("profiles", bot["model_profile_id"])
        provider = self.store.get("providers", profile["provider_id"]) if profile else None
        # Command/incident responses have no model request: never reuse old timings.
        return render_footer(bot, profile, provider, redact=self.vault.redact)

    async def footer_command(self, actor, hortator, rest):
        args = rest.split(maxsplit=1)
        actions = {"enable", "enabled", "disable", "disabled", "template"}
        bot_id = hortator["id"]
        if args and args[0].lower() not in actions:
            bot_id = args[0]
            args = args[1].split(maxsplit=1) if len(args) > 1 else []
        bot = self.service.entity("bots", bot_id)
        if args:
            action = args[0].lower()
            if action == "template" and len(args) == 2:
                data = {"footer_template": args[1]}
            elif action in actions - {"template"} and len(args) == 1:
                data = {"footer_enabled": action in {"enable", "enabled"}}
            else:
                raise ControlError("Use !footer [bot-id] enable|disable or !footer [bot-id] template <text>")
            bot = await self.service.control(
                actor, {"action": "save", "kind": "bots", "id": bot_id, "data": data}
            )
        settings = footer_settings(bot)
        return CodeBlock(
            f"Footer · {bot_id}: {'enabled' if settings['footer_enabled'] else 'disabled'}\n"
            f"Template: {settings['footer_template']}"
        )

    @staticmethod
    def parse_json(raw):
        raw = raw.strip()
        if raw.startswith("```") and raw.endswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ControlError("Configuration must be a JSON object")
        return result

    async def typing(self, bot, channel_id, *, turn_id):
        reported_failure = False
        while not self.closed:
            current = self.store.get("bots", bot["id"])
            if not current or not self.service.engine.channel_allowed(current, channel_id):
                return
            client = self.clients.get(bot["id"])
            if client and client.is_ready():
                try:
                    # Renew before Discord's ten-second expiry. A separate task prevents a slow
                    # presence request from delaying context, tools, generation, or delivery.
                    async with asyncio.timeout(TYPING_TIMEOUT_SECONDS):
                        channel = client.get_channel(int(channel_id)) or await client.fetch_channel(
                            int(channel_id)
                        )
                        if isinstance(channel, discord.ForumChannel):
                            return  # Forum discussions take place inside their posts/threads.
                        await channel.typing()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not reported_failure:
                        self.store.emit(
                            "discord.typing_failed",
                            {"channel_id": channel_id, "error": self.vault.redact(str(exc))},
                            bot_id=bot["id"],
                            turn_id=turn_id,
                            level="warning",
                        )
                        reported_failure = True
            await asyncio.sleep(TYPING_INTERVAL_SECONDS)

    async def send(self, bot, channel_id, content, reply_to, paths, *, nonce=None, footer=None):
        client = self.clients.get(bot["id"])
        if not client or not client.is_ready():
            raise DeliveryError("Discord gateway is not ready")
        files = []
        try:
            channel = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
            if isinstance(channel, discord.ForumChannel):
                raise DeliveryError("Forum messages must target a thread; create one with !thread")
            files = [discord.File(path) for path in paths]
            footer = self.footer(bot) if footer is None else footer
            message, attached = model_message(content, footer)
            if attached:
                files.append(discord.File(io.BytesIO(content.encode()), filename="full-response.txt"))
            reference = (
                discord.MessageReference(
                    message_id=int(reply_to), channel_id=int(channel_id), fail_if_not_exists=False
                )
                if reply_to
                else None
            )
            async with asyncio.timeout(60):
                sent = await channel.send(
                    message,
                    files=files,
                    reference=reference,
                    nonce=nonce,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            return str(sent.id)
        except discord.HTTPException as exc:
            raise DeliveryError(
                f"Discord HTTP {exc.status}: {exc.text}", uncertain=exc.status >= 500
            ) from exc
        except (ValueError, discord.InvalidData) as exc:
            raise DeliveryError(str(exc)) from exc
        finally:
            for file in files:
                file.close()

    async def create_thread(self, room, name):
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise ControlError("Thread name must have 1–100 characters")
        bot = next(
            (
                b
                for b in self.store.list("bots")
                if b["id"] in self.clients and (room["id"] in b["room_ids"] or b["role"] == "hortator")
            ),
            None,
        )
        if not bot:
            raise ControlError("Connect an application with access to this room first")
        client = self.clients[bot["id"]]
        channel = client.get_channel(int(room["channel_id"])) or await client.fetch_channel(
            int(room["channel_id"])
        )
        if str(channel.guild.id) != room["guild_id"]:
            raise ControlError("Room channel does not belong to the configured guild")
        if isinstance(channel, discord.ForumChannel):
            created = await channel.create_thread(
                name=name,
                content=with_footer("Council discussion opened by The Boss.", self.footer(bot)),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            thread = created.thread
        elif isinstance(channel, discord.TextChannel):
            thread = await channel.create_thread(name=name, type=discord.ChannelType.public_thread)
        else:
            raise ControlError("Create threads from a text or forum channel")
        for member in self.store.list("bots"):
            if room["id"] in member["room_ids"]:
                self.store.context(member["id"], str(thread.id))
        return {"thread_id": str(thread.id), "url": thread.jump_url}

    async def notifications(self):
        settings = self.store.get("settings", "global")
        if not settings["incident_notifications"]:
            self.pending_reconnects.clear()
        events = self.store.events(after=self.notification_cursor, limit=100)
        for event in events:
            self.notification_cursor = max(self.notification_cursor, event["seq"])
            if event["kind"] in ("discord.online", "discord.offline", "discord.failed"):
                self.pending_reconnects.pop(event["bot_id"], None)
            reportable = event["kind"] in (
                "provider.failure",
                "provider.circuit_open",
                "provider.recovered",
                "discord.failed",
                "discord.reconnecting",
                "discord.history_failed",
                "discord.history_gap",
                "discord.online",
                "turn.failed",
                "compaction.completed",
                "delivery.unknown",
                "delivery.failed",
                "activation.budget_blocked",
            )
            if not reportable or not settings["incident_notifications"]:
                continue
            if event["kind"] == "discord.reconnecting":
                self.pending_reconnects[event["bot_id"]] = event
                continue
            duration = event["data"].get("reconnect_duration_ms")
            if (
                event["kind"] == "discord.online"
                and duration is not None
                and duration < RECONNECT_NOTICE_DELAY_SECONDS * 1000
            ):
                continue  # Brief resumes remain in the ledger without a pair of chat messages.
            await self.notify_event(event, settings)
        for bot_id, event in list(self.pending_reconnects.items()):
            if self.store.runtime(bot_id)["gateway_status"] != "reconnecting":
                self.pending_reconnects.pop(bot_id, None)
                continue
            started = event["data"].get("reconnect_started_at", event["at"])
            if time.time() - started >= RECONNECT_NOTICE_DELAY_SECONDS:
                await self.notify_event(event, settings)

    async def notify_event(self, event, settings):
        data = event["data"]
        duration = data.get("reconnect_duration_ms")
        # A startup connection notice must not suppress a later outage recovery.
        kind = (
            "discord.recovered"
            if event["kind"] == "discord.online" and duration is not None
            else event["kind"]
        )
        key = (kind, event["bot_id"] or data.get("provider_id"))
        if time.time() - self.last_notification.get(key, 0) < 300:
            return
        hortator = next((b for b in self.store.list("bots") if b["role"] == "hortator"), None)
        client = self.clients.get(hortator["id"]) if hortator else None
        if not client or not client.is_ready() or not settings["control_channel_id"]:
            return
        try:
            channel = client.get_channel(int(settings["control_channel_id"])) or await client.fetch_channel(
                int(settings["control_channel_id"])
            )
            if not getattr(channel, "guild", None) or str(channel.guild.id) != settings["control_guild_id"]:
                raise ControlError("Reporting channel does not match the configured guild")
            if event["kind"] == "discord.reconnecting":
                elapsed = max(0, time.time() - data.get("reconnect_started_at", event["at"]))
                detail = f"Discord connection has been interrupted for {elapsed:.1f}s; reconnecting. "
                detail += data.get("error") or data.get("reason") or "Disconnect reason unavailable."
                if data.get("close_code") is not None:
                    detail += f" (close code {data['close_code']})"
            elif event["kind"] == "discord.online":
                detail = (
                    f"Discord connection restored after {duration / 1000:.1f}s."
                    if duration is not None
                    else "Discord connected."
                )
            else:
                info = {
                    k: v
                    for k, v in data.items()
                    if v is not None
                    and k
                    in (
                        "error",
                        "provider_id",
                        "consecutive_failures",
                        "retry_in_seconds",
                        "after_tokens",
                        "before_tokens",
                    )
                }
                detail = json.dumps(info, ensure_ascii=False)
            await self.reply(
                channel,
                f"[{event['kind']}] {event['bot_id'] or 'provider'} · event #{event['seq']}\n"
                + preview(detail, 1300),
            )
            self.last_notification[key] = time.time()
        except Exception as exc:
            self.store.emit(
                "notification.failed", {"source_event": event["seq"], "error": str(exc)}, level="warning"
            )

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        tasks = [entry[1] for entry in self.runners.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.runners.clear()
        self.clients.clear()
