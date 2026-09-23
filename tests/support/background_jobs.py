"""Shared background jobs test helpers."""

from unittest.mock import AsyncMock

from conftest import configured, ingest

from hortator.plugins import ToolContext


def setup(k, run):
    bot = configured(k, interval_seconds=0, enabled_plugins=["memory"])
    ingest(k)
    k.jobs.register("memory", run=run, check=lambda _: None)
    k.engine.transport = AsyncMock()
    k.engine.transport.send.return_value = "777777777777777777"
    return ToolContext(bot, "222222222222222222", "origin")


async def finish(k):
    tasks = list(k.jobs.tasks.values())
    for task in tasks:
        await task
