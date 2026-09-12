"""Shared per-bot note accounting and bounded consolidation headroom."""

DEFAULT_MEMORY_CHAR_LIMIT = 48_000
NOTE_CHAR_LIMIT = 8_000


def hard_limit(limit):
    return limit + limit // 20  # At most 5%; tiny budgets never round upward.


def effective_limit(store, bot):
    current = store.get("bots", bot["id"]) or {}
    return min(
        bot.get("memory_char_limit", DEFAULT_MEMORY_CHAR_LIMIT),
        current.get("memory_char_limit", DEFAULT_MEMORY_CHAR_LIMIT),
    )


def budget_for(store, bot, channel_id):
    limit = effective_limit(store, bot)
    # Count stored Unicode values exactly; SQLite length() stops at embedded NUL.
    used = sum(
        len(note["value"])
        for note in store.rows(
            "SELECT value FROM memories WHERE bot_id=? AND channel_id=?", (bot["id"], channel_id)
        )
    )
    return {
        "limit_chars": limit,
        "used_chars": used,
        "remaining_chars": max(0, limit - used),
        "grace_chars": limit // 20,
        "hard_limit_chars": hard_limit(limit),
        "over_budget_chars": max(0, used - limit),
        "must_consolidate": used > limit,
        "note_limit_chars": NOTE_CHAR_LIMIT,
    }


def largest_channel_usage(store, bot_id):
    totals = {}
    for note in store.rows("SELECT channel_id,value FROM memories WHERE bot_id=?", (bot_id,)):
        channel = note["channel_id"]
        totals[channel] = totals.get(channel, 0) + len(note["value"])
    if not totals:
        return None
    channel = max(totals, key=totals.get)
    return {"channel_id": channel, "used": totals[channel]}


def describe_budget(budget):
    text = (
        f"Private memory: {budget['used_chars']:,}/{budget['limit_chars']:,} characters used in this channel; "
        f"{budget['remaining_chars']:,} remain within budget. Each note allows at most {NOTE_CHAR_LIMIT:,} characters. "
        f"Small overshoots have a 5% allowance (hard ceiling {budget['hard_limit_chars']:,}). "
        "While over budget, only deletes or writes that reduce the total are accepted; consolidate below the budget before adding more. "
    )
    if budget["must_consolidate"]:
        text += (
            f"WARNING: {budget['over_budget_chars']:,} characters over budget. "
            f"Your next memory changes must shrink or delete existing notes until at most {budget['limit_chars']:,} remain. "
        )
    return text
