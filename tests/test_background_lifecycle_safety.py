"""Cross-owner cancellation gates for background work and conversation turns."""

import asyncio
import time
from unittest.mock import AsyncMock

from conftest import configured, ingest
from hortator.plugins import ToolContext
from test_addressing import message
from test_provider import install_client
from test_runtime import completion


async def test_cancel_before_turn_entry_records_terminal_state(kernel):
    bot = configured(kernel)
    ingest(kernel)
    turn_id = kernel.engine.launch(bot, "222222222222222222")
    await kernel.engine.cancel([bot["id"]], "Test cancellation before first await")
    await asyncio.sleep(0)
    assert kernel.store.one("SELECT status FROM turns WHERE id=?", (turn_id,))["status"] == "cancelled"
    assert not kernel.engine.tasks and not kernel.jobs.origin_gates


async def test_clean_slate_joins_job_turn_and_typing_before_cutoff(kernel, owner):
    bot = configured(kernel, interval_seconds=0, enabled_plugins=["memory"])
    ingest(kernel)
    job_entered, job_exited = asyncio.Event(), asyncio.Event()
    model_entered, model_exited = asyncio.Event(), asyncio.Event()
    typing_entered, typing_exited = asyncio.Event(), asyncio.Event()

    async def work(_):
        job_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            job_exited.set()

    async def model(_):
        model_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            model_exited.set()
        return completion()

    async def typing(*_, **__):
        typing_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            typing_exited.set()

    kernel.jobs.register("memory", run=work, check=lambda _: None)
    kernel.engine.transport = AsyncMock()
    kernel.engine.transport.typing.side_effect = typing
    await install_client(kernel, model)
    context = ToolContext(bot, "222222222222222222", "origin")
    job_id = kernel.jobs.submit(context, "memory", "reset", {}, seconds=60)["job_id"]
    await job_entered.wait()
    kernel.engine.launch(bot, context.channel_id, human_directed=True)
    await asyncio.wait_for(asyncio.gather(model_entered.wait(), typing_entered.wait()), 5)

    receipt = await kernel.service.control(
        owner,
        {"action": "reset_context", "kind": "bots", "id": bot["id"], "data": {"confirm_bot_id": bot["id"]}},
    )

    assert receipt["after_at"] > 0
    assert job_exited.is_set() and model_exited.is_set() and typing_exited.is_set()
    assert not kernel.jobs.tasks and not kernel.engine.tasks
    job = kernel.jobs.get(job_id)
    assert job["state"] == "cancelled" and job["notification"] != "pending"
    assert kernel.store.one("SELECT status FROM turns")["status"] == "cancelled"
    kernel.engine.transport.send.assert_not_awaited()
    await kernel.engine.tick()
    assert not kernel.engine.tasks


async def test_human_supersession_preserves_independent_worker(kernel):
    bot = configured(kernel, interval_seconds=0, cooldown_seconds=30, enabled_plugins=["memory"])
    ingest(kernel)
    entered, exited = asyncio.Event(), asyncio.Event()

    async def work(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    kernel.jobs.register("memory", run=work, check=lambda _: None)
    context = ToolContext(bot, "222222222222222222", "origin")
    job_id = kernel.jobs.submit(context, "memory", "human", {}, seconds=60)["job_id"]
    await entered.wait()
    kernel.engine.transport = AsyncMock()
    kernel.engine.transport.send.return_value = "777777777777777777"
    await install_client(kernel, lambda _: completion("Draft awaiting cooldown"))
    kernel.store.execute("UPDATE bot_runtime SET last_sent=? WHERE bot_id=?", (time.time(), bot["id"]))
    turn_id = kernel.engine.launch(bot, context.channel_id)
    try:
        async with asyncio.timeout(5):
            while kernel.engine.delivery_waiting.get(bot["id"]) != turn_id:
                await asyncio.sleep(0.01)
        await kernel.connector.receive(bot["id"], message(555555555555555556, mentions=[bot]))
        await kernel.engine.tick()
        assert job_id in kernel.jobs.tasks and not exited.is_set()
        assert kernel.jobs.get(job_id)["state"] == "running"
        assert kernel.jobs.get(job_id)["notification"] == "none"
    finally:
        await kernel.jobs.cancel_job(kernel.jobs.get(job_id), "Test cleanup")


async def test_scope_change_joins_job_and_revokes_followup(kernel, owner):
    bot = configured(kernel, interval_seconds=0, enabled_plugins=["memory"])
    ingest(kernel)
    entered, exited = asyncio.Event(), asyncio.Event()

    async def work(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    kernel.jobs.register("memory", run=work, check=lambda _: None)
    context = ToolContext(bot, "222222222222222222", "origin")
    job_id = kernel.jobs.submit(context, "memory", "scope", {}, seconds=60)["job_id"]
    await entered.wait()

    await kernel.service.save(owner, "rooms", "council", {"channel_id": "333333333333333334"})

    job = kernel.jobs.get(job_id)
    assert exited.is_set() and not kernel.jobs.tasks
    assert job["state"] == "cancelled" and job["notification"] != "pending"
    assert kernel.jobs.pending(kernel.store.get("bots", bot["id"])) is None
    await kernel.engine.tick()
    assert not kernel.engine.tasks
