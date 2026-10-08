"""Model-visible pages must make progress independent of the model tokenizer."""

import copy
import hashlib
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from hortator.http_evidence import HTTPResponse
from hortator.models import Bot
from hortator.plugins import ToolContext
from hortator.store import dumps
from hortator.working_set import bound_exchanges, page_result, prompt_exchanges
from support.agentic_runtime import grant
from support.provider import install_client
from support.runtime import settle
from support.runtime_feedback import ready, reply, responses, tool


async def test_token_dense_web_read_is_visible_on_first_exposure_and_reaches_eof(kernel):
    ready(kernel, enabled_plugins=["web_fetch"], max_tool_rounds=4)
    context = grant(kernel, "web_fetch", max_tool_rounds=4)
    profile = kernel.store.get("profiles", "balanced")
    kernel.store.put("profiles", {**profile, "context_window": 128000})
    text = "\n".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(340))
    fetcher = AsyncMock(return_value=(text.encode(), "text/plain", "https://example.com/source"))
    kernel.registry.fetched_documents.fetcher = fetcher
    chunks, bodies = [], []

    async def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return reply(tool("web_fetch", {"url": "https://example.com/source"}))
        result = responses(body)[-1]
        assert not result.get("omitted_from_active_prompt")
        assert result["text"]
        assert result["range"]["start"] == sum(map(len, chunks))
        chunks.append(result["text"])
        if result["next"]:
            return reply(tool("web_fetch", result["next"]))
        return reply(tool("council_silence", {"label": "Read complete"}))

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    turn = kernel.store.one("SELECT status,error FROM turns")
    assert turn["status"] == "silent", turn["error"]
    assert "".join(chunks) == text and len(bodies) == 3
    fetcher.assert_awaited_once()
    assert kernel.engine.contexts.estimate(responses(bodies[1])[-1]) > 6000
    assert context.bot["tool_working_set_chars"] == 120000


async def test_generic_large_result_is_saved_intact_and_readable_without_reexecuting(kernel):
    context = grant(kernel, "workspace", "web_fetch")
    expected = {"ok": True, "payload": 'café 🙂 \\"\n' * 14000, "tail": "PRESERVED-END"}
    handler = AsyncMock(return_value=expected)
    kernel.registry.specs["workspace"] = replace(kernel.registry.specs["workspace"], handler=handler)
    first = await kernel.registry.call("workspace", {"operation": "list"}, context, "original")
    assert first["result_is_paged"] and first["text"]
    source_id = first["source_result_id"]
    saved = kernel.store.one("SELECT content FROM tool_result_evidence WHERE id=?", (source_id,))
    assert json.loads(saved["content"]) == expected
    chunks, page = [first["text"]], first
    while page["next"]:
        page = await kernel.registry.call("web_fetch", page["next"], context, "page")
        assert page["range"]["start"] == sum(map(len, chunks))
        assert len(dumps(page)) <= 30000
        chunks.append(page["text"])
    assert json.loads("".join(chunks)) == expected
    handler.assert_awaited_once()


def test_parallel_native_pages_keep_text_and_exact_document_continuations():
    extras = [{"role": "assistant", "tool_calls": []}]
    for i in range(8):
        extras[0]["tool_calls"].append(tool("web_fetch", {"operation": "read"}, str(i)))
        extras.append(
            {
                "role": "tool",
                "tool_call_id": str(i),
                "content": dumps(
                    {
                        "result_id": f"result_{i}",
                        "document_id": f"fetch_{i}",
                        "text": '🙂 café \\"\n' * 2000,
                        "range": {"start": 18000, "end": 38000},
                        "total_chars": 90000,
                    }
                ),
            }
        )
    original = copy.deepcopy(extras)
    bounded, meta = bound_exchanges(extras, 16000, available_readers=("web_fetch",))
    assert extras == original
    assert meta["characters"] <= 16000
    values = [json.loads(m["content"]) for m in bounded if m["role"] == "tool"]
    assert len(values) == 8
    for value in values:
        assert value["text"] and not value.get("omitted_from_active_prompt")
        assert value["range"]["end"] == 18000 + len(value["text"])
        assert value["next"]["offset"] == value["range"]["end"]
        assert value["next"]["operation"] == "read"


def test_repeated_page_projection_does_not_nest_json_or_shift_source_offsets():
    value = {
        "result_id": "page_receipt",
        "source_result_id": "original_receipt",
        "source_tool": "memory",
        "text": "界\n\\" * 6000,
        "range": {"start": 7000, "end": 25000},
        "total_chars": 50000,
    }
    for limit in (8000, 4000, 2000):
        value = page_result(value, limit, available_readers=("memory",), tool_name="memory")
        assert len(dumps(value)) <= limit
        assert value["source_result_id"] == "original_receipt"
        assert value["range"]["start"] == 7000
        assert value["next"]["offset"] == 7000 + len(value["text"])
        assert value["next"]["result_id"] == "original_receipt"


def test_overall_context_guard_can_shorten_a_page_without_omitting_it():
    extras = [
        {"role": "assistant", "tool_calls": [tool("memory", {"operation": "read"})]},
        {
            "role": "tool",
            "tool_call_id": "call",
            "content": dumps({"result_id": "original", "notes": "x" * 20000}),
        },
    ]
    bounded, _ = bound_exchanges(
        extras,
        120000,
        available_readers=("memory",),
        fits_context=lambda messages: len(dumps(messages)) < 5000,
    )
    assert len(dumps(prompt_exchanges(bounded))) < 5000
    result = json.loads(bounded[-1]["content"])
    assert result["text"] and result["next"]["offset"] == len(result["text"])


def test_legacy_token_setting_is_accepted_but_does_not_control_character_pages():
    bot = Bot.model_validate(
        {"id": "test", "name": "Test", "model_profile_id": "balanced", "tool_working_set_tokens": 512}
    )
    assert bot.model_dump()["tool_working_set_chars"] == 120000
    assert "tool_working_set_tokens" not in bot.model_dump()


@pytest.mark.parametrize("shape", ["object", "list", "string", "foreign_source"])
async def test_plugin_without_native_reader_gets_scoped_fallback_and_preserves_source_grants(kernel, shape):
    context = grant(kernel, "research_assistant")
    expected = {"rows": ["🙂 observation " * 3000], "tail": "complete"}
    result = expected if shape == "object" else [expected] if shape == "list" else dumps(expected)
    if shape == "foreign_source":
        result = {
            "source_result_id": "plugin-document-42",
            "text": "x" * 40000,
            "range": {"start": 0, "end": 40000},
            "total_chars": 40000,
        }
    handler = AsyncMock(return_value=result)
    kernel.registry.specs["research_assistant"] = replace(
        kernel.registry.specs["research_assistant"], handler=handler
    )
    names = {x["function"]["name"] for x in kernel.registry.schemas(context)}
    assert names == {"research_assistant", "tool_result_read"}
    first = await kernel.registry.call_raw(
        "research_assistant", dumps({"operation": "list"}), context, "list"
    )
    assert first["next_tool"] == "tool_result_read"
    foreign = ToolContext(context.bot, "other-channel", context.turn_id)
    denied = await kernel.registry.call("tool_result_read", first["next"], foreign, "foreign")
    assert not denied["ok"] and "another bot, channel or turn" in denied["error"]
    parts, page = [first["text"]], first
    while page["next"]:
        page = await kernel.registry.call_raw("tool_result_read", dumps(page["next"]), context, "page")
        parts.append(page["text"])
    assert json.loads("".join(parts)) == (result if shape == "object" else {"value": result})
    handler.assert_awaited_once()
    # A newly granted reader cannot recover pages whose original grant was revoked.
    current = grant(kernel, "memory")
    denied = await kernel.registry.call(
        "memory", {"operation": "read_result", "result_id": page["result_id"]}, current, "revoked"
    )
    assert not denied["ok"] and "grant" in denied["error"]


async def test_paged_native_fetch_keeps_http_failure_evidence_and_exhausted_round_is_explicit(kernel):
    context = grant(kernel, "web_fetch")
    url = "https://example.com/error"
    text = "\n" * 18000
    kernel.registry.fetched_documents.fetcher = AsyncMock(
        return_value=HTTPResponse(text.encode(), "text/plain", url, 404, "Not Found")
    )
    value = await kernel.registry.call_raw("web_fetch", dumps({"url": url}), context, "fetch")
    assert value["result_is_paged"] and value["text"]
    assert value["truncated"] is True and value["has_more"] is True
    assert value["url"] == url and value["status"] == "ready"
    assert value["http_response"]["http_status"] == 404
    assert value["http_response"]["http_reason"] == "Not Found"
    assert value["http_response"]["capture_complete"] is True
    raw = await kernel.registry.call_raw(
        "web_fetch", dumps(value["http_response"]["read_response"]), context, "response"
    )
    assert '"http_status":404' in raw["text"]
    # Fresh native output can also be first projected by the working-set bound
    # when the result itself fits the per-result ceiling but tool rounds ran out.
    fresh = {**value, "text": "x" * 18000, "range": {"start": 0, "end": 18000}}
    fresh.pop("result_is_paged")
    extras = [
        {"role": "assistant", "tool_calls": [tool("web_fetch", {"url": url})]},
        {"role": "tool", "tool_call_id": "call", "content": dumps(fresh)},
    ]
    bounded, _ = bound_exchanges(extras, 12000, available_readers=())
    page = json.loads(bounded[-1]["content"])
    assert page["text"] and page["has_more"] and page["reread_unavailable"]
    assert page["next"] is None and page["next_tool"] is None
    assert page["http_response"]["http_status"] == 404


def test_zero_fit_native_reference_does_not_advertise_an_unavailable_document_read():
    body = {
        "result_id": "result_receipt",
        "document_id": "fetch_document",
        "text": "x" * 18000,
        "range": {"start": 0, "end": 18000},
        "total_chars": 30000,
        "next": {"operation": "read", "document_id": "fetch_document", "offset": 18000},
        "http_response": {"http_status": 404, "url": "https://example.com/" + "x" * 900},
    }
    result = page_result(body, 800, available_readers=(), tool_name="web_fetch")
    assert result["reread_unavailable"]
    assert not result.get("next") and not result.get("next_tool")


async def test_many_tool_pairs_fit_the_final_request_including_message_overhead(kernel):
    ready(kernel, enabled_plugins=["memory"], max_tool_rounds=20)
    grant(kernel, "memory", max_tool_rounds=20)
    profile = kernel.store.get("profiles", "balanced")
    kernel.store.put("profiles", {**profile, "context_window": 8000, "response_tokens": 1000})
    handler = AsyncMock(return_value={"notes": "Small observation. " * 25})
    kernel.registry.specs["memory"] = replace(kernel.registry.specs["memory"], handler=handler)
    count = 0

    async def handle(request):
        nonlocal count
        count += 1
        body = json.loads(request.content)
        assert kernel.engine.contexts.estimate_request(body["messages"], body.get("tools", [])) < 7000
        if count <= 19:
            return reply(
                tool("memory", {"operation": "read"}, "first"),
                tool("memory", {"operation": "read"}, "second"),
            )
        return reply(tool("council_silence", {"label": "Completed tool sequence"}))

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    turn = kernel.store.one("SELECT status,error FROM turns")
    assert turn["status"] == "silent", turn["error"]
    assert count == 20
