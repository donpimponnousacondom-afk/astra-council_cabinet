"""Optional owner-only slash ingress; ordinary channel intake is never widened.

Discord tokens live only in the current Interaction object. Model/tool work is
owned by the existing runtime TaskGroup and shares its per-bot active-turn slot.
"""

from __future__ import annotations

import asyncio
import io
import time

import aiohttp
import discord

from .concurrency import error_text
from .context import ContextBuilder
from .diagnostics import reasoning_settings
from .discord_text import markdown_preview, model_message
from .footer import render_footer
from .models import ControlError, OWNER_ID
from .provider import strip_reasoning
from .runtime import Engine, DeliveryError
from .store import dumps, uid
from .vision_turn import select_images

PLUGIN_ID = "slash_commands"
MAX_PROMPT_CHARS = 6000
MAX_SECONDS = 14 * 60  # Whole slash task; reserve one minute before Discord's 15-minute token expiry.
ACK_SECONDS = 2.9  # Discord invalidates an unacknowledged interaction at three seconds.
ACK_ATTEMPTS = 30
ACK_RETRY_DELAY = 0.090  # 25 ms after a transient failure, never a per-request timeout.
ACK_RECEIPT_ATTEMPTS = 3
ACK_RECEIPT_SECONDS = 5
ACK_RECEIPT_DELAY = 0.25


def transient_discord_error(exc):
    if isinstance(exc, discord.HTTPException):
        return exc.status >= 500
    if isinstance(exc, (aiohttp.ClientSSLError, aiohttp.InvalidURL)):
        return False
    return isinstance(exc, (OSError, TimeoutError, aiohttp.ClientConnectionError, aiohttp.ClientPayloadError))


COMMAND = {
    "name": "prompt",
    "description": "Ask this assistant a question or request a task (owner only).",
    "type": 1,
    "integration_types": [0, 1],
    "contexts": [0, 1, 2],
    "options": [
        {
            "type": 3,
            "name": "text",
            "description": "Your prompt; this does not read the surrounding channel history.",
            "required": True,
            "min_length": 1,
            "max_length": MAX_PROMPT_CHARS,
        },
        {
            "type": 5,
            "name": "private",
            "description": "Only you see the response when true. Default false: posts it in this channel.",
            "required": False,
        },
    ],
}


def register(registry):
    from .plugins import PluginSpec

    async def ingress_only(*args):
        raise ControlError("Slash commands are invoked by the owner through Discord, never by a model tool")

    registry.register(
        PluginSpec(
            PLUGIN_ID,
            "Slash command assistant",
            "Owner-only /prompt through this bot's Discord application. Supports user and server installs. "
            "Keeps ordinary assigned-room chat unchanged; each prompt starts a fresh conversation, with granted "
            "private/global notes and tools. Public responses by default; set private:true for only you. "
            "Maximum 14 minutes per invocation. "
            "Council bots only; Hortator's control-channel scope is unchanged.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            ingress_only,
            {},
            model_tool=False,
        )
    )


def granted(store, bot):
    config = store.get("plugins", PLUGIN_ID) or {}
    return bool(
        bot
        and bot.get("role") == "council"
        and PLUGIN_ID in bot.get("enabled_plugins", [])
        and config.get("enabled", False)
    )


def parse_options(data):
    """Bounded validation even when a stale Discord command schema is cached."""
    options = data.get("options", [])
    if not isinstance(options, list) or len(options) > 2:
        raise ControlError("Usage: /prompt text:<1–6,000 characters> private:<true|false, optional>")
    values = {}
    errors = []
    for item in options:
        if not isinstance(item, dict):
            errors.append("Each option must be an object")
            continue
        name = item.get("name")
        if name not in ("text", "private"):
            errors.append("Only text and private options are supported")
        elif name in values:
            errors.append(f"Duplicate {name} option")
        else:
            values[name] = item.get("value")
    if not isinstance(values.get("text"), str) or not 1 <= len(values["text"].strip()) <= MAX_PROMPT_CHARS:
        errors.append("text must contain 1–6,000 characters")
    if "private" in values and type(values["private"]) is not bool:
        errors.append("private must be a boolean, true or false")
    if errors:
        raise ControlError(
            "; ".join(errors) + ". Usage: /prompt text:<prompt> private:<true|false, optional>"
        )
    return values["text"].strip(), values.get("private", False)


def matches_definition(actual, expected):
    """Compare owned fields, including Discord's omitted optional-option default."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        for key, value in expected.items():
            # Discord may omit required=false; required=true must remain explicit.
            if key == "required" and value is False and key not in actual:
                continue
            if key not in actual or not matches_definition(actual[key], value):
                return False
        return True
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(matches_definition(left, right) for left, right in zip(actual, expected))
        )
    return type(actual) is type(expected) and actual == expected


class SlashContexts(ContextBuilder):
    def visible_rows(self, bot, rows):
        # Each invocation supplies one fresh synthetic message, with no shared
        # transcript sequence. Owner reset cancels the previous invocation.
        return rows

    def __init__(self, store, pool, invocation, global_memory, application_emojis=None):
        super().__init__(store, pool, global_memory, application_emojis)
        self.invocation = invocation

    def layers(self, bot, channel_id):
        item = self.prompt(bot, "slash_invocation", {"invocation": dumps(self.invocation)})
        return super().layers(bot, channel_id) + ([item] if item else [])

    async def prepare(self, bot, profile, channel_id, turn_id, tools, force=False):
        # No messages are ingested into the ordinary transcript. In particular,
        # an interaction ID is not a replyable Discord message ID.
        prompt = self.store.one("SELECT prompt FROM slash_invocations WHERE turn_id=?", (turn_id,))["prompt"]
        rows = [
            {
                "discord_id": "interaction:" + self.invocation["interaction_id"],
                "seq": 0,
                "at": time.time(),
                "author_id": OWNER_ID,
                "author_name": "The Boss",
                "bot_id": None,
                "reply_to": None,
                "deleted": False,
                "content": prompt,
                "attachments": [],
                "channel_id": channel_id,
                "addressing": {
                    "author_kind": "human",
                    "live": False,
                    "targets": [
                        {
                            "user_id": bot["application_id"],
                            "bot_id": bot["id"],
                            "name": bot["name"],
                            "via": ["slash"],
                        }
                    ],
                },
            }
        ]
        self.store.context(bot["id"], channel_id)
        image_plan = select_images([], 0, profile, allow_images=bot.get("allow_images", True))
        _, meta = await self.assemble(bot, profile, channel_id, rows, "", tools=tools, image_plan=image_plan)
        meta.update(checkpoint=0, calibration_factor=1)
        if meta["estimated_tokens"] + profile["response_tokens"] >= profile["context_window"]:
            raise ControlError(
                "Slash prompt and granted notes/tools exceed the model context budget; shorten the prompt or notes"
            )
        return rows, "", 0, meta


class SlashEngine(Engine):
    """The standard reasoning/tool loop with an interaction-specific boundary."""

    def __init__(self, manager, interaction, invocation):
        main = manager.service.engine
        super().__init__(main.store, main.vault, main.pool, main.registry)
        self.manager, self.interaction, self.invocation = manager, interaction, invocation
        self.contexts = SlashContexts(
            self.store, self.pool, invocation, main.registry.global_memory, main.registry.application_emojis
        )
        self.scope = f"slash:{invocation['bot_id']}:{invocation['channel_id']}"

    def channel_allowed(self, bot, channel_id):
        current = self.store.get("bots", bot["id"])
        return channel_id == self.scope and granted(self.store, current)

    def attention(self, *args, **kwargs):
        return []

    def claim_attention(self, *args, **kwargs):
        return None

    async def deliver(
        self, bot, profile, provider, context, content, reply_to, artifact_ids, *, request_id=None, **kwargs
    ):
        if not self.valid(bot, profile, provider) or not self.channel_allowed(bot, context.channel_id):
            raise ControlError("Slash permission or configuration changed; answer withheld")
        content = self.vault.redact(strip_reasoning(content))
        if not content or len(content) > 12000:
            raise ControlError("Slash answer must contain 1–12,000 visible characters")
        if reply_to:
            raise ControlError(
                "Slash answers use their interaction response; Discord message reply targets are unavailable"
            )
        paths = [self.registry.resolve_artifact(value, context) for value in artifact_ids]
        request = (
            self.store.one(
                "SELECT model,input_tokens,output_tokens,ttft_ms,duration_ms,json_extract(response,'$.footer_tokens') AS footer_tokens FROM requests WHERE id=? AND bot_id=? AND turn_id=? AND status='completed'",
                (request_id, bot["id"], context.turn_id),
            )
            if request_id
            else None
        )
        footer = render_footer(bot, profile, provider, request, redact=self.vault.redact)
        text, attached = model_message(content, footer)
        outbox_id = uid("out_")
        self.store.execute(
            "INSERT INTO outbox(id,turn_id,bot_id,channel_id,content,artifacts,status,created_at,routing) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                outbox_id,
                context.turn_id,
                bot["id"],
                self.invocation["channel_id"],
                content,
                dumps(artifact_ids),
                "sending",
                time.time(),
                dumps(
                    {
                        "kind": "slash",
                        "interaction_id": str(self.interaction.id),
                        "private": self.invocation["private"],
                    }
                ),
            ),
        )
        self.store.emit(
            "delivery.queued",
            {
                "outbox_id": outbox_id,
                "content": content,
                "footer": footer,
                "routing": "slash",
                "channel_id": self.invocation["channel_id"],
            },
            bot_id=bot["id"],
            turn_id=context.turn_id,
        )
        files = []
        # Only delivery of the finished answer/files, inside the overall slash deadline.
        timer = asyncio.timeout(30)
        try:
            files = [discord.File(path) for path in paths]
            if attached:
                files.append(discord.File(io.BytesIO(content.encode()), filename="full-response.txt"))
            async with timer:
                sent = await self.interaction.edit_original_response(
                    content=text,
                    attachments=files,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            self.store.execute(
                "UPDATE outbox SET status='sent',discord_id=?,sent_at=? WHERE id=?",
                (str(sent.id), time.time(), outbox_id),
            )
            self.store.execute(
                "UPDATE slash_invocations SET response_id=? WHERE interaction_id=?",
                (str(sent.id), str(self.interaction.id)),
            )
            self.store.emit(
                "delivery.sent",
                {"outbox_id": outbox_id, "discord_id": str(sent.id), "routing": "slash"},
                bot_id=bot["id"],
                turn_id=context.turn_id,
            )
        except (Exception, asyncio.CancelledError) as exc:
            uncertain = not isinstance(exc, discord.HTTPException) or exc.status >= 500
            error = self.manager.slash.safe_error(exc, self.interaction)
            self.store.execute(
                "UPDATE outbox SET status=?,error=? WHERE id=?",
                ("unknown" if uncertain else "failed", error, outbox_id),
            )
            self.store.emit(
                "delivery.unknown" if uncertain else "delivery.failed",
                {
                    "outbox_id": outbox_id,
                    "channel_id": self.invocation["channel_id"],
                    "interaction_id": str(self.interaction.id),
                    "operation": "edit slash answer",
                    "phase": "edit_original_response",
                    **self.manager.slash.error_fields(exc, self.interaction, local_timeout=timer.expired()),
                    **({"timeout_seconds": 30} if timer.expired() else {}),
                    "reason": "Interaction acceptance is unknown; no automatic resend"
                    if uncertain
                    else "Interaction response was rejected",
                },
                bot_id=bot["id"],
                turn_id=context.turn_id,
                level="error",
            )
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise DeliveryError(error, uncertain=uncertain) from None
        finally:
            for file in files:
                file.close()


class DiscordSlash:
    def __init__(self, manager):
        self.manager = manager
        self.store = manager.store
        self.next_sync = {}
        self.syncing = set()
        self.store.execute("""CREATE TABLE IF NOT EXISTS slash_commands (
            bot_id TEXT PRIMARY KEY, application_id TEXT NOT NULL, command_id TEXT NOT NULL,
            definition TEXT NOT NULL, updated_at REAL NOT NULL)""")
        self.store.execute("""CREATE TABLE IF NOT EXISTS slash_invocations (
            interaction_id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, application_id TEXT NOT NULL,
            channel_id TEXT NOT NULL, guild_id TEXT, actor_id TEXT NOT NULL, prompt TEXT NOT NULL,
            private INTEGER NOT NULL, status TEXT NOT NULL, turn_id TEXT, response_id TEXT,
            error TEXT, started_at REAL NOT NULL, ended_at REAL)""")
        self.store.execute(
            "UPDATE slash_invocations SET status='interrupted',error='Runtime stopped; interaction tokens are not persisted and work will not be replayed',ended_at=? WHERE status IN ('claimed','running')",
            (time.time(),),
        )

    def safe_error(self, exc, interaction=None):
        text = error_text(exc)
        token = getattr(interaction, "token", None)
        if token:
            text = text.replace(token, "[REDACTED interaction token]")
        return self.manager.vault.redact(text)[:3000]

    def error_fields(self, exc, interaction, *, local_timeout=False):
        """Only selected exception facts: never serialize a token-bearing request/response."""
        result = {
            "error": self.safe_error(exc, interaction),
            "error_origin": "local_deadline"
            if local_timeout
            else "discord_http"
            if isinstance(exc, discord.HTTPException)
            else "transport"
            if transient_discord_error(exc)
            else "local_client",
        }
        if isinstance(exc, discord.HTTPException):
            result.update(http_status=exc.status, discord_code=exc.code)
        causes, seen = [], set()
        cause = exc
        while cause is not None and id(cause) not in seen and len(causes) < 5:
            seen.add(id(cause))
            causes.append(self.safe_error(cause, interaction))
            if isinstance(cause, OSError) and cause.errno is not None:
                result["errno"] = cause.errno
            cause = cause.__cause__ or cause.__context__
        result["causes"] = causes
        return result

    def ack_event(self, bot_id, interaction, kind, *, level="info", **data):
        state = self.store.runtime(bot_id)
        self.store.emit(
            "discord.slash_" + kind,
            {
                "operation": "acknowledge /prompt",
                "interaction_id": str(interaction.id),
                "channel_id": str(interaction.channel_id),
                "application_id": str(interaction.application_id),
                "interaction_age_ms": max(0, time.time() - interaction.created_at.timestamp()) * 1000,
                "gateway_status": state["gateway_status"],
                "heartbeat_ms": state.get("heartbeat_ms"),
                **data,
            },
            bot_id=bot_id,
            level=level,
        )

    async def acknowledge(self, bot_id, interaction, private):
        # Never give a new attempt a fresh three-second window. Use monotonic
        # time once the interaction's remaining lifetime has been calculated.
        loop = asyncio.get_running_loop()
        began = loop.time()
        remaining = ACK_SECONDS - max(0, time.time() - interaction.created_at.timestamp())
        deadline = began + max(0, remaining)
        attempt, fields, uncertain = 0, {}, False
        self.ack_event(
            bot_id, interaction, "received", phase="acknowledge", remaining_ms=max(0, remaining) * 1000
        )
        try:
            for candidate in range(1, ACK_ATTEMPTS + 1):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                attempt = candidate
                timer = asyncio.timeout_at(deadline)
                try:
                    async with timer:
                        await interaction.response.defer(thinking=True, ephemeral=private)
                    self.ack_event(
                        bot_id,
                        interaction,
                        "acknowledged",
                        phase="acknowledge",
                        attempt=attempt,
                        duration_ms=(loop.time() - began) * 1000,
                    )
                    return True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    fields = self.error_fields(exc, interaction, local_timeout=timer.expired())
                    retryable = transient_discord_error(exc)
                    uncertain = (
                        retryable
                        or isinstance(exc, discord.InteractionResponded)
                        or (isinstance(exc, discord.HTTPException) and exc.code == 40060)
                    )
                    if timer.expired():
                        fields["timeout_seconds"] = ACK_SECONDS
                        fields["error"] = (
                            f"Local Discord acknowledgement wait expired after {ACK_SECONDS} seconds from interaction creation; remote acceptance is unknown"
                        )
                    if not retryable or attempt == ACK_ATTEMPTS or deadline - loop.time() <= ACK_RETRY_DELAY:
                        break
                    self.ack_event(
                        bot_id,
                        interaction,
                        "ack_retry",
                        level="warning",
                        phase="acknowledge",
                        attempt=attempt,
                        max_attempts=ACK_ATTEMPTS,
                        next_attempt=attempt + 1,
                        retry_in_seconds=ACK_RETRY_DELAY,
                        remaining_ms=max(0, deadline - loop.time()) * 1000,
                        **fields,
                        reason="Retrying only this interaction acknowledgement inside Discord's original three-second window; no model work has started",
                    )
                    await asyncio.sleep(ACK_RETRY_DELAY)
            if uncertain:
                self.ack_event(
                    bot_id,
                    interaction,
                    "ack_checking",
                    level="warning",
                    phase="read_ack_receipt",
                    attempt=attempt,
                    **fields,
                    reason="Checking the original response to determine whether Discord accepted the acknowledgement; not sending a new message",
                )
                if await self.recover_ack(bot_id, interaction, private):
                    return True
            if not fields:
                fields = {
                    "error_origin": "platform_deadline",
                    "error": "Discord's initial three-second response window elapsed before acknowledgement could start",
                    "timeout_seconds": ACK_SECONDS,
                }
            message = (
                "Discord acknowledgement could not be confirmed; no model or tool work was started. "
                "Invoke /prompt again. "
                + fields.get(
                    "error",
                    "Discord's initial three-second response window elapsed before acknowledgement could start",
                )
            )
            self.finish(str(interaction.id), "failed", message)
            self.ack_event(
                bot_id,
                interaction,
                "ack_failed",
                level="error",
                phase="acknowledge",
                attempt=attempt,
                max_attempts=ACK_ATTEMPTS,
                duration_ms=(loop.time() - began) * 1000,
                **fields,
                reason=message,
            )
            return False
        except asyncio.CancelledError:
            self.finish(str(interaction.id), "cancelled", "Cancelled while acknowledging Discord")
            self.ack_event(
                bot_id,
                interaction,
                "ack_cancelled",
                level="warning",
                phase="acknowledge",
                reason="Owner/runtime cancelled acknowledgement or receipt recovery; no model work started",
            )
            raise

    async def recover_ack(self, bot_id, interaction, private):
        for attempt in range(1, ACK_RECEIPT_ATTEMPTS + 1):
            remaining = MAX_SECONDS - max(0, time.time() - interaction.created_at.timestamp())
            if remaining <= 0:
                return False
            seconds = min(ACK_RECEIPT_SECONDS, remaining)
            timer = asyncio.timeout(seconds)
            try:
                async with timer:
                    response = await interaction.original_response()
                # A GET on this token-specific endpoint proves acceptance, but
                # only a deferred placeholder of the expected visibility is ours
                # to finish. Never overwrite an already-completed response.
                flags = response.flags
                app = getattr(response, "application_id", None)
                metadata = getattr(response, "interaction_metadata", None)
                if (
                    not flags.loading
                    or flags.ephemeral != private
                    or app is not None
                    and str(app) != str(interaction.application_id)
                    or metadata is not None
                    and str(metadata.id) != str(interaction.id)
                ):
                    self.ack_event(
                        bot_id,
                        interaction,
                        "ack_receipt_mismatch",
                        level="error",
                        phase="read_ack_receipt",
                        response_id=str(response.id),
                        reason="Original response is not the expected deferred placeholder/visibility; no generation or overwrite",
                    )
                    return False
                self.store.execute(
                    "UPDATE slash_invocations SET response_id=? WHERE interaction_id=?",
                    (str(response.id), str(interaction.id)),
                )
                self.ack_event(
                    bot_id,
                    interaction,
                    "ack_recovered",
                    phase="read_ack_receipt",
                    attempt=attempt,
                    response_id=str(response.id),
                    reason="Discord confirmed the existing deferred response; continuing this invocation once",
                )
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                retryable = transient_discord_error(exc) or isinstance(exc, discord.NotFound)
                retry = retryable and attempt < ACK_RECEIPT_ATTEMPTS
                fields = self.error_fields(exc, interaction, local_timeout=timer.expired())
                if timer.expired():
                    fields.update(
                        timeout_seconds=seconds,
                        error=f"Local Discord acknowledgement receipt lookup deadline exceeded: {seconds:g} seconds",
                    )
                self.ack_event(
                    bot_id,
                    interaction,
                    "ack_receipt_retry" if retry else "ack_receipt_failed",
                    level="warning",
                    phase="read_ack_receipt",
                    attempt=attempt,
                    max_attempts=ACK_RECEIPT_ATTEMPTS,
                    next_attempt=attempt + 1 if retry else None,
                    retry_in_seconds=ACK_RECEIPT_DELAY if retry else None,
                    **fields,
                    reason="Retrying read-only acknowledgement lookup"
                    if retry
                    else "Could not confirm Discord accepted the acknowledgement",
                )
                if not retry:
                    return False
                await asyncio.sleep(ACK_RECEIPT_DELAY)
        return False

    def schedule_sync(self, client):
        bot_id = client.bot_id
        if client.stopping or self.manager.closed or not client.is_ready() or bot_id in self.syncing:
            return
        bot = self.store.get("bots", bot_id)
        config = self.store.get("plugins", PLUGIN_ID) or {}
        state = (bot.get("revision") if bot else None, config.get("revision"))
        previous, next_at = self.next_sync.get(bot_id, (None, 0))
        if state == previous and time.monotonic() < next_at:
            return
        self.next_sync[bot_id] = state, time.monotonic() + 60
        self.syncing.add(bot_id)
        task = self.manager.background.spawn(
            self.sync(client), name=f"slash-registration:{bot_id}", bot_id=bot_id
        )
        task.add_done_callback(lambda _: self.syncing.discard(bot_id))

    async def sync(self, client):
        bot_id = client.bot_id
        bot = self.store.get("bots", bot_id)
        owned = self.store.one("SELECT * FROM slash_commands WHERE bot_id=?", (bot_id,))
        enabled = granted(self.store, bot)
        if not enabled and not owned:
            return  # Disabled installations perform no command API operations.
        if not bot or str(client.application_id) != bot["application_id"]:
            return
        application_id = int(bot["application_id"])
        if owned and owned["application_id"] != str(application_id):
            self.store.emit(
                "discord.slash_registration_failed",
                {
                    "error": "Application identity changed; previous command ownership retained for operator review"
                },
                bot_id=bot_id,
                level="warning",
            )
            return
        try:
            if not enabled:
                try:
                    await client.http.delete_global_command(application_id, int(owned["command_id"]))
                except discord.NotFound:
                    pass
                self.store.execute("DELETE FROM slash_commands WHERE bot_id=?", (bot_id,))
                self.store.emit("discord.slash_unregistered", {"command": "/prompt"}, bot_id=bot_id)
                return
            commands = await client.http.get_global_commands(application_id)
            collision = next((c for c in commands if c["name"] == "prompt" and c.get("type", 1) == 1), None)
            if collision and (not owned or str(collision["id"]) != owned["command_id"]):
                raise ControlError(
                    "This application already has an unowned /prompt command; choose a separate Discord application or remove that command yourself before enabling this plugin"
                )
            if (
                collision
                and owned
                and owned["definition"] == dumps(COMMAND)
                and matches_definition(collision, COMMAND)
            ):
                return
            if not granted(self.store, self.store.get("bots", bot_id)):
                return
            remote = await client.http.upsert_global_command(application_id, payload=COMMAND)
            self.store.execute(
                "INSERT INTO slash_commands VALUES(?,?,?,?,?) ON CONFLICT(bot_id) DO UPDATE SET command_id=excluded.command_id,definition=excluded.definition,updated_at=excluded.updated_at",
                (bot_id, str(application_id), str(remote["id"]), dumps(COMMAND), time.time()),
            )
            self.store.emit(
                "discord.slash_registered",
                {
                    "command": "/prompt",
                    "application_id": str(application_id),
                    "command_id": str(remote["id"]),
                    "contexts": COMMAND["contexts"],
                    "integration_types": COMMAND["integration_types"],
                },
                bot_id=bot_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.store.emit(
                "discord.slash_registration_failed",
                {
                    "error": self.safe_error(exc),
                    "command": "/prompt",
                    "reason": "Ordinary Discord chat is unaffected; check User Install/Guild Install portal settings. Retry in 60 seconds.",
                },
                bot_id=bot_id,
                level="warning",
            )

    async def notice(self, interaction, message, *, deferred=False, trace=None):
        suffix = f"\nTrace: {trace}" if trace else ""
        if len((message + suffix).encode("utf-16-le")) > 4000:
            suffix = "\nFull diagnostics are available in Trajectory." + suffix
            message = markdown_preview(message, 2000 - len(suffix.encode("utf-16-le")) // 2 - 4)
        message += suffix
        timer = asyncio.timeout(10)
        try:
            async with timer:
                if deferred:
                    await interaction.edit_original_response(
                        content=message, allowed_mentions=discord.AllowedMentions.none()
                    )
                else:
                    await interaction.response.send_message(
                        message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            claim = self.store.one(
                "SELECT bot_id FROM slash_invocations WHERE interaction_id=?", (str(interaction.id),)
            )
            self.store.emit(
                "discord.slash_notice_failed",
                {
                    "interaction_id": str(interaction.id),
                    "channel_id": str(interaction.channel_id),
                    "operation": "deliver slash status notice",
                    "phase": "edit_original_response" if deferred else "initial_response",
                    **self.error_fields(exc, interaction, local_timeout=timer.expired()),
                    **({"timeout_seconds": 10} if timer.expired() else {}),
                    "reason": "Interaction notice could not be delivered; no automatic resend",
                },
                bot_id=claim["bot_id"] if claim else None,
                turn_id=trace,
                level="warning",
            )

    async def receive(self, bot_id, interaction):
        if interaction.type != discord.InteractionType.application_command or not isinstance(
            interaction.data, dict
        ):
            return
        data = interaction.data
        if data.get("name") != "prompt" or data.get("type", 1) != 1:
            return
        if getattr(self.manager.service, "snapshot_maintenance", False) or self.manager.closed:
            await self.notice(
                interaction, "Snapshot maintenance is in progress; retry when the council resumes."
            )
            return
        bot = self.store.get("bots", bot_id)
        # Identity comes from Discord's authenticated interaction, never a prompt,
        # display name, selected room, or application installer's display metadata.
        if str(interaction.user.id) != OWNER_ID or interaction.user.bot:
            await self.notice(
                interaction, "This assistant command is available only to its configured owner."
            )
            return
        if not granted(self.store, bot) or str(interaction.application_id) != bot["application_id"]:
            await self.notice(interaction, "Slash commands are disabled for this bot.")
            return
        if not interaction.channel_id:
            await self.notice(interaction, "Discord did not supply a channel identity for this invocation.")
            return
        owned = self.store.one("SELECT command_id FROM slash_commands WHERE bot_id=?", (bot_id,))
        if not owned or str(data.get("id")) != owned["command_id"]:
            await self.notice(
                interaction,
                "This command registration is stale or not managed by this plugin; refresh the command and try again.",
            )
            return
        try:
            prompt, private = parse_options(data)
        except ControlError as exc:
            await self.notice(interaction, str(exc))
            return
        invocation_id = str(interaction.id)
        if self.store.one("SELECT 1 FROM slash_invocations WHERE interaction_id=?", (invocation_id,)):
            return  # Never replay model work or respond twice to one interaction.
        self.store.execute(
            "INSERT INTO slash_invocations(interaction_id,bot_id,application_id,channel_id,guild_id,actor_id,prompt,private,status,started_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                invocation_id,
                bot_id,
                bot["application_id"],
                str(interaction.channel_id),
                str(interaction.guild_id) if interaction.guild_id else None,
                OWNER_ID,
                self.manager.vault.redact(prompt),
                int(private),
                "claimed",
                time.time(),
            ),
        )
        # Acknowledge first. Do not wait for a provider slot or model preparation.
        if not await self.acknowledge(bot_id, interaction, private):
            return
        main = self.manager.service.engine
        bot = self.store.get("bots", bot_id)
        settings = self.store.get("settings", "global")
        profile, provider = main.configuration(bot) if bot else (None, None)
        boundary = self.store.context_boundary(bot_id, f"slash:{bot_id}:{interaction.channel_id}")
        reason = (
            "This slash request predates the owner's clean-slate cutoff; send a new /prompt"
            if boundary and interaction.created_at.timestamp() <= boundary["after_at"]
            else "Snapshot maintenance or runtime shutdown is in progress; retry after resume"
            if getattr(self.manager.service, "snapshot_maintenance", False)
            or main.closed
            or self.manager.background.closing
            else "Slash commands were disabled while acknowledging this request"
            if not granted(self.store, bot)
            else "Council, bot or provider is paused"
            if not main.available(bot)
            else "This bot is already busy; retry after its current turn finishes"
            if bot_id in main.tasks
            else "The council is at its concurrent-turn limit; retry when a slot is free"
            if len(main.tasks) >= settings["max_concurrent_turns"]
            else "The provider circuit is temporarily open; retry after its cooldown"
            if self.store.health(provider["id"])["circuit_until"] > time.time()
            else "This bot is waiting after a provider failure; retry after its configured backoff"
            if self.store.runtime(bot_id)["retry_until"] > time.time()
            else main.budget_error(bot)
        )
        if reason:
            self.finish(invocation_id, "rejected", reason)
            await self.notice(interaction, reason, deferred=True)
            return
        invocation = {
            "kind": "slash",
            "interaction_id": invocation_id,
            "bot_id": bot_id,
            "channel_id": str(interaction.channel_id),
            "guild_id": str(interaction.guild_id) if interaction.guild_id else None,
            "private": private,
        }
        bot = {**bot, "invocation": invocation}
        turn_id = uid("turn_")
        scope = f"slash:{bot_id}:{interaction.channel_id}"
        self.store.execute(
            "INSERT INTO turns(id,bot_id,channel_id,profile_id,provider_id,model,started_at,status,trigger,revision) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                turn_id,
                bot_id,
                scope,
                profile["id"],
                provider["id"],
                profile["model"],
                time.time(),
                "running",
                "slash_prompt",
                bot["revision"],
            ),
        )
        self.store.execute(
            "UPDATE slash_invocations SET status='running',turn_id=? WHERE interaction_id=?",
            (turn_id, invocation_id),
        )
        self.store.emit(
            "turn.started",
            {
                "channel_id": scope,
                "source_channel_id": str(interaction.channel_id),
                "trigger": "slash_prompt",
                "interaction_id": invocation_id,
                "profile_id": profile["id"],
                "provider_id": provider["id"],
                "model": profile["model"],
                "reasoning": reasoning_settings(profile["request_json"], self.manager.vault.redact),
                "profile_revision": profile["revision"],
            },
            bot_id=bot_id,
            turn_id=turn_id,
        )
        task = self.manager.background.spawn(
            self.execute(interaction, invocation, bot, profile, provider, turn_id),
            name=f"slash:{bot_id}:{turn_id}",
            bot_id=bot_id,
            turn_id=turn_id,
        )
        main.tasks[bot_id] = task

        def finished(done):
            if main.tasks.get(bot_id) is done:
                main.tasks.pop(bot_id, None)
            if done.cancelled():
                reason = "Stopped by operator, configuration change, or shutdown"
                self.store.execute(
                    "UPDATE turns SET status='cancelled',error=?,ended_at=? WHERE id=? AND status='running'",
                    (reason, time.time(), turn_id),
                )
                self.store.execute(
                    "UPDATE slash_invocations SET status='cancelled',error=?,ended_at=? WHERE interaction_id=? AND status='running'",
                    (reason, time.time(), invocation_id),
                )

        task.add_done_callback(finished)

    def finish(self, invocation_id, status, error=None):
        self.store.execute(
            "UPDATE slash_invocations SET status=?,error=?,ended_at=? WHERE interaction_id=?",
            (status, error, time.time(), invocation_id),
        )

    async def execute(self, interaction, invocation, bot, profile, provider, turn_id):
        runner = SlashEngine(self.manager, interaction, invocation)
        error = None
        try:
            # Reserve a full minute before Discord's 15-minute token expiry,
            # including time already spent receiving/acknowledging the command.
            elapsed = max(0, time.time() - interaction.created_at.timestamp())
            async with asyncio.timeout(max(0, MAX_SECONDS - elapsed)):
                await runner.run(bot, profile, provider, runner.scope, turn_id, False)
        except TimeoutError:
            error = f"Local slash invocation deadline exceeded: {MAX_SECONDS} seconds. Work stopped; saved files and notes remain. Nothing will automatically resume."
            self.store.execute(
                "UPDATE turns SET status='failed',error=?,ended_at=? WHERE id=?",
                (error, time.time(), turn_id),
            )
            self.store.emit(
                "discord.slash_deadline",
                {"error": error, "interaction_id": invocation["interaction_id"], "seconds": MAX_SECONDS},
                bot_id=bot["id"],
                turn_id=turn_id,
                level="warning",
            )
        except asyncio.CancelledError:
            self.finish(
                invocation["interaction_id"],
                "cancelled",
                "Stopped by operator, configuration change, or shutdown",
            )
            # Operator cancellation gets a receipt; shutdown/maintenance must
            # not hold database quiescence waiting on Discord.
            if (
                not self.manager.closed
                and not self.manager.background.closing
                and not getattr(self.manager.service, "snapshot_maintenance", False)
                and not self.store.one(
                    "SELECT 1 FROM outbox WHERE turn_id=? AND status='unknown'", (turn_id,)
                )
            ):
                await self.notice(interaction, "Request cancelled.", deferred=True, trace=turn_id)
            raise
        except Exception as exc:
            error = self.safe_error(exc, interaction)
            self.store.execute(
                "UPDATE turns SET status='failed',error=?,ended_at=? WHERE id=?",
                (error, time.time(), turn_id),
            )
            self.store.emit(
                "discord.slash_failed",
                {"error": error, "interaction_id": invocation["interaction_id"]},
                bot_id=bot["id"],
                turn_id=turn_id,
                level="error",
            )
        turn = self.store.one("SELECT status,error,decision FROM turns WHERE id=?", (turn_id,))
        self.finish(invocation["interaction_id"], turn["status"], error or turn["error"])
        uncertain = self.store.one("SELECT 1 FROM outbox WHERE turn_id=? AND status='unknown'", (turn_id,))
        if uncertain:
            return
        if turn["status"] == "silent":
            await self.notice(
                interaction,
                "This bot chose to remain silent. Its provider/tool trace is available in the dashboard.",
                deferred=True,
            )
        elif turn["status"] != "sent":
            await self.notice(
                interaction,
                "Request stopped. " + (error or turn["error"] or "No answer was delivered"),
                deferred=True,
                trace=turn_id,
            )


def public_status(store, bot):
    """Owner dashboard metadata; application IDs and install URLs are public data."""
    config = store.get("plugins", PLUGIN_ID) or {}
    installed = store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='slash_commands'")
    owned = (
        store.one("SELECT application_id,command_id FROM slash_commands WHERE bot_id=?", (bot["id"],))
        if installed
        else None
    )
    application_id = bot.get("application_id", "")
    return {
        "eligible": bot.get("role") == "council",
        "global_enabled": bool(config.get("enabled")),
        "granted": PLUGIN_ID in bot.get("enabled_plugins", []),
        "enabled": granted(store, bot),
        "registered": bool(owned and owned["application_id"] == application_id),
        "command_id": owned["command_id"] if owned and owned["application_id"] == application_id else None,
        "user_install_url": (
            "https://discord.com/oauth2/authorize?client_id="
            + application_id
            + "&scope=applications.commands&integration_type=1"
            if isinstance(application_id, str) and application_id.isdecimal()
            else None
        ),
    }
