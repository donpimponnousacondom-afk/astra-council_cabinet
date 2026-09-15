"""Owner-requested, Hortator-only cross-channel delivery through the normal outbox."""

import hashlib
import json

from .models import ControlError

DESCRIPTION = (
    "For an explicit request from The Boss, post as Hortator in another configured council channel. "
    "Use targets to discover stable room/channel IDs. send accepts target='random' or its channel ID. "
    "mode=cross_post posts a labelled message; mode=directed replies to a target-channel message ID. "
    "This never starts a new model turn or changes where you accept owner requests. "
    "Use a unique delivery_key for each intended post and reuse it when retrying in this turn. "
    "An unknown receipt means delivery is uncertain: inspect status; never automatically resend under a new key. "
    "Ordinary answers in your current channel remain normal assistant text."
)
PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "operation": {"type": "string", "enum": ["targets", "send", "status"]},
        "target": {"type": "string", "minLength": 1, "maxLength": 80, "examples": ["random"]},
        "content": {"type": "string", "minLength": 1, "maxLength": 12000},
        "mode": {"type": "string", "enum": ["cross_post", "directed"], "default": "cross_post"},
        "reply_to": {"type": "string", "pattern": "^[0-9]{17,20}$"},
        "delivery_key": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80,
            "examples": ["owner-announcement-1"],
        },
        "delivery_id": {"type": "string", "minLength": 1, "maxLength": 80},
        "artifact_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 4, "uniqueItems": True},
    },
    "required": ["operation"],
    "allOf": [
        {
            "if": {"properties": {"operation": {"const": "send"}}, "required": ["operation"]},
            "then": {"required": ["target", "content", "delivery_key"]},
        },
        {
            "if": {"properties": {"operation": {"const": "status"}}, "required": ["operation"]},
            "then": {"required": ["delivery_id"]},
        },
        {
            "if": {"properties": {"mode": {"const": "directed"}}, "required": ["mode"]},
            "then": {"required": ["reply_to"]},
        },
    ],
}


class DiscordDispatch:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def target(self, value):
        room = next((r for r in self.store.list("rooms") if value in (r["id"], r["channel_id"])), None)
        if room and room["guild_id"] and room["channel_id"]:
            return room, room["channel_id"]
        channel = self.store.one("SELECT * FROM channels WHERE id=?", (value,))
        if channel:
            room = next(
                (
                    r
                    for r in self.store.list("rooms")
                    if r["include_threads"]
                    and r["channel_id"] == channel["parent_id"]
                    and r["guild_id"] == channel["guild_id"]
                ),
                None,
            )
            if room:
                return room, value
        raise ControlError(
            "Target must be a configured room ID/channel ID or an observed thread allowed by that room. Use operation=targets."
        )

    def check(self, context, route):
        engine = self.service.engine
        if (
            not context.owner_verified
            or context.bot["role"] != "hortator"
            or not engine.registry.allowed("discord_send", context)
        ):
            raise ControlError("discord_send requires an enabled Hortator grant and a trusted owner request")
        room, channel_id = self.target(route["target_channel_id"])
        if (
            room["id"] != route["room_id"]
            or room["guild_id"] != route["guild_id"]
            or channel_id == context.channel_id
        ):
            raise ControlError("Destination changed or is the current channel; answer normally here")
        return room

    def receipt(self, row):
        route = json.loads(row["routing"])
        sent = row["status"] == "sent"
        return {
            "ok": sent,
            "status": row["status"],
            "delivery_id": row["id"],
            "target_channel_id": row["channel_id"],
            "mode": route["mode"],
            "message_id": row["discord_id"],
            "url": f"https://discord.com/channels/{route['guild_id']}/{row['channel_id']}/{row['discord_id']}"
            if sent
            else None,
            "error": row["error"],
            "note": "Confirmed Discord delivery."
            if sent
            else "No confirmed delivery. Do not automatically resend under a new delivery key.",
        }

    async def call(self, args, context, config, key):
        if args["operation"] == "targets":
            return {
                "targets": [
                    {k: r[k] for k in ("id", "name", "channel_id", "guild_id", "include_threads")}
                    for r in self.store.list("rooms")
                    if r["channel_id"] and r["guild_id"]
                ],
                "note": "Observed threads of rooms with include_threads are also supported by channel ID. Hortator intake remains owner/control-channel/DM only.",
            }
        if args["operation"] == "status":
            row = self.store.one(
                "SELECT * FROM outbox WHERE id=? AND bot_id=? AND routing!='{}'",
                (args["delivery_id"], context.bot["id"]),
            )
            if not row:
                raise ControlError("No routed delivery with that ID belongs to Hortator")
            return self.receipt(row)
        room, channel_id = self.target(args["target"])
        route = {
            "source_channel_id": context.channel_id,
            "target_channel_id": channel_id,
            "room_id": room["id"],
            "guild_id": room["guild_id"],
            "mode": args.get("mode", "cross_post"),
        }
        self.check(context, route)
        if args.get("reply_to") and route["mode"] != "directed":
            raise ControlError("reply_to requires mode=directed; use cross_post without a reply target")
        content = self.service.vault.redact(args["content"])
        artifacts = args.get("artifact_ids", [])
        fingerprint = hashlib.sha256(
            json.dumps([route, content, args.get("reply_to"), artifacts], sort_keys=True).encode()
        ).hexdigest()
        route["fingerprint"] = fingerprint
        delivery_id = (
            "out_"
            + hashlib.sha256(
                f"{context.bot['id']}:{context.turn_id}:{args['delivery_key']}".encode()
            ).hexdigest()[:20]
        )
        existing = self.store.one("SELECT * FROM outbox WHERE id=?", (delivery_id,))
        if existing:
            if json.loads(existing["routing"]).get("fingerprint") != fingerprint:
                raise ControlError(
                    "That delivery_key already identifies a different post; reuse its original arguments or choose a key for an intentionally new post"
                )
            return self.receipt(existing)
        connector = self.service.connector
        await connector.check_destination(
            context.bot,
            channel_id,
            route["guild_id"],
            None if channel_id == room["channel_id"] else room["channel_id"],
            args.get("reply_to"),
        )
        self.check(context, route)
        profile = self.store.get("profiles", context.bot["model_profile_id"])
        provider = self.store.get("providers", profile["provider_id"])
        request = self.store.one(
            "SELECT id FROM requests WHERE turn_id=? AND bot_id=? AND status='completed' AND purpose='generation' ORDER BY started_at DESC LIMIT 1",
            (context.turn_id, context.bot["id"]),
        )
        try:
            await self.service.engine.deliver(
                context.bot,
                profile,
                provider,
                context,
                content,
                args.get("reply_to"),
                artifacts,
                request_id=request["id"] if request else None,
                routing=route,
                delivery_id=delivery_id,
            )
        except Exception:
            row = self.store.one("SELECT * FROM outbox WHERE id=?", (delivery_id,))
            if not row:
                raise
            return self.receipt(row)
        return self.receipt(self.store.one("SELECT * FROM outbox WHERE id=?", (delivery_id,)))
