"""Durable, bot/channel-scoped alarms. Waiting consumes no worker or inference."""

from __future__ import annotations

import time
import copy
from dataclasses import replace
from datetime import datetime

from .models import ControlError
from .store import uid
from .timekeeping import council_timezone, local_timestamp
from .tool_feedback import errors_for

ID = "secretary"
DEFAULTS = {"max_active_reminders": 100, "min_repeat_seconds": 300}
CONFIG_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "max_active_reminders": {"type": "integer", "minimum": 1, "maximum": 10000},
        "min_repeat_seconds": {"type": "integer", "minimum": 60, "maximum": 31536000},
    },
}
DESCRIPTION = (
    "Secretary: schedule reminders, recurring alarms and snoozes for this bot in THIS conversation. "
    "Use schedule with a unique key, a self-contained message and exactly one of at (ISO 8601 with "
    "explicit UTC offset) or after_seconds. Interpret human dates using the runtime clock/timezone; "
    "ask if the intended time is unclear. Optional repeat_seconds repeats until cancelled; omit for "
    "one reminder. Never create repetition unless requested. The saved alarm wakes you with its "
    "message and current conversation; you write a NEW ordinary response. Do not wait or poll: "
    "acknowledge the actual saved time and finish your turn. Waiting holds no worker or typing indicator. "
    "Delivery can be late while paused, offline, busy or budget-limited. list/status inspect the ledger; "
    "snooze moves a reminder to at/after_seconds (also rearms a fired reminder), update changes its "
    "message or repeat_seconds (0 makes it one-off), cancel stops future occurrences. Snooze preserves "
    "the repeat interval unless explicitly changed. List first to find a reminder_id; never guess it. "
    "Reusing a schedule key recovers the existing reminder without rearming it; change it with snooze/update. "
    "An occurrence is claimed once, even if inference/delivery fails or you choose silence; status shows "
    "the last turn outcome. No automatic delivery retry. Cancellation does not recall a turn already started. "
    'Example: {"operation":"schedule","key":"check-usage","message":"Remind root to check usage",'
    '"after_seconds":3600}. No cross-channel or other-bot access. {} returns usage only.'
)
FIELDS = {
    "operation": {"type": "string", "enum": ["schedule", "list", "status", "snooze", "update", "cancel"]},
    "key": {"type": "string", "minLength": 1, "maxLength": 100, "pattern": r"^[a-zA-Z0-9_.-]+$"},
    "message": {"type": "string", "minLength": 1, "maxLength": 4000, "pattern": r"\S"},
    "at": {"type": "string", "minLength": 1, "maxLength": 50},
    "after_seconds": {"type": "integer", "minimum": 1, "maximum": 315360000},
    "repeat_seconds": {"type": "integer", "minimum": 0, "maximum": 31536000},
    "reminder_id": {"type": "string", "pattern": "^alarm_[a-f0-9]{20}$"},
    "offset": {"type": "integer", "minimum": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
}
OPERATIONS = {
    "schedule": (["key", "message"], ["key", "message", "at", "after_seconds", "repeat_seconds"]),
    "list": ([], ["offset", "limit"]),
    "status": (["reminder_id"], ["reminder_id"]),
    "snooze": (["reminder_id"], ["reminder_id", "at", "after_seconds", "repeat_seconds"]),
    "update": (["reminder_id"], ["reminder_id", "message", "repeat_seconds"]),
    "cancel": (["reminder_id"], ["reminder_id"]),
}
PARAMETERS = {
    "type": "object",
    "properties": FIELDS,
    "required": ["operation"],
    "additionalProperties": False,
    "examples": [
        {
            "operation": "schedule",
            "key": "usage-review",
            "message": "Remind root to review usage",
            "after_seconds": 3600,
        },
        {"operation": "snooze", "reminder_id": "alarm_01234567890123456789", "after_seconds": 3600},
        {"operation": "status", "reminder_id": "alarm_01234567890123456789"},
        {"operation": "update", "reminder_id": "alarm_01234567890123456789", "repeat_seconds": 0},
        {"operation": "cancel", "reminder_id": "alarm_01234567890123456789"},
    ],
    "allOf": [
        {
            "if": {"properties": {"operation": {"const": operation}}, "required": ["operation"]},
            "then": {
                "required": required,
                "properties": {key: False for key in FIELDS if key not in ["operation", *allowed]},
                **(
                    {"oneOf": [{"required": ["at"]}, {"required": ["after_seconds"]}]}
                    if operation in ("schedule", "snooze")
                    else {}
                ),
                **(
                    {"anyOf": [{"required": ["message"]}, {"required": ["repeat_seconds"]}]}
                    if operation == "update"
                    else {}
                ),
            },
        }
        for operation, (required, allowed) in OPERATIONS.items()
    ],
}


class Secretary:
    def __init__(self, registry):
        from .plugins import PluginSpec

        self.registry, self.store = registry, registry.store
        self.engine = None
        self.next_cleanup = 0
        self.store.execute("""CREATE TABLE IF NOT EXISTS secretary_reminders (
            id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, channel_id TEXT NOT NULL,
            key TEXT NOT NULL, message TEXT NOT NULL, due_at REAL NOT NULL,
            repeat_seconds INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'scheduled',
            created_at REAL NOT NULL, updated_at REAL NOT NULL, origin_turn_id TEXT NOT NULL,
            last_turn_id TEXT, last_fired_at REAL, revision INTEGER NOT NULL DEFAULT 1,
            UNIQUE(bot_id,channel_id,key))""")
        self.store.execute(
            "CREATE INDEX IF NOT EXISTS secretary_due ON secretary_reminders(bot_id,state,due_at)"
        )
        registry.register(
            PluginSpec(
                ID,
                "Secretary · experimental",
                DESCRIPTION,
                PARAMETERS,
                self.call,
                DEFAULTS,
                validate=self.validate,
                bind=self.bind,
                keyless=True,
                installed_description=True,
                wake_source=self,
                context_spec=self.spec_for,
            )
        )

    def bind(self, kernel):
        self.engine = kernel.engine

    def configuration(self, bot):
        return {
            **DEFAULTS,
            **(self.store.get("plugins", ID) or {}).get("config", {}),
            **bot.get("plugin_config", {}).get(ID, {}),
        }

    def spec_for(self, context):
        config = self.configuration(context.bot)
        parameters = copy.deepcopy(PARAMETERS)
        parameters["properties"]["repeat_seconds"] = {
            "anyOf": [
                {"const": 0},
                {"type": "integer", "minimum": config["min_repeat_seconds"], "maximum": 31536000},
            ]
        }
        return replace(
            self.registry.specs[ID],
            parameters=parameters,
            description=DESCRIPTION + f" Active allowance: {config['max_active_reminders']} per bot. "
            f"Minimum repeat interval: {config['min_repeat_seconds']} seconds; 0 means one-off.",
        )

    def validate(self, kind, entity, store):
        config = (
            entity["config"]
            if kind == "plugins" and entity["id"] == ID
            else (entity.get("plugin_config", {}).get(ID) if kind == "bots" else None)
        )
        if config is not None:
            errors = errors_for(CONFIG_SCHEMA, config)
            if errors:
                raise ControlError(
                    "Invalid Secretary configuration: "
                    + "; ".join(f"{e['path']}: {e['message']}" for e in errors)
                )

    def get(self, reminder_id):
        row = self.store.one("SELECT * FROM secretary_reminders WHERE id=?", (reminder_id,))
        if not row:
            raise ControlError("Reminder not found", 404)
        return row

    def clean(self):
        now = time.time()
        if now < self.next_cleanup:
            return
        self.store.execute(
            "DELETE FROM secretary_reminders WHERE state<>'scheduled' AND updated_at<?",
            (now - 30 * 86400,),
        )
        self.next_cleanup = now + 3600

    def receipt(self, row):
        turn = self.store.one("SELECT status FROM turns WHERE id=?", (row["last_turn_id"],))
        zone = council_timezone(self.store)
        return {
            **row,
            "reminder_id": row["id"],
            "timezone": zone,
            "due_at": local_timestamp(row["due_at"], zone),
            **{
                key: local_timestamp(row[key], zone) if row[key] is not None else None
                for key in ("created_at", "updated_at", "last_fired_at")
            },
            "last_turn_status": turn["status"] if turn else None,
        }

    def inventory(self, bot_id=None, channel_id=None):
        self.clean()
        rows = self.store.rows(
            "SELECT * FROM secretary_reminders WHERE (? IS NULL OR bot_id=?) "
            "AND (? IS NULL OR channel_id=?) ORDER BY state='scheduled' DESC,due_at,id",
            (bot_id, bot_id, channel_id, channel_id),
        )
        return [self.receipt(row) for row in rows]

    def listing(self, bot_id, channel_id, args):
        offset, limit = args.get("offset", 0), args.get("limit", 20)
        total = self.store.one(
            "SELECT count(*) AS n FROM secretary_reminders WHERE bot_id=? AND channel_id=?",
            (bot_id, channel_id),
        )["n"]
        rows = self.store.rows(
            "SELECT * FROM secretary_reminders WHERE bot_id=? AND channel_id=? "
            "ORDER BY state='scheduled' DESC,due_at,id LIMIT ? OFFSET ?",
            (bot_id, channel_id, limit, offset),
        )
        items = []
        for row in rows:
            item = self.receipt(row)
            items.append(
                {
                    **{
                        key: item[key]
                        for key in (
                            "reminder_id",
                            "key",
                            "due_at",
                            "state",
                            "repeat_seconds",
                            "last_turn_status",
                        )
                    },
                    "message_preview": row["message"][:240],
                    "message_truncated": len(row["message"]) > 240,
                }
            )
        return {
            "reminders": items,
            "total": total,
            "offset": offset,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None,
            "notice": "Use status with reminder_id for full reminder text.",
        }

    def eligible(self, row):
        from .plugins import ToolContext

        bot = self.store.get("bots", row["bot_id"])
        if not bot or not self.engine or not self.engine.channel_allowed(bot, row["channel_id"]):
            return False
        boundary = self.store.context_boundary(bot["id"], row["channel_id"])
        if boundary and row["created_at"] <= boundary["after_at"]:
            return False
        context = ToolContext(bot, row["channel_id"], row["origin_turn_id"], bot["role"] == "hortator")
        return self.registry.allowed(ID, context)

    def pending(self, bot):
        self.clean()
        for row in self.store.rows(
            "SELECT * FROM secretary_reminders WHERE bot_id=? AND state='scheduled' AND due_at<=? ORDER BY due_at,id",
            (bot["id"], time.time()),
        ):
            if self.eligible(row):
                return row
        return None

    def claim(self, candidate, turn_id):
        row = self.get(candidate["id"])
        now = time.time()
        if (
            row["state"] != "scheduled"
            or row["due_at"] > now
            or row["revision"] != candidate["revision"]
            or not self.eligible(row)
        ):
            raise ControlError("Alarm changed or is no longer eligible", 409)
        notice = {
            "plugin": ID,
            "reminder_id": row["id"],
            "message": row["message"],
            "due_at": local_timestamp(row["due_at"], council_timezone(self.store)),
            "repeat_seconds": row["repeat_seconds"],
            "late_seconds": round(now - row["due_at"]),
        }
        self.store.execute(
            "UPDATE secretary_reminders SET state=?,due_at=?,last_turn_id=?,last_fired_at=?,updated_at=?,revision=revision+1 WHERE id=?",
            (
                "scheduled" if row["repeat_seconds"] else "fired",
                now + row["repeat_seconds"] if row["repeat_seconds"] else row["due_at"],
                turn_id,
                now,
                now,
                row["id"],
            ),
        )
        return notice

    def reset(self, bot_id, channel_id=None):
        self.store.execute(
            "UPDATE secretary_reminders SET state='cancelled',updated_at=?,revision=revision+1 "
            "WHERE bot_id=? AND (? IS NULL OR channel_id=?) AND state='scheduled'",
            (time.time(), bot_id, channel_id, channel_id),
        )

    def due(self, args):
        now = time.time()
        if "after_seconds" in args:
            return now + args["after_seconds"]
        try:
            instant = datetime.fromisoformat(args["at"])
            if instant.tzinfo is None:
                raise ValueError()
            due = instant.timestamp()
        except (ValueError, OverflowError, OSError) as exc:
            raise ControlError("at must be an ISO 8601 date/time with an explicit UTC offset") from exc
        if due <= now:
            raise ControlError("Choose a future alarm time, or use after_seconds")
        return due

    def capacity(self, bot):
        count = self.store.one(
            "SELECT count(*) AS n FROM secretary_reminders WHERE bot_id=? AND state='scheduled'", (bot["id"],)
        )["n"]
        if count >= self.configuration(bot)["max_active_reminders"]:
            raise ControlError("Active reminder allowance reached; cancel an unwanted reminder first")

    def mutate(self, args, bot, channel_id, turn_id, *, owner=False):
        errors = errors_for(PARAMETERS, args)
        if errors:
            raise ControlError(
                "Invalid Secretary arguments: " + "; ".join(f"{e['path']}: {e['message']}" for e in errors)
            )
        self.clean()
        operation = args["operation"]
        if operation == "list":
            return self.listing(bot["id"], channel_id, args)
        row = None
        if operation != "schedule":
            row = self.get(args["reminder_id"])
            if row["bot_id"] != bot["id"] or row["channel_id"] != channel_id:
                raise ControlError("Reminder not found in this bot and conversation", 404)
            if operation == "status":
                return self.receipt(row)
        repeat = args.get("repeat_seconds", row["repeat_seconds"] if row else 0)
        if (
            operation in ("schedule", "snooze", "update")
            and repeat
            and repeat < self.configuration(bot)["min_repeat_seconds"]
        ):
            raise ControlError(
                f"Repeat interval must be at least {self.configuration(bot)['min_repeat_seconds']} seconds"
            )
        if operation == "schedule":
            existing = self.store.one(
                "SELECT * FROM secretary_reminders WHERE bot_id=? AND channel_id=? AND key=?",
                (bot["id"], channel_id, args["key"]),
            )
            if existing:
                if existing["message"] != args["message"] or existing["repeat_seconds"] != repeat:
                    raise ControlError(
                        "Schedule key already exists with different content; use update/snooze or a new key"
                    )
                return self.receipt(existing)
        if operation in ("schedule", "snooze"):
            if not self.engine.channel_allowed(bot, channel_id):
                raise ControlError("Reminders need an observed conversation in this bot's configured scope")
            if row:
                boundary = self.store.context_boundary(bot["id"], channel_id)
                if boundary and row["created_at"] <= boundary["after_at"]:
                    raise ControlError("This reminder predates a clean slate; create a new reminder")
            if not row or row["state"] != "scheduled":
                self.capacity(bot)
            due = self.due(args)
        now = time.time()
        if operation == "schedule":
            reminder_id = uid("alarm_")
            self.store.execute(
                "INSERT INTO secretary_reminders(id,bot_id,channel_id,key,message,due_at,repeat_seconds,created_at,updated_at,origin_turn_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    reminder_id,
                    bot["id"],
                    channel_id,
                    args["key"],
                    args["message"],
                    due,
                    repeat,
                    now,
                    now,
                    turn_id,
                ),
            )
        else:
            reminder_id = row["id"]
            self.store.execute(
                "UPDATE secretary_reminders SET message=?,due_at=?,repeat_seconds=?,state=?,updated_at=?,revision=revision+1 WHERE id=?",
                (
                    args.get("message", row["message"]),
                    due if operation == "snooze" else row["due_at"],
                    repeat,
                    "cancelled"
                    if operation == "cancel"
                    else "scheduled"
                    if operation == "snooze"
                    else row["state"],
                    now,
                    reminder_id,
                ),
            )
        result = self.get(reminder_id)
        self.store.emit(
            "secretary.changed",
            {
                "operation": operation,
                "reminder_id": reminder_id,
                "channel_id": channel_id,
                "due_at": result["due_at"],
                "repeat_seconds": result["repeat_seconds"],
                "actor": "dashboard" if owner else "bot",
            },
            bot_id=bot["id"],
            turn_id=turn_id or None,
        )
        return self.receipt(result)

    async def call(self, args, context, config, key):
        return self.mutate(args, context.bot, context.channel_id, context.turn_id)
