import asyncio
import io
import json

import httpx
import pytest

from conftest import configured
from hortator.console import OperationalConsole
from hortator.dumb_search import DEFAULTS, ENDPOINT
from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.web_search import MAX_BYTES


def setup(kernel, monkeypatch, handler, *, key="parallel-fixture-key", config=None, bot_config=None):
    bot = configured(kernel, enabled_plugins=["dumb_search"], plugin_config={"dumb_search": bot_config or {}})
    plugin = kernel.store.get("plugins", "dumb_search")
    plugin.update(enabled=True, config={**plugin["config"], **(config or {})})
    kernel.store.put("plugins", plugin)
    if key:
        kernel.vault.put("plugin/dumb_search/api_key", key)
    original = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs == {"trust_env": False, "follow_redirects": False}
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return ToolContext(bot, "channel", "turn-search")


async def call(kernel, context, **args):
    return await kernel.registry.call("dumb_search", {"query": "test search", **args}, context, "search-call")


def test_separate_disabled_plugin_and_catalog(kernel):
    saved = kernel.store.get("plugins", "dumb_search")
    assert not saved["enabled"] and saved["config"] == DEFAULTS
    public = kernel.service.public("plugins", saved)
    assert not public["keyless"] and not public["key_configured"]
    bot = kernel.store.get("bots", "ada")
    assert "dumb_search" not in bot["enabled_plugins"]
    assert "dumb_search" not in {
        row["function"]["name"] for row in kernel.registry.schemas(ToolContext(bot, "c", "t"))
    }


async def test_request_contract_key_override_and_full_excerpt_evidence(kernel, monkeypatch):
    content = "Useful excerpt " * 400 + " THE-END"
    seen = []

    def respond(request):
        seen.append(request)
        assert request.method == "POST" and str(request.url) == ENDPOINT
        assert request.headers["x-api-key"] == "parallel-bot-key"
        assert "Authorization" not in request.headers and "X-Subscription-Token" not in request.headers
        assert json.loads(request.content) == {
            "search_queries": ["test search"],
            "objective": "Find primary sources",
            "mode": "fast",
            "max_chars_total": 12000,
            "advanced_settings": {"max_results": 3},
        }
        return httpx.Response(
            200,
            json={
                "search_id": "search-live-shape",
                "session_id": "session-id",
                "results": [
                    {
                        "url": "https://example.com/source",
                        "title": None,
                        "publish_date": None,
                        "excerpts": [content],
                    }
                ],
                "usage": [{"name": "sku_search", "count": 1}],
                "warnings": None,
            },
        )

    context = setup(kernel, monkeypatch, respond, config={"count": 4}, bot_config={"count": 2})
    kernel.vault.put("bot/ada/plugin:dumb_search", "parallel-bot-key")
    kernel.vault.put("plugin/web_search/api_key", "unrelated-brave-key")
    kernel.vault.put("bot/ada/provider_key", "unrelated-model-key")
    result = await call(kernel, context, objective="Find primary sources", count=3)
    assert result["ok"] and not result["truncated"] and result["mode"] == "fast"
    assert result["results"][0]["excerpts"] == [content]
    assert result["reported_usage"] == [{"name": "sku_search", "count": 1}]
    assert result["http_response"]["http_status"] == 200
    assert "body" not in result["http_response"]
    read = await kernel.registry.call(
        "dumb_search", {**result["http_response"]["read_response"], "length": 18000}, context, "read"
    )
    assert json.loads(json.loads(read["text"])["body"])["results"][0]["excerpts"] == [content]
    assert len(seen) == 1
    assert "parallel-bot-key" not in json.dumps(kernel.store.events())


async def test_defaults_discovery_validation_and_credential_free_reads(kernel, monkeypatch):
    def reject(request):
        pytest.fail("No request expected")

    context = setup(kernel, monkeypatch, reject, key="", config={"count": 4}, bot_config={"count": 2})
    help_result = await kernel.registry.call("dumb_search", {}, context, "help")
    assert "Configured default: 2 results" in json.dumps(help_result)
    advertised = next(
        r["function"] for r in kernel.registry.schemas(context) if r["function"]["name"] == "dumb_search"
    )
    assert advertised["parameters"]["properties"]["count"]["default"] == 2
    for args in (
        {"query": ""},
        {"query": "  "},
        {"query": "x", "count": 11},
        {"query": "x", "count": True},
        {"query": "x", "mode": "advanced"},
        {"query": "x", "search_queries": ["x", "y"]},
        {"operation": "read_result", "query": "x", "result_id": "missing"},
    ):
        result = await kernel.registry.call("dumb_search", args, context, "invalid")
        assert not result["ok"] and result["usage"]
    missing = await call(kernel, context)
    assert not missing["ok"] and "Parallel key missing" in missing["error"]


@pytest.mark.parametrize(
    "bad",
    [
        {"count": 0},
        {"count": 11},
        {"max_chars_total": 999},
        {"max_chars_total": 24001},
        {"mode": "advanced"},
        {"endpoint": "https://elsewhere.test"},
    ],
)
async def test_configuration_save_validation(kernel, owner, bad):
    with pytest.raises(ControlError, match="Invalid dumb_search configuration"):
        await kernel.service.save(owner, "plugins", "dumb_search", {"config": bad})
    with pytest.raises(ControlError, match="Invalid dumb_search configuration"):
        await kernel.service.save(owner, "bots", "ada", {"plugin_config": {"dumb_search": bad}})


async def test_effective_defaults_and_bounded_excerpts_keep_raw_response(kernel, monkeypatch):
    rows = [{"url": f"https://example.com/{n}", "excerpts": ["a" * 900, "b" * 900]} for n in range(3)]

    def respond(request):
        assert json.loads(request.content)["advanced_settings"] == {"max_results": 2}
        assert "objective" not in json.loads(request.content)
        return httpx.Response(200, json={"search_id": "id", "results": rows})

    context = setup(
        kernel, monkeypatch, respond, config={"count": 4, "max_chars_total": 1000}, bot_config={"count": 2}
    )
    result = await call(kernel, context)
    assert result["ok"] and result["truncated"] and len(result["results"]) == 2
    assert sum(len(s) for r in result["results"] for s in r["excerpts"]) == 1000
    read = await kernel.registry.call(
        "dumb_search", {**result["http_response"]["read_response"], "length": 18000}, context, "read"
    )
    assert json.loads(json.loads(read["text"])["body"])["results"] == rows


@pytest.mark.parametrize("usable", [True, False])
async def test_rejected_urls_preserve_other_results_and_original_evidence(kernel, monkeypatch, usable):
    # Shape from Loki's two HTTP-200 failures on 2026-10-02; excerpt text is a fixture.
    rows = [
        {"url": "https://support:@parallel.ai/products/search", "excerpts": ["bad" * 1000]},
        {
            "url": "https://docs.parallel.ai/api-reference/search",
            "title": None,
            "publish_date": None,
            "excerpts": ["a" * 600],
        },
        {"url": "https://support:@parallel.ai/ai/products/search", "excerpts": []},
        {"url": "https://support:@parallel.ai/ai/products/responses", "excerpts": ["bad"]},
        {"url": "https://parallel.ai/blog/parallel-search-fast", "excerpts": ["b" * 600]},
    ]
    if not usable:
        rows = [rows[i] for i in (0, 2, 3)]
    payload = {
        "search_id": "search-mixed",
        "results": rows,
        "warnings": ["upstream warning"],
        "usage": [{"name": "sku_search", "count": 1}],
    }
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=payload)

    context = setup(kernel, monkeypatch, respond, config={"max_chars_total": 1000})
    result = await call(kernel, context)
    assert result["ok"] is usable and result["partial"] is usable
    assert result["warnings"] == payload["warnings"]
    assert result["reported_usage"] == payload["usage"] and result["search_id"] == "search-mixed"
    assert [r["index"] for r in result["rejected_results"]] == ([0, 2, 3] if usable else [0, 1, 2])
    assert all(r["field"] == "url" and r["reason"] for r in result["rejected_results"])
    assert "No automatic retry" in result["notice"] and len(seen) == 1
    if usable:
        assert [r["url"] for r in result["results"]] == [rows[1]["url"], rows[4]["url"]]
        assert [r["excerpts"] for r in result["results"]] == [["a" * 600], ["b" * 400]]
        assert result["results"][0]["title"] is None and result["results"][0]["publish_date"] is None
        assert result["truncated"] and result["results"][1]["excerpts_truncated"]
        assert result["error"] is None and "usage" not in result
    else:
        assert result["category"] == "response_format" and "no usable" in result["error"]
        assert not result["results"] and result["usage"]
    event = next(
        e for e in kernel.store.events() if e["kind"] == ("tool.completed" if usable else "tool.failed")
    )
    assert event["level"] == "warning"
    with OperationalConsole(stream=io.StringIO(), keys=False, color=False) as console:
        console.bind(kernel)
        assert "Original HTTP response" in console.evidence_text({**event, "scope": "tools"})
    read = await kernel.registry.call(
        "dumb_search", {**result["http_response"]["read_response"], "length": 18000}, context, "read"
    )
    assert json.loads(json.loads(read["text"])["body"]) == payload
    assert len(seen) == 1


async def test_partial_diagnostics_remain_visible_when_result_is_paged(kernel, monkeypatch):
    title = "Large upstream title " * 3500
    context = setup(
        kernel,
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "search_id": "id",
                "results": [
                    {"url": "https://support:@parallel.ai/products/search", "excerpts": []},
                    {"url": "https://example.com", "title": title, "excerpts": ["source"]},
                ],
            },
        ),
    )
    result = await call(kernel, context)
    assert result["ok"] and result["partial"] and result["result_is_paged"]
    assert result["rejected_results"][0]["field"] == "url"
    args, chunks = result["read_response"], []
    while args:
        page = await kernel.registry.call("dumb_search", args, context, "read")
        chunks.append(page["text"])
        args = page["next"]
    full = json.loads("".join(chunks))
    assert full["results"][0]["title"] == title and full["http_response"]["read_response"]
    assert full["rejected_results"] == result["rejected_results"]


@pytest.mark.parametrize(
    "bad,field",
    [
        (None, "row"),
        ([], "row"),
        ({"url": "https://example.com", "excerpts": None}, "excerpts"),
        ({"url": "https://example.com", "excerpts": ["text", None]}, "excerpts"),
        ({"url": "https://example.com", "excerpts": [], "title": 42}, "title"),
        ({"url": "https://example.com", "excerpts": [], "publish_date": {}}, "publish_date"),
        ({"url": "javascript:alert(1)", "excerpts": []}, "url"),
        ({"url": "https://[broken", "excerpts": []}, "url"),
    ],
)
async def test_malformed_rows_are_isolated_without_changing_count_limit(kernel, monkeypatch, bad, field):
    good = {"url": "https://example.com/valid", "excerpts": ["source"]}
    context = setup(
        kernel,
        monkeypatch,
        lambda request: httpx.Response(200, json={"search_id": "id", "results": [bad, good, good]}),
    )
    result = await call(kernel, context, count=2)
    assert result["ok"] and result["partial"] and result["truncated"]
    assert len(result["results"]) == 1 and result["results"][0]["excerpts"] == ["source"]
    assert result["rejected_results"] == [
        {"index": 0, "field": field, "reason": result["rejected_results"][0]["reason"]}
    ]
    assert result["rejected_results"][0]["reason"]


@pytest.mark.parametrize(
    "status,payload,category",
    [
        (200, {"search_id": "id", "results": []}, None),
        (200, {"error": "provider error"}, "upstream_error"),
        (200, {}, "response_format"),
        (
            200,
            {"search_id": "id", "results": [{"url": "javascript:alert(1)", "excerpts": []}]},
            "response_format",
        ),
        (
            200,
            {"search_id": "id", "results": [{"url": "https://example.com", "excerpts": None}]},
            "response_format",
        ),
        (
            200,
            {
                "search_id": "id",
                "results": [{"url": "https://example.com", "excerpts": ["text"], "title": 42}],
            },
            "response_format",
        ),
        (401, {"error": "parallel-fixture-key"}, "http_status"),
        (429, {"error": "rate limit"}, "http_status"),
        (503, {"error": "upstream unavailable"}, "http_status"),
        (302, {}, "http_status"),
    ],
)
async def test_failure_evidence_no_retries_and_console(kernel, monkeypatch, status, payload, category):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            status, json=payload, headers={"Retry-After": "30", "Location": "https://elsewhere.test"}
        )

    context = setup(kernel, monkeypatch, respond)
    result = await call(kernel, context)
    assert result["ok"] is (category is None) and result.get("category") == category
    assert result["http_response"]["http_status"] == status and len(seen) == 1
    if category:
        assert result["usage"]
        event = next(e for e in kernel.store.events() if e["kind"] == "tool.failed")
        with OperationalConsole(stream=io.StringIO(), keys=False, color=False) as console:
            console.bind(kernel)
            assert "Original HTTP response" in console.evidence_text({**event, "scope": "tools"})
    assert "parallel-fixture-key" not in json.dumps(result)
    assert "parallel-fixture-key" not in json.dumps(kernel.store.events())


@pytest.mark.parametrize(
    "body,category", [(b"not json", "response_format"), (b"x" * (MAX_BYTES + 1), "response_limit")]
)
async def test_invalid_and_oversized_body_evidence(kernel, monkeypatch, body, category):
    context = setup(kernel, monkeypatch, lambda request: httpx.Response(200, content=body))
    result = await call(kernel, context)
    assert not result["ok"] and result["category"] == category
    assert result["http_response"]["body_bytes"] == min(len(body), MAX_BYTES)
    assert result["http_response"]["capture_complete"] is (len(body) <= MAX_BYTES)


async def test_cancel_propagates_to_request(kernel, monkeypatch):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def respond(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    context = setup(kernel, monkeypatch, respond)
    async with asyncio.TaskGroup() as group:
        task = group.create_task(call(kernel, context))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
    assert task.cancelled() and cancelled.is_set()
    assert "tool.cancelled" in {e["kind"] for e in kernel.store.events()}


async def test_deadline_and_transport_are_explicit(kernel, monkeypatch):
    async def respond(request):
        await asyncio.Event().wait()

    context = setup(kernel, monkeypatch, respond)
    monkeypatch.setattr("hortator.dumb_search.TIMEOUT_SECONDS", 0.01)
    result = await call(kernel, context)
    assert not result["ok"] and result["category"] == "local_deadline"


@pytest.mark.parametrize("interruption", ["deadline", "transport", "owner_cancel"])
async def test_interrupted_stream_keeps_received_status_headers_and_bytes(kernel, monkeypatch, interruption):
    started, closed = asyncio.Event(), asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"error":"partial response'
            started.set()
            if interruption == "transport":
                raise httpx.ReadError("connection reset")
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    context = setup(
        kernel,
        monkeypatch,
        lambda r: httpx.Response(429, stream=Stream(), headers={"X-Request-ID": "received-id"}),
    )
    if interruption == "deadline":
        monkeypatch.setattr("hortator.dumb_search.TIMEOUT_SECONDS", 0.02)
    async with asyncio.TaskGroup() as group:
        task = group.create_task(call(kernel, context))
        await asyncio.wait_for(started.wait(), 2)
        if interruption == "owner_cancel":
            task.cancel()
    assert closed.is_set()
    if interruption == "owner_cancel":
        assert task.cancelled()
        stored = kernel.store.one(
            "SELECT content FROM tool_result_evidence WHERE tool='dumb_search' AND json_extract(content,'$.http_status')=429"
        )
        evidence = json.loads(stored["content"])
        event = next(e for e in kernel.store.events() if e["kind"] == "tool.cancelled")
        with OperationalConsole(stream=io.StringIO(), keys=False, color=False) as console:
            console.bind(kernel)
            shown = console.evidence_text({**event, "scope": "tools"})
            assert "Original HTTP response" in shown and "partial response" in shown
    else:
        result = task.result()
        assert not result["ok"] and result["category"] == (
            "local_deadline" if interruption == "deadline" else "transport"
        )
        evidence = result["http_response"]
        read = await kernel.registry.call("dumb_search", evidence["read_response"], context, "read")
        assert json.loads(read["text"])["body"] == '{"error":"partial response'
    assert evidence["http_status"] == 429 and not evidence["capture_complete"]
    assert {"name": "x-request-id", "value": "received-id"} in evidence["response_headers"]
    assert evidence["body"] == '{"error":"partial response'


@pytest.mark.parametrize("interruption", ["deadline", "owner_cancel"])
async def test_capture_failure_does_not_swallow_cancellation(kernel, monkeypatch, interruption):
    started = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial response"
            started.set()
            await asyncio.Event().wait()

    def failed_capture(*args):
        raise RuntimeError("fixture evidence write failed parallel-fixture-key")

    context = setup(kernel, monkeypatch, lambda r: httpx.Response(200, stream=Stream()))
    monkeypatch.setattr("hortator.dumb_search.record_response", failed_capture)
    if interruption == "deadline":
        monkeypatch.setattr("hortator.dumb_search.TIMEOUT_SECONDS", 0.02)
    async with asyncio.TaskGroup() as group:
        task = group.create_task(call(kernel, context))
        await asyncio.wait_for(started.wait(), 2)
        if interruption == "owner_cancel":
            task.cancel()
    if interruption == "owner_cancel":
        assert task.cancelled()
        event = next(e for e in kernel.store.events() if e["kind"] == "tool.cancelled")
        result = event["data"]["result"]
    else:
        result = task.result()
        assert result["category"] == "local_deadline" and not result["ok"]
    assert "fixture evidence write failed" in result["evidence_error"]
    assert "RuntimeError" in result["evidence_error"]
    assert "http_response" not in result
    assert "parallel-fixture-key" not in json.dumps(kernel.store.events())


async def test_evidence_scope_and_origin_grants(kernel, monkeypatch):
    context = setup(
        kernel, monkeypatch, lambda r: httpx.Response(200, json={"search_id": "id", "results": []})
    )
    result = await call(kernel, context)
    args = result["http_response"]["read_response"]
    kernel.vault.put("plugin/dumb_search/api_key", "")
    assert "text" in await kernel.registry.call("dumb_search", args, context, "read")
    for other in (
        ToolContext(context.bot, "other-channel", context.turn_id),
        ToolContext(context.bot, context.channel_id, "other-turn"),
    ):
        assert not (await kernel.registry.call("dumb_search", args, other, "denied"))["ok"]
    # A reread through another tool keeps the original plugin grant requirement.
    bot = context.bot
    bot["enabled_plugins"].append("web_fetch")
    kernel.store.put("bots", bot)
    read = await kernel.registry.call("web_fetch", args, context, "cross-tool-read")
    assert "text" in read
    bot["enabled_plugins"].remove("dumb_search")
    kernel.store.put("bots", bot)
    denied = await kernel.registry.call(
        "web_fetch", {"operation": "read_result", "result_id": read["result_id"]}, context, "denied"
    )
    assert not denied["ok"]
