"""Memory snapshots must remain completely readable without another tool grant."""

import json
from dataclasses import replace

import pytest

from hortator.models import ControlError
from hortator.store import dumps
from hortator.working_set import result_reference
from support.agentic_runtime import grant


@pytest.mark.parametrize("name", ["memory", "global_memory"])
async def test_large_memory_reply_preserves_snapshot_and_pages_with_own_tool(kernel, name):
    context = grant(kernel, name, memory_char_limit=128000, global_memory_char_limit=128000)
    expected = [(f"note-{n:02}", (f'{n}: café 🦆 \\"\n' * 420)) for n in range(14)]
    for key, value in expected:
        written = await kernel.registry.call(
            name, {"operation": "write", "key": key, "value": value}, context, key
        )
        assert written.get("saved"), written
    first = await kernel.registry.call(name, {"operation": "read"}, context, "read-all")
    assert first.get("result_is_paged"), first.keys()
    assert first["total_chars"] > 60000
    assert first["range"]["start"] == 0
    assert len(dumps(first)) < 18000
    assert first["next_tool"] == name
    assert first["budget"]["used_chars"] == sum(len(v) for _, v in expected)
    source_id = first["source_result_id"]
    saved = kernel.store.one("SELECT content FROM tool_result_evidence WHERE id=?", (source_id,))["content"]
    assert first["total_chars"] == len(saved)
    # Subsequent edits cannot shift an already-read snapshot's pages.
    await kernel.registry.call(
        name, {"operation": "write", "key": expected[0][0], "value": "updated"}, context, "edit"
    )
    chunks, page = [first["text"]], first
    while page["next"]:
        args = page["next"]
        assert args["result_id"] == source_id
        page = await kernel.registry.call(name, args, context, f"page-{args['offset']}")
        assert "error" not in page, page
        assert page["source_result_id"] == source_id
        assert page["range"]["start"] == sum(map(len, chunks))
        assert len(dumps(page)) < 18000
        chunks.append(page["text"])
    assert "".join(chunks) == saved
    assert [(n["key"], n["value"]) for n in json.loads(saved)["notes"]] == expected
    reference = result_reference({"content": dumps(first)}, tool_name=name, available_readers=(name,))
    assert reference["reread_tool"] == name
    recovered = await kernel.registry.call(name, reference["reread"], context, "recover")
    assert recovered["text"] == saved[:2000]
    assert recovered["source_result_id"] == source_id
    assert not kernel.store.rows("SELECT seq FROM events WHERE kind='tool.failed'")


@pytest.mark.parametrize("name", ["memory", "global_memory"])
async def test_key_read_returns_only_exact_note_or_empty_without_changing_budget(kernel, name):
    context = grant(kernel, name)
    for key, value in (("topic", "first"), ("topic-extra", "second")):
        await kernel.registry.call(name, {"operation": "write", "key": key, "value": value}, context, key)
    for key, expected in ((" topic ", ["topic"]), ("missing", [])):
        read = await kernel.registry.call(name, {"operation": "read", "key": key}, context, "selected")
        assert [note["key"] for note in read["notes"]] == expected
        assert read["budget"]["used_chars"] == len("firstsecond")
    all_notes = await kernel.registry.call(name, {"operation": "read"}, context, "all")
    assert len(all_notes["notes"]) == 2


@pytest.mark.parametrize("name", ["memory", "global_memory"])
async def test_native_memory_reader_preserves_scope_and_transitive_grant_checks(kernel, name):
    context = grant(kernel, name, "web_fetch")
    original = kernel.registry.evidence.record(context, "web_fetch", "original", {"text": "retained source"})
    args = {"operation": "read_result", "result_id": original["result_id"], "offset": 0, "length": 1000}
    page = await kernel.registry.call(name, args, context, "first-page")
    assert "error" not in page
    other_bot = grant(kernel, name, "web_fetch", bot_id="socrates")
    for other in (
        replace(context, turn_id="other"),
        replace(context, channel_id="other"),
        other_bot,
    ):
        assert kernel.registry.allowed(name, other)
        denied = await kernel.registry.call(name, args, other, "wrong-scope")
        assert denied.get("ok") is False
        assert "another bot, channel or turn" in denied["error"]
    current = kernel.store.get("bots", context.bot["id"])
    kernel.store.put("bots", {**current, "enabled_plugins": [name]})
    for result_id in (original["result_id"], page["result_id"]):
        denied = await kernel.registry.call(name, {**args, "result_id": result_id}, context, "revoked-source")
        assert denied.get("ok") is False and "grant" in denied["error"]


@pytest.mark.parametrize("name", ["memory", "global_memory"])
@pytest.mark.parametrize(
    "arguments",
    [
        {"operation": "read_result"},
        {"operation": "read_result", "result_id": "missing", "offset": -1},
        {"operation": "read_result", "result_id": "missing", "length": "2000"},
        {"operation": "read_result", "result_id": "missing", "length": 18001},
        {"operation": "read", "offset": 2000},
        {"operation": "write", "key": "keep", "value": "wrong", "result_id": "missing"},
        {"operation": "read_result", "result_id": "missing", "key": "keep"},
    ],
)
async def test_native_memory_reader_argument_errors_never_mutate_notes(kernel, name, arguments):
    context = grant(kernel, name)
    await kernel.registry.call(
        name, {"operation": "write", "key": "keep", "value": "unchanged"}, context, "seed"
    )
    invalid = await kernel.registry.call(name, arguments, context, "invalid")
    assert invalid.get("ok") is False
    assert invalid.get("usage")
    notes = await kernel.registry.call(name, {"operation": "read"}, context, "check")
    assert notes["notes"][0]["value"] == "unchanged"


@pytest.mark.parametrize("name", ["memory", "global_memory"])
async def test_escaped_large_reply_keeps_budget_warning_and_maximum_page_is_bounded(kernel, name):
    context = grant(kernel, name, memory_char_limit=50000, global_memory_char_limit=50000)
    for n in range(7):
        written = await kernel.registry.call(
            name, {"operation": "write", "key": str(n), "value": "\x00" * 7200}, context, str(n)
        )
        assert written.get("saved"), written
    first = await kernel.registry.call(name, {"operation": "read"}, context, "large")
    assert first["budget"]["must_consolidate"]
    assert first["budget"]["used_chars"] == 50400
    assert "400 characters over budget" in first["warning"]
    page = await kernel.registry.call(name, {**first["next"], "length": 18000}, context, "maximum")
    assert len(page["text"]) == 18000
    assert len(dumps(page)) < 60000
    assert not page.get("truncated") and not page.get("result_is_paged")


async def test_operator_global_memory_still_reads_current_notes_and_rejects_turn_receipts(kernel, owner):
    context = grant(kernel, "global_memory")
    memory = kernel.registry.global_memory
    await kernel.registry.call(
        "global_memory", {"operation": "write", "key": "topic", "value": "preserved"}, context, "seed"
    )
    assert (
        await kernel.service.global_memory_change(
            owner, context.bot["id"], {"operation": "read", "key": "missing"}
        )
    )["notes"] == []
    with pytest.raises(ControlError):
        await kernel.service.global_memory_change(
            owner, context.bot["id"], {"operation": "read_result", "result_id": "not-an-owner-selector"}
        )
    assert memory.notes(context.bot["id"])[0]["value"] == "preserved"
