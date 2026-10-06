"""Recovery instructions must name an executable reader, including native memory paging."""

import copy
import json
from unittest.mock import AsyncMock

import pytest

from hortator.plugins import ToolContext
from hortator.store import dumps
from hortator.working_set import RESULT_READERS, bound_exchanges, result_reference
from support.agentic_runtime import grant
from support.provider import install_client
from support.runtime import settle
from support.runtime_feedback import ready, reply, responses, tool


@pytest.mark.parametrize("memory", ["memory", "global_memory"])
@pytest.mark.parametrize("reader", RESULT_READERS)
async def test_memory_reference_names_an_executable_granted_reader(kernel, memory, reader):
    dependencies = ["workspace"] if reader == "shell" else []
    context = grant(kernel, memory, reader, *dependencies, role="hortator")
    context = ToolContext(context.bot, context.channel_id, context.turn_id, owner_verified=True)
    written = await kernel.registry.call(
        memory, {"operation": "write", "key": "topic", "value": "café 🙂 preserved"}, context, "write"
    )
    assert "error" not in written
    original = await kernel.registry.call(memory, {"operation": "read"}, context, "read")
    reference = result_reference({"content": dumps(original)}, tool_name=memory, available_readers=(reader,))
    assert reference["reread_tool"] == reader
    assert type(reference["reread"]["offset"]) is int
    assert type(reference["reread"]["length"]) is int
    page = await kernel.registry.call(reader, reference["reread"], context, "recover")
    assert "error" not in page
    assert json.loads(page["text"])["notes"][0]["value"] == "café 🙂 preserved"
    # Advice is not a grant: the existing source-grant check still owns access.
    current = kernel.store.get("bots", "ada")
    kernel.store.put(
        "bots", {**current, "enabled_plugins": [reader, *dependencies] if reader != memory else []}
    )
    denied = await kernel.registry.call(reader, reference["reread"], context, "revoked")
    assert denied["ok"] is False
    assert "grant" in denied["error"] or "not enabled" in denied["error"]


@pytest.mark.parametrize("indexed", [False, True])
def test_carried_references_refresh_reader_even_below_budget(kernel, indexed):
    reference = result_reference(
        {"content": dumps({"result_id": "source"})},
        tool_name="global_memory",
        available_readers=("web_fetch",),
    )
    if indexed:
        extras = [{"role": "user", "content": "old index", "_working_set_index": {"references": [reference]}}]
    else:
        extras = [
            {"role": "assistant", "tool_calls": [tool("global_memory", {"operation": "read"})]},
            {"role": "tool", "tool_call_id": "call", "content": dumps(reference)},
        ]
    before = copy.deepcopy(extras)
    changed, _ = bound_exchanges(
        extras, kernel.engine.contexts.estimate, 6000, available_readers=("dumb_search",)
    )
    assert extras == before

    def get_reference(messages):
        return (
            messages[0]["_working_set_index"]["references"][0]
            if indexed
            else json.loads(messages[-1]["content"])
        )

    assert get_reference(changed)["reread_tool"] == "dumb_search"
    exhausted, _ = bound_exchanges(changed, kernel.engine.contexts.estimate, 6000, available_readers=())
    final = get_reference(exhausted)
    assert final["reread_unavailable"] is True
    assert "reread" not in final and "reread_tool" not in final


def test_native_reader_is_preferred_and_inspector_fallback_converts_paging():
    message = {"content": dumps({"result_id": "source"})}
    reference = result_reference(
        message, tool_name="dumb_search", available_readers=("web_fetch", "dumb_search")
    )
    assert reference["reread_tool"] == "dumb_search"
    page = {
        "result_id": "page",
        "source_result_id": "original",
        "source_tool": "council_inspect",
        "range": {"start": 2000, "end": 4000},
        "next": {"resource": "read_result", "result_id": "original", "offset": 4000, "length": 2000},
        "read_response": {"resource": "read_result", "result_id": "page"},
    }
    reference = result_reference({"content": dumps(page)}, available_readers=("web_fetch",))
    assert reference["reread_tool"] == reference["next_tool"] == "web_fetch"
    assert reference["reread"]["operation"] == reference["next"]["operation"] == "read_result"
    assert reference["reread"]["offset"] == 2000 and reference["next"]["offset"] == 4000
    assert "read_response" not in reference
    missing = result_reference({"content": dumps(reference)}, available_readers=())
    assert missing["reread_unavailable"] and "reread" not in missing and "next" not in missing


@pytest.mark.parametrize("memory", ["memory", "global_memory"])
@pytest.mark.parametrize("exhausted", [False, True])
async def test_runtime_large_memory_read_recovers_every_note_through_named_local_reader(
    kernel, memory, exhausted
):
    rounds = 1 if exhausted else 30
    ready(kernel, enabled_plugins=[memory], max_tool_rounds=rounds, tool_working_set_tokens=3000)
    context = grant(kernel, memory, max_tool_rounds=rounds, tool_working_set_tokens=3000)
    expected = [
        "".join(f"Note {key} entry {n:05d}: café 🙂 verified observation.\n" for n in range(90))
        for key in range(5)
    ]
    for key, value in enumerate(expected):
        result = await kernel.registry.call(
            memory, {"operation": "write", "key": str(key), "value": value}, context, f"seed-{key}"
        )
        assert "error" not in result
    profile = kernel.store.get("profiles", "balanced")
    kernel.store.put("profiles", {**profile, "context_window": 128000})
    fetcher = AsyncMock(side_effect=AssertionError("Evidence reads must not fetch a URL"))
    kernel.registry.fetched_documents.fetcher = fetcher
    bodies, chunks = [], []

    async def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return reply(tool(memory, {"operation": "read"}))
        result = responses(body)[-1]
        if len(bodies) == 2:
            assert result["omitted_from_active_prompt"]
            if exhausted:
                assert result["reread_unavailable"] and "reread_tool" not in result
                assert "reread" not in result
                return reply(tool("council_silence", {"label": "Unable to verify omitted notes"}))
            assert result["reread_tool"] == memory
            assert result["reread_tool"] in {t["function"]["name"] for t in body["tools"]}
            return reply(tool(result["reread_tool"], result["reread"]))
        assert "error" not in result and not result.get("omitted_from_active_prompt")
        chunks.append(result["text"])
        if result["next"]:
            return reply(tool(memory, result["next"]))
        return reply(tool("council_silence", {"label": "Every note read"}))

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    turn = kernel.store.one("SELECT status,error FROM turns")
    assert turn["status"] == "silent", turn["error"]
    if exhausted:
        assert not chunks and len(bodies) == 2
    else:
        assert [note["value"] for note in json.loads("".join(chunks))["notes"]] == expected
        assert len(chunks) > 1
    assert not kernel.store.rows("SELECT seq FROM events WHERE kind='tool.failed'")
    fetcher.assert_not_awaited()
