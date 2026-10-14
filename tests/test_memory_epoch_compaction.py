"""Memory promotion is part of successful context compaction, never a partial chunk."""

import asyncio
import json

import httpx
import pytest

from conftest import configured, ingest
from hortator.context import ContextBuilder
from hortator.models import ControlError
from support.provider import install_client


CHANNEL = "222222222222222222"
FINAL = "Finished retained summary."


def complete(text=FINAL, reason="stop"):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": reason}]})


def save_note(kernel, value):
    kernel.store.execute(
        "INSERT OR REPLACE INTO memories VALUES(?,?,?,?,?)", ("ada", CHANNEL, "topic", value, 1)
    )


def setup(kernel, monkeypatch):
    bot = configured(kernel, enabled_plugins=["memory"])
    for i in range(20):
        ingest(kernel, discord_id=str(600000000000000000 + i), content=f"Retain source fact {i}")
    profile = kernel.store.get("profiles", "balanced")
    profile.update(context_window=16000, summary_tokens=1000, keep_recent_messages=0)
    ordinary = ContextBuilder.estimate_request

    def measured(builder, messages, tools):
        if messages[0]["content"].startswith("Summarize this council conversation"):
            return 1000 + 1000 * len(json.loads(messages[-1]["content"]))
        return ordinary(builder, messages, tools)

    monkeypatch.setattr(ContextBuilder, "estimate_request", measured)
    save_note(kernel, "OLD_MEMORY")
    builder = kernel.engine.contexts
    original = builder.memory_prompt.prepare(bot, CHANNEL)
    save_note(kernel, "NEW_MEMORY")
    return bot, profile, builder, original


def saved(builder):
    return builder.memory_prompt.epochs.load("ada", CHANNEL)


async def test_final_fit_uses_fresh_notes_and_promotes_pin_with_summary(kernel, monkeypatch):
    bot, profile, builder, original = setup(kernel, monkeypatch)
    measured = ContextBuilder.estimate_request
    final_fits = []

    def fit(instance, messages, tools):
        content = json.dumps(messages)
        if FINAL in content and not messages[0]["content"].startswith("Summarize"):
            final_fits.append(content)
            # Explicit layout pressure: old snapshot plus its replacement would
            # not fit, while a consolidated snapshot does. This is not a token benchmark.
            if "OLD_MEMORY" in content and "NEW_MEMORY" in content:
                return profile["context_window"] + 100
        return measured(instance, messages, tools)

    monkeypatch.setattr(ContextBuilder, "estimate_request", fit)
    await install_client(kernel, lambda request: complete())
    rows, summary, _, meta = await builder.prepare(bot, profile, CHANNEL, "promote", [], force=True)
    assert summary == FINAL and not rows
    assert final_fits and all("OLD_MEMORY" not in text for text in final_fits)
    assert saved(builder)["epoch_id"] != original["epoch_id"]
    assert saved(builder) == meta["_memory_snapshot"]
    assert saved(builder)["notes"]["memory"]["topic"]["value"] == "NEW_MEMORY"
    assert kernel.store.context("ada", CHANNEL)["checkpoint"] == 20
    assert not kernel.store.rows("SELECT * FROM compaction_progress")
    _, current = await builder.assemble(bot, profile, CHANNEL, rows, summary)
    assert current["memory_snapshot"]["delta_keys"] == {"memory": []}


async def test_later_failed_chunk_keeps_pin_and_resume_promotes_once(kernel, monkeypatch):
    bot, profile, builder, original = setup(kernel, monkeypatch)
    calls = []

    def first_attempt(request):
        calls.append(json.loads(request.content))
        return complete("Saved first chunk.") if len(calls) == 1 else complete("unfinished", "length")

    await install_client(kernel, first_attempt)
    with pytest.raises(ControlError, match="finish_reason=length"):
        await builder.prepare(bot, profile, CHANNEL, "partial", [], force=True)
    assert len(calls) == 2 and saved(builder) == original
    progress = kernel.store.one("SELECT * FROM compaction_progress")
    assert progress["completed_batches"] == 1
    assert kernel.store.context("ada", CHANNEL)["checkpoint"] == 0
    resumed = []

    def finish(request):
        resumed.append(json.loads(request.content))
        return complete()

    await install_client(kernel, finish)
    await builder.prepare(bot, profile, CHANNEL, "resumed", [])
    assert len(resumed) == 1
    assert resumed[0]["messages"][-1] == calls[1]["messages"][-1]
    assert saved(builder)["epoch_id"] != original["epoch_id"]
    assert not kernel.store.rows("SELECT * FROM compaction_progress")


async def test_final_fit_failure_keeps_old_pin_and_completed_chunks(kernel, monkeypatch):
    bot, profile, builder, original = setup(kernel, monkeypatch)
    measured = ContextBuilder.estimate_request

    def too_large(instance, messages, tools):
        if FINAL in json.dumps(messages) and not messages[0]["content"].startswith("Summarize"):
            return profile["context_window"]
        return measured(instance, messages, tools)

    monkeypatch.setattr(ContextBuilder, "estimate_request", too_large)
    await install_client(kernel, lambda request: complete())
    with pytest.raises(ControlError, match="Compacted context still exceeds"):
        await builder.prepare(bot, profile, CHANNEL, "too-large", [], force=True)
    assert saved(builder) == original
    assert kernel.store.context("ada", CHANNEL)["checkpoint"] == 0
    assert kernel.store.one("SELECT message_count FROM compaction_progress")["message_count"] == 20


@pytest.mark.parametrize("change", ["note", "delta_template", "budget_template"])
async def test_write_during_final_validation_rejects_unmeasured_pin(kernel, monkeypatch, change):
    bot, profile, builder, original = setup(kernel, monkeypatch)
    validate = builder.validated_compaction_source_revision

    async def concurrent_write(*args):
        result = await validate(*args)
        if change == "note":
            save_note(kernel, "CONCURRENT_MEMORY")
        else:
            key = "runtime-memory-delta" if change == "delta_template" else "runtime-memory-budget"
            prompt = kernel.store.get("prompts", key)
            kernel.store.put("prompts", {**prompt, "content": prompt["content"] + " edited " * 100})
        return result

    monkeypatch.setattr(builder, "validated_compaction_source_revision", concurrent_write)
    await install_client(kernel, lambda request: complete())
    with pytest.raises(ControlError, match="Memory or its templates changed"):
        await builder.prepare(bot, profile, CHANNEL, "changed", [], force=True)
    assert saved(builder) == original
    assert kernel.store.context("ada", CHANNEL)["checkpoint"] == 0
    assert kernel.store.one("SELECT message_count FROM compaction_progress")["message_count"] == 20
    if change == "note":
        assert kernel.store.one("SELECT value FROM memories")["value"] == "CONCURRENT_MEMORY"


async def test_promotion_storage_failure_rolls_back_summary_pin_and_chunk_retirement(kernel, monkeypatch):
    bot, profile, builder, original = setup(kernel, monkeypatch)
    persist = builder.memory_prompt.epochs.save

    def fail_after_save(snapshot):
        persist(snapshot)
        raise RuntimeError("Simulated interrupted promotion")

    monkeypatch.setattr(builder.memory_prompt.epochs, "save", fail_after_save)
    await install_client(kernel, lambda request: complete())
    with pytest.raises(RuntimeError, match="Simulated interrupted promotion"):
        await builder.prepare(bot, profile, CHANNEL, "atomic", [], force=True)
    assert saved(builder) == original
    assert kernel.store.context("ada", CHANNEL)["checkpoint"] == 0
    assert kernel.store.one("SELECT message_count FROM compaction_progress")["message_count"] == 20


async def test_cancellation_after_saved_chunk_preserves_pin(kernel, monkeypatch):
    bot, profile, builder, original = setup(kernel, monkeypatch)
    waiting = asyncio.Event()
    calls = 0

    async def respond(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return complete("Saved first chunk.")
        waiting.set()
        await asyncio.Event().wait()

    await install_client(kernel, respond)
    async with asyncio.TaskGroup() as group:
        task = group.create_task(builder.prepare(bot, profile, CHANNEL, "cancelled", [], force=True))
        await asyncio.wait_for(waiting.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert saved(builder) == original
    assert kernel.store.one("SELECT completed_batches FROM compaction_progress")["completed_batches"] == 1
    assert kernel.store.context("ada", CHANNEL)["checkpoint"] == 0
