"""Runtime scratch ownership, with mocked providers and no sandbox subprocesses."""

import asyncio
from dataclasses import replace
import json
from unittest.mock import Mock

import httpx
import pytest

from hortator.models import OWNER_ID
from hortator.plugins import ToolContext
from support.provider import install_client
from support.runtime import completion, settle
from support.runtime_feedback import ready, reply, responses, tool


CHANNEL = "222222222222222222"
FIRST = "555555555555555561"
SECOND = "555555555555555562"


def directed(kernel, bot, message_id, *, author_id=OWNER_ID, channel_id=CHANNEL):
    kernel.store.ingest(
        discord_id=message_id,
        channel_id=channel_id,
        room_id="council",
        author_id=author_id,
        author_name="Human fixture",
        content="Continue using the results already prepared.",
        addressing={
            "author_kind": "human",
            "live": True,
            "targets": [{"bot_id": bot["id"], "via": ["mention"]}],
        },
    )


@pytest.fixture
def lifecycle(kernel, monkeypatch):
    events = []
    runner = kernel.registry.agentic.runner
    monkeypatch.setattr(
        runner,
        "begin_turn",
        lambda context, *, owner_id=None: events.append(("begin", context.turn_id, owner_id)),
        raising=False,
    )
    monkeypatch.setattr(
        runner,
        "finish_turn",
        lambda context, *, preserve_for=None: events.append(("finish", context.turn_id, preserve_for)),
        raising=False,
    )
    monkeypatch.setattr(
        runner, "clear_bot", lambda bot_id: events.append(("clear", bot_id, None)), raising=False
    )
    monkeypatch.setattr(
        runner,
        "clear_conversation",
        lambda bot_id, channel_id: events.append(("clear_conversation", bot_id, channel_id)),
    )
    return events


def setup(kernel, *, human=True, protocol="progress", **changes):
    bot, transport = ready(
        kernel,
        response_protocol=protocol,
        enabled_plugins=["memory"],
        cooldown_seconds=0,
        interval_seconds=0,
        **changes,
    )
    if human:
        directed(kernel, bot, FIRST)
    return bot, transport


@pytest.mark.parametrize("outcome", ["sent", "silent", "failed"])
async def test_terminal_turn_always_releases_scratch(kernel, lifecycle, outcome):
    setup(kernel)

    def handle(request):
        if outcome == "failed":
            return httpx.Response(400, json={"error": "Rejected fixture"})
        return completion(silence=outcome == "silent")

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    turn = kernel.store.one("SELECT id,status FROM turns")
    assert turn["status"] == outcome
    assert lifecycle == [("begin", turn["id"], OWNER_ID), ("finish", turn["id"], None)]


async def test_same_human_followup_preserves_scratch_but_blocks_superseded_attachment(
    kernel, lifecycle, monkeypatch
):
    bot, transport = setup(kernel)
    original = kernel.registry.specs["memory"]
    artifact = None
    calls = []

    async def export_fixture(args, context, config, key):
        nonlocal artifact
        result = await original.handler(args, context, config, key)
        artifact = kernel.registry.artifact(b"MP3 fixture", "audio/mpeg", ".mp3", context)["artifact_id"]
        return {**result, "artifact_id": artifact, "posted": False}

    monkeypatch.setitem(kernel.registry.specs, "memory", replace(original, handler=export_fixture))
    deliver = kernel.engine.deliver

    async def supersede_before_dispatch(*args, **kwargs):
        if args[6]:
            assert args[6] == [artifact]
            assert kernel.registry.resolve_artifact(artifact, args[3]).is_file()
            directed(kernel, bot, SECOND)
        return await deliver(*args, **kwargs)

    monkeypatch.setattr(kernel.engine, "deliver", supersede_before_dispatch)

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return reply(tool("memory", {"operation": "write", "key": "export", "value": "saved"}))
        if len(calls) == 2:
            assert '"exported":1' in json.dumps(body, separators=(",", ":")).replace("\\", "")
            return reply(tool("discord_attach", {"artifact_ids": [artifact]}))
        assert "discord_attach" in {item["function"]["name"] for item in body.get("tools", [])}
        assert "attachment_state" not in str(body["messages"])
        if len(calls) == 3:
            return reply(tool("discord_attach", {"artifact_ids": [artifact]}))
        assert len(calls) == 4
        assert responses(body)[-1]["ok"] is False
        transport.send.assert_not_awaited()
        return completion("Answer to the updated request; the old artifact cannot be reused.")

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    first = kernel.store.one("SELECT id,status FROM turns")
    assert first["status"] == "cancelled"
    assert lifecycle == [("begin", first["id"], OWNER_ID), ("finish", first["id"], OWNER_ID)]
    transport.send.assert_not_awaited()
    await kernel.engine.tick()
    await settle(kernel)
    second = kernel.store.one("SELECT id FROM turns WHERE status='sent'")["id"]
    assert lifecycle[-2:] == [("begin", second, OWNER_ID), ("finish", second, None)]
    assert kernel.store.rows("SELECT status,artifacts FROM outbox WHERE turn_id=?", (first["id"],)) == [
        {"status": "suppressed", "artifacts": json.dumps([artifact], separators=(",", ":"))}
    ]
    assert all(
        json.loads(row["artifacts"]) == []
        for row in kernel.store.rows("SELECT artifacts FROM outbox WHERE turn_id=?", (second,))
    )
    transport.send.assert_awaited_once()
    assert len(calls) == 4


@pytest.mark.parametrize("other_scope", ["author", "channel"])
async def test_other_human_or_channel_cannot_replace_scratch_owner(kernel, lifecycle, other_scope):
    bot, transport = setup(kernel)
    other_channel = "999999999999999999"
    if other_scope == "channel":
        kernel.store.context(bot["id"], other_channel)
        kernel.store.remember_channel(other_channel, guild_id="111111111111111111", parent_id=CHANNEL)
        assert kernel.engine.channel_allowed(bot, other_channel)

    def handle(request):
        directed(
            kernel,
            bot,
            SECOND,
            author_id="777777777777777777" if other_scope == "author" else OWNER_ID,
            channel_id=other_channel if other_scope == "channel" else CHANNEL,
        )
        return completion("Finish the original human's answer.")

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    assert lifecycle[-1][0::2] == ("finish", None)
    transport.send.assert_awaited_once()


async def test_end_cycle_has_no_human_scratch_handoff(kernel, lifecycle):
    setup(kernel, protocol="end_cycle")
    await install_client(kernel, lambda request: completion(silence=True))
    await kernel.engine.tick()
    await settle(kernel)
    turn = kernel.store.one("SELECT id,status FROM turns")
    assert turn["status"] == "silent"
    assert lifecycle == [("begin", turn["id"], None), ("finish", turn["id"], None)]


async def test_invocation_cannot_claim_human_scratch_handoff(kernel, lifecycle):
    bot, _ = setup(kernel)
    kernel.engine.claim_attention(bot, CHANNEL, "turn-fixture")
    directed(kernel, bot, SECOND)
    assert (
        kernel.engine.shell_handoff_owner(
            {**bot, "invocation": {"request_id": "invocation-fixture"}},
            ToolContext(bot, CHANNEL, "turn-fixture"),
            OWNER_ID,
            None,
        )
        is None
    )


async def test_routine_turn_yielding_to_human_does_not_transfer_scratch(kernel, lifecycle):
    bot, transport = setup(kernel, human=False)

    def handle(request):
        directed(kernel, bot, SECOND)
        return completion("Stale routine draft")

    await install_client(kernel, handle)
    kernel.engine.launch(bot, CHANNEL)
    await settle(kernel)
    turn = kernel.store.one("SELECT id,status FROM turns")
    assert turn["status"] == "cancelled"
    assert lifecycle == [("begin", turn["id"], None), ("finish", turn["id"], None)]
    transport.send.assert_not_awaited()


@pytest.mark.parametrize("reason", ["Operator pause", "Configuration changed", "Shutdown"])
async def test_explicit_cancellation_destroys_scratch_even_with_new_human_input(kernel, lifecycle, reason):
    bot, transport = setup(kernel)
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def handle(request):
        entered.set()
        await hold.wait()
        raise AssertionError("Cancelled provider must not resume")

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await asyncio.wait_for(entered.wait(), 2)
    directed(kernel, bot, SECOND)
    await kernel.engine.cancel([bot["id"]], reason)
    await settle(kernel)
    turn = kernel.store.one("SELECT id,status FROM turns")
    assert turn["status"] == "cancelled"
    assert lifecycle == [
        ("begin", turn["id"], OWNER_ID),
        ("finish", turn["id"], None),
        ("clear", bot["id"], None),
    ]
    transport.send.assert_not_awaited()


async def test_idle_operator_cancellation_clears_pending_scratch(kernel, lifecycle):
    bot, _ = setup(kernel)

    def handle(request):
        directed(kernel, bot, SECOND)
        return completion("Superseded draft")

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    assert lifecycle[-1][0::2] == ("finish", OWNER_ID)
    assert bot["id"] not in kernel.engine.tasks
    await kernel.engine.cancel([bot["id"]], "Idle operator stop")
    assert lifecycle[-1] == ("clear", bot["id"], None)


async def test_simultaneous_configuration_change_and_followup_cannot_transfer_scratch(kernel, lifecycle):
    bot, _ = setup(kernel)

    def handle(request):
        directed(kernel, bot, SECOND)
        kernel.store.put("bots", {**bot, "name": "Updated configuration"})
        return completion("Stale configuration draft")

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "cancelled"
    assert lifecycle[-1][0::2] == ("finish", None)


@pytest.mark.parametrize("operator_cancel", [False, True])
async def test_cooldown_cancellation_marker_does_not_override_operator_cancel(
    kernel, lifecycle, monkeypatch, operator_cancel
):
    bot, _ = setup(kernel)
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def waiting_delivery(bot, profile, provider, context, *args, **kwargs):
        # The personal-cooldown scheduler owns this marker. Emulate its wait
        # without changing real cooldown/priority policy or waiting in a test.
        kernel.engine.delivery_waiting[bot["id"]] = context.turn_id
        entered.set()
        try:
            await hold.wait()
        finally:
            kernel.engine.delivery_waiting.pop(bot["id"], None)

    monkeypatch.setattr(kernel.engine, "deliver", waiting_delivery)
    await install_client(kernel, lambda request: completion())
    await kernel.engine.tick()
    await asyncio.wait_for(entered.wait(), 2)
    directed(kernel, bot, SECOND)
    await kernel.engine.tick()
    if operator_cancel:
        await kernel.engine.cancel([bot["id"]], "Operator stop during scheduler cancellation")
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "cancelled"
    if operator_cancel:
        assert lifecycle[-1] == ("clear", bot["id"], None)
    else:
        assert lifecycle[-1][0::2] == ("finish", OWNER_ID)


async def test_preparation_failure_clears_unadopted_pending_scratch(kernel, lifecycle, monkeypatch):
    bot, _ = setup(kernel)

    async def failed_preparation(*args, **kwargs):
        raise RuntimeError("Preparation failed before scratch adoption")

    monkeypatch.setattr(kernel.engine.contexts, "prepare", failed_preparation)
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "failed"
    assert lifecycle == [("clear_conversation", bot["id"], CHANNEL)]


@pytest.mark.parametrize("status", ["sending", "sent", "unknown"])
async def test_confirmed_or_uncertain_final_cannot_transfer_scratch(kernel, lifecycle, status):
    bot, _ = setup(kernel)
    kernel.engine.claim_attention(bot, CHANNEL, "turn-fixture")
    directed(kernel, bot, SECOND)
    kernel.store.execute(
        "INSERT INTO outbox(id,turn_id,bot_id,channel_id,content,status,created_at) VALUES(?,?,?,?,?,?,0)",
        ("out-fixture", "turn-fixture", bot["id"], CHANNEL, "Possibly sent", status),
    )
    assert (
        kernel.engine.shell_handoff_owner(
            bot, ToolContext(bot, CHANNEL, "turn-fixture"), OWNER_ID, "out-fixture"
        )
        is None
    )


async def test_preparation_failure_keeps_other_conversations_pending_scratch(kernel, monkeypatch):
    bot, _ = setup(kernel)
    runner = kernel.registry.agentic.runner
    current = Mock()
    unrelated = Mock()
    runner._scratch_pending[(bot["id"], CHANNEL)] = {"owner_id": OWNER_ID, "namespace": current}
    runner._scratch_pending[(bot["id"], "other-channel")] = {"owner_id": OWNER_ID, "namespace": unrelated}

    async def failed_preparation(*args, **kwargs):
        raise RuntimeError("Preparation failed before scratch adoption")

    monkeypatch.setattr(kernel.engine.contexts, "prepare", failed_preparation)
    await kernel.engine.tick()
    await settle(kernel)
    turn = kernel.store.one("SELECT status,error FROM turns")
    assert turn["status"] == "failed"
    assert "Preparation failed before scratch adoption" in turn["error"]
    current.close.assert_called_once_with()
    unrelated.close.assert_not_called()
    assert set(runner._scratch_pending) == {(bot["id"], "other-channel")}


async def test_explicit_bot_cancellation_releases_every_pending_conversation(kernel):
    bot, _ = setup(kernel)
    runner = kernel.registry.agentic.runner
    namespaces = [Mock(), Mock()]
    for channel, namespace in zip((CHANNEL, "other-channel"), namespaces, strict=True):
        runner._scratch_pending[(bot["id"], channel)] = {"owner_id": OWNER_ID, "namespace": namespace}
    await kernel.engine.cancel([bot["id"]], "Explicit operator stop")
    assert not runner._scratch_pending
    for namespace in namespaces:
        namespace.close.assert_called_once_with()
