"""Discord changes during asynchronous final source validation cannot race promotion."""

import asyncio
import json

import pytest

from conftest import ingest
from hortator.models import ControlError, OWNER_ID
from support.provider import install_client
from test_compaction_resume import CHANNEL, complete, progress, sequences, setup, source


def notify_after_source_snapshot(kernel, monkeypatch):
    snapshot_read = asyncio.Event()
    original = kernel.store.rows

    def observed(sql, args=()):
        result = original(sql, args)
        if sql.startswith("SELECT * FROM messages WHERE channel_id=? AND seq IN ("):
            snapshot_read.set()
        return result

    monkeypatch.setattr(kernel.store, "rows", observed)
    return snapshot_read


async def run_during_recheck(kernel, monkeypatch, bot, profile, mutate, *, rejected):
    snapshot_read = notify_after_source_snapshot(kernel, monkeypatch)
    changed = asyncio.Event()

    async def concurrent_gateway_event():
        await asyncio.wait_for(snapshot_read.wait(), 3)
        mutate()
        changed.set()

    async def compact():
        if rejected:
            with pytest.raises(ControlError, match="changed during.*compaction"):
                await kernel.engine.contexts.prepare(
                    bot, profile, CHANNEL, "final-source-race", [], force=True
                )
            return None
        return await kernel.engine.contexts.prepare(
            bot, profile, CHANNEL, "final-source-race", [], force=True
        )

    async with asyncio.TaskGroup() as group:
        group.create_task(concurrent_gateway_event())
        compaction = group.create_task(compact())
    assert snapshot_read.is_set() and changed.is_set()
    return compaction.result()


@pytest.mark.parametrize("mutation", ["edit", "deleted_marker", "physical_delete", "reply_arrival"])
async def test_source_change_after_final_recheck_read_prevents_promotion(kernel, monkeypatch, mutation):
    bot, profile = setup(kernel, monkeypatch)
    if mutation == "reply_arrival":
        kernel.store.execute("UPDATE messages SET reply_to='missing-reply-target' WHERE seq=1")
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return complete()

    def mutate():
        if mutation == "edit":
            kernel.store.execute(
                "UPDATE messages SET content='Corrected after final source read' WHERE seq=1"
            )
        elif mutation == "deleted_marker":
            kernel.store.execute("UPDATE messages SET deleted=1 WHERE seq=1")
        elif mutation == "physical_delete":
            kernel.store.execute("DELETE FROM messages WHERE seq=1")
        else:
            kernel.store.ingest(
                discord_id="missing-reply-target",
                channel_id=CHANNEL,
                room_id="council",
                author_id=OWNER_ID,
                author_name="The Boss",
                content="Reply target discovered after the source recheck",
            )

    await install_client(kernel, respond)
    await run_during_recheck(kernel, monkeypatch, bot, profile, mutate, rejected=True)
    active = kernel.store.context(bot["id"], CHANNEL)
    assert active["checkpoint"] == 0 and active["summary"] == "" and active["compactions"] == 0
    assert progress(kernel.store)["message_count"] == 20
    assert len(bodies) == 2
    assert not kernel.store.one("SELECT 1 FROM events WHERE kind='compaction.completed'")

    retried = []

    def retry(request):
        retried.append(json.loads(request.content))
        return complete()

    await install_client(kernel, retry)
    await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "after-source-race", [], force=True)
    assert sequences(retried[0])[0] == (2 if mutation == "physical_delete" else 1)
    first = source(retried[0])[0]
    if mutation == "edit":
        assert first["content"] == "Corrected after final source read"
    elif mutation == "deleted_marker":
        assert first["content"] == "[message deleted]"
    elif mutation == "reply_arrival":
        assert "Reply target discovered after the source recheck" in str(first["addressing"])
    assert kernel.store.context(bot["id"], CHANNEL)["checkpoint"] == (
        21 if mutation == "reply_arrival" else 20
    )
    assert progress(kernel.store) is None
    assert kernel.store.one("SELECT 1 FROM events WHERE kind='compaction.progress_invalidated'")


async def test_unrelated_append_during_final_recheck_promotes_and_keeps_new_input(kernel, monkeypatch):
    bot, profile = setup(kernel, monkeypatch)
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return complete()

    def append():
        ingest(kernel, discord_id="new-during-recheck", content="New input during final recheck")

    await install_client(kernel, respond)
    rows, _, tail, _ = await run_during_recheck(kernel, monkeypatch, bot, profile, append, rejected=False)
    assert rows == [] and tail == 20
    assert kernel.store.context(bot["id"], CHANNEL)["checkpoint"] == 20
    assert len(bodies) == 2
    assert progress(kernel.store) is None
    next_rows, _, next_tail, _ = await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "new-input", [])
    assert [row["seq"] for row in next_rows] == [21] and next_tail == 21
    assert next_rows[0]["content"] == "New input during final recheck"
    assert len(bodies) == 2
