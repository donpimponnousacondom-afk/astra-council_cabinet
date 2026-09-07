import pytest

from hortator.app import Kernel
from hortator.security import Actor
from hortator.models import OWNER_ID


@pytest.fixture
async def kernel(tmp_path):
    k = Kernel(tmp_path)
    yield k
    await k.close()


@pytest.fixture
def owner():
    return Actor("dashboard", OWNER_ID)


def configured(k, bot_id="ada", **changes):
    provider = k.store.get("providers", "openrouter")
    provider.update(requires_key=False, base_url="https://provider.test/v1")
    provider.pop("revision")
    k.store.put("providers", provider)
    profile = k.store.get("profiles", "balanced")
    profile.update(model="test-model")
    profile.pop("revision")
    k.store.put("profiles", profile)
    room = k.store.get("rooms", "council")
    room.update(guild_id="111111111111111111", channel_id="222222222222222222", send_gap_seconds=.02)
    room.pop("revision")
    k.store.put("rooms", room)
    bot = k.store.get("bots", bot_id)
    bot.pop("revision")
    bot.update(enabled=True, application_id="333333333333333333" if bot_id == "ada" else "444444444444444444", **changes)
    k.vault.put(f"bot/{bot_id}/token", "fake-token-for-tests-only")
    k.store.put("bots", bot)
    k.store.runtime(bot_id)
    k.store.execute("UPDATE bot_runtime SET gateway_status='online' WHERE bot_id=?", (bot_id,))
    return k.store.get("bots", bot_id)


def ingest(k, bot_id="ada", content="Hello council", discord_id="555555555555555555", channel_id="222222222222222222"):
    k.store.ingest(discord_id=discord_id, channel_id=channel_id, room_id="council", author_id="1482143139828596916", author_name="The Boss", content=content)
    k.store.context(bot_id, channel_id)
