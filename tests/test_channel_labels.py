import sqlite3
from types import SimpleNamespace as NS

from conftest import configured
from hortator.discord_gateway import CouncilClient
from hortator.store import Store
from support.addressing import CHANNEL, message

THREAD = "777777777777777777"


def test_channel_name_migration_preserves_old_identifiers(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE channels(id TEXT PRIMARY KEY,guild_id TEXT,parent_id TEXT)")
        db.execute("INSERT INTO channels VALUES('thread','guild','root')")
    store = Store(path)
    try:
        assert store.one("SELECT * FROM channels") == {
            "id": "thread",
            "guild_id": "guild",
            "parent_id": "root",
            "name": None,
        }
        store.remember_channel("thread", name="Research")
        store.remember_channel("thread")
        assert store.one("SELECT name FROM channels")["name"] == "Research"
    finally:
        store.close()


async def test_observed_thread_names_reach_context_and_engram_inspection_without_state_changes(kernel):
    bot = configured(kernel)
    kernel.store.remember_channel(CHANNEL, name="chaos")
    incoming = message(555555555555555555)
    incoming.channel = NS(id=int(THREAD), parent_id=int(CHANNEL), name="Model experiments")
    await kernel.connector.receive(bot["id"], incoming, historical=True)
    kernel.store.execute(
        "INSERT INTO engram_states(bot_id,channel_id,epoch,summary_hash,mem,facts,updated_at) "
        "VALUES(?,?,'0:0','','Remember this','Preserve that',100)",
        (bot["id"], THREAD),
    )
    before = kernel.store.one("SELECT * FROM engram_states")
    public = kernel.service.public("bots", bot)
    context = next(c for c in public["contexts"] if c["channel_id"] == THREAD)
    state = kernel.service.engram_view(bot["id"])["states"][0]
    for row in (context, state):
        assert row["channel_id"] == THREAD
        assert row["channel_name"] == "Model experiments"
        assert row["parent_id"] == CHANNEL and row["parent_name"] == "chaos"
    assert state["MEM"] == "Remember this"
    assert kernel.store.one("SELECT * FROM engram_states") == before
    assert not kernel.store.rows("SELECT * FROM requests")
    assert kernel.store.get("bots", bot["id"]) == bot


async def test_rename_updates_known_channels_only(kernel):
    kernel.store.remember_channel(THREAD, parent_id=CHANNEL, name="Before")
    client = NS(manager=kernel.connector)
    await CouncilClient.on_thread_update(client, None, NS(id=int(THREAD), name="After"))
    await CouncilClient.on_guild_channel_update(client, None, NS(id=int(CHANNEL), name="Unobserved"))
    assert kernel.store.one("SELECT name FROM channels WHERE id=?", (THREAD,))["name"] == "After"
    assert not kernel.store.one("SELECT 1 FROM channels WHERE id=?", (CHANNEL,))


async def test_reconnect_learns_existing_thread_name_even_without_new_messages(kernel):
    bot = configured(kernel)
    kernel.store.remember_channel(THREAD, guild_id="111111111111111111", parent_id=CHANNEL)
    kernel.store.context(bot["id"], THREAD)

    async def history(**kwargs):
        for item in []:
            yield item

    def get(channel_id):
        return NS(
            id=channel_id,
            guild=NS(id=111111111111111111),
            parent_id=int(CHANNEL) if str(channel_id) == THREAD else None,
            name="Old thread" if str(channel_id) == THREAD else "chaos",
            history=history,
        )

    await kernel.connector.backfill(NS(bot_id=bot["id"], get_channel=get))
    assert kernel.store.one("SELECT name FROM channels WHERE id=?", (THREAD,))["name"] == "Old thread"
    assert not kernel.store.rows("SELECT * FROM messages")
    assert not kernel.store.rows("SELECT * FROM requests")
