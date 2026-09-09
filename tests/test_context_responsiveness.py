import asyncio
import json
import threading
import time
from datetime import datetime

import httpx
import pytest

from conftest import configured, ingest
from test_provider import install_client
from hortator.models import ControlError
from hortator.timekeeping import local_timestamp, present_times


async def test_compaction_preparation_yields_and_preserves_every_message(kernel, monkeypatch):
    bot = configured(kernel)
    for i in range(300):
        ingest(kernel, discord_id=str(600000000000000000 + i), content=f"Fact {i}. " * 25)
    profile = kernel.store.get("profiles", "balanced")
    profile.update(context_window=500000, keep_recent_messages=0)
    context = kernel.engine.contexts
    owner_thread = threading.get_ident()
    original = context.estimate_request
    calls = []

    def slow_estimate(*args):
        calls.append(threading.get_ident())
        time.sleep(0.025)
        return original(*args)

    monkeypatch.setattr(context, "estimate_request", slow_estimate)
    seen = []

    async def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "All facts retained in source."}, "finish_reason": "stop"}
                ]
            },
        )

    await install_client(kernel, respond)
    gaps, done = [], asyncio.Event()

    async def ticker():
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.005)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    async with asyncio.TaskGroup() as group:
        tick = group.create_task(ticker())
        try:
            await context.prepare(bot, profile, "222222222222222222", "responsiveness", [], force=True)
        finally:
            done.set()
    assert len(gaps) >= 10 and max(gaps) < 0.5
    assert calls and all(thread != owner_thread for thread in calls)
    assert len(calls) < 15  # No repeated full-prefix scan over 300 messages.
    transcript = json.loads(seen[0]["messages"][-1]["content"])
    assert len(transcript) == 300 and len(kernel.store.rows("SELECT seq FROM messages")) == 300
    assert kernel.store.context(bot["id"], "222222222222222222")["checkpoint"] == 300
    assert tick.done()


async def test_reasoning_exhaustion_reports_actual_usage_and_keeps_old_context(kernel):
    bot = configured(kernel)
    ingest(kernel)
    profile = kernel.store.get("profiles", "balanced")
    profile["summary_tokens"] = 1024
    await install_client(
        kernel,
        lambda request: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"reasoning_content": "private diagnostic fixture", "content": ""},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"completion_tokens": 1024, "completion_tokens_details": {"reasoning_tokens": 1024}},
            },
        ),
    )
    with pytest.raises(ControlError) as caught:
        await kernel.engine.contexts.prepare(
            bot, profile, "222222222222222222", "reasoning-cap", [], force=True
        )
    assert "finish_reason=length" in str(caught.value)
    assert "output_tokens=1024, reasoning_tokens=1024" in str(caught.value)
    assert "visible_chars=0" in str(caught.value)
    assert kernel.store.context(bot["id"], "222222222222222222")["checkpoint"] == 0
    assert "private diagnostic fixture" not in str(kernel.store.events())


async def test_cancel_during_worker_preparation_never_commits_or_calls_provider(kernel, monkeypatch):
    bot = configured(kernel)
    ingest(kernel)
    context = kernel.engine.contexts
    entered, release = threading.Event(), threading.Event()
    original = context.compaction_batch

    def held(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(context, "compaction_batch", held)
    async with asyncio.TaskGroup() as group:
        task = group.create_task(
            context.prepare(
                bot,
                kernel.store.get("profiles", "balanced"),
                "222222222222222222",
                "cancel-prepare",
                [],
                force=True,
            )
        )
        try:
            async with asyncio.timeout(3):
                while not entered.is_set():
                    await asyncio.sleep(0.005)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
    assert not kernel.store.rows("SELECT id FROM requests")
    assert kernel.store.context(bot["id"], "222222222222222222")["checkpoint"] == 0


async def test_bisected_batches_keep_all_source_rows_when_later_summary_fails(kernel, monkeypatch):
    bot = configured(kernel)
    for i in range(20):
        ingest(kernel, discord_id=str(600000000000000000 + i), content=f"Retain fact {i}.")
    context = kernel.engine.contexts
    profile = kernel.store.get("profiles", "balanced")
    profile.update(context_window=16000, summary_tokens=1000, keep_recent_messages=0)
    original = context.estimate_request

    def measured(messages, tools):
        if messages[0]["content"].startswith("Summarize this council conversation"):
            # Force a real bisection without coupling the test to prompt prose/tokenizer revisions.
            return 1000 + 1000 * len(json.loads(messages[-1]["content"]))
        return original(messages, tools)

    monkeypatch.setattr(context, "estimate_request", measured)
    batches = []

    async def response(request):
        body = json.loads(request.content)
        batches.append(json.loads(body["messages"][-1]["content"]))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "Incomplete" if len(batches) > 1 else "First portion summarized."
                        },
                        "finish_reason": "length" if len(batches) > 1 else "stop",
                    }
                ]
            },
        )

    await install_client(kernel, response)
    with pytest.raises(ControlError, match="finish_reason=length"):
        await context.prepare(bot, profile, "222222222222222222", "later-batch-fails", [], force=True)
    assert len(batches) == 2
    assert [item["seq"] for batch in batches for item in batch] == list(range(1, 21))
    assert kernel.store.context(bot["id"], "222222222222222222")["checkpoint"] == 0
    assert len(kernel.store.rows("SELECT seq FROM messages")) == 20
    prepared = [e["data"] for e in kernel.store.events() if e["kind"] == "compaction.batch_prepared"]
    assert any(p["tokenization_probes"] > 1 for p in prepared)


async def test_transcript_clock_and_tool_metadata_use_configured_zone_without_rewriting_sources(kernel):
    bot = configured(kernel)
    ingest(kernel)
    at = datetime.fromisoformat("2026-09-08T23:46:00+00:00").timestamp()
    kernel.store.execute("UPDATE messages SET at=?", (at,))
    rows = kernel.store.transcript("222222222222222222")
    transcript = kernel.engine.contexts.conversation(rows, bot)
    assert transcript[0]["at"] == "2026-09-09T01:46:00+02:00"
    assert kernel.store.one("SELECT at FROM messages")["at"] == at
    profile = kernel.store.get("profiles", "balanced")
    dynamic = kernel.engine.contexts.dynamic(bot, profile, "222222222222222222", 0, 100)
    assert "Europe/Madrid" in dynamic and "offset twice" in dynamic
    source = {"at": at, "body": {"at": at}, "content": "Old note: 23:46 UTC", "next_at": 0}
    shown = present_times(source, "Europe/Madrid")
    assert shown["at"] == transcript[0]["at"]
    assert shown["body"] == source["body"] and shown["content"] == source["content"] and shown["next_at"] == 0
    assert local_timestamp("2026-01-08T23:46:00Z") == "2026-01-09T00:46:00+01:00"
    assert local_timestamp("2026-09-08T23:46:00Z", "Asia/Tokyo") == "2026-09-09T08:46:00+09:00"
