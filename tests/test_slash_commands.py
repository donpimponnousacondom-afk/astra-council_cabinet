import asyncio
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import httpx
import pytest

from conftest import configured, ingest
from test_provider import install_client
from test_runtime import completion, settle
from hortator.models import OWNER_ID, ControlError
from hortator.plugins import ToolContext
from hortator.slash_commands import COMMAND, MAX_PROMPT_CHARS, parse_options


def enable(k, bot_id="ada", **changes):
    bot = configured(k, bot_id, enabled_plugins=["slash_commands", "memory"], **changes)
    plugin = k.store.get("plugins", "slash_commands")
    plugin.pop("revision")
    plugin["enabled"] = True
    k.store.put("plugins", plugin)
    k.store.execute(
        "INSERT INTO slash_commands VALUES(?,?,?,?,?)",
        (bot_id, bot["application_id"], "123456789012345678", json.dumps(COMMAND), time.time()),
    )
    return bot


def interaction(
    bot, *, actor=OWNER_ID, private=True, prompt="Find something useful", interaction_id="987654321012345678"
):
    return SimpleNamespace(
        id=int(interaction_id),
        token="SECRET-INTERACTION-TOKEN",
        application_id=int(bot["application_id"]),
        type=discord.InteractionType.application_command,
        user=SimpleNamespace(id=int(actor), bot=False),
        channel_id=987654321098765432,
        guild_id=777777777777777777,
        created_at=datetime.now(timezone.utc),
        data={
            "name": "prompt",
            "type": 1,
            "id": "123456789012345678",
            "options": [
                {"name": "text", "type": 3, "value": prompt},
                {"name": "private", "type": 5, "value": private},
            ],
        },
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(return_value=SimpleNamespace(id=888888888888888888)),
    )


async def test_disabled_is_inert_and_is_not_a_model_tool(kernel):
    assert "slash_commands" in kernel.registry.specs
    assert kernel.registry.specs["slash_commands"].model_tool is False
    plugin = kernel.service.public("plugins", kernel.store.get("plugins", "slash_commands"))
    assert plugin["keyless"] and plugin["capability_type"] == "input"
    assert plugin["enabled"] is False
    bot = configured(kernel)
    fake_client = SimpleNamespace(bot_id="ada", application_id=int(bot["application_id"]), http=AsyncMock())
    await kernel.connector.slash.sync(fake_client)
    fake_client.http.get_global_commands.assert_not_awaited()
    names = [t["function"]["name"] for t in kernel.registry.schemas(ToolContext(bot, "x", "t"))]
    assert "slash_commands" not in names
    result = await kernel.registry.call("slash_commands", {}, ToolContext(bot, "x", "t"), "call")
    assert result["ok"] is False
    item = interaction(bot)
    await kernel.connector.slash.receive("ada", item)
    item.response.send_message.assert_awaited_once()
    item.response.defer.assert_not_awaited()
    assert not kernel.store.rows("SELECT * FROM turns")


async def test_ingress_cannot_be_called_as_tool_even_when_granted(kernel):
    bot = enable(kernel)
    context = ToolContext(bot, "x", "t")
    assert "slash_commands" not in [schema["function"]["name"] for schema in kernel.registry.schemas(context)]
    result = await kernel.registry.call("slash_commands", {}, context, "call")
    assert not result["ok"]
    raw = await kernel.registry.call_raw("slash_commands", "{}", context, "call2")
    assert not raw["ok"]
    assert not kernel.store.rows("SELECT * FROM slash_invocations")


async def test_owner_only_stale_ids_and_duplicate_invocations(kernel):
    bot = enable(kernel)
    await install_client(kernel, lambda _: completion())
    stranger = interaction(bot, actor="555555555555555555")
    await kernel.connector.slash.receive("ada", stranger)
    stranger.response.defer.assert_not_awaited()
    stale = interaction(bot)
    stale.data["id"] = "unowned-command"
    await kernel.connector.slash.receive("ada", stale)
    stale.response.defer.assert_not_awaited()
    item = interaction(bot)
    await kernel.connector.slash.receive("ada", item)
    await settle(kernel)
    turn = kernel.store.one("SELECT status,error FROM turns")
    assert turn["status"] == "sent", turn
    await kernel.connector.slash.receive("ada", item)
    item.response.defer.assert_awaited_once()
    assert kernel.store.one("SELECT count(*) AS n FROM turns")["n"] == 1
    assert kernel.store.one("SELECT count(*) AS n FROM requests")["n"] == 1
    assert kernel.store.one("SELECT status FROM slash_invocations")["status"] == "sent"


async def test_tool_loop_isolated_prompt_notes_and_footer(kernel):
    bot = enable(kernel, footer_enabled=True)
    ingest(kernel, content="Ordinary channel transcript must never leak into slash")
    scope = "slash:ada:987654321098765432"
    kernel.store.execute(
        "INSERT INTO memories VALUES(?,?,?,?,?)",
        ("ada", "222222222222222222", "normal", "ordinary-secret-note", time.time()),
    )
    seen = []

    def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "note1",
                                        "type": "function",
                                        "function": {
                                            "name": "memory",
                                            "arguments": json.dumps(
                                                {"operation": "write", "key": "topic", "value": "loki note"}
                                            ),
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 200, "completion_tokens": 20},
                },
            )
        return completion("A **useful** answer")

    await install_client(kernel, handle)
    item = interaction(bot, private=False)
    await kernel.connector.slash.receive("ada", item)
    item.response.defer.assert_awaited_once_with(thinking=True, ephemeral=False)
    await settle(kernel)
    assert len(seen) == 2
    assert all("Ordinary channel transcript" not in json.dumps(body) for body in seen)
    assert all("ordinary-secret-note" not in json.dumps(body) for body in seen)
    assert "Find something useful" in json.dumps(seen[0])
    assert all(
        t["function"]["name"] not in ("slash_commands", "council_inspect", "discord_send")
        for t in seen[0]["tools"]
    )
    assert (
        kernel.store.one("SELECT value FROM memories WHERE bot_id='ada' AND channel_id=?", (scope,))["value"]
        == "loki note"
    )
    assert not kernel.engine.channel_allowed(bot, scope)
    assert kernel.engine.pick_channel(bot) != scope
    assert not kernel.store.rows("SELECT * FROM messages WHERE channel_id=?", (scope,))
    item.edit_original_response.assert_awaited_once()
    assert item.edit_original_response.call_args.kwargs["content"].startswith("A **useful** answer")
    assert "-# TTFT:" in item.edit_original_response.call_args.kwargs["content"]
    assert item.edit_original_response.call_args.kwargs["allowed_mentions"].everyone is False
    for table in ("slash_invocations", "events", "requests", "outbox"):
        assert item.token not in json.dumps(kernel.store.rows(f"SELECT * FROM {table}"))


async def test_busy_rejected_after_defer_and_regular_cancel_reaches_slash(kernel):
    bot = enable(kernel)
    started = asyncio.Event()

    async def handler(_):
        started.set()
        await asyncio.sleep(100)
        return completion()

    await install_client(kernel, handler)
    first = interaction(bot)
    await kernel.connector.slash.receive("ada", first)
    await asyncio.wait_for(started.wait(), 3)
    second = interaction(bot, interaction_id="987654321012345679")
    await kernel.connector.slash.receive("ada", second)
    second.response.defer.assert_awaited_once()
    assert "already busy" in second.edit_original_response.call_args.kwargs["content"]
    await kernel.engine.cancel(["ada"], "test stop")
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "cancelled"
    assert (
        kernel.store.one("SELECT status FROM slash_invocations WHERE interaction_id=?", (str(first.id),))[
            "status"
        ]
        == "cancelled"
    )
    assert kernel.store.one("SELECT count(*) AS n FROM requests")["n"] == 1


async def test_grant_revoked_while_deferring_is_rejected(kernel):
    bot = enable(kernel)
    item = interaction(bot)

    async def revoke(**kwargs):
        current = kernel.store.get("bots", "ada")
        current.pop("revision")
        current["enabled_plugins"] = []
        kernel.store.put("bots", current)

    item.response.defer.side_effect = revoke
    await kernel.connector.slash.receive("ada", item)
    assert not kernel.store.rows("SELECT * FROM turns")
    assert "disabled" in item.edit_original_response.call_args.kwargs["content"]


async def test_registration_does_not_overwrite_or_delete_other_commands(kernel):
    bot = enable(kernel)
    kernel.store.execute("DELETE FROM slash_commands")
    http = AsyncMock()
    client = SimpleNamespace(bot_id="ada", application_id=int(bot["application_id"]), http=http)
    http.get_global_commands.return_value = [
        {"name": "prompt", "type": 1, "id": "999"},
        {"name": "unrelated", "id": "123"},
    ]
    await kernel.connector.slash.sync(client)
    http.upsert_global_command.assert_not_awaited()
    assert not kernel.store.rows("SELECT * FROM slash_commands")
    http.get_global_commands.return_value = [{"name": "unrelated", "id": "123"}]
    http.upsert_global_command.return_value = {"id": "456"}
    await kernel.connector.slash.sync(client)
    http.upsert_global_command.assert_awaited_once_with(int(bot["application_id"]), payload=COMMAND)
    current = kernel.store.get("bots", "ada")
    current.pop("revision")
    current["enabled_plugins"] = []
    kernel.store.put("bots", current)
    await kernel.connector.slash.sync(client)
    http.delete_global_command.assert_awaited_once_with(int(bot["application_id"]), 456)
    assert not kernel.store.rows("SELECT * FROM slash_commands")


async def test_registration_ignores_discord_added_option_defaults(kernel):
    from copy import deepcopy
    from hortator.store import dumps

    bot = enable(kernel)
    remote = deepcopy(COMMAND)
    remote.update(id="123456789012345678", application_id=bot["application_id"], version="123")
    remote["options"][0]["autocomplete"] = False
    remote["options"][1]["name_localizations"] = None
    kernel.store.execute("UPDATE slash_commands SET definition=?", (dumps(COMMAND),))
    http = AsyncMock()
    http.get_global_commands.return_value = [remote]
    client = SimpleNamespace(bot_id="ada", application_id=int(bot["application_id"]), http=http)
    await kernel.connector.slash.sync(client)
    http.upsert_global_command.assert_not_awaited()
    # Actual drift in a field we own still gets repaired.
    remote["options"][0]["max_length"] = 10
    http.upsert_global_command.return_value = {"id": "123456789012345678"}
    await kernel.connector.slash.sync(client)
    http.upsert_global_command.assert_awaited_once()


async def test_deadline_cancels_stream_and_does_not_retry(kernel, monkeypatch):
    bot = enable(kernel)
    monkeypatch.setattr("hortator.slash_commands.MAX_SECONDS", 0.1)

    async def handler(_):
        await asyncio.sleep(100)
        return completion()

    await install_client(kernel, handler)
    item = interaction(bot)
    await kernel.connector.slash.receive("ada", item)
    await settle(kernel)
    turn = kernel.store.one("SELECT status,error FROM turns")
    assert turn["status"] == "failed"
    assert "Local slash invocation deadline exceeded" in turn["error"]
    assert "Nothing will automatically resume" in item.edit_original_response.call_args.kwargs["content"]
    assert kernel.engine.pick_channel(bot) is None
    assert kernel.store.health("openrouter")["consecutive_failures"] == 0


async def test_delivery_uncertainty_is_recorded_and_token_never_logged(kernel):
    bot = enable(kernel)
    await install_client(kernel, lambda _: completion())
    item = interaction(bot)
    item.edit_original_response.side_effect = RuntimeError("transport " + item.token)
    await kernel.connector.slash.receive("ada", item)
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM outbox")["status"] == "unknown"
    item.edit_original_response.assert_awaited_once()
    for table in ("slash_invocations", "events", "requests", "outbox", "turns"):
        assert item.token not in json.dumps(kernel.store.rows(f"SELECT * FROM {table}"))


async def test_long_failure_receipt_fits_discord_and_preserves_trace(kernel):
    bot = enable(kernel)
    await install_client(kernel, lambda _: httpx.Response(400, json={"error": {"message": "🚨" * 2500}}))
    item = interaction(bot)
    await kernel.connector.slash.receive("ada", item)
    await settle(kernel)
    turn = kernel.store.one("SELECT id,status,error FROM turns")
    assert turn["status"] == "failed"
    item.edit_original_response.assert_awaited_once()
    content = item.edit_original_response.call_args.kwargs["content"]
    assert len(content.encode("utf-16-le")) <= 4000
    assert "Full diagnostics are available in Trajectory." in content
    assert content.endswith("Trace: " + turn["id"])
    assert len(turn["error"].encode("utf-16-le")) > len(content.encode("utf-16-le"))


@pytest.mark.parametrize(
    "options",
    [
        [],
        [{"name": "text", "value": 1}, {"name": "private", "value": "yes"}],
        [{"name": "text", "value": "x" * (MAX_PROMPT_CHARS + 1)}],
        [{"name": "text", "value": "ok"}, {"name": "text", "value": "duplicate"}],
    ],
)
def test_complete_option_validation(options):
    with pytest.raises(ControlError, match="Usage"):
        parse_options({"options": options})


def test_multi_error_options_feedback():
    with pytest.raises(ControlError) as error:
        parse_options({"options": [{"name": "text", "value": 1}, {"name": "private", "value": "yes"}]})
    assert "text must" in str(error.value)
    assert "private must" in str(error.value)


async def test_global_notebook_follows_bot_and_preserves_real_source(kernel):
    bot = enable(kernel)
    plugin = kernel.store.get("plugins", "global_memory")
    plugin.pop("revision")
    plugin["enabled"] = True
    kernel.store.put("plugins", plugin)
    bot.pop("revision")
    bot["enabled_plugins"].append("global_memory")
    kernel.store.put("bots", bot)
    bot = kernel.store.get("bots", "ada")
    kernel.store.execute(
        "INSERT INTO global_memories VALUES(?,?,?,?,?)",
        ("ada", "previous", "known from another channel", "111111111111111111", time.time()),
    )
    kernel.store.execute(
        "INSERT INTO global_memories VALUES(?,?,?,?,?)",
        ("dirac", "private", "other-bot-secret", "111111111111111111", time.time()),
    )
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "global1",
                                        "type": "function",
                                        "function": {
                                            "name": "global_memory",
                                            "arguments": json.dumps(
                                                {
                                                    "operation": "write",
                                                    "key": "slash_fact",
                                                    "value": "Fresh slash fact",
                                                }
                                            ),
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                },
            )
        return completion("Noted")

    await install_client(kernel, handler)
    await kernel.connector.slash.receive("ada", interaction(bot))
    await settle(kernel)
    assert "known from another channel" in json.dumps(seen[0])
    assert "other-bot-secret" not in json.dumps(seen)
    note = kernel.store.one(
        "SELECT source_channel_id FROM global_memories WHERE bot_id='ada' AND key='slash_fact'"
    )
    assert note["source_channel_id"] == "987654321098765432"


async def test_gateway_hook_uses_owned_background_lifetime(kernel):
    from hortator.discord_gateway import CouncilClient
    from hortator.concurrency import join_tasks

    bot = enable(kernel)
    await install_client(kernel, lambda _: completion())
    client = CouncilClient(kernel.connector, "ada")
    try:
        item = interaction(bot)
        await client.on_interaction(item)
        assert any(t.get_name().startswith("slash-intake:ada:") for t in kernel.background.pending)
        await join_tasks(*list(kernel.background.pending))
        await settle(kernel)
        item.edit_original_response.assert_awaited_once()
    finally:
        await client.close()


async def test_snapshot_maintenance_rejects_without_provider_work(kernel):
    bot = enable(kernel)
    kernel.service.snapshot_maintenance = True
    item = interaction(bot)
    await kernel.connector.slash.receive("ada", item)
    item.response.defer.assert_not_awaited()
    assert "maintenance" in item.response.send_message.call_args.args[0]
    assert not kernel.store.rows("SELECT * FROM turns")


async def test_hortator_control_scope_is_not_extended(kernel):
    bot = enable(kernel, bot_id="hortator")
    item = interaction(bot)
    await kernel.connector.slash.receive("hortator", item)
    item.response.defer.assert_not_awaited()
    assert "disabled" in item.response.send_message.call_args.args[0]
    assert not kernel.store.rows("SELECT * FROM turns")


async def test_backoff_and_paused_bot_do_not_launch_providers(kernel):
    bot = enable(kernel)
    kernel.store.execute("UPDATE bot_runtime SET retry_until=? WHERE bot_id='ada'", (time.time() + 60,))
    item = interaction(bot)
    await kernel.connector.slash.receive("ada", item)
    assert "backoff" in item.edit_original_response.call_args.kwargs["content"]
    assert not kernel.store.rows("SELECT * FROM requests")
    kernel.store.execute("UPDATE bot_runtime SET retry_until=0 WHERE bot_id='ada'")
    bot.pop("revision")
    bot["enabled"] = False
    kernel.store.put("bots", bot)
    item = interaction(bot, interaction_id="987654321012345699")
    await kernel.connector.slash.receive("ada", item)
    assert "paused" in item.edit_original_response.call_args.kwargs["content"]
    assert not kernel.store.rows("SELECT * FROM turns")


async def test_public_setup_metadata_contains_only_matching_public_identity(kernel):
    from hortator.slash_commands import public_status
    from urllib.parse import urlparse, parse_qs

    bot = configured(kernel)
    inactive = public_status(kernel.store, bot)
    assert inactive["eligible"]
    assert not inactive["enabled"] and not inactive["registered"]
    assert inactive["command_id"] is None
    # Reading setup data never registers a command or writes a claim.
    assert not kernel.store.rows("SELECT * FROM slash_commands")
    assert not kernel.store.rows("SELECT * FROM slash_invocations")
    query = parse_qs(urlparse(inactive["user_install_url"]).query)
    assert query == {
        "client_id": [bot["application_id"]],
        "scope": ["applications.commands"],
        "integration_type": ["1"],
    }
    assert "fake-token" not in json.dumps(inactive)
    active = enable(kernel)
    state = public_status(kernel.store, active)
    assert state["enabled"] and state["registered"]
    assert state["command_id"] == "123456789012345678"
    switched = public_status(kernel.store, {**active, "application_id": "888888888888888888"})
    assert not switched["registered"] and switched["command_id"] is None
    director = public_status(kernel.store, {**active, "role": "hortator"})
    assert not director["eligible"] and not director["enabled"]
    invalid_id = public_status(kernel.store, {**active, "application_id": "not-a-discord-id"})
    assert invalid_id["user_install_url"] is None
    assert kernel.service.public("bots", active)["slash_commands"] == state
    # Service inspection is also valid before the Discord manager installs its tables.
    kernel.store.execute("DROP TABLE slash_commands")
    assert not public_status(kernel.store, active)["registered"]
