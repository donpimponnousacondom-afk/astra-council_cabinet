import asyncio
import copy
import io
import json

import httpx
import pytest

from conftest import configured
from hortator.console import OperationalConsole
from hortator.deepseek_search import ENDPOINT
from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.tool_feedback import errors_for, full_schema
from hortator.web_search import MAX_BYTES


def setup(kernel, monkeypatch, handler, *, key="", config=None):
    bot = configured(kernel, enabled_plugins=["deepseek_search"])
    plugin = kernel.store.get("plugins", "deepseek_search")
    plugin.update(enabled=True, config={**plugin["config"], **(config or {})})
    kernel.store.put("plugins", plugin)
    if key:
        kernel.vault.put("plugin/web_search/api_key", key)
    original = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs == {"trust_env": False, "follow_redirects": False}
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return ToolContext(bot, "channel", "turn-search")


async def call(kernel, context, **args):
    return await kernel.registry.call(
        "deepseek_search", {"query": "test search", **args}, context, "search-call"
    )


def provider(kernel, provider_id="deepseek", *, key="deepseek-fixture-key"):
    record = {
        **kernel.store.get("providers", "openrouter"),
        "id": provider_id,
        "base_url": "https://api.deepseek.com/v1",
        "enabled": False,
        "headers": {"X-Unrelated-Model-Header": "not-for-search"},
    }
    kernel.store.put("providers", record)
    if key:
        kernel.vault.put(f"provider/{provider_id}/api_key", key)
    return record


def row(index=0, **changes):
    return {
        "url": f"https://example.com/source/{index}",
        "title": f"Source {index}",
        "snippet": "Short excerpt",
        "summary": "Full search summary",
        "content": "",
        "site_name": "Example",
        "site_icon": None,
        "date": "2026-10-22T00:00:00+00:00",
        "authority": -1,
        "rerank_score": 2.4,
        **changes,
    }


def advertised(kernel, context):
    return next(
        tool["function"]
        for tool in kernel.registry.schemas(context)
        if tool["function"]["name"] == "deepseek_search"
    )


async def read_all(kernel, context, args):
    chunks = []
    while args:
        page = await kernel.registry.call("deepseek_search", args, context, "read")
        chunks.append(page["text"])
        args = page["next"]
    return json.loads("".join(chunks))


async def test_exact_request_credential_isolation_and_complete_paged_summaries(kernel, monkeypatch):
    query = '"Python TaskGroup" site:docs.python.org -"3.10"'
    summary = "Verbatim <T> source evidence\n" * 70 + "FINAL-SENTENCE"
    rows = [row(i, summary=summary) for i in range(50)]
    seen = []

    def respond(request):
        seen.append(request)
        assert request.method == "POST" and str(request.url) == ENDPOINT
        assert json.loads(request.content) == {"query": query, "count": 50, "need_content": False}
        assert request.headers["Authorization"] == "Bearer deepseek-fixture-key"
        assert "X-Subscription-Token" not in request.headers
        assert "X-Unrelated-Model-Header" not in request.headers
        return httpx.Response(200, json={"results": rows})

    context = setup(kernel, monkeypatch, respond, key="brave-key", config={"provider_id": "deepseek"})
    provider(kernel)
    kernel.vault.put("bot/ada/provider_key", "unrelated-bot-model-key")
    kernel.vault.put("bot/ada/plugin:web_search", "unrelated-brave-override")
    before = advertised(kernel, context)
    result = await call(kernel, context, query=query, count=50)
    assert result["ok"] and result["result_is_paged"]
    complete = await read_all(kernel, context, {"operation": "read_result", "result_id": result["result_id"]})
    assert len(complete["results"]) == 50
    assert all(item["description"] == summary for item in complete["results"])
    assert complete["results"][-1] == {
        **{
            name: rows[-1][name]
            for name in ("title", "url", "snippet", "date", "site_name", "authority", "rerank_score")
        },
        "description": summary,
    }
    evidence = complete["http_response"]
    assert "body" not in evidence and evidence["body_paged"]
    kernel.vault.put("provider/deepseek/api_key", "")
    raw = await read_all(kernel, context, evidence["read_response"])
    assert json.loads(raw["body"])["results"] == rows
    assert len(seen) == 1 and advertised(kernel, context) == before
    assert "deepseek-fixture-key" not in json.dumps(kernel.store.events())


@pytest.mark.parametrize("unit", ["x", "研", "😀", "e\u0301"])
@pytest.mark.parametrize("size", [127, 128, 129])
@pytest.mark.parametrize("selection", ["global", "bot"])
async def test_unicode_query_boundaries_match_schema_help_and_execution(
    kernel, monkeypatch, unit, size, selection
):
    query = (unit * size)[:size]
    seen = []

    def respond(request):
        seen.append(json.loads(request.content)["query"])
        return httpx.Response(200, json={"results": []})

    config = {"provider_id": "deepseek" if selection == "global" else ""}
    context = setup(kernel, monkeypatch, respond, config=config)
    provider(kernel)
    if selection == "bot":
        context.bot["plugin_config"]["deepseek_search"] = {"provider_id": "deepseek"}
    args = {"query": query}
    tool = advertised(kernel, context)
    assert bool(errors_for(tool["parameters"], args)) is (size > 128)
    help_result = await kernel.registry.call("deepseek_search", {}, context, "help")
    assert help_result["usage"]["parameters"] == full_schema(tool["parameters"])
    if size > 128:
        monkeypatch.setattr(
            kernel.vault, "get", lambda _: pytest.fail("Invalid arguments must not resolve a key")
        )
    result = await kernel.registry.call_raw("deepseek_search", json.dumps(args), context, "boundary")
    assert result["ok"] is (size <= 128)
    assert seen == ([query] if size <= 128 else [])
    if size > 128:
        assert not result["executed"]
        assert "129 Unicode characters; maximum is 128. No request was sent" in result["errors"][0]["message"]


@pytest.mark.parametrize("count", [0, 51, True, "5"])
async def test_invalid_counts_do_not_execute(kernel, monkeypatch, count):
    context = setup(kernel, monkeypatch, lambda _: pytest.fail("Invalid count sent"))
    result = await call(kernel, context, count=count)
    assert not result["ok"] and not result["executed"]


@pytest.mark.parametrize("case", ["unset", "missing-provider", "missing-key", "wrong-host"])
async def test_configuration_failures_are_actionable_without_network_or_fallback(kernel, monkeypatch, case):
    context = setup(
        kernel,
        monkeypatch,
        lambda _: pytest.fail("No request allowed"),
        config={"provider_id": "" if case == "unset" else "deepseek"},
    )
    if case in {"missing-key", "wrong-host"}:
        saved = provider(kernel, key="" if case == "missing-key" else "deepseek-fixture-key")
        if case == "wrong-host":
            kernel.store.put("providers", {**saved, "base_url": "https://proxy.example/v1"})
    result = await call(kernel, context)
    assert not result["ok"]
    assert result["category"] == "configuration" and result["usage"]


@pytest.mark.parametrize(
    "status,payload,category",
    [
        (200, {"results": []}, None),
        (200, {"error": "unavailable"}, "upstream_error"),
        (200, {}, "response_format"),
        (200, {"results": None}, "response_format"),
        (200, {"results": [row(summary=None)]}, "response_format"),
        (200, {"results": [row(url="javascript:alert(1)")]}, "response_format"),
        (200, {"results": [row(rerank_score=True)]}, "response_format"),
        (200, {"results": [row(rerank_score=10**400)]}, "response_format"),
        (200, {"results": [row(authority=5)]}, "response_format"),
        (302, {"results": []}, "http_status"),
        (401, {"error": "deepseek-fixture-key"}, "http_status"),
        (402, {"error": "insufficient balance"}, "http_status"),
        (429, {"error": "rate limit"}, "http_status"),
        (500, {"error": "failed"}, "http_status"),
    ],
)
async def test_responses_keep_status_evidence_and_do_not_retry(
    kernel, monkeypatch, status, payload, category
):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            status, json=payload, headers={"Location": "https://elsewhere.test/", "Retry-After": "10"}
        )

    context = setup(kernel, monkeypatch, respond, config={"provider_id": "deepseek"})
    provider(kernel)
    result = await call(kernel, context)
    evidence = result
    assert result["ok"] is (category is None)
    assert evidence.get("category") == category and evidence["http_response"]["http_status"] == status
    raw = await read_all(kernel, context, evidence["http_response"]["read_response"])
    assert raw["http_status"] == status and raw["capture_complete"]
    assert {"name": "retry-after", "value": "10"} in raw["response_headers"]
    assert len(seen) == 1 and "deepseek-fixture-key" not in json.dumps([result, raw, kernel.store.events()])


async def test_response_download_ceiling_preserves_partial_body(kernel, monkeypatch):
    context = setup(
        kernel,
        monkeypatch,
        lambda _: httpx.Response(200, content=b"x" * (MAX_BYTES + 1)),
        config={"provider_id": "deepseek"},
    )
    provider(kernel)
    result = await call(kernel, context)
    status = result
    assert status["category"] == "response_limit"
    assert status["http_response"]["body_bytes"] == MAX_BYTES
    assert not status["http_response"]["capture_complete"]


@pytest.mark.parametrize("interruption", ["owner_cancel", "deadline", "transport"])
async def test_interrupted_body_remains_inspectable_without_masking_cancellation(
    kernel, monkeypatch, interruption
):
    started, closed = asyncio.Event(), asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"results":['
            started.set()
            if interruption == "transport":
                raise httpx.ReadError("interrupted body")
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    context = setup(
        kernel,
        monkeypatch,
        lambda _: httpx.Response(200, stream=Stream()),
        config={"provider_id": "deepseek"},
    )
    provider(kernel)
    original_timeout = asyncio.timeout
    if interruption == "deadline":
        monkeypatch.setattr(
            asyncio, "timeout", lambda seconds: original_timeout(0.03 if seconds == 25 else seconds)
        )
    async with asyncio.TaskGroup() as group:
        task = group.create_task(call(kernel, context))
        await asyncio.wait_for(started.wait(), 2)
        if interruption == "owner_cancel":
            task.cancel()
    assert closed.is_set()
    if interruption == "owner_cancel":
        assert task.cancelled()
        event = next(event for event in kernel.store.events() if event["kind"] == "tool.cancelled")
        evidence = event["data"]["result"]["http_response"]
        with OperationalConsole(stream=io.StringIO(), keys=False, color=False) as console:
            console.bind(kernel)
            assert "Original HTTP response" in console.evidence_text({**event, "scope": "tools"})
    else:
        result = task.result()
        assert not result["ok"]
        assert result["category"] == ("local_deadline" if interruption == "deadline" else "transport")
        evidence = result["http_response"]
    raw = await read_all(kernel, context, evidence["read_response"])
    assert raw["body"] == '{"results":[' and not raw["capture_complete"]


async def test_saved_response_requires_original_scope_and_grant(kernel, monkeypatch):
    context = setup(
        kernel,
        monkeypatch,
        lambda _: httpx.Response(200, json={"results": [row()]}),
        config={"provider_id": "deepseek"},
    )
    provider(kernel)
    result = await call(kernel, context)
    args = result["http_response"]["read_response"]
    other_bot = configured(kernel, "socrates", enabled_plugins=["deepseek_search"])
    for other in (
        ToolContext(other_bot, context.channel_id, context.turn_id),
        ToolContext(context.bot, "another-channel", context.turn_id),
        ToolContext(context.bot, context.channel_id, "another-turn"),
    ):
        assert not (await kernel.registry.call("deepseek_search", args, other, "denied"))["ok"]
    context.bot["enabled_plugins"] = []
    assert not (await kernel.registry.call("deepseek_search", args, context, "revoked"))["ok"]


async def test_opt_in_default_and_existing_search_schemas_unchanged(kernel, monkeypatch):
    from hortator.web_search import MODES, PARAMETERS as WEB_PARAMETERS
    from hortator.dumb_search import PARAMETERS as PARALLEL_PARAMETERS

    plugin = kernel.store.get("plugins", "deepseek_search")
    assert not plugin["enabled"] and plugin["config"] == {"count": 5, "provider_id": ""}
    assert kernel.service.public("plugins", plugin)["keyless"]
    assert MODES == ["auto", "brave", "duckduckgo", "both", "ollama"]
    assert WEB_PARAMETERS["properties"]["count"]["maximum"] == 10
    assert PARALLEL_PARAMETERS["properties"]["count"]["maximum"] == 10
    bot = configured(kernel, enabled_plugins=["web_search", "dumb_search"])
    context = ToolContext(bot, "channel", "turn")
    old_schemas = kernel.registry.schemas(context)
    plugin["enabled"] = True
    kernel.store.put("plugins", plugin)
    assert kernel.registry.schemas(context) == old_schemas
    monkeypatch.setattr(
        kernel.vault, "get", lambda _: pytest.fail("Ungranted calls must not read credentials")
    )
    denied = await kernel.registry.call("deepseek_search", {"query": "x"}, context, "no-grant")
    assert not denied["ok"]


async def test_config_references_and_per_bot_defaults_remain_separate(kernel, owner, monkeypatch):
    seen = []

    def respond(request):
        seen.append(json.loads(request.content)["count"])
        return httpx.Response(200, json={"results": []})

    context = setup(kernel, monkeypatch, respond)
    provider(kernel)
    await kernel.service.save(
        owner, "plugins", "deepseek_search", {"config": {"count": 50, "provider_id": "deepseek"}}
    )
    await kernel.service.save(owner, "bots", "ada", {"plugin_config": {"deepseek_search": {"count": 49}}})
    context.bot = kernel.store.get("bots", "ada")
    original = copy.deepcopy(kernel.registry.specs["deepseek_search"].parameters)
    tool = advertised(kernel, context)
    assert tool["parameters"]["properties"]["count"]["default"] == 49
    other = configured(
        kernel,
        "socrates",
        enabled_plugins=["deepseek_search"],
        plugin_config={"deepseek_search": {"count": 3}},
    )
    other_context = ToolContext(other, "channel", "turn")
    assert advertised(kernel, other_context)["parameters"]["properties"]["count"]["default"] == 3
    assert (await call(kernel, context))["ok"] and (await call(kernel, other_context))["ok"]
    assert seen == [49, 3]
    assert advertised(kernel, context) == tool
    assert kernel.registry.specs["deepseek_search"].parameters == original
    for config in ({"count": 51}, {"count": True}, {"provider_id": "missing"}, {"engine": "brave"}):
        before = kernel.store.get("plugins", "deepseek_search")
        with pytest.raises(ControlError):
            await kernel.service.save(owner, "plugins", "deepseek_search", {"config": config})
        assert kernel.store.get("plugins", "deepseek_search") == before
    with pytest.raises(ControlError, match="Still referenced"):
        await kernel.service.delete(owner, "providers", "deepseek")
    with pytest.raises(ControlError, match="official https://api.deepseek.com"):
        await kernel.service.save(owner, "providers", "deepseek", {"base_url": "https://elsewhere.test/v1"})
    await kernel.service.save(
        owner, "bots", "ada", {"plugin_config": {"deepseek_search": {"provider_id": "deepseek"}}}
    )
    await kernel.service.save(owner, "plugins", "deepseek_search", {"config": {}})
    with pytest.raises(ControlError, match="bots/ada/deepseek_search"):
        await kernel.service.delete(owner, "providers", "deepseek")


@pytest.mark.parametrize(
    "args",
    [
        {"query": " "},
        {"query": "x", "engine": "deepseek"},
        {"query": "x", "ori_question": "x"},
        {"query": "x", "need_content": True},
    ],
)
async def test_help_and_invalid_arguments_have_no_effects(kernel, monkeypatch, args):
    context = setup(kernel, monkeypatch, lambda _: pytest.fail("No request expected"))
    monkeypatch.setattr(kernel.vault, "get", lambda _: pytest.fail("No key access expected"))
    help_result = await kernel.registry.call("deepseek_search", {}, context, "help")
    assert help_result["usage_only"] and not help_result["executed"]
    result = await kernel.registry.call("deepseek_search", args, context, "invalid")
    assert not result["ok"] and not result["executed"]
    assert result["usage"]["parameters"] == full_schema(advertised(kernel, context)["parameters"])


async def test_capture_failure_preserves_owner_cancellation(kernel, monkeypatch):
    started = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            started.set()
            await asyncio.Event().wait()

    def failed_capture(*args):
        raise RuntimeError("injected evidence failure")

    context = setup(
        kernel,
        monkeypatch,
        lambda _: httpx.Response(200, stream=Stream()),
        config={"provider_id": "deepseek"},
    )
    provider(kernel)
    monkeypatch.setattr("hortator.deepseek_search.record_response", failed_capture)
    async with asyncio.TaskGroup() as group:
        task = group.create_task(call(kernel, context))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
    assert task.cancelled()
    event = next(event for event in kernel.store.events() if event["kind"] == "tool.cancelled")
    assert "injected evidence failure" in event["data"]["result"]["evidence_error"]
