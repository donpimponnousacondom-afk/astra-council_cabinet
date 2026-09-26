"""Bot-wide ledger operations keep delivery independent of the caller's channel."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from conftest import configured, ingest
from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.store import uid
from support.provider import install_client
from support.runtime import completion, settle

ROOM = "222222222222222222"
DM = "666666666666666666"
ARGS = {"operation": "schedule", "key": "usage", "message": "Check usage", "after_seconds": 3600}


def setup(k):
    bot = configured(k, interval_seconds=0, enabled_plugins=["secretary"])
    k.store.put("plugins", {**k.store.get("plugins", "secretary"), "enabled": True})
    ingest(k)
    k.store.execute("UPDATE channels SET guild_id='111111111111111111' WHERE id=?", (ROOM,))
    k.store.remember_owner_dm(bot, DM)
    k.engine.transport = AsyncMock()
    k.engine.transport.send.return_value = "777777777777777777"
    return ToolContext(bot, ROOM, "origin"), ToolContext(bot, DM, "private")


async def call(k, context, **args):
    return await k.registry.secretary.apply(args, context)


async def test_all_contexts_share_ledger_and_keys_but_keep_delivery(kernel):
    room, dm = setup(kernel)
    saved = await call(kernel, room, **ARGS)
    private = await call(kernel, dm, **{**ARGS, "key": "private", "message": "Private task"})
    slash = ToolContext({**room.bot, "invocation": {"kind": "slash"}}, "slash:ada:outside", "listing")
    for context in (room, dm, slash):
        listing = await call(kernel, context, operation="list")
        assert listing["scope"] == "bot" and listing["total"] == 2
        assert {r["destination"]["channel_id"] for r in listing["reminders"]} == {ROOM, DM}
        recovered = await call(kernel, context, **ARGS)
        assert recovered["id"] == saved["id"] and recovered["due_at"] == saved["due_at"]
        assert (await call(kernel, context, operation="status", reminder_id=private["id"]))[
            "message"
        ] == "Private task"
    kernel.engine.transport.owner_dm.assert_not_awaited()
    await call(kernel, dm, operation="snooze", reminder_id=saved["id"], after_seconds=7200)
    edited = await call(kernel, dm, operation="update", reminder_id=saved["id"], message="Check usage again")
    assert edited["channel_id"] == ROOM
    cancelled = await call(kernel, room, operation="cancel", reminder_id=private["id"])
    assert cancelled["channel_id"] == DM and cancelled["state"] == "cancelled"
    assert len(kernel.registry.secretary.inventory()) == 2


async def test_explicit_destinations_move_delivery_and_preserve_due_time(kernel):
    room, dm = setup(kernel)
    saved = await call(kernel, room, **ARGS, destination="owner_dm")
    assert saved["channel_id"] == DM
    targets = await call(kernel, dm, operation="destinations")
    assert [row["channel_id"] for row in targets["channels"]] == [ROOM]
    assert targets["channels"][0]["label"]
    with pytest.raises(ControlError, match="already exists at another destination"):
        await call(kernel, dm, **ARGS, destination="channel", channel_id=ROOM)
    moved = await call(
        kernel, dm, operation="update", reminder_id=saved["id"], destination="channel", channel_id=ROOM
    )
    assert moved["channel_id"] == ROOM and moved["due_at"] == saved["due_at"]
    assert moved["state"] == "scheduled" and moved["id"] == saved["id"]
    kernel.store.execute("UPDATE secretary_reminders SET due_at=?", (time.time() - 1,))
    await install_client(kernel, lambda _: completion("Time to check usage."))
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.engine.transport.send.call_args.args[1] == ROOM
    fired = await call(kernel, room, operation="update", reminder_id=saved["id"], destination="owner_dm")
    assert fired["state"] == "fired"  # A move never implicitly rearms a consumed alarm.


@pytest.mark.parametrize("operation", ["status", "snooze", "update", "cancel"])
async def test_global_still_excludes_other_bots(kernel, operation):
    room, _ = setup(kernel)
    saved = await call(kernel, room, **ARGS)
    other = ToolContext({**room.bot, "id": "socrates"}, ROOM, "other")
    args = {"operation": operation, "reminder_id": saved["id"]}
    args.update(
        {"after_seconds": 60}
        if operation == "snooze"
        else {"message": "Changed"}
        if operation == "update"
        else {}
    )
    with pytest.raises(ControlError, match="not found"):
        await call(kernel, other, **args)
    assert kernel.registry.secretary.listing("socrates", {})["total"] == 0


async def test_revoked_destination_is_visible_and_cancellable_but_cannot_be_rearmed(kernel):
    room, dm = setup(kernel)
    saved = await call(kernel, room, **ARGS)
    kernel.store.put("bots", {**room.bot, "room_ids": []})
    dm.bot = kernel.store.get("bots", room.bot["id"])
    assert (await call(kernel, dm, operation="list"))["total"] == 1
    assert (await call(kernel, dm, operation="destinations"))["channels"] == []
    with pytest.raises(ControlError, match="configured scope"):
        await call(kernel, dm, operation="snooze", reminder_id=saved["id"], after_seconds=60)
    with pytest.raises(ControlError, match="permitted channel"):
        await call(kernel, dm, **{**ARGS, "key": "new"}, destination="channel", channel_id=ROOM)
    assert (await call(kernel, dm, operation="cancel", reminder_id=saved["id"]))["state"] == "cancelled"


async def test_destination_resolution_does_not_overwrite_concurrent_cancel(kernel):
    room, _ = setup(kernel)
    saved = await call(kernel, room, **ARGS)
    kernel.store.execute("DELETE FROM owner_dm_channels")

    async def resolve(bot):
        await call(kernel, room, operation="cancel", reminder_id=saved["id"])
        kernel.store.remember_owner_dm(bot, DM)
        return DM

    kernel.engine.transport.owner_dm.side_effect = resolve
    with pytest.raises(ControlError, match="changed while resolving"):
        await call(kernel, room, operation="update", reminder_id=saved["id"], destination="owner_dm")
    row = kernel.registry.secretary.get(saved["id"])
    assert row["channel_id"] == ROOM and row["state"] == "cancelled"


async def test_captured_context_cannot_rearm_into_a_revoked_room(kernel):
    room, dm = setup(kernel)
    saved = await call(kernel, room, **ARGS)
    kernel.store.put("bots", {**room.bot, "room_ids": []})
    assert (await call(kernel, dm, operation="destinations"))["channels"] == []
    with pytest.raises(ControlError, match="configured scope"):
        await call(kernel, dm, operation="snooze", reminder_id=saved["id"], after_seconds=60)
    assert (await call(kernel, dm, operation="status", reminder_id=saved["id"]))["due_at"] == saved["due_at"]


async def test_revoked_plugin_while_resolving_dm_does_not_save_or_move(kernel):
    room, _ = setup(kernel)
    saved = await call(kernel, room, **ARGS)
    kernel.store.execute("DELETE FROM owner_dm_channels")

    async def resolve(bot):
        kernel.store.put("plugins", {**kernel.store.get("plugins", "secretary"), "enabled": False})
        kernel.store.remember_owner_dm(bot, DM)
        return DM

    kernel.engine.transport.owner_dm.side_effect = resolve
    with pytest.raises(ControlError, match="grant changed"):
        await call(kernel, room, operation="update", reminder_id=saved["id"], destination="owner_dm")
    assert kernel.registry.secretary.get(saved["id"])["channel_id"] == ROOM


async def test_parallel_cross_context_dispatch_with_same_key_saves_once(kernel):
    room, dm = setup(kernel)
    async with asyncio.TaskGroup() as group:
        jobs = [group.create_task(call(kernel, ctx, **ARGS)) for ctx in (room, dm, room, dm)]
    assert len({job.result()["id"] for job in jobs}) == 1
    assert kernel.registry.secretary.listing(room.bot["id"], {})["total"] == 1


async def test_global_key_migration_preserves_collisions_without_deleting_or_rescheduling(kernel):
    room, _ = setup(kernel)
    active = await call(kernel, room, **ARGS)
    s = kernel.registry.secretary
    original = s.get(active["id"])
    kernel.store.execute("DROP INDEX secretary_bot_key")
    legacy = {
        **original,
        "id": uid("alarm_"),
        "channel_id": DM,
        "state": "cancelled",
        "created_at": original["created_at"] - 100,
    }
    conflict = {**original, "id": uid("alarm_"), "key": "usage-" + legacy["id"]}
    for row in (legacy, conflict):
        fields = list(row)
        kernel.store.execute(
            f"INSERT INTO secretary_reminders ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
            tuple(row.values()),
        )
    s.global_keys()
    assert s.get(active["id"])["key"] == "usage"
    renamed = s.get(legacy["id"])
    assert renamed["key"] == "usage-" + legacy["id"] + "-1"
    for field in ("id", "channel_id", "due_at", "message", "state", "last_turn_id"):
        assert renamed[field] == legacy[field]
    before = kernel.store.rows("SELECT * FROM secretary_reminders ORDER BY id")
    s.global_keys()
    assert kernel.store.rows("SELECT * FROM secretary_reminders ORDER BY id") == before
    assert len(before) == 3


@pytest.mark.parametrize(
    "args",
    [
        {"destination": "channel"},
        {"channel_id": ROOM},
        {"destination": "owner_dm", "channel_id": ROOM},
        {"destination": "channel", "channel_id": "123"},
    ],
)
async def test_destination_schema_rejects_incomplete_or_conflicting_choices(kernel, args):
    room, _ = setup(kernel)
    result = await kernel.registry.call("secretary", {**ARGS, **args}, room, "invalid")
    assert "error" in result and not kernel.registry.secretary.inventory()


async def test_clean_slate_cannot_be_bypassed_by_moving_an_old_alarm(kernel, owner):
    room, dm = setup(kernel)
    saved = await call(kernel, room, **ARGS)
    await kernel.service.control(
        owner,
        {
            "action": "reset_context",
            "kind": "bots",
            "id": room.bot["id"],
            "data": {"confirm_bot_id": room.bot["id"], "channel_id": ROOM},
        },
    )
    with pytest.raises(ControlError, match="clean slate"):
        await call(kernel, dm, operation="update", reminder_id=saved["id"], destination="owner_dm")
