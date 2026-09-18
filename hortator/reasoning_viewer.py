"""Opt-in owner-only Discord access to recorded provider reasoning.

This is presentation, not a model tool. Public controls carry opaque handles;
reasoning is read only after authenticating a click and never enters transcripts.
"""

import asyncio
import io
import json
import re
import time

import discord

from .discord_text import preview
from .footer_tokens import reasoning_text
from .models import ControlError, OWNER_ID
from .store import dumps, uid
from .timekeeping import council_timezone, local_timestamp


PLUGIN_ID = "reasoning_viewer"
PREFIX = "hrv:"
DESCRIPTION = (
    "Automatically add a REASONING button when an answer's generation requests contain readable "
    "provider reasoning. Only the configured human owner can read it, privately, with separate "
    "rounds, pages and a text download. Reads saved diagnostics without model calls or tool rounds. "
    "Disabling immediately revokes access through existing buttons; stored diagnostics are retained."
)


def granted(store, bot):
    return bool(
        bot
        and PLUGIN_ID in bot.get("enabled_plugins", [])
        and (store.get("plugins", PLUGIN_ID) or {}).get("enabled")
    )


def register(registry):
    from .plugins import PluginSpec

    async def presentation_only(*args):
        raise ControlError(
            "Reasoning is inspected by the owner through a saved Discord button, not a model tool"
        )

    registry.reasoning_viewer = ReasoningViewer(registry.store, registry.vault)
    registry.register(
        PluginSpec(
            PLUGIN_ID,
            "Reasoning viewer",
            DESCRIPTION,
            {"type": "object", "properties": {}, "additionalProperties": False},
            presentation_only,
            {},
            model_tool=False,
        )
    )


def captured_text(raw):
    data = json.loads(raw) if raw else {}
    # Full native text wins over mirrored details/inline text. Encrypted payloads,
    # summaries, prior-request replay and token counts alone are not reasoning text.
    native = data.get("reasoning_content") or ""
    text = reasoning_text("", native if native.strip() else "", data.get("reasoning_details") or [])
    return (
        text
        if text.strip()
        else "\n".join(item.get("text") or "" for item in data.get("inline_reasoning", []))
    )


def has_capture(rows):
    return any(captured_text(row["diagnostics"]).strip() for row in rows)


def page_capture(raw, page, *, download=False, file_limit=0, secrets=()):
    text = captured_text(raw)
    for value in sorted(secrets, key=len, reverse=True):
        if len(value) >= 6:
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", text)
    if download:
        if not text.strip():
            raise ControlError("No readable reasoning was captured for this request")
        content = text.encode("utf-8")
        if len(content) > file_limit:
            raise ControlError(
                f"Reasoning download is {len(content)} bytes; Discord allows {file_limit}. "
                "Use the paged viewer or the authenticated dashboard; no text was discarded."
            )
        return content, 0
    if not text.strip():
        text = (
            "No readable reasoning was recorded for this request."
            if raw is None
            else "The provider returned no readable reasoning text for this request. "
            "A token count or encrypted reasoning cannot be displayed as text."
        )
    # Fixed UTF-16 boundaries make each page read linear in capture size rather
    # than repeatedly slicing/encoding the remaining trace. Never split an emoji
    # surrogate pair; both adjacent pages use the same adjusted boundary.
    wire = text.replace("```", "``\u200b`").encode("utf-16-le")
    width = 1480 * 2

    def boundary(offset):
        offset = min(offset, len(wire))
        if 0 < offset < len(wire) and 0xD800 <= int.from_bytes(wire[offset - 2 : offset], "little") <= 0xDBFF:
            offset += 2
        return offset

    count = (len(wire) + width - 1) // width
    if count > 1 and boundary((count - 1) * width) == len(wire):
        count -= 1
    if not 0 <= page < count:
        raise ControlError("That reasoning page does not exist")
    selected = wire[boundary(page * width) : boundary((page + 1) * width)].decode("utf-16-le")
    return f"```\n{selected}\n```", count


def button(binding):
    return discord.ui.Button(
        label="REASONING", style=discord.ButtonStyle.secondary, custom_id=f"{PREFIX}{binding['id']}:open"
    )


def answer_view(binding):
    view = discord.ui.View(timeout=None)
    view.add_item(button(binding))
    view.stop()  # The central gateway dispatcher owns callbacks, including after restart.
    return view


def reader_view(session_id, index, page, rounds, pages):
    view = discord.ui.View(timeout=None)
    for label, action, target, disabled, row in (
        ("Previous round", "page", (max(0, index - 1), 0), index == 0, 0),
        ("Next round", "page", (min(rounds - 1, index + 1), 0), index == rounds - 1, 0),
        ("Previous page", "page", (index, max(0, page - 1)), page == 0, 1),
        ("Next page", "page", (index, min(pages - 1, page + 1)), page == pages - 1, 1),
        ("Download text", "download", (index, 0), False, 1),
    ):
        view.add_item(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                custom_id=f"{PREFIX}{session_id}:{action}:{target[0]}:{target[1]}",
                disabled=disabled,
                row=row,
            )
        )
    view.stop()
    return view


class ReasoningViewer:
    def __init__(self, store, vault):
        self.store, self.vault = store, vault
        self.slots = asyncio.Semaphore(2)
        store.execute("""CREATE TABLE IF NOT EXISTS reasoning_bindings (
            id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, application_id TEXT NOT NULL,
            turn_id TEXT NOT NULL, final_request_id TEXT NOT NULL, request_ids TEXT NOT NULL,
            outbox_id TEXT NOT NULL UNIQUE, channel_id TEXT NOT NULL, message_id TEXT,
            created_at REAL NOT NULL)""")
        store.execute("""CREATE TABLE IF NOT EXISTS reasoning_views (
            id TEXT PRIMARY KEY, binding_id TEXT NOT NULL, interaction_id TEXT NOT NULL UNIQUE,
            message_id TEXT, created_at REAL NOT NULL)""")

    async def prepare(self, bot, turn_id, request_id, outbox_id, channel_id):
        if not request_id or not granted(self.store, self.store.get("bots", bot["id"])):
            return None
        rows = self.store.rows(
            """SELECT r.id,r.status,d.body AS diagnostics FROM requests r
            LEFT JOIN request_diagnostics d ON d.request_id=r.id
            WHERE r.bot_id=? AND r.turn_id=? AND r.purpose='generation' ORDER BY r.started_at,r.rowid""",
            (bot["id"], turn_id),
        )
        final = next(
            (i for i, row in enumerate(rows) if row["id"] == request_id and row["status"] == "completed"),
            None,
        )
        if final is None:
            return None
        rows = rows[: final + 1]
        async with self.slots:
            present = await asyncio.to_thread(has_capture, rows)
        current = self.store.get("bots", bot["id"])
        if (
            not present
            or not granted(self.store, current)
            or current["application_id"] != bot["application_id"]
        ):
            return None
        identity = uid("reason_")
        self.store.execute(
            "INSERT INTO reasoning_bindings VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                identity,
                bot["id"],
                bot["application_id"],
                turn_id,
                request_id,
                dumps([row["id"] for row in rows]),
                outbox_id,
                str(channel_id),
                None,
                time.time(),
            ),
        )
        return self.store.one("SELECT * FROM reasoning_bindings WHERE id=?", (identity,))

    def bind(self, binding, message_id):
        self.store.execute(
            "UPDATE reasoning_bindings SET message_id=? WHERE id=?", (str(message_id), binding["id"])
        )

    def resolve(self, bot_id, interaction):
        if str(interaction.user.id) != OWNER_ID or interaction.user.bot:
            raise ControlError("Reasoning is available only to the configured human owner")
        bot = self.store.get("bots", bot_id)
        if not granted(self.store, bot):
            raise ControlError("Reasoning viewer is disabled for this bot")
        if str(interaction.application_id) != bot["application_id"]:
            raise ControlError("This reasoning control belongs to another application")
        parts = str(interaction.data.get("custom_id", "")).split(":")
        if len(parts) not in (3, 5) or parts[0] != "hrv" or interaction.data.get("component_type") != 2:
            raise ControlError("Unknown reasoning control")
        session = None
        if len(parts) == 3 and parts[2] == "open":
            binding_id, index, page = parts[1], None, 0
        elif len(parts) == 5 and parts[2] in ("page", "download"):
            session = self.store.one("SELECT * FROM reasoning_views WHERE id=?", (parts[1],))
            if not session or not all(p.isascii() and p.isdecimal() and len(p) <= 9 for p in parts[3:]):
                raise ControlError("Unknown reasoning page")
            binding_id, index, page = session["binding_id"], int(parts[3]), int(parts[4])
        else:
            raise ControlError("Unknown reasoning action")
        binding = self.store.one(
            "SELECT * FROM reasoning_bindings WHERE id=? AND bot_id=?", (binding_id, bot_id)
        )
        if not binding or binding["application_id"] != bot["application_id"]:
            raise ControlError("Saved reasoning binding is unavailable for this bot/application")
        outbox = self.store.one(
            "SELECT * FROM outbox WHERE id=? AND bot_id=? AND turn_id=?",
            (binding["outbox_id"], bot_id, binding["turn_id"]),
        )
        message = getattr(interaction, "message", None)
        if (
            not outbox
            or outbox["status"] != "sent"
            or not binding["message_id"]
            or outbox["discord_id"] != binding["message_id"]
            or str(interaction.channel_id) != binding["channel_id"]
            or not message
            or str(message.id) != (session["message_id"] if session else binding["message_id"])
            or str(message.author.id) != (self.vault.get(f"bot/{bot_id}/user_id") or bot["application_id"])
            or session
            and not getattr(getattr(message, "flags", None), "ephemeral", False)
        ):
            raise ControlError("Control does not match a confirmed message, channel and private viewer")
        ids = json.loads(binding["request_ids"])
        index = ids.index(binding["final_request_id"]) if index is None else index
        if not 0 <= index < len(ids):
            raise ControlError("That reasoning round does not exist")
        request = self.store.one(
            """SELECT id,model,provider_id,started_at,status,reasoning_tokens,
            json_extract(response,'$.finish_reason') AS finish_reason,
            json_array_length(response,'$.tool_calls') AS tool_count,
            json_extract(response,'$.footer_tokens.reasoning') AS measured_reasoning
            FROM requests WHERE id=? AND bot_id=? AND turn_id=? AND purpose='generation'""",
            (ids[index], bot_id, binding["turn_id"]),
        )
        if not request:
            raise ControlError("The selected request is no longer available in saved diagnostics")
        return binding, session, request, index, page, len(ids), parts[2]

    def event(self, bot_id, interaction, kind, *, level="info", **data):
        self.store.emit(
            "discord.reasoning_" + kind,
            {
                "operation": "read saved reasoning",
                "interaction_id": str(interaction.id),
                "channel_id": str(interaction.channel_id),
                **data,
            },
            bot_id=bot_id,
            request_id=data.get("request_id"),
            level=level,
        )

    async def receive(self, manager, bot_id, interaction):
        deferred, files = False, []
        try:
            if manager.closed or getattr(manager.service, "snapshot_maintenance", False):
                raise ControlError("Snapshot maintenance or shutdown is in progress; retry after resume")
            binding, session, request, index, page, rounds, action = self.resolve(bot_id, interaction)
            if action == "open" and self.store.one(
                "SELECT 1 FROM reasoning_views WHERE interaction_id=?", (str(interaction.id),)
            ):
                return  # A duplicate gateway interaction must not create another viewer.
            if action == "open":
                session = {"id": uid("view_")}
                self.store.execute(
                    "INSERT INTO reasoning_views VALUES(?,?,?,?,?)",
                    (session["id"], binding["id"], str(interaction.id), None, time.time()),
                )
            # A quiet update acknowledges paging without creating a second card.
            # The shared acknowledgement recovery verifies that exact private message.
            existing = str(interaction.message.id) if action == "page" else None
            if not await manager.slash.acknowledge(bot_id, interaction, True, existing_message_id=existing):
                return
            deferred = True
            binding, _, request, index, page, rounds, action = self.resolve(bot_id, interaction)
            row = self.store.one("SELECT body FROM request_diagnostics WHERE request_id=?", (request["id"],))
            async with asyncio.timeout(30), self.slots:
                while True:
                    secrets = tuple(v for k, v in self.vault.values.items() if not k.endswith("/user_id"))
                    content, pages = await asyncio.to_thread(
                        page_capture,
                        row["body"] if row else None,
                        page,
                        download=action == "download",
                        file_limit=getattr(interaction, "filesize_limit", 10 * 1024 * 1024),
                        secrets=secrets,
                    )
                    if secrets == tuple(
                        v for k, v in self.vault.values.items() if not k.endswith("/user_id")
                    ):
                        break  # Also mask credentials installed while a worker was reading.
            self.resolve(bot_id, interaction)  # Revocation/identity recheck after worker I/O.
            if action == "download":
                files.append(discord.File(io.BytesIO(content), filename=f"reasoning-{request['id']}.txt"))
                text, view = (
                    f"Saved provider reasoning · {request['id']}\nCredentials masked; no model call was made.",
                    None,
                )
            else:
                phase = (
                    "Final answer"
                    if request["id"] == binding["final_request_id"]
                    else "Tool round"
                    if request["tool_count"]
                    else "Earlier request"
                )
                status = request["status"]
                count = request["reasoning_tokens"]
                measured = json.loads(request["measured_reasoning"] or "{}")
                if count is None and measured.get("value") is not None:
                    count = ("~" if measured.get("source") == "estimated" else "") + str(measured["value"])
                title = preview(self.vault.redact(f"{request['provider_id']} / {request['model']}"), 180)
                stamp = local_timestamp(
                    request["started_at"], council_timezone(self.store), timespec="seconds"
                )
                text = (
                    f"**REASONING · {phase} · Request {index + 1}/{rounds}**\n{title}\n"
                    f"{stamp} · reasoning tokens: {count if count is not None else 'none'}\n"
                    f"{'Partial capture · ' if status != 'completed' or request['finish_reason'] in ('length', 'content_filter') else ''}"
                    f"{status} · Page {page + 1}/{pages}\n{content}"
                )
                view = reader_view(session["id"], index, page, rounds, pages)
            async with asyncio.timeout(30):
                sent = await interaction.edit_original_response(
                    content=text,
                    view=view,
                    attachments=files,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            if action == "open":
                self.store.execute(
                    "UPDATE reasoning_views SET message_id=? WHERE id=?", (str(sent.id), session["id"])
                )
            self.event(
                bot_id,
                interaction,
                "viewed",
                request_id=request["id"],
                action=action,
                round=index + 1,
                page=page + 1,
            )
        except asyncio.CancelledError:
            raise
        except ControlError as exc:
            self.event(bot_id, interaction, "rejected", level="warning", reason=str(exc))
            await manager.slash.notice(interaction, str(exc), deferred=deferred)
        except Exception as exc:
            self.event(
                bot_id,
                interaction,
                "failed",
                level="warning",
                **manager.slash.error_fields(exc, interaction),
                reason="Saved reasoning could not be displayed; no inference was started or response replayed",
            )
        finally:
            for file in files:
                file.close()
