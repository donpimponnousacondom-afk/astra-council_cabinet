import asyncio
import time

import pytest

from conftest import configured, ingest
from hortator.models import ControlError
from hortator.security import Actor

CHANNEL = "222222222222222222"


async def reset(k, owner, bot="ada", **data):
    return await k.service.control(
        owner, {"action": "reset_context", "kind": "bots", "id": bot, "data": {"confirm_bot_id": bot, **data}}
    )


async def test_reset_excludes_backfill_and_reply_previews_but_keeps_notes_other_bots(kernel, owner):
    bot = configured(kernel)
    ingest(kernel, content="old poisoned conversation")
    kernel.store.context("socrates", CHANNEL)
    kernel.store.execute("UPDATE contexts SET summary='poisoned summary',compactions=3")
    kernel.store.execute("INSERT INTO memories VALUES(?,?,?,?,?)", ("ada", CHANNEL, "keep", "remember me", 1))
    kernel.store.execute(
        "INSERT INTO global_memories VALUES(?,?,?,?,?)", ("ada", "keep", "global note", CHANNEL, 1)
    )
    before_bot = kernel.store.get("bots", "ada")
    receipt = await reset(kernel, owner)
    assert kernel.store.get("bots", "ada") == before_bot
    assert kernel.store.context("ada", CHANNEL)["summary"] == ""
    assert kernel.store.context("socrates", CHANNEL)["summary"] == "poisoned summary"
    assert kernel.store.one("SELECT value FROM memories")["value"] == "remember me"
    assert kernel.store.one("SELECT value FROM global_memories")["value"] == "global note"
    # History arriving after the reset is still old by its original Discord timestamp.
    kernel.store.ingest(
        discord_id="666666666666666666",
        channel_id=CHANNEL,
        author_id="human",
        author_name="human",
        content="late old poison",
        at=receipt["after_at"] - 60,
    )
    kernel.store.ingest(
        discord_id="777777777777777777",
        channel_id=CHANNEL,
        author_id="human",
        author_name="human",
        content="fresh question",
        reply_to="555555555555555555",
        at=time.time(),
    )
    rows = kernel.store.transcript(CHANNEL)
    conversation = kernel.engine.contexts.conversation(rows, bot)
    assert [r["content"] for r in conversation] == ["fresh question"]
    assert "old poisoned" not in str(conversation)
    assert "preview_omitted" in conversation[0]["addressing"]["reply_target"]
    assert len(kernel.engine.contexts.conversation(rows, {"id": "socrates"})) == 3
    profile = kernel.store.get("profiles", "balanced")
    rows, summary, _, _ = await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "test", [])
    assert [r["content"] for r in rows] == ["fresh question"]
    assert summary == ""
    view = kernel.service.context("ada", CHANNEL)
    assert view["message_count"] == 1
    assert view["reset_boundary"]["after_at"] == receipt["after_at"]
    assert len(kernel.store.transcript(CHANNEL)) == 3


async def test_all_channels_cutoff_applies_to_future_contexts_and_is_durable(kernel, owner):
    bot = configured(kernel, evaluate_when_idle=True)
    ingest(kernel)
    receipt = await reset(kernel, owner)
    assert kernel.engine.pick_channel(bot) is None
    kernel.store.ingest(
        discord_id="history",
        channel_id="future",
        author_id="human",
        author_name="human",
        content="historical",
        at=receipt["after_at"] - 100,
    )
    kernel.store.context("ada", "future")
    assert kernel.engine.contexts.conversation(kernel.store.transcript("future"), bot) == []
    from hortator.store import Store

    reopened = Store(kernel.store.path)
    try:
        assert not reopened.after_context_reset("ada", reopened.transcript("future")[0])
    finally:
        reopened.close()


async def test_single_channel_reset_preserves_other_context(kernel, owner):
    bot = configured(kernel)
    ingest(kernel)
    ingest(kernel, channel_id="other", discord_id="other", content="other context")
    await reset(kernel, owner, channel_id=CHANNEL)
    assert (
        kernel.engine.contexts.conversation(kernel.store.transcript("other"), bot)[0]["content"]
        == "other context"
    )


async def test_reset_drains_and_blocks_new_turns_before_cutoff(kernel, owner, monkeypatch):
    bot = configured(kernel)
    ingest(kernel)
    entered = asyncio.Event()
    drained = asyncio.Event()

    async def cancel(*args):
        entered.set()
        assert not kernel.engine.available(bot, manual=True)
        await asyncio.sleep(0)
        ingest(kernel, discord_id="during-cancel", content="cancelled draft")
        drained.set()

    monkeypatch.setattr(kernel.engine, "cancel", cancel)
    await reset(kernel, owner)
    assert entered.is_set() and drained.is_set()
    assert kernel.engine.available(bot)
    assert not kernel.engine.resetting
    assert kernel.engine.contexts.conversation(kernel.store.transcript(CHANNEL), bot) == []


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"confirm_bot_id": "socrates"},
        {"confirm_bot_id": "ada", "channel_id": "*"},
        {"confirm_bot_id": "ada", "clear_channel_notes": True},
    ],
)
async def test_reset_requires_exact_explicit_confirmation_and_known_fields(kernel, owner, data):
    configured(kernel)
    with pytest.raises(ControlError):
        await kernel.service.control(
            owner, {"action": "reset_context", "kind": "bots", "id": "ada", "data": data}
        )
    assert not kernel.store.rows("SELECT * FROM context_resets")


async def test_reset_is_not_available_to_model_inspection_or_other_people(kernel):
    configured(kernel)
    with pytest.raises(ControlError):
        await reset(kernel, Actor("discord", "other-person"))
    assert not kernel.store.rows("SELECT * FROM context_resets")


async def test_slash_prompt_remains_fresh_after_all_channel_reset(kernel, owner):
    from test_slash_commands import enable, interaction
    from test_provider import install_client
    from test_runtime import completion, settle

    bot = enable(kernel)
    await reset(kernel, owner)
    bodies = []

    def response(request):
        import json

        bodies.append(json.loads(request.content))
        return completion("Fresh slash answer")

    await install_client(kernel, response)
    message = interaction(bot, prompt="Fresh prompt after reset")
    await kernel.connector.slash.receive(bot["id"], message)
    await settle(kernel)
    assert "Fresh prompt after reset" in str(bodies)
    assert kernel.store.one("SELECT status FROM slash_invocations")["status"] == "sent"


async def test_pending_slash_ack_cannot_start_old_work_after_reset(kernel, owner):
    from test_slash_commands import enable, interaction
    from unittest.mock import AsyncMock

    bot = enable(kernel)
    message = interaction(bot)
    entered, released = asyncio.Event(), asyncio.Event()

    async def defer(**kwargs):
        entered.set()
        await released.wait()

    message.response.defer = AsyncMock(side_effect=defer)
    async with asyncio.TaskGroup() as group:
        group.create_task(kernel.connector.slash.receive(bot["id"], message))
        await entered.wait()
        await reset(kernel, owner)
        released.set()
    assert kernel.store.one("SELECT status,error FROM slash_invocations")["status"] == "rejected"
    assert not kernel.store.rows("SELECT * FROM requests")
    assert not kernel.engine.tasks
