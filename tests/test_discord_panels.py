import json
from types import SimpleNamespace

import discord
import httpx
import pytest

from conftest import configured, ingest
from support.provider import install_client
from support.runtime import completion, settle
from support.slash_commands import interaction
from hortator.discord_panels import DiscordPanels, PARAMETERS, DESCRIPTION, panel_view
from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.tool_feedback import feedback
from support.discord_panels import BUTTONS, CHANNEL, enable, prepare


async def active(k, bot):
    context, row = await prepare(k, bot)
    k.registry.panels.dispatch(row, "out-panel")
    k.registry.panels.bind(row, "888888888888888888", "Ready for your request")
    return k.registry.panels.owned(row["id"], bot["id"])


def click(bot, row, **changes):
    item = interaction(bot, private=False)
    item.type = discord.InteractionType.component
    item.channel_id = int(row["channel_id"])
    item.data = {"custom_id": f"hcp:{row['id']}:b:0", "component_type": 2}
    item.message = SimpleNamespace(
        id=int(row["message_id"]), author=SimpleNamespace(id=int(bot["application_id"]))
    )
    for key, value in changes.items():
        setattr(item, key, value)
    return item


async def test_opt_in_usage_all_errors_and_no_new_attachment_toggle(kernel):
    bot = configured(kernel)
    assert not kernel.store.get("plugins", "discord_panel")["enabled"]
    public = kernel.service.public("plugins", kernel.store.get("plugins", "discord_panel"))
    assert public["keyless"]
    ctx = ToolContext(bot, CHANNEL, "turn1")
    assert "discord_panel" not in [v["function"]["name"] for v in kernel.registry.schemas(ctx)]
    assert "discord_attach" not in kernel.registry.specs
    result = await kernel.registry.call(
        "discord_panel", {"operation": "prepare", "title": "Blocked", "buttons": BUTTONS}, ctx, "denied"
    )
    assert not result["ok"]
    bot = enable(kernel)
    usage = await kernel.registry.call("discord_panel", {}, ToolContext(bot, CHANNEL, "turn2"), "usage")
    assert usage["ok"] and not kernel.store.rows("SELECT * FROM discord_panels")
    bad = feedback(
        "discord_panel",
        {"operation": "prepare", "title": 1, "buttons": [{"label": 1}]},
        PARAMETERS,
        DESCRIPTION,
    )
    assert bad and "prompt" in json.dumps(bad) and "title" in json.dumps(bad)


async def test_staging_replace_cancel_and_serialized_view(kernel):
    bot = enable(kernel)
    ctx, first = await prepare(kernel, bot)
    _, second = await prepare(
        kernel, bot, select_options=[{"label": "Explain", "prompt": "Explain your answer"}]
    )
    assert kernel.registry.panels.owned(first["id"], bot["id"])["status"] == "cancelled"
    view = panel_view(second, "A real response\n-# TPS: 10", ["report.txt"])
    assert view.is_finished()  # Central gateway dispatch, no duplicate ViewStore callbacks.
    wire = view.to_components()
    assert wire[0]["type"] == 17 and wire[0]["accent_color"] == 0xD6A447
    assert "attachment://report.txt" in json.dumps(wire)
    assert f"hcp:{second['id']}:b:0" in json.dumps(wire)
    assert f"hcp:{second['id']}:s" in json.dumps(wire)
    assert "Give me a factual briefing" not in json.dumps(wire)  # Callback prompt stays local.
    await kernel.registry.panels.call({"operation": "cancel"}, ctx, {})
    assert not kernel.registry.panels.prepared(ctx)


async def test_button_survives_restart_and_runs_real_tool_loop_without_slash_grant(kernel):
    bot = enable(kernel)
    row = await active(kernel, bot)
    kernel.registry.panels = DiscordPanels(kernel.store, kernel.vault)
    ingest(kernel, content="SECRET AMBIENT HISTORY MUST NOT LEAK")
    item = click(bot, row)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return completion("A real model response")

    await install_client(kernel, respond)
    await kernel.connector.slash.receive(bot["id"], item)
    await settle(kernel)
    item.response.defer.assert_awaited_once_with(ephemeral=False, thinking=True)
    assert item.edit_original_response.await_args.kwargs["content"] == "A real model response"
    assert "SECRET AMBIENT" not in json.dumps(requests)
    assert "Give me a factual briefing" in json.dumps(requests)
    turn = kernel.store.one("SELECT * FROM turns")
    assert turn["trigger"] == "panel_action" and turn["status"] == "sent"
    assert turn["channel_id"] == f"panel:ada:{CHANNEL}"
    await kernel.connector.slash.receive(bot["id"], item)
    assert len(requests) == 1
    assert "SECRET-INTERACTION-TOKEN" not in str(kernel.store.rows("SELECT * FROM events"))


@pytest.mark.parametrize(
    "tamper",
    [
        "actor",
        "application",
        "message",
        "channel",
        "author",
        "unknown_action",
        "expired",
        "disabled",
        "grant",
        "global_grant",
        "unconfirmed",
    ],
)
async def test_forged_stale_disabled_and_unconfirmed_controls_do_not_run(kernel, tamper):
    bot = enable(kernel)
    row = await active(kernel, bot)
    item = click(bot, row)
    if tamper == "actor":
        item.user.id = 111111111111111111
    elif tamper == "application":
        item.application_id += 1
    elif tamper == "message":
        item.message.id += 1
    elif tamper == "channel":
        item.channel_id += 1
    elif tamper == "author":
        item.message.author.id += 1
    elif tamper == "unknown_action":
        item.data["custom_id"] = f"hcp:{row['id']}:b:50"
    elif tamper == "expired":
        kernel.store.execute("UPDATE discord_panels SET expires_at=0")
    elif tamper == "disabled":
        kernel.store.execute("UPDATE discord_panels SET status='disabled'")
    elif tamper == "unconfirmed":
        kernel.store.execute("UPDATE discord_panels SET status='sending'")
    elif tamper == "grant":
        current = kernel.store.get("bots", "ada")
        current.pop("revision")
        current["enabled_plugins"] = []
        kernel.store.put("bots", current)
    elif tamper == "global_grant":
        plugin = kernel.store.get("plugins", "discord_panel")
        plugin.pop("revision")
        plugin["enabled"] = False
        kernel.store.put("plugins", plugin)
    await kernel.connector.slash.receive(bot["id"], item)
    item.response.defer.assert_not_awaited()
    item.response.send_message.assert_awaited_once()
    assert not kernel.store.rows("SELECT * FROM turns")


async def test_dropdown_resolves_only_saved_option_and_private_visibility(kernel):
    bot = enable(kernel)
    ctx, row = await prepare(
        kernel, bot, select_options=[{"label": "Explain", "prompt": "Explain the numbers"}]
    )
    kernel.registry.panels.dispatch(row, "out-panel")
    kernel.registry.panels.bind(row, "888888888888888888", "Numbers")
    kernel.store.execute("UPDATE discord_panels SET private=1 WHERE id=?", (row["id"],))
    row = kernel.registry.panels.owned(row["id"], bot["id"])
    item = click(bot, row)
    item.data = {"custom_id": f"hcp:{row['id']}:s", "component_type": 3, "values": ["0"]}
    await install_client(kernel, lambda _: completion())
    await kernel.connector.slash.receive(bot["id"], item)
    await settle(kernel)
    item.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert "Explain the numbers" in kernel.store.one("SELECT prompt FROM slash_invocations")["prompt"]


async def test_grant_revoked_during_ack_and_busy_slot_reject(kernel):
    bot = enable(kernel)
    row = await active(kernel, bot)
    item = click(bot, row)

    async def revoke(**kwargs):
        kernel.store.execute("UPDATE discord_panels SET status='disabled'")

    item.response.defer.side_effect = revoke
    await kernel.connector.slash.receive(bot["id"], item)
    assert "disabled" in item.edit_original_response.await_args.kwargs["content"]
    assert not kernel.store.rows("SELECT * FROM turns")


async def test_slash_can_deliver_panel_with_files_and_full_answer_attachment(kernel):
    from support.slash_commands import enable as enable_slash

    bot = enable_slash(kernel)
    current = kernel.store.get("bots", "ada")
    current.pop("revision")
    current["enabled_plugins"].append("discord_panel")
    kernel.store.put("bots", current)
    plugin = kernel.store.get("plugins", "discord_panel")
    plugin.pop("revision")
    plugin["enabled"] = True
    kernel.store.put("plugins", plugin)
    bot = kernel.store.get("bots", "ada")
    item = interaction(bot, private=False)
    calls = 0

    async def respond(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "prepare1",
                                        "type": "function",
                                        "function": {
                                            "name": "discord_panel",
                                            "arguments": json.dumps(
                                                {
                                                    "operation": "prepare",
                                                    "title": "👑 Desk",
                                                    "buttons": BUTTONS,
                                                }
                                            ),
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            )
        return completion("A long answer. " * 300)

    await install_client(kernel, respond)
    capture = {}

    async def delivered(**kwargs):
        capture.update(kwargs)
        capture["file_text"] = kwargs["attachments"][0].fp.read().decode()
        return SimpleNamespace(id=888888888888888888)

    item.edit_original_response.side_effect = delivered
    await kernel.connector.slash.receive("ada", item)
    await settle(kernel)
    assert kernel.store.one("SELECT status,error FROM turns")["status"] == "sent"
    assert capture["content"] is None and capture["embeds"] == []
    assert "attachment://full-response.txt" in json.dumps(capture["view"].to_components())
    assert capture["file_text"] == ("A long answer. " * 300).strip()
    row = kernel.store.one("SELECT * FROM discord_panels")
    assert row["status"] == "active" and row["channel_id"] == str(item.channel_id)
    assert row["private"] == 0


async def test_document_export_is_owned_pinned_and_turn_scoped_without_other_plugins(kernel):
    bot = enable(kernel)
    ctx = ToolContext(bot, CHANNEL, "document-turn")

    async def call(operation, **args):
        return await kernel.registry.documents.call(
            {"operation": operation, "site": "report", **args}, ctx, {}
        )

    await call("create")
    await call("write", path="index.html", content="<h1>Original</h1>")
    await call("write", path="index.html", content="<h1>Edited</h1>")
    result = await call("export", path="index.html", revision=1)
    path = kernel.registry.resolve_artifact(result["artifact_id"], ctx)
    assert path.read_text() == "<h1>Original</h1>" and result["revision"] == 1
    assert kernel.registry.documents._site("ada", "report")["revision"] == 2
    with pytest.raises(ControlError):
        kernel.registry.resolve_artifact(result["artifact_id"], ToolContext(bot, CHANNEL, "other-turn"))
    with pytest.raises(ControlError):
        await kernel.registry.documents.call(
            {"operation": "export", "site": "report", "path": "index.html"},
            ToolContext({"id": "loki"}, CHANNEL, "t"),
            {},
        )
    with pytest.raises(ControlError):
        await call("export", path="../index.html")
    with pytest.raises(ControlError):
        await call("export", path="missing.html")


async def test_ordinary_turn_posts_component_panel_and_preserves_canonical_content(kernel):
    from unittest.mock import AsyncMock

    enable(kernel)
    ingest(kernel)
    channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=888888888888888888)))
    kernel.connector.clients["ada"] = SimpleNamespace(is_ready=lambda: True, get_channel=lambda _: channel)
    calls = 0

    async def respond(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "prepare",
                                        "type": "function",
                                        "function": {
                                            "name": "discord_panel",
                                            "arguments": json.dumps(
                                                {
                                                    "operation": "prepare",
                                                    "title": "👑 Desk",
                                                    "buttons": BUTTONS,
                                                }
                                            ),
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            )
        return completion("A real ordinary answer")

    await install_client(kernel, respond)
    await kernel.engine.tick()
    await settle(kernel)
    turn = kernel.store.one("SELECT * FROM turns")
    assert turn["status"] == "sent", turn["error"]
    sent = channel.send.await_args
    assert sent.args[0] is None and isinstance(sent.kwargs["view"], discord.ui.LayoutView)
    assert sent.kwargs["allowed_mentions"].everyone is False
    panel = kernel.store.one("SELECT * FROM discord_panels")
    assert panel["status"] == "active"
    assert (
        kernel.store.one("SELECT content FROM messages WHERE bot_id='ada'")["content"]
        == "A real ordinary answer"
    )
    outbox = kernel.store.one("SELECT * FROM outbox")
    assert json.loads(outbox["routing"])["panel_id"] == panel["id"]


async def test_panel_provider_retry_preserves_request_and_does_not_replay_tools(kernel):
    bot = enable(kernel)
    row = await active(kernel, bot)
    provider = kernel.store.get("providers", "openrouter")
    provider.pop("revision")
    provider.update(retry_count=1, retry_delay_seconds=0)
    kernel.store.put("providers", provider)
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return (
            httpx.Response(503, json={"error": "transient test failure"})
            if len(bodies) == 1
            else completion("Recovered")
        )

    await install_client(kernel, respond)
    item = click(bot, row)
    await kernel.connector.slash.receive("ada", item)
    await settle(kernel)
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    item.edit_original_response.assert_awaited_once()
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"


async def test_panel_operator_cancellation_and_busy_slot_are_observed(kernel, owner):
    import asyncio

    bot = enable(kernel)
    row = await active(kernel, bot)
    started = asyncio.Event()

    async def respond(request):
        started.set()
        await asyncio.sleep(30)
        return completion()

    await install_client(kernel, respond)
    item = click(bot, row)
    await kernel.connector.slash.receive("ada", item)
    await started.wait()
    second = click(bot, row, id=item.id + 1)
    await kernel.connector.slash.receive("ada", second)
    assert "busy" in second.edit_original_response.await_args.kwargs["content"]
    await kernel.service.control(owner, {"action": "stop", "id": "ada"})
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "cancelled"
    assert "cancelled" in item.edit_original_response.await_args.kwargs["content"]
    assert kernel.store.one("SELECT status FROM discord_panels")["status"] == "active"


async def test_hortator_panel_does_not_widen_director_scope(kernel):
    from support.typing import prepare_bot

    bot, channel_id = prepare_bot(kernel, "hortator")
    bot.pop("revision")
    bot["enabled_plugins"] = ["discord_panel"]
    kernel.store.put("bots", bot)
    bot = kernel.store.get("bots", "hortator")
    plugin = kernel.store.get("plugins", "discord_panel")
    plugin.pop("revision")
    plugin["enabled"] = True
    kernel.store.put("plugins", plugin)
    ctx = ToolContext(bot, channel_id, "hortator-panel", True)
    await kernel.registry.panels.call(
        {"operation": "prepare", "title": "Director", "buttons": BUTTONS}, ctx, {}
    )
    row = kernel.registry.panels.prepared(ctx)
    kernel.registry.panels.dispatch(row, "out")
    kernel.registry.panels.bind(row, "888888888888888888", "Director's desk")
    row = kernel.registry.panels.owned(row["id"], "hortator")
    item = click(bot, row)
    assert kernel.connector.slash.invocation_granted(
        bot, {"kind": "panel", "panel_id": row["id"], "channel_id": channel_id}
    )
    kernel.store.execute("DELETE FROM contexts WHERE bot_id='hortator'")
    await kernel.connector.slash.receive("hortator", item)
    item.response.defer.assert_not_awaited()
    assert not kernel.store.rows("SELECT * FROM turns")


async def test_application_change_during_ack_cannot_cross_applications(kernel):
    bot = enable(kernel)
    row = await active(kernel, bot)
    item = click(bot, row)

    async def change_app(**kwargs):
        current = kernel.store.get("bots", "ada")
        current.pop("revision")
        current["application_id"] = "666666666666666666"
        kernel.store.put("bots", current)

    item.response.defer.side_effect = change_app
    await kernel.connector.slash.receive("ada", item)
    assert "disabled" in item.edit_original_response.await_args.kwargs["content"]
    assert not kernel.store.rows("SELECT * FROM turns")
