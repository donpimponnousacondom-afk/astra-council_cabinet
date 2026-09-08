from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from conftest import configured, ingest


CHANNEL = "222222222222222222"
GUILD = "111111111111111111"
OLD = "777777777777777777"


def channel(*, guild=GUILD, history_error=None):
    calls = []

    async def history(**kwargs):
        calls.append(kwargs)
        if history_error:
            raise history_error
        for message in []:
            yield message

    return NS(id=int(CHANNEL), guild=NS(id=int(guild)), history=history), calls


@pytest.mark.parametrize("obsolete", ["unassigned_room", "removed_thread_grant", "wrong_stored_guild"])
async def test_reconnect_skips_obsolete_context_without_fetch_or_runtime_error(kernel, obsolete):
    bot = configured(kernel)
    ingest(kernel)
    room = kernel.store.get("rooms", "council")
    if obsolete == "removed_thread_grant":
        room["include_threads"] = False
        kernel.store.put("rooms", room)
    kernel.store.ingest(
        discord_id="666666666666666666",
        channel_id=OLD,
        room_id="retained",
        parent_id=CHANNEL if obsolete != "unassigned_room" else None,
        guild_id="999999999999999999" if obsolete == "wrong_stored_guild" else GUILD,
        author_id="1482143139828596916",
        author_name="The Boss",
        content="Retained old history",
    )
    kernel.store.context(bot["id"], OLD)
    stored, calls = channel()
    fetched = []

    def get(channel_id):
        fetched.append(str(channel_id))
        assert str(channel_id) == CHANNEL
        return stored

    await kernel.connector.backfill(NS(bot_id=bot["id"], get_channel=get))
    assert fetched == [CHANNEL] and calls
    assert kernel.store.runtime(bot["id"])["error"] is None
    assert kernel.store.transcript(OLD)[0]["content"] == "Retained old history"
    assert kernel.store.one("SELECT 1 FROM contexts WHERE channel_id=?", (OLD,))
    events = kernel.store.events()
    skipped = next(e for e in events if e["kind"] == "discord.history_skipped")
    assert skipped["level"] == "debug" and skipped["data"]["channel_id"] == OLD
    assert not any(e["kind"] == "discord.history_failed" for e in events)


@pytest.mark.parametrize("failure", ["fetch_channel", "validate_scope", "read_history"])
async def test_current_room_failures_remain_visible_with_channel_and_stage(kernel, failure):
    bot = configured(kernel)
    current, _ = channel(
        guild="999999999999999999" if failure == "validate_scope" else GUILD,
        history_error=PermissionError("Read Message History denied") if failure == "read_history" else None,
    )
    client = NS(
        bot_id=bot["id"],
        get_channel=lambda _: None if failure == "fetch_channel" else current,
        fetch_channel=AsyncMock(side_effect=PermissionError("View Channel denied")),
    )
    await kernel.connector.backfill(client)
    failed = next(e for e in kernel.store.events() if e["kind"] == "discord.history_failed")
    assert failed["level"] == "warning"
    assert failed["data"]["channel_id"] == CHANNEL and failed["data"]["stage"] == failure
    assert CHANNEL in failed["data"]["error"] and failure in failed["data"]["error"]
    assert CHANNEL in kernel.store.runtime(bot["id"])["error"]


async def test_scope_removed_during_fetch_skips_history_without_failure(kernel):
    bot = configured(kernel)
    current, calls = channel()

    async def fetch(_):
        updated = kernel.store.get("bots", bot["id"])
        updated["room_ids"] = []
        kernel.store.put("bots", updated)
        return current

    await kernel.connector.backfill(NS(bot_id=bot["id"], get_channel=lambda _: None, fetch_channel=fetch))
    assert not calls
    assert not any(e["kind"] == "discord.history_failed" for e in kernel.store.events())
    assert kernel.store.runtime(bot["id"])["error"] is None
