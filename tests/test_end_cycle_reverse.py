"""One first-answer request after end_cycle, never another tool or retry loop."""

import asyncio
import json

import pytest

from hortator.runtime import DeliveryError
from support.provider import install_client
from support.runtime import completion
from support.runtime_feedback import ready
from test_end_cycle import cycle, end, events, mixed, run, tool_names, write_call
from test_engram_integration import PRIVATE, enable, response, state


@pytest.mark.parametrize("strikes", [1, 2, 5])
@pytest.mark.parametrize("budget", [0, 1, 8])
async def test_bare_finish_gets_exactly_one_first_answer_even_at_budget_boundary(kernel, strikes, budget):
    _, transport = ready(
        kernel, response_protocol="end_cycle", narration_strikes=strikes, max_tool_rounds=budget
    )
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return mixed("", end())
        assert len(bodies) == 2 and not tool_names(body)
        assert "Write your first public answer now" in str(body["messages"])
        return completion("The first public answer")

    await install_client(kernel, handler)
    await run(kernel)
    assert len(bodies) == 2
    assert transport.send.await_args.args[2] == "The first public answer"
    assert cycle(kernel)["strikes"] == 0
    assert cycle(kernel)["reason"] == "end_cycle_then_text"
    assert not events(kernel, "cycle.draft_retained")
    assert len(events(kernel, "cycle.answer_pending")) == 1


@pytest.mark.parametrize(
    "failure", ["empty", "end", "action", "text_action", "length", "content_filter", "wrapped"]
)
async def test_first_answer_failure_never_executes_tools_or_retries(kernel, failure):
    _, transport = ready(
        kernel, response_protocol="end_cycle", narration_strikes=5, enabled_plugins=["memory"]
    )
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return mixed("", end())
        assert count == 2 and not tool_names(json.loads(request.content))
        return {
            "empty": completion(""),
            "end": mixed("", end()),
            "action": mixed("", write_call()),
            "text_action": mixed("Don't publish this", write_call()),
            "length": mixed("Partial", finish="length"),
            "content_filter": mixed("Partial", finish="content_filter"),
            "wrapped": completion('<tool_call>{"name":"end_cycle"}</tool_call>'),
        }[failure]

    await install_client(kernel, handler)
    await run(kernel)
    assert count == 2
    transport.send.assert_not_awaited()
    assert not kernel.store.rows("SELECT * FROM memories")
    assert not events(kernel, "cycle.draft_retained")
    assert cycle(kernel)["published"] is False


async def test_failed_companion_does_not_open_first_answer_request(kernel):
    _, transport = ready(kernel, response_protocol="end_cycle", enabled_plugins=["memory"])
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        assert count == 1
        return mixed("", write_call(call_id="good"), write_call(call_id="bad", value=42), end())

    await install_client(kernel, handler)
    await run(kernel)
    assert kernel.store.one("SELECT value FROM memories")["value"] == "Saved fact"
    assert cycle(kernel)["actions_executed"] == 1
    assert not events(kernel, "cycle.answer_pending")
    transport.send.assert_not_awaited()


@pytest.mark.parametrize("failure", [None, "failed", "uncertain", "cancelled", "revoked", "bad_provenance"])
async def test_reverse_engram_uses_first_answer_and_requires_delivery(kernel, failure):
    bot, transport = enable(kernel)
    bot.pop("revision")
    bot["response_protocol"] = "end_cycle"
    kernel.store.put("bots", bot)
    if failure == "cancelled":
        transport.send.side_effect = asyncio.CancelledError()
    elif failure in ("failed", "uncertain"):
        transport.send.side_effect = DeliveryError("fixture", uncertain=failure == "uncertain")
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return mixed("", end())
        assert count == 2 and not tool_names(json.loads(request.content))
        if failure == "revoked":
            kernel.store.put("bots", {**kernel.store.get("bots", "ada"), "enabled": False})
        if failure == "bad_provenance":
            kernel.store.execute(
                "UPDATE events SET data=json_set(data,'$.end_call_id','wrong') WHERE kind='cycle.answer_pending'"
            )
        return completion(response(json.loads(request.content)))

    await install_client(kernel, handler)
    await run(kernel)
    assert count == 2
    if failure:
        assert not state(kernel)
        assert kernel.registry.engrams.recover()["committed"] == 0
        assert not cycle(kernel)["published"]
        if failure in ("revoked", "bad_provenance"):
            transport.send.assert_not_awaited()
    else:
        assert transport.send.await_args.args[2] == "Public answer"
        assert state(kernel)[0]["MEM"] == PRIVATE
        answer_request = kernel.store.one("SELECT id FROM requests ORDER BY started_at DESC LIMIT 1")["id"]
        assert kernel.store.one("SELECT request_id,status FROM engram_candidates") == {
            "request_id": answer_request,
            "status": "committed",
        }
        assert events(kernel, "cycle.answer_published")[0]["end_request_id"] != answer_request
        assert events(kernel, "delivery.queued")[0]["request_id"] == answer_request


@pytest.mark.parametrize("diagnostic_failure", [False, True])
async def test_confirmed_first_answer_keeps_reverse_linkage_if_voice_companion_is_cancelled(
    kernel, monkeypatch, diagnostic_failure
):
    from hortator.plugins import ToolContext
    from test_end_cycle import read_call, tool
    from test_voice_notes import note

    bot, transport = ready(
        kernel, response_protocol="end_cycle", enabled_plugins=["memory"], cooldown_seconds=0
    )
    transport.send.side_effect = ["666666666666666666", asyncio.CancelledError()]
    emit = kernel.store.emit

    def record(kind, *args, **kwargs):
        if diagnostic_failure and kind == "cycle.answer_published":
            raise OSError("Injected diagnostic event-write failure")
        return emit(kind, *args, **kwargs)

    monkeypatch.setattr(kernel.store, "emit", record)

    count = 0
    artifact_id = None

    def handler(request):
        nonlocal count, artifact_id
        count += 1
        if count == 1:
            turn_id = kernel.store.one("SELECT id FROM turns")["id"]
            artifact_id = note(kernel, ToolContext(bot, "222222222222222222", turn_id))
            return mixed("", read_call())
        if count == 2:
            return mixed("", tool("discord_attach", {"artifact_ids": [artifact_id]}, call_id="attach"), end())
        assert count == 3 and not tool_names(json.loads(request.content))
        return completion("Confirmed first answer")

    await install_client(kernel, handler)
    await run(kernel)
    assert count == 3
    assert [r["status"] for r in kernel.store.rows("SELECT status FROM outbox ORDER BY created_at")] == [
        "sent",
        "unknown",
    ]
    assert cycle(kernel)["published"] is True
    assert cycle(kernel)["reason"] == "end_cycle_then_text"
    assert len(events(kernel, "cycle.answer_published")) == (0 if diagnostic_failure else 1)
    turn = kernel.store.one("SELECT status,error FROM turns")
    assert turn["status"] == "sent"
    assert "Companion delivery interrupted" in turn["error"]
