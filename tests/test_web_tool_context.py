import copy
import json

from hortator.http_evidence import HTTPResponse, record_response
from hortator.store import dumps
from hortator.working_set import bound_exchanges, result_reference
from support.agentic_runtime import grant
from support.runtime_feedback import tool


async def test_search_results_survive_paging_http_evidence_without_changing_capture(kernel):
    context = grant(kernel, "web_search")
    extras = [{"role": "assistant", "tool_calls": []}]
    originals = []
    for number in range(2):
        body = " ".join(f"upstream-{i:04d}" for i in range(1000)).encode()
        response = HTTPResponse(
            body,
            "application/json",
            f"https://example.com/search?q={number}",
            200,
            "OK",
            headers=[{"name": "x-received", "value": " ".join(str(i) for i in range(1000))}],
        )
        http = record_response(kernel.store, kernel.vault, context, "web_search", response)
        value = kernel.registry.evidence.record(
            context,
            "web_search",
            str(number),
            {
                "ok": True,
                "results": [
                    {
                        "title": f"Result {number}",
                        "url": f"https://example.com/{number}",
                        "description": "Received snippet",
                    }
                ],
                "engine_status": [
                    {"engine": "brave", "status": "ok", "http_status": 200, "http_response": http}
                ],
            },
        )
        originals.append(value)
        extras[0]["tool_calls"].append(tool("web_search", {"query": str(number)}, str(number)))
        extras.append({"role": "tool", "tool_call_id": str(number), "content": dumps(value)})
    before = copy.deepcopy(extras)
    estimate = kernel.engine.contexts.estimate
    assert estimate(extras) > 6000
    bounded, meta = bound_exchanges(extras, estimate, 6000)
    assert extras == before and meta["estimated_tokens"] <= 6000
    assert meta["paged_http_results"] and not meta["minimized_results"]
    values = [json.loads(m["content"]) for m in bounded if m["role"] == "tool"]
    assert [v["results"] for v in values] == [v["results"] for v in originals]
    for index, value in enumerate(values):
        http = value["engine_status"][0]["http_response"]
        assert http["http_status"] == 200 and http["http_reason"] == "OK"
        stored = kernel.store.one(
            "SELECT content FROM tool_result_evidence WHERE id=?", (originals[index]["result_id"],)
        )
        assert (
            json.loads(stored["content"])["engine_status"][0]["http_response"]
            == originals[index]["engine_status"][0]["http_response"]
        )
        raw = await kernel.registry.call("web_search", http["read_response"], context, f"read-{index}")
        chunks = [raw["text"]]
        while raw["next"]:
            raw = await kernel.registry.call("web_search", raw["next"], context, f"page-{index}")
            chunks.append(raw["text"])
        captured = json.loads("".join(chunks))
        assert captured["body"] == body.decode()
        assert captured["response_headers"] == response.headers


async def test_omitted_reread_returns_to_original_source_and_same_unicode_offset(kernel):
    context = grant(kernel, "web_search", "web_fetch")
    original = kernel.registry.evidence.record(
        context, "web_search", "original", {"text": "café 🙂 observation " * 1000}
    )
    page = await kernel.registry.call(
        "web_search",
        {
            "operation": "read_result",
            "result_id": original["result_id"],
            "offset": 2000,
            "length": 2000,
        },
        context,
        "first-read",
    )
    message = {"role": "tool", "content": dumps(page)}
    reference = result_reference(message)
    assert reference["result_id"] == original["result_id"]
    assert reference["page_result_id"] == page["result_id"]
    for _ in range(3):
        reread = await kernel.registry.call("web_search", reference["reread"], context, "again")
        assert reread["text"] == page["text"]
        assert reread["source_result_id"] == original["result_id"]
        assert reread["range"]["start"] == 2000
        reference = result_reference({"role": "tool", "content": dumps(reread)})
    repeated = result_reference({"role": "tool", "content": dumps(reference)})
    assert repeated["reread"] == reference["reread"]


def test_inspector_reread_keeps_its_native_resource_operation():
    reference = result_reference(
        {
            "content": dumps(
                {
                    "result_id": "page",
                    "source_result_id": "source",
                    "source_tool": "council_inspect",
                    "range": {"start": 4000, "end": 6000},
                    "read_response": {"resource": "read_result", "result_id": "page"},
                }
            )
        }
    )
    assert reference["reread"] == {
        "resource": "read_result",
        "result_id": "source",
        "offset": 4000,
        "length": 2000,
    }
    assert reference["read_response"] == reference["reread"]


def test_parallel_batch_minimizes_only_until_remaining_results_fit(kernel):
    calls = [tool("web_fetch", {"url": "https://example.com"}, str(i)) for i in range(3)]
    extras = [{"role": "assistant", "tool_calls": calls}]
    extras += [
        {
            "role": "tool",
            "tool_call_id": str(i),
            "content": dumps(
                {
                    "result_id": f"result_{i}",
                    "text": "source words " * (5000 if i == 0 else 160),
                }
            ),
        }
        for i in range(3)
    ]
    estimate = kernel.engine.contexts.estimate
    bounded, meta = bound_exchanges(extras, estimate, 2000)
    assert meta["estimated_tokens"] <= 2000 and meta["minimized_results"] == 1
    results = [json.loads(m["content"]) for m in bounded if m["role"] == "tool"]
    assert results[0]["omitted_from_active_prompt"]
    assert results[1]["text"] == results[2]["text"] == "source words " * 160


def test_failed_engine_body_is_not_paged_as_a_success(kernel):
    from hortator.working_set import page_search_http

    value = {
        "results": [{"title": "Other engine result"}],
        "engine_status": [
            {
                "status": "failed",
                "http_response": {
                    "http_status": 202,
                    "body": "Actual challenge",
                    "read_response": {"result_id": "r"},
                },
            }
        ],
    }
    message = {"content": dumps(value)}
    assert not page_search_http(message)
    assert json.loads(message["content"]) == value
