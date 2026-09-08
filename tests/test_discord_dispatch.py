import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from conftest import configured
from hortator.models import OWNER_ID
from hortator.plugins import ToolContext
from hortator.runtime import DeliveryError

SOURCE = "666666666666666666"
TARGET = "222222222222222222"
GUILD = "111111111111111111"


def setup(kernel):
    bot = configured(kernel, "hortator", enabled_plugins=["discord_send"], cooldown_seconds=0)
    plugin = kernel.store.get("plugins", "discord_send")
    plugin["enabled"] = True
    kernel.store.put("plugins", plugin)
    settings = kernel.store.get("settings", "global")
    settings.update(control_channel_id=SOURCE, control_guild_id=GUILD)
    kernel.store.put("settings", settings)
    kernel.store.ingest(
        discord_id="555555555555555555",
        channel_id=SOURCE,
        guild_id=GUILD,
        room_id="owner:hortator",
        author_id=OWNER_ID,
        author_name="The Boss",
        content="Send a message to the council",
    )
    kernel.store.context("hortator", SOURCE)
    kernel.connector.check_destination = AsyncMock()
    kernel.engine.transport = AsyncMock()
    kernel.engine.transport.send.return_value = "888888888888888888"
    return ToolContext(bot, SOURCE, "turn-route", owner_verified=True)


def send(**changes):
    return {
        "operation": "send",
        "target": "council",
        "content": "Owner announcement",
        "delivery_key": "announcement-1",
        **changes,
    }


async def call(kernel, context, args, call_id="call-route"):
    return await kernel.registry.call("discord_send", args, context, call_id)


async def test_discovery_full_argument_feedback_and_keyless_owner_only_grant(kernel):
    context = setup(kernel)
    assert kernel.service.public("plugins", kernel.service.entity("plugins", "discord_send"))["keyless"]
    help = await call(kernel, context, {})
    assert help["usage_only"] and not help["executed"]
    bad = await call(
        kernel, context, {"operation": "send", "target": 5, "content": False, "mode": "directed"}
    )
    assert len(bad["errors"]) >= 4 and bad["usage"]
    kernel.engine.transport.send.assert_not_called()
    assert not kernel.store.rows("SELECT * FROM outbox")
    for role, verified in (("hortator", False), ("council", True)):
        context.bot = {**context.bot, "role": role}
        context.owner_verified = verified
        assert not kernel.registry.allowed("discord_send", context)


@pytest.mark.parametrize("target", ["council", TARGET])
async def test_routed_send_has_confirmed_receipt_separate_origin_and_no_new_turn(kernel, target):
    context = setup(kernel)
    targets = await call(kernel, context, {"operation": "targets"})
    assert targets["targets"][0]["channel_id"] == TARGET
    result = await call(kernel, context, send(target=target))
    assert result["ok"] and result["status"] == "sent", result
    assert result["url"] == f"https://discord.com/channels/{GUILD}/{TARGET}/888888888888888888"
    args = kernel.engine.transport.send.await_args
    assert args.args[1:4] == (TARGET, "Owner announcement", None)
    assert "-# Cross-post from Hortator" in args.kwargs["footer"]
    assert args.kwargs["strict_reply"] is True
    assert not kernel.store.rows("SELECT * FROM turns")
    row = kernel.store.transcript(TARGET)[0]
    assert row["bot_id"] == "hortator" and row["room_id"] == "council"
    assert row["addressing"]["routing"] == {"mode": "cross_post", "source_bot_id": "hortator"}
    assert row["addressing"]["author_kind"] == "bot"
    outbox = kernel.store.one("SELECT * FROM outbox")
    assert json.loads(outbox["routing"])["source_channel_id"] == SOURCE
    assert not kernel.engine.channel_allowed(context.bot, TARGET)


async def test_directed_mode_requires_existing_target_channel_message_and_uses_native_reply(kernel):
    context = setup(kernel)
    result = await call(kernel, context, send(mode="directed", reply_to="999999999999999999"))
    assert result["ok"] and result["mode"] == "directed"
    kernel.connector.check_destination.assert_awaited_once_with(
        context.bot, TARGET, GUILD, None, "999999999999999999"
    )
    assert kernel.engine.transport.send.await_args.args[3] == "999999999999999999"
    assert "Directed reply" in kernel.engine.transport.send.await_args.kwargs["footer"]


async def test_delivery_key_prevents_repeat_and_conflicting_reuse(kernel):
    context = setup(kernel)
    first = await call(kernel, context, send())
    second = await call(kernel, context, send(), "retry")
    assert first["delivery_id"] == second["delivery_id"]
    kernel.engine.transport.send.assert_awaited_once()
    conflict = await call(kernel, context, send(content="Different body"), "conflict")
    assert not conflict["ok"] and "different post" in conflict["error"]
    assert len(kernel.store.rows("SELECT * FROM outbox")) == 1
    status = await call(kernel, context, {"operation": "status", "delivery_id": first["delivery_id"]})
    assert status["status"] == "sent"


@pytest.mark.parametrize("uncertain", [False, True])
async def test_failed_or_unknown_receipt_does_not_claim_delivery_or_retry(kernel, uncertain):
    context = setup(kernel)
    kernel.engine.transport.send.side_effect = DeliveryError("fixture delivery failure", uncertain=uncertain)
    first = await call(kernel, context, send())
    assert not first["ok"] and first["url"] is None
    assert first["status"] == ("unknown" if uncertain else "failed")
    assert "Do not automatically resend" in first["note"]
    second = await call(kernel, context, send(), "retry")
    assert second["status"] == first["status"]
    kernel.engine.transport.send.assert_awaited_once()


async def test_cancelled_in_flight_send_retains_unknown_outbox_receipt(kernel):
    context = setup(kernel)
    sending = asyncio.Event()

    async def blocked(*args, **kwargs):
        sending.set()
        await asyncio.Event().wait()

    kernel.engine.transport.send.side_effect = blocked
    task = asyncio.create_task(call(kernel, context, send()))
    await asyncio.wait_for(sending.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert kernel.store.one("SELECT status FROM outbox")["status"] == "unknown"


@pytest.mark.parametrize(
    "denied", ["unknown_target", "current_channel", "revoked", "wrong_artifact", "invalid_reply"]
)
async def test_routing_preflight_and_current_grants_fail_before_sending(kernel, denied):
    context = setup(kernel)
    args = send()
    if denied == "unknown_target":
        args["target"] = "999999999999999999"
    elif denied == "current_channel":
        room = kernel.store.get("rooms", "council")
        room["channel_id"] = SOURCE
        kernel.store.put("rooms", room)
    elif denied == "revoked":

        async def revoke(*args):
            bot = kernel.store.get("bots", "hortator")
            bot["enabled_plugins"] = []
            kernel.store.put("bots", bot)

        kernel.connector.check_destination.side_effect = revoke
    elif denied == "wrong_artifact":
        args["artifact_ids"] = ["someone-elses-file"]
    else:
        args.update(mode="directed", reply_to="999999999999999999")
        kernel.connector.check_destination.side_effect = ValueError("missing reply target")
    result = await call(kernel, context, args)
    assert not result["ok"]
    kernel.engine.transport.send.assert_not_awaited()


async def test_connector_sends_strict_reply_and_suppresses_mentions(kernel):
    context = setup(kernel)
    # Use the real connector; only the Discord channel is synthetic.
    channel = NS(
        id=int(TARGET),
        guild=NS(id=int(GUILD)),
        send=AsyncMock(return_value=NS(id=888888888888888888)),
        fetch_message=AsyncMock(return_value=NS(channel=NS(id=int(TARGET)))),
    )
    kernel.connector.clients["hortator"] = NS(is_ready=lambda: True, get_channel=lambda _: channel)
    from hortator.discord_gateway import DiscordManager

    await DiscordManager.check_destination(
        kernel.connector, context.bot, TARGET, GUILD, None, "999999999999999999"
    )
    await kernel.connector.send(
        context.bot, TARGET, "**Markdown** @everyone", "999999999999999999", [], strict_reply=True, footer=""
    )
    kwargs = channel.send.await_args.kwargs
    assert kwargs["reference"].fail_if_not_exists is True
    assert kwargs["allowed_mentions"].everyone is False and kwargs["allowed_mentions"].users is False
    assert channel.send.await_args.args[0] == "**Markdown** @everyone"


async def test_owner_turn_posts_to_target_then_answers_normally_in_origin(kernel):
    import httpx

    from test_provider import install_client
    from test_runtime import completion, settle

    setup(kernel)
    kernel.engine.transport.send.side_effect = ["888888888888888888", "888888888888888889"]
    bodies = []

    async def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            assert "discord_send" in [t["function"]["name"] for t in body["tools"]]
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "route-call",
                                        "type": "function",
                                        "function": {
                                            "name": "discord_send",
                                            "arguments": json.dumps(send()),
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 30},
                },
            )
        receipt = next(m for m in body["messages"] if m["role"] == "tool")
        assert json.loads(receipt["content"])["status"] == "sent"
        return completion("Posted in the council channel.")

    await install_client(kernel, respond)
    await kernel.engine.tick()
    await settle(kernel)
    turn = kernel.store.one("SELECT * FROM turns")
    assert turn["status"] == "sent", turn["error"]
    deliveries = kernel.engine.transport.send.await_args_list
    assert [d.args[1] for d in deliveries] == [TARGET, SOURCE]
    assert [d.args[2] for d in deliveries] == ["Owner announcement", "Posted in the council channel."]
    assert len(bodies) == 2


async def test_observed_thread_requires_current_parent_grant(kernel):
    context = setup(kernel)
    thread = "777777777777777777"
    kernel.store.ingest(
        discord_id="123456789012345678",
        channel_id=thread,
        parent_id=TARGET,
        guild_id=GUILD,
        room_id="council",
        author_id=OWNER_ID,
        author_name="The Boss",
        content="Thread",
    )
    room = kernel.store.get("rooms", "council")
    room["include_threads"] = True
    kernel.store.put("rooms", room)
    result = await call(kernel, context, send(target=thread))
    assert result["status"] == "sent"
    kernel.connector.check_destination.assert_awaited_once_with(context.bot, thread, GUILD, TARGET, None)
    room["include_threads"] = False
    kernel.store.put("rooms", room)
    result = await call(kernel, context, send(target=thread, delivery_key="second"), "second")
    assert not result["ok"]
    kernel.engine.transport.send.assert_awaited_once()


async def test_connector_revocation_before_actual_send_is_definite_failure(kernel):
    from hortator.models import ControlError

    context = setup(kernel)
    channel = NS(id=int(TARGET), guild=NS(id=int(GUILD)), send=AsyncMock())
    kernel.connector.clients["hortator"] = NS(is_ready=lambda: True, get_channel=lambda _: channel)

    def revoked():
        raise ControlError("Grant revoked")

    with pytest.raises(DeliveryError) as error:
        await kernel.connector.send(context.bot, TARGET, "Hello", None, [], destination_guard=revoked)
    assert not error.value.uncertain
    channel.send.assert_not_awaited()
