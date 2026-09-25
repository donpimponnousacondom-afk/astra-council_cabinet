"""Owner-only, bot-scoped conversation cutoffs; shared transcripts remain evidence."""

import time

from .models import ControlError


async def reset(service, actor, bot_id, data):
    actor.require_owner()
    if not isinstance(data, dict) or set(data) - {"confirm_bot_id", "channel_id"}:
        raise ControlError("Unknown clean-slate parameters")
    if data.get("confirm_bot_id") != bot_id:
        raise ControlError("Confirm the exact bot ID before clearing its context")
    channel_id = data.get("channel_id")
    if channel_id is not None and (not isinstance(channel_id, str) or not channel_id or channel_id == "*"):
        raise ControlError("Choose an existing channel, or omit channel_id for all channels")
    async with service.lock:
        service.entity("bots", bot_id)
        store, engine = service.store, service.engine
        if channel_id and not store.one(
            "SELECT 1 FROM contexts WHERE bot_id=? AND channel_id=?", (bot_id, channel_id)
        ):
            raise ControlError("This bot has no context in the selected channel", 404)
        engine.resetting.add(bot_id)
        try:
            # Block all ingress while cancel/join drains tools, inference and delivery.
            await engine.cancel([bot_id], "Owner requested a clean context slate")
            now = time.time()
            sequence = store.one("SELECT coalesce(max(seq),0) AS seq FROM messages")["seq"]
            clause, args = "bot_id=?", [bot_id]
            if channel_id:
                clause += " AND channel_id=?"
                args.append(channel_id)
            store.execute("BEGIN IMMEDIATE")
            try:
                service.registry.reset_wakes(bot_id, channel_id)
                if not channel_id:
                    store.execute("DELETE FROM context_resets WHERE bot_id=?", (bot_id,))
                store.execute(
                    "INSERT OR REPLACE INTO context_resets VALUES(?,?,?,?)",
                    (bot_id, channel_id or "*", sequence, now),
                )
                for context in store.rows(f"SELECT * FROM contexts WHERE {clause}", args):
                    tail = store.one(
                        "SELECT coalesce(max(seq),0) AS seq FROM messages WHERE channel_id=?",
                        (context["channel_id"],),
                    )["seq"]
                    store.execute(
                        "UPDATE contexts SET summary='',checkpoint=?,last_seen=?,estimated_tokens=0,"
                        "compactions=0,updated_at=? WHERE bot_id=? AND channel_id=?",
                        (tail, tail, now, bot_id, context["channel_id"]),
                    )
                result = {
                    "bot_id": bot_id,
                    "channel_id": channel_id,
                    "after_seq": sequence,
                    "after_at": now,
                    "reason": "Active turn cancelled; previous messages and summaries excluded. Memories, shared history and configuration retained.",
                }
                store.emit("context.reset", {**result, "actor": actor.label}, bot_id=bot_id, level="warning")
                store.execute("COMMIT")
            except BaseException:
                store.execute("ROLLBACK")
                raise
            return result
        finally:
            engine.resetting.discard(bot_id)
