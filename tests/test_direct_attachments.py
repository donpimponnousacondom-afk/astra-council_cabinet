"""One call sends saved files and an optional caption through final delivery."""

import asyncio
import json

import pytest

from hortator.plugins import ToolContext
from hortator.runtime import DeliveryError
from support.provider import install_client
from support.runtime import completion, settle
from support.runtime_feedback import ready, reply, responses, tool
from test_end_cycle import mixed


CHANNEL = "222222222222222222"


async def saved_file(kernel, **changes):
    bot, transport = ready(kernel, enabled_plugins=["filesystem", "memory"], **changes)
    plugin = kernel.store.get("plugins", "filesystem")
    kernel.store.put("plugins", {**plugin, "enabled": True})
    result = await kernel.registry.call(
        "filesystem",
        {"operation": "write", "path": "report.md", "content": "Saved report"},
        ToolContext(bot, CHANNEL, "setup"),
        "write",
    )
    assert result.get("ok") is not False, result
    return bot, transport


@pytest.mark.parametrize("mode", ["narrate", "progress", "end_cycle"])
@pytest.mark.parametrize("caption", [None, "Here is the report."])
async def test_saved_file_sent_in_one_response_without_export_or_final_text(kernel, mode, caption):
    _, transport = await saved_file(kernel, response_protocol=mode)
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert "discord_attach" in {t["function"]["name"] for t in body["tools"]}
        args = {"files": ["/workspace/report.md"]}
        if caption is not None:
            args["message"] = caption
        return reply(tool("discord_attach", args))

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await settle(kernel)
    turn = kernel.store.one("SELECT status,error FROM turns")
    assert turn["status"] == "sent", turn
    assert len(requests) == 1
    transport.send.assert_awaited_once()
    assert transport.send.await_args.args[2] == (caption or "")
    assert transport.send.await_args.args[4][0].read_text() == "Saved report"
    receipt = json.loads(
        kernel.store.one(
            "SELECT data FROM events WHERE kind='tool.completed' AND json_extract(data,'$.name')='discord_attach'"
        )["data"]
    )
    assert receipt["posted"] is True and receipt["discord_id"] == "888888888888888888"


@pytest.mark.parametrize("mode", ["progress", "end_cycle"])
async def test_failed_file_does_not_post_caption_and_can_be_corrected(kernel, mode):
    _, transport = await saved_file(kernel, response_protocol=mode)
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return mixed(
                "Do not announce this",
                tool(
                    "discord_attach",
                    {
                        "files": ["missing.md"],
                        "message": "Do not send this either",
                    },
                ),
            )
        assert responses(body)[-1]["ok"] is False
        transport.send.assert_not_awaited()
        assert requests[0]["tools"] == body["tools"]
        return reply(tool("discord_attach", {"files": ["report.md"], "message": "Actual report"}))

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(requests) == 2
    transport.send.assert_awaited_once()
    assert transport.send.await_args.args[2] == "Actual report"


@pytest.mark.parametrize("failure", ["failed", "uncertain", "cancelled"])
async def test_delivery_failure_never_causes_an_extra_generation_or_send(kernel, failure):
    _, transport = await saved_file(kernel)
    transport.send.side_effect = (
        asyncio.CancelledError()
        if failure == "cancelled"
        else DeliveryError("Delivery fixture", uncertain=failure == "uncertain")
    )
    requests = []

    def handler(request):
        requests.append(request)
        return reply(tool("discord_attach", {"files": ["report.md"], "message": "Report"}))

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(requests) == 1
    transport.send.assert_awaited_once()
    assert kernel.store.one("SELECT status FROM outbox")["status"] == (
        "failed" if failure == "failed" else "unknown"
    )
    assert not kernel.store.one(
        "SELECT 1 FROM events WHERE kind='tool.completed' AND json_extract(data,'$.name')='discord_attach'"
    )


async def test_attachment_does_not_bypass_round_limit(kernel):
    _, transport = await saved_file(kernel, max_tool_rounds=1)
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return reply(tool("memory", {"operation": "read"}))
        assert "discord_attach" not in {t["function"]["name"] for t in requests[-1]["tools"]}
        return reply(tool("discord_attach", {"files": ["report.md"]}))

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await settle(kernel)
    transport.send.assert_not_awaited()
    assert len(requests) == 2


async def test_attachment_cannot_run_beside_file_mutation(kernel):
    _, transport = await saved_file(kernel)
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply(
                tool("filesystem", {"operation": "write", "path": "extra.txt", "content": "bad"}, "write"),
                tool("discord_attach", {"files": ["report.md"]}, "attach"),
            )
        assert all(not r["executed"] for r in responses(body))
        return completion("Batch refused")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await settle(kernel)
    assert not kernel.store.rows("SELECT * FROM artifacts")
    assert len(requests) == 2
    assert transport.send.await_args.args[4] == []


async def test_attachment_native_schema_is_stable_when_artifacts_appear(kernel):
    bot, _ = ready(kernel)
    context = ToolContext(bot, CHANNEL, "schema-turn")
    before, available = kernel.engine.plan_round_tools(context, [], 0, 3)
    assert available
    kernel.registry.artifact(b"image", "image/png", ".png", context)
    after, available = kernel.engine.plan_round_tools(context, [], 1, 3)
    assert available and before == after


@pytest.mark.parametrize("reverse", [False, True])
async def test_existing_end_cycle_with_attachment_sends_once(kernel, reverse):
    _, transport = await saved_file(kernel, response_protocol="end_cycle")
    calls = [
        tool("discord_attach", {"files": ["report.md"], "message": "Report"}, "attach"),
        tool("end_cycle", {}, "end"),
        tool("memory", {"operation": "write", "key": "done", "value": "yes"}, "write"),
    ]
    await install_client(kernel, lambda _: reply(*(list(reversed(calls)) if reverse else calls)))
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.store.one("SELECT count(*) n FROM requests")["n"] == 1
    transport.send.assert_awaited_once()
    assert transport.send.await_args.args[2] == "Report"
    assert kernel.store.one("SELECT value FROM memories WHERE key='done'")["value"] == "yes"


@pytest.mark.parametrize("uncertain", [False, True])
async def test_private_engram_in_caption_is_never_posted_and_requires_confirmed_send(kernel, uncertain):
    from test_engram_integration import PRIVATE, enable, response, state

    bot, transport = enable(kernel)
    if uncertain:
        transport.send.side_effect = DeliveryError("Uncertain fixture", uncertain=True)

    def handler(request):
        body = json.loads(request.content)
        context = ToolContext(bot, CHANNEL, kernel.store.one("SELECT id FROM turns")["id"])
        artifact = kernel.registry.artifact(b"report", "text/plain", ".txt", context)["artifact_id"]
        return reply(tool("discord_attach", {"artifact_ids": [artifact], "message": response(body)}))

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.store.one("SELECT count(*) n FROM requests")["n"] == 1
    assert transport.send.await_args is not None, kernel.store.one("SELECT status,error FROM turns")
    assert transport.send.await_args.args[2] == "Public answer"
    assert PRIVATE not in str(kernel.store.rows("SELECT content FROM messages"))
    if uncertain:
        assert not state(kernel)
    else:
        assert state(kernel)[0]["MEM"] == PRIVATE
    turn_id = kernel.store.one("SELECT id FROM turns")["id"]
    assert PRIVATE not in str(kernel.service.inspect_model("turn", turn_id))


async def test_file_only_interaction_keeps_private_response_and_finishes_once(kernel):
    from support.slash_commands import enable, interaction

    bot = enable(kernel)
    item = interaction(bot)

    def handler(request):
        context = ToolContext(bot, str(item.channel_id), kernel.store.one("SELECT id FROM turns")["id"])
        artifact = kernel.registry.artifact(b"report", "text/plain", ".txt", context)["artifact_id"]
        return reply(tool("discord_attach", {"artifact_ids": [artifact]}))

    await install_client(kernel, handler)
    await kernel.connector.slash.receive(bot["id"], item)
    await settle(kernel)
    assert kernel.store.one("SELECT count(*) n FROM requests")["n"] == 1
    assert kernel.store.one("SELECT status,private FROM slash_invocations") == {
        "status": "sent",
        "private": 1,
    }
    item.edit_original_response.assert_awaited_once()
    assert len(item.edit_original_response.call_args.kwargs["attachments"]) == 1
