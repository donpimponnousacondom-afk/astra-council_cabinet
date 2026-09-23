"""Shared provider test helpers."""

import asyncio

import httpx


class Fragments(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        for start in range(0, len(self.data), 7):
            yield self.data[start : start + 7]
            await asyncio.sleep(0)


async def install_client(k, handler):
    await k.pool.client.aclose()
    k.pool.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def call_args(k, bot):
    return dict(
        bot=bot,
        profile=k.store.get("profiles", "balanced"),
        messages=[{"role": "system", "content": "test"}],
        tools=[],
        turn_id="turn-test",
        context={"estimated_tokens": 30},
    )
