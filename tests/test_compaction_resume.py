"""Resumable compaction with real request construction and mocked provider I/O."""

import asyncio
import json

import httpx
import pytest

from conftest import configured, ingest
from hortator.compaction_progress import CompactionProgress
from hortator.context import ContextBuilder
from hortator.models import ControlError, OWNER_ID
from hortator.store import Store
from support.provider import install_client


CHANNEL = "222222222222222222"
ACCEPTED = "First chunk: the council agreed to preserve all source facts."
FINISHED = "Completed summary of the council facts."


def complete(text=FINISHED, finish_reason="stop"):
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": text}, "finish_reason": finish_reason}]},
    )


def progress(store, bot_id="ada", channel_id=CHANNEL):
    return store.one(
        "SELECT * FROM compaction_progress WHERE bot_id=? AND channel_id=?", (bot_id, channel_id)
    )


def source(body):
    return json.loads(body["messages"][-1]["content"])


def sequences(body):
    return [item["seq"] for item in source(body)]


def setup(kernel, monkeypatch, *, keep=0):
    bot = configured(kernel)
    for index in range(20):
        ingest(kernel, discord_id=str(600000000000000000 + index), content=f"Retain fact {index}.")
    profile = kernel.store.get("profiles", "balanced")
    profile.update(context_window=16000, summary_tokens=1000, keep_recent_messages=keep)
    original = ContextBuilder.estimate_request

    def measured(builder, messages, tools):
        if messages[0]["content"].startswith("Summarize this council conversation"):
            # Exercise the actual batching/bisection and HTTP bodies independently
            # of incidental prompt lengths and tokenizer revisions.
            return 1000 + 1000 * len(json.loads(messages[-1]["content"]))
        return original(builder, messages, tools)

    monkeypatch.setattr(ContextBuilder, "estimate_request", measured)
    return bot, profile


async def failed_after_first_chunk(kernel, bot, profile):
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return complete(ACCEPTED) if len(bodies) == 1 else complete("Truncated", "length")

    await install_client(kernel, respond)
    with pytest.raises(ControlError, match="finish_reason=length"):
        await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "first-attempt", [], force=True)
    assert len(bodies) == 2
    saved = progress(kernel.store)
    assert saved["summary"] == ACCEPTED
    assert saved["message_count"] == len(source(bodies[0]))
    assert saved["completed_batches"] == 1
    assert 0 < saved["message_count"] < 20
    active = kernel.store.context(bot["id"], CHANNEL)
    assert active["checkpoint"] == 0 and active["summary"] == "" and active["compactions"] == 0
    return bodies, saved


async def test_resume_survives_store_and_builder_recreation_without_repeating_accepted_input(
    kernel, monkeypatch
):
    bot, profile = setup(kernel, monkeypatch)
    failed, saved = await failed_after_first_chunk(kernel, bot, profile)
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return complete()

    await install_client(kernel, respond)
    reopened = Store(kernel.store.path)
    try:
        builder = ContextBuilder(reopened, kernel.pool)
        assert progress(reopened) == saved
        # Saved progress itself triggers resumption, even below the threshold.
        rows, summary, tail, _ = await builder.prepare(bot, profile, CHANNEL, "resumed", [])
        assert len(bodies) == 1
        assert sequences(bodies[0]) == sequences(failed[1])
        assert ACCEPTED in str(bodies[0]["messages"][:-1])
        assert not set(sequences(failed[0])) & set(sequences(bodies[0]))
        assert rows == [] and summary == FINISHED and tail == 20
        active = reopened.context(bot["id"], CHANNEL)
        assert active["checkpoint"] == 20 and active["compactions"] == 1
        assert progress(reopened) is None
        assert len(reopened.transcript(CHANNEL)) == 20
        resumed = reopened.one("SELECT data FROM events WHERE kind='compaction.resumed'")
        assert json.loads(resumed["data"])["completed_messages"] == saved["message_count"]
    finally:
        reopened.close()


@pytest.mark.parametrize("failure", ["truncated", "empty", "oversized", "transport"])
async def test_rejected_later_batch_never_replaces_last_accepted_chunk(kernel, monkeypatch, failure):
    bot, profile = setup(kernel, monkeypatch)
    _, saved = await failed_after_first_chunk(kernel, bot, profile)
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        if failure == "transport":
            raise httpx.ReadError("mock transport failed", request=request)
        return {
            "truncated": complete("New but unfinished", "length"),
            "empty": complete(""),
            "oversized": complete("overflow " * 2000),
        }[failure]

    await install_client(kernel, respond)
    with pytest.raises(ControlError):
        await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "rejected-resume", [], force=True)
    assert len(bodies) == 1
    assert min(sequences(bodies[0])) == saved["message_count"] + 1
    assert progress(kernel.store) == saved
    assert kernel.store.context(bot["id"], CHANNEL)["checkpoint"] == 0


async def test_cancellation_keeps_accepted_chunk_and_can_resume(kernel, monkeypatch):
    bot, profile = setup(kernel, monkeypatch)
    entered = asyncio.Event()
    bodies = []

    async def respond(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return complete(ACCEPTED)
        entered.set()
        await asyncio.Event().wait()

    await install_client(kernel, respond)
    async with asyncio.TaskGroup() as group:
        task = group.create_task(
            kernel.engine.contexts.prepare(bot, profile, CHANNEL, "cancelled-attempt", [], force=True)
        )
        await asyncio.wait_for(entered.wait(), 3)
        saved = progress(kernel.store)
        assert saved["summary"] == ACCEPTED
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert progress(kernel.store) == saved
    assert kernel.store.context(bot["id"], CHANNEL)["checkpoint"] == 0
    retried = []

    def finish(request):
        retried.append(json.loads(request.content))
        return complete()

    await install_client(kernel, finish)
    await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "after-cancel", [], force=True)
    assert len(retried) == 1 and sequences(retried[0]) == sequences(bodies[1])
    assert progress(kernel.store) is None


@pytest.mark.parametrize("change", ["edit", "delete", "prompt", "checkpoint", "summary_limit"])
async def test_changed_source_or_instructions_invalidates_saved_prefix(kernel, monkeypatch, change):
    bot, profile = setup(kernel, monkeypatch)
    _, saved = await failed_after_first_chunk(kernel, bot, profile)
    if change == "edit":
        kernel.store.execute("UPDATE messages SET content='Corrected source fact' WHERE seq=1")
    elif change == "delete":
        kernel.store.execute("UPDATE messages SET deleted=1 WHERE seq=1")
    elif change == "prompt":
        prompt = kernel.store.get("prompts", "runtime-compaction-instructions")
        prompt["content"] += "\nPreserve the corrected factual interpretation."
        prompt.pop("revision")
        kernel.store.put("prompts", prompt)
    elif change == "checkpoint":
        kernel.store.execute("UPDATE contexts SET checkpoint=1,summary='New active baseline'")
    else:
        profile["summary_tokens"] = saved["summary_tokens"] - 1
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return complete("Done")

    await install_client(kernel, respond)
    await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "changed-source", [], force=True)
    assert min(sequences(bodies[0])) == (2 if change == "checkpoint" else 1)
    assert ACCEPTED not in str(bodies[0]["messages"][:-1])
    if change == "edit":
        assert source(bodies[0])[0]["content"] == "Corrected source fact"
    elif change == "delete":
        assert source(bodies[0])[0]["content"] == "[message deleted]"
    elif change == "checkpoint":
        assert "New active baseline" in str(bodies[0]["messages"][:-1])
    assert kernel.store.one("SELECT 1 FROM events WHERE kind='compaction.progress_invalidated'")
    assert progress(kernel.store) is None


async def test_model_output_cap_and_larger_retained_limit_reuse_validated_work(kernel, monkeypatch):
    bot, profile = setup(kernel, monkeypatch)
    _, saved = await failed_after_first_chunk(kernel, bot, profile)
    profile.update(model="replacement-model", compaction_max_tokens=3000, summary_tokens=2000)
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return complete()

    await install_client(kernel, respond)
    await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "larger-output", [], force=True)
    assert bodies[0]["model"] == "replacement-model"
    assert bodies[0]["max_tokens"] == 3000
    assert ACCEPTED in str(bodies[0]["messages"][:-1])
    assert min(sequences(bodies[0])) == saved["message_count"] + 1
    assert [seq for body in bodies for seq in sequences(body)] == list(range(saved["message_count"] + 1, 21))
    assert progress(kernel.store) is None


async def test_appended_history_and_recent_tail_remain_after_resumption(kernel, monkeypatch):
    bot, profile = setup(kernel, monkeypatch, keep=2)
    _, saved = await failed_after_first_chunk(kernel, bot, profile)
    for index in range(3):
        ingest(kernel, discord_id=f"appended-{index}", content=f"New fact {index}.")
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return complete()

    await install_client(kernel, respond)
    rows, summary, tail, _ = await kernel.engine.contexts.prepare(
        bot, profile, CHANNEL, "appended-resume", [], force=True
    )
    assert [seq for body in bodies for seq in sequences(body)] == list(range(saved["message_count"] + 1, 22))
    assert [row["seq"] for row in rows] == [22, 23]
    assert summary == FINISHED and tail == 23
    assert kernel.store.context(bot["id"], CHANNEL)["checkpoint"] == 21
    assert len(kernel.store.transcript(CHANNEL)) == 23
    assert progress(kernel.store) is None


async def test_pending_directed_message_stays_verbatim_across_resumed_compaction(kernel, monkeypatch):
    bot, profile = setup(kernel, monkeypatch, keep=2)
    _, saved = await failed_after_first_chunk(kernel, bot, profile)
    kernel.store.ingest(
        discord_id="pending-directed",
        channel_id=CHANNEL,
        room_id="council",
        author_id=OWNER_ID,
        author_name="The Boss",
        content="Please answer this exact pending question",
        addressing={"author_kind": "human", "live": True, "targets": [{"bot_id": bot["id"]}]},
    )
    for index in range(3):
        ingest(kernel, discord_id=f"after-question-{index}", content=f"Later chatter {index}.")
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return complete()

    await install_client(kernel, respond)
    rows, _, tail, _ = await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "directed-resume", [])
    compacted = [seq for body in bodies for seq in sequences(body)]
    assert compacted == [*range(saved["message_count"] + 1, 21), 22]
    assert [row["seq"] for row in rows] == [21, 23, 24]
    assert rows[0]["content"] == "Please answer this exact pending question"
    assert tail == 24 and kernel.store.context(bot["id"], CHANNEL)["checkpoint"] == 22
    # A checkpoint beyond the queued question still cannot hide it on the next
    # turn; this is why resumable progress must not become an active checkpoint.
    later_rows, _, _, _ = await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "next-turn", [])
    assert [row["seq"] for row in later_rows] == [21, 23, 24]
    assert progress(kernel.store) is None


@pytest.mark.parametrize("change", ["edit", "delete", "prompt"])
async def test_midflight_source_change_prevents_stale_summary_promotion(kernel, monkeypatch, change):
    bot, profile = setup(kernel, monkeypatch)
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 2:
            if change == "prompt":
                prompt = kernel.store.get("prompts", "runtime-compaction-instructions")
                prompt["content"] += "\nKeep all corrected factual details."
                prompt.pop("revision")
                kernel.store.put("prompts", prompt)
            elif change == "edit":
                kernel.store.execute("UPDATE messages SET content='Changed during the request' WHERE seq=1")
            else:
                kernel.store.execute("UPDATE messages SET deleted=1 WHERE seq=1")
        return complete()

    await install_client(kernel, respond)
    with pytest.raises(ControlError, match="changed during compaction"):
        await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "source-race", [], force=True)
    assert kernel.store.context(bot["id"], CHANNEL)["checkpoint"] == 0
    assert kernel.store.context(bot["id"], CHANNEL)["summary"] == ""
    assert progress(kernel.store)["message_count"] == 20
    assert not kernel.store.one("SELECT 1 FROM events WHERE kind='compaction.completed'")
    retried = []

    def finish(request):
        retried.append(json.loads(request.content))
        return complete()

    await install_client(kernel, finish)
    await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "after-source-race", [], force=True)
    assert sequences(retried[0])[0] == 1
    assert kernel.store.context(bot["id"], CHANNEL)["checkpoint"] == 20
    assert kernel.store.one("SELECT 1 FROM events WHERE kind='compaction.progress_invalidated'")


async def test_midflight_append_is_left_for_next_turn_without_invalidating_accepted_work(kernel, monkeypatch):
    bot, profile = setup(kernel, monkeypatch)
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 2:
            ingest(kernel, discord_id="arrived-during-compaction", content="Please retain this new message")
        return complete()

    await install_client(kernel, respond)
    rows, _, tail, _ = await kernel.engine.contexts.prepare(
        bot, profile, CHANNEL, "new-arrival", [], force=True
    )
    assert rows == [] and tail == 20
    assert [seq for body in bodies for seq in sequences(body)] == list(range(1, 21))
    assert kernel.store.context(bot["id"], CHANNEL)["checkpoint"] == 20
    assert progress(kernel.store) is None
    next_rows, _, next_tail, _ = await kernel.engine.contexts.prepare(
        bot, profile, CHANNEL, "after-arrival", []
    )
    assert len(bodies) == 2
    assert [row["seq"] for row in next_rows] == [21] and next_tail == 21
    assert next_rows[0]["content"] == "Please retain this new message"


async def test_promotion_failure_rolls_back_checkpoint_and_reuses_all_finished_chunks(kernel, monkeypatch):
    bot, profile = setup(kernel, monkeypatch)
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return complete()

    def fail_clear(self):
        raise RuntimeError("Injected finalization failure")

    await install_client(kernel, respond)
    with monkeypatch.context() as promotion:
        promotion.setattr(CompactionProgress, "clear", fail_clear)
        with pytest.raises(RuntimeError, match="Injected finalization failure"):
            await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "promotion-failed", [], force=True)
    active = kernel.store.context(bot["id"], CHANNEL)
    assert active["checkpoint"] == 0 and active["summary"] == "" and active["compactions"] == 0
    assert progress(kernel.store)["message_count"] == 20
    assert len(bodies) == 2
    _, summary, _, _ = await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "promote-saved", [])
    assert len(bodies) == 2  # All source was summarized: promotion needs no new model request.
    assert summary == FINISHED
    assert kernel.store.context(bot["id"], CHANNEL)["checkpoint"] == 20
    assert progress(kernel.store) is None


async def test_final_context_budget_failure_reuses_finished_chunks_after_prompt_is_shortened(
    kernel, monkeypatch
):
    bot, profile = setup(kernel, monkeypatch)
    profile["response_tokens"] = 9000
    bot["persona"] = "Long ordinary generation persona. " * 2000
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return complete()

    await install_client(kernel, respond)
    with pytest.raises(ControlError, match="Compacted context still exceeds the model budget"):
        await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "too-large-context", [], force=True)
    assert len(bodies) == 2
    assert progress(kernel.store)["message_count"] == 20
    assert kernel.store.context(bot["id"], CHANNEL)["checkpoint"] == 0
    bot["persona"] = "Brief ordinary generation persona."
    _, summary, _, _ = await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "context-now-fits", [])
    assert len(bodies) == 2
    assert summary == FINISHED
    assert progress(kernel.store) is None
    assert kernel.store.context(bot["id"], CHANNEL)["checkpoint"] == 20


@pytest.mark.parametrize("channel_id", [CHANNEL, None])
async def test_clean_slate_removes_only_matching_compaction_progress(kernel, monkeypatch, owner, channel_id):
    bot, profile = setup(kernel, monkeypatch)
    await failed_after_first_chunk(kernel, bot, profile)
    # Other scopes are deliberately distinct; no operation may erase another
    # bot's progress, and a single-channel reset must preserve the other channel.
    kernel.store.execute(
        "INSERT INTO compaction_progress SELECT bot_id,?,basis,source_hash,message_count,summary,"
        "summary_tokens,completed_batches,request_id,updated_at FROM compaction_progress",
        ("another-channel",),
    )
    kernel.store.execute(
        "INSERT INTO compaction_progress SELECT ?,channel_id,basis,source_hash,message_count,summary,"
        "summary_tokens,completed_batches,request_id,updated_at FROM compaction_progress WHERE channel_id=?",
        ("socrates", CHANNEL),
    )
    data = {"confirm_bot_id": bot["id"]}
    if channel_id:
        data["channel_id"] = channel_id
    await kernel.service.control(
        owner, {"action": "reset_context", "kind": "bots", "id": bot["id"], "data": data}
    )
    assert progress(kernel.store) is None
    assert progress(kernel.store, bot_id="socrates") is not None
    assert (progress(kernel.store, channel_id="another-channel") is not None) == bool(channel_id)
    active = kernel.store.context(bot["id"], CHANNEL)
    assert active["summary"] == "" and active["checkpoint"] == 20
    ingest(kernel, discord_id="fresh-after-reset", content="Fresh after reset")
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return complete()

    await install_client(kernel, respond)
    await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "after-reset", [], force=True)
    assert len(bodies) == 1 and sequences(bodies[0]) == [21]
    assert ACCEPTED not in str(bodies[0]["messages"])
