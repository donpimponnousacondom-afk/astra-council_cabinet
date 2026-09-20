import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from conftest import configured
from hortator.addressing import for_viewer, merge_live_roles
from hortator.models import OWNER_ID
from test_addressing import CHANNEL, message, pair
from test_provider import install_client
from test_runtime import completion, settle


ROLE = 666666666666666666
MESSAGE = 555555555555555555


def role_message(receiver, *, number=MESSAGE, member=True, **kwargs):
    msg = message(number, content=f"<@&{ROLE}> yolo?", **kwargs)
    msg.role_mentions = [NS(id=ROLE, name="syndicate")]
    msg.guild.me = NS(
        id=int(receiver["application_id"]),
        roles=[NS(id=msg.guild.id), *([NS(id=ROLE)] if member else [])],
    )
    return msg


@pytest.mark.parametrize("member_first", [False, True])
@pytest.mark.parametrize("interval", [0, 120])
async def test_role_ping_wakes_only_receiving_member_once(kernel, member_first, interval):
    ada, socrates = pair(kernel, interval_seconds=interval)
    receivers = [socrates, ada] if member_first else [ada, socrates]
    for bot in receivers:
        await kernel.connector.receive(bot["id"], role_message(bot, member=bot["id"] == "socrates"))
    row = kernel.store.transcript(CHANNEL)[-1]
    target = row["addressing"]["targets"]
    assert target == [
        {
            "user_id": socrates["application_id"],
            "bot_id": "socrates",
            "name": socrates["name"],
            "via": ["role_mention"],
            "role_ids": [str(ROLE)],
        }
    ]
    assert for_viewer(kernel.store, row, "socrates")["audience"] == "you"
    assert for_viewer(kernel.store, row, "ada")["audience"] == "other_participant"
    assert "role_observers" not in for_viewer(kernel.store, row, "ada")
    assert not kernel.engine.attention(ada)
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert "human_mention" in body["messages"][-1]["content"]
        return completion("Role ping received")

    await install_client(kernel, respond)
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 1)
    assert len(requests) == 1
    turn = kernel.store.one("SELECT * FROM turns")
    assert (turn["bot_id"], turn["trigger"], turn["status"]) == ("socrates", "human_mention", "sent")
    assert kernel.engine.transport.send.await_args.args[3] == str(MESSAGE)
    for bot in receivers:
        await kernel.connector.receive(bot["id"], role_message(bot))
        await kernel.connector.receive(bot["id"], role_message(bot), historical=True)
    await kernel.engine.tick()
    assert not kernel.engine.tasks
    assert kernel.store.one("SELECT count(*) n FROM human_attention_claims")["n"] == 1


async def test_shared_role_each_client_contributes_one_target_and_one_claim(kernel):
    bots = pair(kernel, interval_seconds=0)
    for bot in bots:
        await kernel.connector.receive(bot["id"], role_message(bot))
        await kernel.connector.receive(bot["id"], role_message(bot))
    addressing = kernel.store.transcript(CHANNEL)[-1]["addressing"]
    assert {t["bot_id"] for t in addressing["targets"]} == {b["id"] for b in bots}
    await install_client(kernel, lambda request: completion("Hello syndicate"))
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 1)
    assert kernel.engine.transport.send.await_count == 2
    assert kernel.store.one("SELECT count(*) n FROM human_attention_claims")["n"] == 2
    await kernel.engine.tick()
    assert not kernel.engine.tasks


async def test_attachment_await_cannot_lose_second_clients_role_target(kernel):
    bots = pair(kernel, interval_seconds=0)
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def capture_image(attachment):
        waiting.set()
        await release.wait()
        return attachment

    kernel.connector.images.capture = capture_image
    slow = role_message(bots[0])
    slow.attachments = [
        NS(id=999, filename="test.jpg", url="https://image.test/test.jpg", size=1, content_type="image/jpeg")
    ]
    async with asyncio.TaskGroup() as group:
        group.create_task(kernel.connector.receive("ada", slow))
        await asyncio.wait_for(waiting.wait(), 1)
        await kernel.connector.receive("socrates", role_message(bots[1]))
        release.set()
    targets = kernel.store.transcript(CHANNEL)[-1]["addressing"]["targets"]
    assert {target["bot_id"] for target in targets} == {"ada", "socrates"}
    assert all(kernel.engine.attention(bot) for bot in bots)


async def test_role_direct_mention_reply_overlap_remains_one_target(kernel):
    ada, socrates = pair(kernel, interval_seconds=0)
    parent = message(MESSAGE - 1, author=socrates["application_id"], bot=True)
    await kernel.connector.receive("ada", parent, historical=True)
    # The nonmember client already resolves user mentions and the reply target.
    for bot in (ada, socrates):
        msg = role_message(bot, member=bot == socrates, mentions=[socrates], reply_to=parent.id)
        await kernel.connector.receive(bot["id"], msg)
    targets = kernel.store.transcript(CHANNEL)[-1]["addressing"]["targets"]
    assert len(targets) == 1
    assert set(targets[0]["via"]) == {"mention", "reply", "role_mention"}
    assert targets[0]["role_ids"] == [str(ROLE)]
    assert len(kernel.engine.attention(socrates)) == 1
    assert not kernel.engine.attention(ada)


@pytest.mark.parametrize(
    "case",
    [
        "bot",
        "webhook",
        "history",
        "quoted_text",
        "everyone",
        "missing_self",
        "wrong_self",
        "nonmember",
        "dm",
    ],
)
async def test_role_ping_does_not_grant_false_human_priority(kernel, case):
    ada, socrates = pair(kernel, interval_seconds=0)
    room = kernel.store.get("rooms", "council")
    kernel.store.put("rooms", {**room, "allow_external_bots": True})
    msg = role_message(
        ada,
        member=case != "nonmember",
        bot=case in ("bot", "webhook"),
        author="777777777777777777",
        webhook=123 if case == "webhook" else None,
    )
    if case == "webhook":
        msg.application_id = 123
    elif case == "quoted_text":
        msg.role_mentions = []
    elif case == "everyone":
        msg.role_mentions = [NS(id=msg.guild.id)]
    elif case == "missing_self":
        msg.guild.me = None
    elif case == "wrong_self":
        msg.guild.me.id = int(socrates["application_id"])
    elif case == "dm":
        msg.guild = None
    await kernel.connector.receive("ada", msg, historical=case == "history")
    assert not kernel.engine.attention(ada)
    assert not kernel.engine.attention(socrates)
    await kernel.engine.tick()
    assert not kernel.engine.tasks


async def test_membership_changes_apply_to_new_messages_not_old_duplicate_creates(kernel):
    ada, socrates = pair(kernel, interval_seconds=0)
    await kernel.connector.receive("ada", role_message(ada, member=False))
    await kernel.connector.receive("ada", role_message(ada, member=True))
    assert not kernel.engine.attention(ada)
    # A fresh message sees the updated self member cached by discord.py.
    await kernel.connector.receive("ada", role_message(ada, number=MESSAGE + 1))
    assert len(kernel.engine.attention(ada)) == 1
    await kernel.connector.receive("ada", role_message(ada, number=MESSAGE + 2, member=False))
    assert len(kernel.engine.attention(ada)) == 1
    # Another client's historical overlap must not retrofit this live message.
    await kernel.connector.receive("socrates", role_message(socrates, number=MESSAGE + 2), historical=True)
    assert not kernel.engine.attention(socrates)


async def test_history_first_and_legacy_rows_cannot_be_promoted_to_live_role_ping(kernel):
    ada, socrates = pair(kernel, interval_seconds=0)
    await kernel.connector.receive("ada", role_message(ada), historical=True)
    await kernel.connector.receive("socrates", role_message(socrates))
    assert not kernel.engine.attention(socrates)
    kernel.store.execute("UPDATE messages SET addressing='{}'")
    await kernel.connector.receive("socrates", role_message(socrates))
    assert not kernel.engine.attention(socrates)


@pytest.mark.parametrize("case", ["wrong_channel", "wrong_guild", "humans_disallowed"])
async def test_role_match_never_widens_room_intake(kernel, case):
    ada, _ = pair(kernel, interval_seconds=0)
    msg = role_message(ada, author="777777777777777777")
    if case == "wrong_channel":
        msg.channel.id += 1
    elif case == "wrong_guild":
        msg.guild.id += 1
    else:
        room = kernel.store.get("rooms", "council")
        kernel.store.put("rooms", {**room, "allow_humans": False})
    await kernel.connector.receive("ada", msg)
    assert not kernel.store.transcript(str(msg.channel.id))
    assert not kernel.engine.attention(ada)


@pytest.mark.parametrize("case", ["owner_control", "other_human", "owner_elsewhere"])
async def test_hortator_role_ping_still_requires_owner_and_control_scope(kernel, case):
    bot = configured(kernel, "hortator", interval_seconds=0)
    settings = kernel.store.get("settings", "global")
    kernel.store.put(
        "settings", {**settings, "control_guild_id": "111111111111111111", "control_channel_id": CHANNEL}
    )
    msg = role_message(bot, author="777777777777777777" if case == "other_human" else OWNER_ID)
    if case == "owner_elsewhere":
        msg.channel.id += 1
    await kernel.connector.receive("hortator", msg)
    assert bool(kernel.engine.attention(bot)) == (case == "owner_control")


def test_merge_keeps_other_addressing_and_does_not_mutate_inputs():
    before = {
        "live": True,
        "targets": [{"user_id": "1", "bot_id": "ada", "via": ["reply"]}],
        "reply_target": {"message_id": "2"},
    }
    incoming = {
        "live": True,
        "role_observers": ["ada"],
        "targets": [{"user_id": "1", "bot_id": "ada", "via": ["role_mention"], "role_ids": [str(ROLE)]}],
    }
    saved = json.dumps(before)
    merged = merge_live_roles(before, incoming)
    assert json.dumps(before) == saved
    assert merged["reply_target"] == before["reply_target"]
    assert merged["targets"][0]["via"] == ["reply", "role_mention"]
    assert merge_live_roles(merged, incoming) == merged
