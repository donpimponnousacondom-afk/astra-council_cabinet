"""Terminal publication keeps memory, action budgets and interaction receipts consistent."""

import asyncio
import json

import pytest

from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.runtime import DeliveryError
from support.provider import install_client
from support.runtime import completion, settle
from support.runtime_feedback import ready
from support.slash_commands import enable as enable_slash, interaction
from test_end_cycle import cycle, end, events, mixed, read_call, run, tool, tool_names, write_call
from test_engram_integration import PRIVATE, enable, response, state


@pytest.mark.parametrize("preparation", ["success", "invalid", "help"])
async def test_attachment_action_count_tracks_successful_preparation(kernel, preparation):
    bot, transport = ready(kernel, response_protocol="end_cycle", enabled_plugins=["memory"])
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            turn_id = kernel.store.one("SELECT id FROM turns")["id"]
            context = ToolContext(bot, "222222222222222222", turn_id)
            kernel.registry.artifact(b"Useful output", "text/plain", ".txt", context)
            return mixed("", read_call())
        if len(requests) == 2:
            assert "discord_attach" in tool_names(body)
            artifact_id = kernel.store.one("SELECT id FROM artifacts")["id"]
            args = {
                "success": {"artifact_ids": [artifact_id]},
                "invalid": {"artifact_ids": ["missing"]},
                "help": {},
            }[preparation]
            return mixed("Here is the file.", tool("discord_attach", args, call_id="attach"), end())
        return mixed("The attachment was not prepared.", end())

    await install_client(kernel, handler)
    await run(kernel)
    assert len(requests) == 2
    assert transport.send.await_count == (1 if preparation == "success" else 0)
    if preparation == "success":
        assert len(transport.send.await_args.args[4]) == 1
    assert cycle(kernel)["actions_executed"] == (2 if preparation == "success" else 1)


@pytest.mark.parametrize("finish", ["tool_calls", "stop"])
@pytest.mark.parametrize("draft", [False, True])
async def test_terminal_engram_commits_only_the_published_source(kernel, finish, draft):
    bot, transport = enable(kernel)
    bot.pop("revision")
    bot["response_protocol"] = "end_cycle"
    kernel.store.put("bots", bot)
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert "append the block to the ordinary answer text that accompanies end_cycle" in str(body)
        if draft:
            return completion(response(body)) if len(requests) == 1 else mixed("", end(), finish=finish)
        return mixed(response(body), end(), finish=finish)

    await install_client(kernel, handler)
    await run(kernel)
    assert transport.send.await_args.args[2] == "Public answer"
    assert state(kernel)[0]["MEM"] == PRIVATE
    assert state(kernel)[0]["covered_through"] == 1
    candidate = kernel.store.one("SELECT request_id,status FROM engram_candidates")
    source = kernel.store.one("SELECT id FROM requests ORDER BY started_at LIMIT 1")
    assert candidate == {"request_id": source["id"], "status": "committed"}
    assert PRIVATE not in str(kernel.store.rows("SELECT content FROM messages"))


@pytest.mark.parametrize("failure", ["filtered", "failed", "uncertain", "cancelled", "companion"])
async def test_unpublished_terminal_engram_never_advances_state(kernel, failure):
    bot, transport = enable(kernel)
    bot.pop("revision")
    bot.update(response_protocol="end_cycle", enabled_plugins=["engram", "memory"])
    kernel.store.put("bots", bot)
    if failure in ("failed", "uncertain"):
        transport.send.side_effect = DeliveryError("fixture", uncertain=failure == "uncertain")
    elif failure == "cancelled":
        transport.send.side_effect = asyncio.CancelledError()
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if failure == "companion" and calls > 1:
            return completion("Missing terminal call")
        companions = [write_call(value=42)] if failure == "companion" else []
        return mixed(
            response(json.loads(request.content)),
            *companions,
            end(),
            finish="content_filter" if failure == "filtered" else "tool_calls",
        )

    await install_client(kernel, handler)
    await run(kernel)
    assert not state(kernel)
    assert kernel.registry.engrams.recover()["committed"] == 0
    assert not kernel.store.rows("SELECT * FROM engram_misses")


async def test_missing_terminal_engram_counts_only_confirmed_reply(kernel):
    bot, _ = enable(kernel)
    bot.pop("revision")
    bot["response_protocol"] = "end_cycle"
    bot = kernel.store.put("bots", bot)
    await install_client(kernel, lambda _: mixed("Public answer without state", end()))
    await run(kernel)
    assert kernel.registry.engrams.capture(bot, "222222222222222222", "next")["missed_updates"]["count"] == 1


@pytest.mark.parametrize("cause", ["strike", "draft", "budget"])
async def test_terminal_only_round_rejects_write_companions(kernel, cause):
    ready(
        kernel,
        response_protocol="end_cycle",
        enabled_plugins=["memory"],
        narration_strikes=2,
        max_tool_rounds=1,
    )
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            if cause == "draft":
                return completion("Draft")
            return mixed("Working" if cause == "strike" else "", write_call(key="first"))
        assert sorted(tool_names(body)) == ["council_silence", "end_cycle"]
        return mixed("Done", write_call(key="forbidden"), end())

    await install_client(kernel, handler)
    await run(kernel)
    assert len(requests) == 2
    assert not kernel.store.one("SELECT 1 FROM memories WHERE key='forbidden'")
    assert not kernel.store.rows("SELECT * FROM outbox")
    assert kernel.store.one("SELECT status FROM turns")["status"] == "failed"


@pytest.mark.parametrize("protocol", ["narrate", "progress", "end_cycle"])
async def test_preparation_failure_still_records_one_cycle(kernel, monkeypatch, protocol):
    ready(kernel, response_protocol=protocol)

    async def fail(*args, **kwargs):
        raise ControlError("Context preparation failed")

    monkeypatch.setattr(kernel.engine.contexts, "prepare", fail)
    await run(kernel)
    ended = events(kernel, "cycle.ended")
    assert len(ended) == 1
    assert ended[0]["reason"] == "failed" and ended[0]["rounds"] == 0
    assert ended[0]["response_protocol"] == protocol


async def test_slash_notices_share_streak_and_do_not_overwrite_receipt(kernel):
    bot = enable_slash(kernel, response_protocol="end_cycle", notify_incomplete_cycle=True)
    await install_client(kernel, lambda _: completion("No terminal call"))
    for index in range(4):
        call = interaction(bot, private=False, interaction_id=str(987654321012345678 + index))
        await kernel.connector.slash.receive("ada", call)
        await settle(kernel)
        assert call.edit_original_response.await_count == 1
        content = call.edit_original_response.await_args.kwargs["content"]
        assert content.startswith("-# ") if index < 3 else content.startswith("Request stopped.")
    notices = kernel.store.rows(
        "SELECT routing FROM outbox WHERE json_extract(routing,'$.kind')='harness_notice'"
    )
    assert len(notices) == 3
    assert all(json.loads(row["routing"])["invocation_kind"] == "slash" for row in notices)
    assert kernel.engine.incomplete_streaks[("ada", str(call.channel_id))] == 4


async def test_slash_end_cycle_delivers_normal_final_and_resets_streak(kernel):
    bot = enable_slash(kernel, response_protocol="end_cycle", notify_incomplete_cycle=True)
    call = interaction(bot)
    key = ("ada", str(call.channel_id))
    kernel.engine.incomplete_streaks[key] = 4
    await install_client(kernel, lambda _: mixed("The final answer", end()))
    await kernel.connector.slash.receive("ada", call)
    await settle(kernel)
    assert call.edit_original_response.await_count == 1
    assert call.edit_original_response.await_args.kwargs["content"] == "The final answer"
    assert key not in kernel.engine.incomplete_streaks
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"


async def test_cancellation_during_incomplete_notice_remains_cancellation(kernel):
    _, transport = ready(kernel, response_protocol="end_cycle", notify_incomplete_cycle=True)
    await install_client(kernel, lambda _: completion("No terminal call"))
    transport.send.side_effect = asyncio.CancelledError()
    await run(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "cancelled"
    assert kernel.store.one("SELECT status FROM outbox")["status"] == "unknown"
    assert events(kernel, "cycle.ended")[0]["reason"] == "cancelled"
    assert not events(kernel, "cycle.notice_failed")


async def test_revoked_bot_gets_no_incomplete_notice(kernel):
    _, transport = ready(kernel, response_protocol="end_cycle", notify_incomplete_cycle=True)
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            bot = kernel.store.get("bots", "ada")
            kernel.store.put("bots", {**bot, "enabled": False})
        return completion("No terminal call")

    await install_client(kernel, handler)
    await run(kernel)
    transport.send.assert_not_awaited()
    assert not kernel.store.rows("SELECT * FROM outbox")
