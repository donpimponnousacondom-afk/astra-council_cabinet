"""Discord-authenticated addressing, distinct from claims inside message text."""

import asyncio
import json


def identities(store, vault):
    return {
        vault.get(f"bot/{bot['id']}/user_id") or bot["application_id"]: {
            "user_id": vault.get(f"bot/{bot['id']}/user_id") or bot["application_id"],
            "bot_id": bot["id"],
            "name": bot["name"],
        }
        for bot in store.list("bots")
        if vault.get(f"bot/{bot['id']}/user_id") or bot["application_id"]
    }


def stored_reference(store, message_id, channel_id):
    row = store.one(
        "SELECT discord_id,author_id,author_name,bot_id,content,deleted FROM messages WHERE discord_id=? AND channel_id=?",
        (message_id, channel_id),
    )
    if not row:
        return None
    return {
        "message_id": row["discord_id"],
        "user_id": row["author_id"],
        "name": row["author_name"],
        "bot_id": row["bot_id"],
        "preview": "[message deleted]" if row["deleted"] else row["content"][:600],
        "preview_truncated": not row["deleted"] and len(row["content"]) > 600,
    }


async def capture(store, vault, message, *, historical=False):
    known = identities(store, vault)
    author = str(message.author.id)
    kind = "webhook" if message.webhook_id else "bot" if message.author.bot or author in known else "human"
    targets = {}
    for user in getattr(message, "mentions", []):
        user_id = str(user.id)
        targets[user_id] = {
            **known.get(user_id, {"user_id": user_id, "name": user.display_name, "bot_id": None}),
            "via": ["mention"],
        }
    ref = getattr(message, "reference", None)
    reply_id = str(ref.message_id) if ref and ref.message_id else None
    reply = None
    if reply_id:
        reply = stored_reference(store, reply_id, str(message.channel.id))
        if not reply:
            prior = store.one(
                "SELECT addressing FROM messages WHERE discord_id=? AND channel_id=?",
                (str(message.id), str(message.channel.id)),
            )
            cached = json.loads(prior["addressing"]).get("reply_target") if prior else None
            if cached and cached.get("user_id") and cached.get("message_id") == reply_id:
                reply = cached
        if not reply:
            resolved = getattr(ref, "resolved", None)
            if not getattr(resolved, "author", None) and hasattr(message.channel, "fetch_message"):
                try:
                    async with asyncio.timeout(4):
                        resolved = await message.channel.fetch_message(int(reply_id))
                except Exception:
                    resolved = None
            if (
                getattr(resolved, "author", None)
                and str(resolved.id) == reply_id
                and str(resolved.channel.id) == str(message.channel.id)
            ):
                user_id = str(resolved.author.id)
                reply = {
                    "message_id": reply_id,
                    **({} if getattr(resolved, "webhook_id", None) else known).get(
                        user_id, {"user_id": user_id, "name": resolved.author.display_name, "bot_id": None}
                    ),
                }
                # Reference identity is enough; do not fetch/embed arbitrary old attachments.
        if reply:
            target = targets.setdefault(
                reply["user_id"], {k: reply[k] for k in ("user_id", "name", "bot_id")}
            )
            target.setdefault("via", []).append("reply")
    return {
        "author_kind": kind,
        **(
            {"application_id": str(message.application_id)}
            if getattr(message, "application_id", None)
            else {}
        ),
        **({"webhook_id": str(message.webhook_id)} if message.webhook_id else {}),
        "live": not historical,
        "directed": bool(reply_id or targets),
        "targets": list(targets.values()),
        "reply_target": reply or ({"message_id": reply_id, "status": "unresolved"} if reply_id else None),
    }


def for_viewer(store, row, bot_id=None):
    data = row.get("addressing") or {}
    if isinstance(data, str):
        data = json.loads(data)
    data = {**data, "targets": [dict(t) for t in data.get("targets", [])]}
    if row.get("reply_to"):
        reference = stored_reference(store, row["reply_to"], row["channel_id"]) or data.get("reply_target")
        data["reply_target"] = reference or {"message_id": row["reply_to"], "status": "unresolved"}
        if (
            reference
            and reference.get("user_id")
            and not any(t["user_id"] == reference["user_id"] for t in data["targets"])
        ):
            data["targets"].append(
                {**{k: reference[k] for k in ("user_id", "name", "bot_id")}, "via": ["reply"]}
            )
    addressed = any(t.get("bot_id") == bot_id for t in data["targets"]) if bot_id else False
    data["audience"] = (
        "you"
        if addressed
        else "other_participant"
        if data["targets"]
        else "unresolved_reply"
        if row.get("reply_to")
        else "channel"
    )
    data["addressed_to_you"] = addressed if data["targets"] else None
    data.setdefault("author_kind", "bot" if row.get("bot_id") else "unknown")
    data.pop("live", None)
    return data


def human_directed_elsewhere(store, row, bot_id):
    data = for_viewer(store, row, bot_id)
    return data["author_kind"] == "human" and data["audience"] in ("other_participant", "unresolved_reply")
