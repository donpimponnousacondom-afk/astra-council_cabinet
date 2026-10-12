"""Immutable recovery reproductions for failed finishes and once-only side effects."""

import asyncio
import json

import pytest

from hortator.runtime import DeliveryError
from support.provider import install_client
from support.runtime import completion
from support.runtime_feedback import ready
from test_end_cycle import (
    cycle,
    end,
    events,
    harness_notices,
    mixed,
    run,
    tool,
    tool_names,
    write_call,
)
from test_engram_integration import PRIVATE, enable, response, state


@pytest.mark.parametrize("terminal", [False, True])
async def test_saved_work_is_not_replayed_when_confirmation_hallucinates_a_write(kernel, terminal):
    _, transport = ready(
        kernel, response_protocol="end_cycle", enabled_plugins=["memory"], narration_strikes=3
    )
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        if calls == 1:
            return mixed("Original answer", write_call(call_id="original"))
        assert sorted(tool_names(body)) == ["council_silence", "end_cycle"]
        if calls == 2:
            return mixed(
                "Replacement",
                write_call(call_id="repeat", value="Must not overwrite"),
                *([end()] if terminal else []),
            )
        assert calls == 3
        return mixed("", end())

    await install_client(kernel, handler)
    await run(kernel)
    assert calls == 3
    assert kernel.store.one("SELECT value FROM memories")["value"] == "Saved fact"
    assert cycle(kernel)["actions_executed"] == 1 and cycle(kernel)["strikes"] == 2
    assert transport.send.await_args.args[2] == "Original answer"
    assert len(events(kernel, "cycle.draft_retained")) == 1


@pytest.mark.parametrize("terminal_error", ["arguments", "duplicate", "no_call"])
async def test_failed_finish_freezes_source_and_uses_only_confirmation(kernel, terminal_error):
    _, transport = ready(kernel, response_protocol="end_cycle")
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            if terminal_error == "arguments":
                return mixed("Source", tool("end_cycle", {"message": "Wrong place"}, call_id="bad"))
            if terminal_error == "duplicate":
                return mixed("Source", end("end1"), end("end2"))
            return completion("Source")
        body = json.loads(request.content)
        assert sorted(tool_names(body)) == ["council_silence", "end_cycle"]
        assert "saved as a frozen draft" in harness_notices(body)[-1]
        return mixed("Regenerated text must not replace Source", end())

    await install_client(kernel, handler)
    await run(kernel)
    assert calls == 2 and transport.send.await_args.args[2] == "Source"
    assert cycle(kernel)["strikes"] == 1


async def test_caterpillar_empty_finish_saves_global_note_once_and_stops(kernel):
    _, transport = ready(kernel, response_protocol="end_cycle", enabled_plugins=["global_memory"])
    plugin = kernel.store.get("plugins", "global_memory")
    kernel.store.put("plugins", {**plugin, "enabled": True})
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        assert count == 1
        return mixed(
            "",
            tool(
                "global_memory",
                {"operation": "write", "key": "live-test", "value": "Actual requested note"},
                call_id="write",
            ),
            end(),
        )

    await install_client(kernel, handler)
    await run(kernel)
    assert (
        kernel.store.one("SELECT value FROM global_memories WHERE key='live-test'")["value"]
        == "Actual requested note"
    )
    assert cycle(kernel)["actions_executed"] == 1
    assert cycle(kernel)["reason"] == "incomplete" and cycle(kernel)["rounds"] == 1
    transport.send.assert_not_awaited()


@pytest.mark.parametrize("source", ["text", "narrated_write", "invalid_end"])
async def test_confirmed_engram_comes_from_frozen_source_not_confirmation(kernel, source):
    bot, transport = enable(kernel)
    bot.pop("revision")
    bot.update(response_protocol="end_cycle", enabled_plugins=["engram", "memory"])
    kernel.store.put("bots", bot)
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            text = response(body)
            if source == "narrated_write":
                return mixed(text, write_call())
            if source == "invalid_end":
                return mixed(text, tool("end_cycle", {"bad": True}, call_id="invalid"))
            return completion(text)
        return mixed(response(body, content="Wrong replacement", mem="Wrong state"), end())

    await install_client(kernel, handler)
    await run(kernel)
    assert len(bodies) == 2
    assert transport.send.await_args.args[2] == "Public answer"
    assert state(kernel)[0]["MEM"] == PRIVATE
    source_request = kernel.store.one("SELECT id FROM requests ORDER BY started_at LIMIT 1")["id"]
    assert kernel.store.one("SELECT request_id,status FROM engram_candidates") == {
        "request_id": source_request,
        "status": "committed",
    }
    assert cycle(kernel)["reason"] == "draft_published"
    assert PRIVATE not in str(kernel.store.rows("SELECT content FROM messages"))


@pytest.mark.parametrize("failure", ["delivery", "uncertain", "cancelled", "revoked"])
async def test_draft_confirmation_never_commits_engram_without_delivery(kernel, failure):
    bot, transport = enable(kernel)
    bot.pop("revision")
    bot["response_protocol"] = "end_cycle"
    kernel.store.put("bots", bot)
    if failure == "cancelled":
        transport.send.side_effect = asyncio.CancelledError()
    elif failure != "revoked":
        transport.send.side_effect = DeliveryError("fixture", uncertain=failure == "uncertain")
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return completion(response(json.loads(request.content)))
        if failure == "revoked":
            current = kernel.store.get("bots", "ada")
            kernel.store.put("bots", {**current, "enabled": False})
        return mixed("", end())

    await install_client(kernel, handler)
    await run(kernel)
    assert calls == 2
    assert not state(kernel)
    assert kernel.registry.engrams.recover()["committed"] == 0
    assert cycle(kernel)["published"] is False
    if failure == "revoked":
        transport.send.assert_not_awaited()


async def test_engram_without_public_answer_is_not_a_publishable_draft(kernel):
    bot, transport = enable(kernel)
    bot.pop("revision")
    bot["response_protocol"] = "end_cycle"
    kernel.store.put("bots", bot)
    await install_client(kernel, lambda req: mixed(response(json.loads(req.content), content=""), end()))
    await run(kernel)
    assert cycle(kernel)["rounds"] == 1 and not state(kernel)
    transport.send.assert_not_awaited()


@pytest.mark.parametrize("failure", ["length", "content_filter"])
async def test_incomplete_generation_never_becomes_confirmable_draft(kernel, failure):
    _, transport = ready(kernel, response_protocol="end_cycle")
    await install_client(kernel, lambda _: mixed("Truncated text", end(), finish=failure))
    await run(kernel)
    assert cycle(kernel)["rounds"] == 1
    assert not events(kernel, "cycle.draft_retained")
    transport.send.assert_not_awaited()


async def test_invalid_terminal_arguments_do_not_invalidate_a_successful_write(kernel):
    _, transport = ready(kernel, response_protocol="end_cycle", enabled_plugins=["memory"])
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return mixed(
                "Saved the note", write_call(), tool("end_cycle", {"text": "wrong"}, call_id="bad_end")
            )
        assert count == 2 and "memory" not in tool_names(json.loads(request.content))
        return mixed("", end())

    await install_client(kernel, handler)
    await run(kernel)
    assert count == 2
    assert cycle(kernel)["actions_executed"] == 1
    assert transport.send.await_args.args[2] == "Saved the note"
    assert len(kernel.store.rows("SELECT * FROM memories")) == 1


async def test_confirmation_uses_source_context_coverage(kernel, monkeypatch):
    bot, transport = enable(kernel)
    bot.pop("revision")
    bot["response_protocol"] = "end_cycle"
    kernel.store.put("bots", bot)
    original_assemble = kernel.engine.contexts.assemble

    async def assemble(*args, **kwargs):
        messages, metadata = await original_assemble(*args, **kwargs)
        if kwargs.get("round_index", args[5] if len(args) > 5 else 0) > 0:
            # Simulate different metadata during later context assembly: the
            # published Engram must still describe the retained source request.
            metadata = {**metadata, "message_sequences": []}
        return messages, metadata

    monkeypatch.setattr(kernel.engine.contexts, "assemble", assemble)
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return completion(response(json.loads(request.content))) if count == 1 else mixed("", end())

    await install_client(kernel, handler)
    await run(kernel)
    assert transport.send.await_args.args[2] == "Public answer"
    assert state(kernel)[0]["covered_through"] == 1


async def test_engram_confirmation_rejects_mismatched_retained_source(kernel):
    bot, transport = enable(kernel)
    bot.pop("revision")
    bot["response_protocol"] = "end_cycle"
    kernel.store.put("bots", bot)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return completion(response(json.loads(request.content)))
        kernel.store.execute(
            "UPDATE events SET data=json_set(data,'$.content_sha256','wrong') WHERE kind='cycle.draft_retained'"
        )
        return mixed("", end())

    await install_client(kernel, handler)
    await run(kernel)
    assert calls == 2 and not state(kernel)
    transport.send.assert_not_awaited()
    assert "differs from its source" in kernel.store.one("SELECT error FROM turns")["error"]
