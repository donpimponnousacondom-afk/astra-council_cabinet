"""Create real engine history against synthetic transports in a TEST-ONLY data directory.

Used only by Playwright. No Discord/provider network calls and no credentials are needed.
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx

from hortator.app import Kernel
from hortator.models import OWNER_ID


async def main(directory):
    if not str(directory).startswith("/tmp/hortator-e2e-"):
        raise SystemExit("This fixture accepts only /tmp/hortator-e2e-* test directories")
    k = Kernel(Path(directory))
    bot = k.store.get("bots", "ada")
    profile = k.store.get("profiles", "balanced")
    provider = k.store.get("providers", "openrouter")
    room = k.store.get("rooms", "council")
    for entity in (bot, profile, provider, room):
        entity.pop("revision")
    bot.update(enabled=True, application_id="333333333333333333", enabled_plugins=["memory"])
    profile.update(model="test/fixture-model")
    provider.update(requires_key=False, base_url="https://synthetic-provider.invalid/v1")
    room.update(guild_id="111111111111111111", channel_id="222222222222222222")
    for kind, entity in (("bots", bot), ("profiles", profile), ("providers", provider), ("rooms", room)):
        k.store.put(kind, entity)
    bot = k.store.get("bots", "ada")
    k.store.runtime("ada")
    k.store.ingest(
        discord_id="555555555555555555",
        channel_id=room["channel_id"],
        room_id="council",
        author_id=OWNER_ID,
        author_name="The Boss",
        content="Browser verification fixture: discuss observability.",
    )
    k.store.context("ada", room["channel_id"])
    request_count = 0

    async def handler(request):
        nonlocal request_count
        request_count += 1
        body = json.loads(request.content)
        if body["messages"][0]["content"].startswith("Summarize"):
            msg = {
                "content": "The Boss asked about observability. Ada suggested inspecting the timeline. This is synthetic verification data."
            }
        elif request_count == 1:
            msg = {
                "reasoning_content": "hidden fixture reasoning",
                "tool_calls": [
                    {
                        "id": "fixture-memory",
                        "type": "function",
                        "function": {
                            "name": "memory",
                            "arguments": '{"operation":"write","key":"fixture","value":"Inspect the timeline for observability."}',
                        },
                    }
                ],
            }
        else:
            msg = {
                "tool_calls": [
                    {
                        "id": "fixture-speak",
                        "type": "function",
                        "function": {
                            "name": "council_speak",
                            "arguments": '{"content":"We can inspect the full request timeline. [Synthetic test output]","reply_to":"555555555555555555"}',
                        },
                    }
                ]
            }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": msg, "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}
                ],
                "usage": {
                    "prompt_tokens": 1100,
                    "completion_tokens": 80,
                    "completion_tokens_details": {"reasoning_tokens": 20},
                    "cost": 0.001,
                },
            },
        )

    class SyntheticDiscord:
        async def send(self, bot, channel_id, content, reply_to, paths, nonce=None):
            return "666666666666666666"

    await k.pool.client.aclose()
    k.pool.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    k.engine.transport = SyntheticDiscord()
    try:
        k.store.emit(
            "verification.fixture", {"note": "Synthetic provider/Discord data, isolated from production."}
        )
        k.engine.launch(bot, room["channel_id"])
        await asyncio.gather(*list(k.engine.tasks.values()))
        await asyncio.sleep(0)
        assert k.store.one("SELECT status FROM turns")["status"] == "sent"
        k.engine.launch(bot, room["channel_id"], compact_only=True)
        await asyncio.gather(*list(k.engine.tasks.values()))
        assert k.store.context("ada", room["channel_id"])["compactions"] == 1
        # Prevent real gateway activity when the browser's API server opens this fixture.
        current = k.store.get("bots", "ada")
        current.pop("revision")
        current["enabled"] = False
        k.store.put("bots", current)
    finally:
        await k.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
