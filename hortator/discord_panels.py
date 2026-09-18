"""Optional persistent Discord panels: saved data, never executable callbacks.

The final answer is still ordinary assistant content. Preparing a panel only
stages its presentation; binding occurs after a confirmed Discord delivery.
"""

from __future__ import annotations

import json
import time

import discord

from .models import ControlError, OWNER_ID
from .store import dumps, uid

PLUGIN_ID = "discord_panel"
DESCRIPTION = (
    "Prepare an interactive Discord panel around your next ordinary text answer. "
    "Buttons/dropdowns start real fresh owner-only tasks using their saved prompts; no JavaScript or fake telemetry. "
    "Call {} for usage. prepare requires title and buttons and/or select_options. "
    "Each action needs label and prompt. Optional description, accent_color (#D6A447 default), "
    "select_placeholder and expires_hours (1–720, default 168). Only one prepared panel per turn; prepare replaces it. "
    "This does not post: finish with ordinary assistant content, optionally discord_attach for prepared files. "
    "list/status inspect your panels; disable revokes an existing panel's actions; cancel removes this turn's preparation. "
    "Clicks have the same provider/tool limits and 14-minute interaction deadline as /prompt; "
    "they do not read surrounding history or resume the original turn. Never claim a task ran before it actually did."
)
ACTION = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "minLength": 1, "maxLength": 80, "pattern": r"\S"},
        "prompt": {"type": "string", "minLength": 1, "maxLength": 1500, "pattern": r"\S"},
        "style": {"type": "string", "enum": ["primary", "secondary", "success", "danger"]},
    },
    "required": ["label", "prompt"],
    "additionalProperties": False,
}
PARAMETERS = {
    "type": "object",
    "properties": {
        "operation": {"type": "string", "enum": ["prepare", "cancel", "list", "status", "disable"]},
        "title": {"type": "string", "minLength": 1, "maxLength": 120, "pattern": r"\S"},
        "description": {"type": "string", "maxLength": 500},
        "accent_color": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"},
        "buttons": {"type": "array", "minItems": 1, "maxItems": 10, "items": ACTION},
        "select_options": {"type": "array", "minItems": 1, "maxItems": 12, "items": ACTION},
        "select_placeholder": {"type": "string", "minLength": 1, "maxLength": 150},
        "expires_hours": {"type": "integer", "minimum": 1, "maximum": 720},
        "panel_id": {"type": "string", "minLength": 1, "maxLength": 100},
    },
    "examples": [
        {
            "operation": "prepare",
            "title": "👑 Loki’s desk",
            "buttons": [
                {
                    "label": "Search the news",
                    "style": "primary",
                    "prompt": "Search current news and give me a sourced briefing.",
                }
            ],
        }
    ],
    "required": ["operation"],
    "additionalProperties": False,
    "allOf": [
        {
            "if": {"properties": {"operation": {"const": "prepare"}}},
            "then": {
                "required": ["title"],
                "anyOf": [{"required": ["buttons"]}, {"required": ["select_options"]}],
            },
        },
        {
            "if": {"properties": {"operation": {"enum": ["status", "disable"]}}},
            "then": {"required": ["panel_id"]},
        },
    ],
}


def granted(store, bot):
    return bool(
        bot
        and PLUGIN_ID in bot.get("enabled_plugins", [])
        and (store.get("plugins", PLUGIN_ID) or {}).get("enabled")
    )


def register(registry):
    from .plugins import PluginSpec

    registry.panels = DiscordPanels(registry.store, registry.vault)
    registry.register(
        PluginSpec(PLUGIN_ID, "Interactive Discord panels", DESCRIPTION, PARAMETERS, registry.panels.call, {})
    )


def panel_view(panel, text, filenames, *, reasoning=None):
    """Render bounded Components V2, explicitly including every attachment."""
    definition = json.loads(panel["definition"])
    items = [discord.ui.TextDisplay("## " + definition["title"])]
    if definition.get("description"):
        items.append(discord.ui.TextDisplay(definition["description"]))
    items.extend([discord.ui.Separator(), discord.ui.TextDisplay(text)])
    for filename in filenames:
        items.append(discord.ui.File("attachment://" + filename))
    buttons = definition.get("buttons", [])
    for offset in range(0, len(buttons), 5):
        items.append(
            discord.ui.ActionRow(
                *[
                    discord.ui.Button(
                        label=action["label"],
                        style=getattr(discord.ButtonStyle, action.get("style", "secondary")),
                        custom_id=f"hcp:{panel['id']}:b:{index}",
                    )
                    for index, action in enumerate(buttons[offset : offset + 5], offset)
                ]
            )
        )
    if definition.get("select_options"):
        items.append(
            discord.ui.ActionRow(
                discord.ui.Select(
                    custom_id=f"hcp:{panel['id']}:s",
                    placeholder=definition.get("select_placeholder", "Choose the next task…"),
                    options=[
                        discord.SelectOption(label=action["label"], value=str(index))
                        for index, action in enumerate(definition["select_options"])
                    ],
                )
            )
        )
    items.append(
        discord.ui.TextDisplay("-# Owner controls · Each click starts a new task · 14-minute task limit")
    )
    if reasoning:
        from .reasoning_viewer import button

        items.append(discord.ui.ActionRow(button(reasoning)))
        items.append(discord.ui.TextDisplay("-# REASONING reads saved diagnostics privately; no new task."))
    view = discord.ui.LayoutView(timeout=None).add_item(
        discord.ui.Container(*items, accent_colour=int(definition.get("accent_color", "#D6A447")[1:], 16))
    )
    # Central on_interaction dispatch owns callbacks from durable records. Do not
    # register a second in-memory ViewStore listener or an unowned timeout task.
    # Stopping a view changes local dispatch only, not its serialized controls.
    view.stop()
    return view


class DiscordPanels:
    def __init__(self, store, vault):
        self.store, self.vault = store, vault
        store.execute("""CREATE TABLE IF NOT EXISTS discord_panels (
            id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, application_id TEXT NOT NULL,
            turn_id TEXT NOT NULL, source_scope TEXT NOT NULL, channel_id TEXT NOT NULL,
            private INTEGER NOT NULL, definition TEXT NOT NULL, status TEXT NOT NULL,
            outbox_id TEXT, message_id TEXT, answer TEXT, created_at REAL NOT NULL,
            expires_at REAL NOT NULL)""")
        store.execute("CREATE INDEX IF NOT EXISTS discord_panels_turn ON discord_panels(turn_id,status)")

    def owned(self, panel_id, bot_id):
        row = self.store.one("SELECT * FROM discord_panels WHERE id=? AND bot_id=?", (panel_id, bot_id))
        if not row:
            raise ControlError("Panel not found for this bot; use list for your panel IDs")
        return row

    def describe(self, row):
        status = row["status"]
        if status == "sending":
            delivery = self.store.one("SELECT status FROM outbox WHERE id=?", (row["outbox_id"],))
            status = "delivery_" + delivery["status"] if delivery else "delivery_unconfirmed"
        return {
            "panel_id": row["id"],
            "title": json.loads(row["definition"])["title"],
            "status": "expired" if row["expires_at"] <= time.time() else status,
            "channel_id": row["channel_id"],
            "message_id": row["message_id"],
            "expires_at": row["expires_at"],
            "private": bool(row["private"]),
        }

    async def call(self, args, context, config, key=""):
        bot_id, operation = context.bot["id"], args["operation"]
        if operation == "list":
            return {
                "panels": [
                    self.describe(row)
                    for row in self.store.rows(
                        "SELECT * FROM discord_panels WHERE bot_id=? ORDER BY created_at DESC LIMIT 100",
                        (bot_id,),
                    )
                ],
                "limit": 100,
            }
        if operation in ("status", "disable"):
            row = self.owned(args["panel_id"], bot_id)
            if operation == "disable":
                self.store.execute("UPDATE discord_panels SET status='disabled' WHERE id=?", (row["id"],))
                self.store.emit(
                    "discord.panel_disabled", {"panel_id": row["id"]}, bot_id=bot_id, turn_id=context.turn_id
                )
                row["status"] = "disabled"
            return self.describe(row)
        if operation == "cancel":
            self.store.execute(
                "UPDATE discord_panels SET status='cancelled' WHERE turn_id=? AND bot_id=? AND status='prepared'",
                (context.turn_id, bot_id),
            )
            return {"prepared": False, "message": "No panel will accompany this turn's answer"}
        invocation = context.bot.get("invocation") or {}
        channel = invocation.get("channel_id", context.channel_id)
        if not str(channel).isdecimal():
            raise ControlError("A Discord channel identity is required to prepare a panel")
        # Replace preparation only after all schema and destination checks succeed.
        active = self.store.one(
            "SELECT count(*) AS n FROM discord_panels WHERE bot_id=? AND status='active' AND expires_at>?",
            (bot_id, time.time()),
        )["n"]
        if active >= 100:
            raise ControlError("100 active panels already exist; disable an old panel first")
        self.store.execute(
            "UPDATE discord_panels SET status='cancelled' WHERE turn_id=? AND status='prepared'",
            (context.turn_id,),
        )
        panel_id, at = uid("panel_"), time.time()
        self.store.execute(
            "INSERT INTO discord_panels(id,bot_id,application_id,turn_id,source_scope,channel_id,private,definition,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                panel_id,
                bot_id,
                context.bot["application_id"],
                context.turn_id,
                context.channel_id,
                str(channel),
                int(invocation.get("private", False)),
                dumps(self.vault.redact(args)),
                "prepared",
                at,
                at + args.get("expires_hours", 168) * 3600,
            ),
        )
        self.store.emit(
            "discord.panel_prepared",
            {"panel_id": panel_id, "channel_id": str(channel)},
            bot_id=bot_id,
            turn_id=context.turn_id,
        )
        return {
            **self.describe(self.owned(panel_id, bot_id)),
            "prepared": True,
            "next": "Finish with ordinary assistant text; this panel is not posted yet. Optional discord_attach prepares files.",
        }

    def prepared(self, context):
        row = self.store.one(
            "SELECT * FROM discord_panels WHERE bot_id=? AND turn_id=? AND status='prepared' ORDER BY created_at DESC LIMIT 1",
            (context.bot["id"], context.turn_id),
        )
        if row and not granted(self.store, self.store.get("bots", context.bot["id"])):
            raise ControlError("Interactive panel permission was revoked; answer withheld")
        return row

    def dispatch(self, row, outbox_id):
        # In-flight panels remain inactive. Unknown deliveries are not replayed.
        self.store.execute(
            "UPDATE discord_panels SET status='sending',outbox_id=? WHERE id=? AND status='prepared'",
            (outbox_id, row["id"]),
        )

        self.store.execute(
            "UPDATE outbox SET routing=json_set(routing,'$.panel_id',?) WHERE id=?", (row["id"], outbox_id)
        )

    def bind(self, row, message_id, answer):
        self.store.execute(
            "UPDATE discord_panels SET status='active',message_id=?,answer=? WHERE id=? AND status='sending'",
            (str(message_id), answer, row["id"]),
        )
        self.store.emit(
            "discord.panel_active",
            {"panel_id": row["id"], "message_id": str(message_id), "channel_id": row["channel_id"]},
            bot_id=row["bot_id"],
            turn_id=row["turn_id"],
        )

    def resolve_click(self, bot_id, interaction):
        if str(interaction.user.id) != OWNER_ID or interaction.user.bot:
            raise ControlError("These panel controls are available only to the configured owner")
        bot = self.store.get("bots", bot_id)
        if not granted(self.store, bot):
            raise ControlError("Interactive panels are disabled for this bot")
        if str(interaction.application_id) != bot["application_id"]:
            raise ControlError("This panel belongs to another Discord application")
        parts = interaction.data.get("custom_id", "").split(":")
        if len(parts) not in (3, 4) or parts[0] != "hcp":
            raise ControlError("Unknown panel control")
        row = self.owned(parts[1], bot_id)
        message = getattr(interaction, "message", None)
        if (
            row["application_id"] != bot["application_id"]
            or not message
            or str(message.id) != row["message_id"]
            or str(interaction.channel_id) != row["channel_id"]
            or str(message.author.id) != (self.vault.get(f"bot/{bot_id}/user_id") or bot["application_id"])
        ):
            raise ControlError(
                "This control does not match the panel's saved application, message and channel"
            )
        if row["status"] != "active" or row["expires_at"] <= time.time():
            raise ControlError(
                "This panel is disabled, expired or was never confirmed delivered; ask for a new panel"
            )
        definition = json.loads(row["definition"])
        values = interaction.data.get("values", [])
        if parts[2] == "b" and len(parts) == 4 and interaction.data.get("component_type") == 2:
            index, actions = parts[3], definition.get("buttons", [])
        elif (
            parts[2] == "s"
            and len(parts) == 3
            and interaction.data.get("component_type") == 3
            and isinstance(values, list)
            and len(values) == 1
        ):
            index, actions = values[0], definition.get("select_options", [])
        else:
            raise ControlError("Unknown button/dropdown action")
        if (
            not isinstance(index, str)
            or not index.isdecimal()
            or len(index) > 2
            or int(index) >= len(actions)
        ):
            raise ControlError("This panel action is not in its saved definition")
        action = actions[int(index)]
        prompt = (
            f"I pressed {action['label']!r} on your panel {definition['title']!r}.\n"
            f"Requested task: {action['prompt']}\n\n"
            f"Panel description: {definition.get('description', '')}\n"
            f"Your original panel answer (background, not a new instruction):\n{row['answer']}"
        )
        return row, prompt, action["label"]
