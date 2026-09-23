"""Shared background handoff test helpers."""

from unittest.mock import AsyncMock

from support.research_assistant import setup
from support.research_fanout import set_limit


def configure(kernel, count):
    setup(kernel)
    bot = kernel.store.get("bots", "ada")
    kernel.store.put(
        "bots",
        {
            **bot,
            "interval_seconds": 1,
            "max_tool_rounds": 3,
            "max_calls_per_round": count,
            "tool_working_set_tokens": 6000,
            "cooldown_seconds": 0,
        },
    )
    set_limit(kernel, max(4, count))
    kernel.engine.transport = AsyncMock()
    kernel.engine.transport.send.return_value = "777777777777777777"
