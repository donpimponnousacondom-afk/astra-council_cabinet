import asyncio
import json
from urllib.parse import quote

import httpx
import pytest

from conftest import configured
from hortator.console import scope_for
from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.web_search import MAX_BYTES, SearchFailure, brave_results, duck_results, result_url


def html_results(urls):
    return (
        '<html><body><div class="results">'
        + "".join(
            f'<div class="result results_links"><h2><a class="result__a" href="//duckduckgo.com/l/?uddg={quote(url, safe="")}">Title <b>{index}</b></a></h2>'
            f'<a class="result__snippet">Snippet &amp; <b>{index}</b><script>unsafe()</script></a></div>'
            for index, url in enumerate(urls)
        )
        + "</div></body></html>"
    )


def setup(kernel, monkeypatch, handler, *, key="", config=None):
    bot = configured(kernel, enabled_plugins=["web_search"])
    plugin = kernel.store.get("plugins", "web_search")
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
    return await kernel.registry.call("web_search", {"query": "test search", **args}, context, "search-call")


def test_duck_html_extracts_visible_nested_text_unwraps_links_and_rejects_unsafe_urls():
    body = html_results(["https://example.com/page?a=1&b=2", "javascript:alert(1)"]).encode()
    values = duck_results(body, 5)
    assert values == [
        {"title": "Title 0", "url": "https://example.com/page?a=1&b=2", "description": "Snippet & 0"}
    ]
    assert result_url("https://user:password@example.com") is None
    assert result_url("https://[broken") is None
    assert scope_for("web_search.engine_failed") == "tools"


@pytest.mark.parametrize(
    "page,category",
    [
        ('<form id="challenge-form">Solve this</form>', "challenge"),
        ('<div class="anomaly-modal">Captcha</div>', "challenge"),
        ("<html>Unrecognized layout</html>", "response_format"),
        ("<div>" * 80, "response_format"),
    ],
)
def test_challenges_and_changed_html_are_failures_not_empty_success(page, category):
    with pytest.raises(SearchFailure) as error:
        duck_results(page.encode(), 5)
    assert error.value.category == category


def test_explicit_empty_results_and_nullable_brave_optional_fields():
    assert duck_results(b'<div class="no-results">No results</div>', 5) == []
    assert brave_results(b'{"web": null, "query": {}}', 5) == []
    assert brave_results(b'{"web":{"results":null}}', 5) == []
    for body in [b"null", b'{"web":[]}', b'{"web":{"results":false}}', b"garbage"]:
        with pytest.raises(SearchFailure) as error:
            brave_results(body, 5)
        assert error.value.category == "response_format"


@pytest.mark.parametrize(
    "failure",
    ["missing_key", "http_401", "http_429", "timeout", "empty", "malformed", "oversized", "redirect"],
)
async def test_auto_preserves_duck_results_after_brave_problem(kernel, monkeypatch, failure):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.host == "html.duckduckgo.com":
            assert "X-Subscription-Token" not in request.headers and "Authorization" not in request.headers
            return httpx.Response(200, text=html_results(["https://example.com/duck"]))
        if failure == "timeout":
            raise httpx.ReadTimeout("slow fixture", request=request)
        if failure.startswith("http_"):
            return httpx.Response(int(failure.split("_")[1]))
        if failure == "oversized":
            return httpx.Response(200, headers={"content-length": str(MAX_BYTES + 1)}, content=b"{}")
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://other.test/"})
        return (
            httpx.Response(200, json={"web": {"results": []}})
            if failure == "empty"
            else httpx.Response(200, text="bad json")
        )

    context = setup(kernel, monkeypatch, respond, key="" if failure == "missing_key" else "private-test-key")
    result = await call(kernel, context)
    assert result["ok"] and result["fallback_used"]
    assert result["results"][0]["engines"] == ["duckduckgo"]
    assert result["partial"] is (failure != "empty")
    assert len(requests) == (1 if failure == "missing_key" else 2)
    assert all(request.url.host in ("api.search.brave.com", "html.duckduckgo.com") for request in requests)
    assert "private-test-key" not in json.dumps(kernel.store.events())


async def test_auto_brave_success_does_not_contact_duck_and_explicit_duck_does_not_use_key(
    kernel, monkeypatch
):
    hosts = []

    def respond(request):
        hosts.append(request.url.host)
        if request.url.host == "html.duckduckgo.com":
            assert "X-Subscription-Token" not in request.headers
            return httpx.Response(200, text=html_results(["https://example.com/duck"]))
        return httpx.Response(
            200, json={"web": {"results": [{"title": "Brave", "url": "https://example.com/brave"}]}}
        )

    context = setup(kernel, monkeypatch, respond, key="private-key")
    result = await call(kernel, context)
    assert result["ok"] and not result["fallback_used"] and hosts == ["api.search.brave.com"]
    result = await call(kernel, context, engine="duckduckgo")
    assert result["ok"] and result["mode"] == "duckduckgo"
    assert hosts == ["api.search.brave.com", "html.duckduckgo.com"]


async def test_both_searches_run_concurrently_and_merge_five_plus_five_with_provenance(kernel, monkeypatch):
    started = set()
    both_started = asyncio.Event()

    async def respond(request):
        started.add(request.url.host)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 2)
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(
                200,
                text=html_results(
                    ["https://example.com/shared#second", *[f"https://example.com/d{i}" for i in range(4)]]
                ),
            )
        assert request.url.params["count"] == "5"
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {"title": "Brave", "url": url}
                        for url in [
                            "https://example.com/shared#first",
                            *[f"https://example.com/b{i}" for i in range(4)],
                        ]
                    ]
                }
            },
        )

    context = setup(kernel, monkeypatch, respond, key="test-key", config={"engine": "both"})
    result = await call(kernel, context)
    assert result["mode"] == "both" and result["ok"] and not result["partial"]
    assert len(result["results"]) == 9
    assert result["results"][0]["engines"] == ["brave", "duckduckgo"]
    assert [s["count"] for s in result["engine_status"]] == [5, 5]


async def test_both_partial_failure_retains_brave_and_all_failure_is_tool_failure(kernel, monkeypatch):
    fail_all = False

    def respond(request):
        if fail_all or request.url.host == "html.duckduckgo.com":
            return httpx.Response(202)
        return httpx.Response(200, json={"web": {"results": [{"url": "https://example.com/brave"}]}})

    context = setup(kernel, monkeypatch, respond, key="key")
    result = await call(kernel, context, engine="both")
    assert result["ok"] and result["partial"] and len(result["results"]) == 1
    fail_all = True
    result = await call(kernel, context, engine="both")
    assert not result["ok"] and not result["results"] and result["usage"]
    assert len(result["engine_status"]) == 2
    assert any(
        e["kind"] == "tool.failed" and e["data"].get("error") == result["error"]
        for e in kernel.store.events()
    )


async def test_missing_brave_key_explicit_mode_has_actionable_configuration_error(kernel, monkeypatch):
    def respond(_):
        raise AssertionError("No network request without the Brave key")

    context = setup(kernel, monkeypatch, respond)
    result = await call(kernel, context, engine="brave")
    assert not result["ok"] and result["engine_status"][0]["category"] == "configuration"
    assert "Save credential" in result["engine_status"][0]["error"]


async def test_empty_usage_and_bad_arguments_never_contact_search_endpoints(kernel, monkeypatch):
    def respond(_):
        raise AssertionError("Invalid arguments should not execute")

    context = setup(kernel, monkeypatch, respond)
    result = await kernel.registry.call("web_search", {}, context, "help")
    assert result["usage_only"] and not result["executed"]
    result = await call(kernel, context, query=" ", engine="invented", count="5")
    assert result["error_count"] == 3 and result["usage"]


@pytest.mark.parametrize(
    "config", [{"count": True}, {"engine": "wrong"}, {"endpoint": "https://key:secret@example.com"}]
)
async def test_search_settings_validation_before_save(kernel, owner, config):
    before = kernel.store.get("plugins", "web_search")
    with pytest.raises(ControlError, match="Invalid web_search configuration"):
        await kernel.service.save(owner, "plugins", "web_search", {"config": config})
    assert kernel.store.get("plugins", "web_search") == before


async def test_streamed_download_limit_without_content_length_preserves_fallback(kernel, monkeypatch):
    closed = []

    class Oversized(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * (MAX_BYTES // 2)
            yield b"x" * (MAX_BYTES // 2 + 1)
            raise AssertionError("Read must stop at the cap")

        async def aclose(self):
            closed.append(True)

    def respond(request):
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(200, text=html_results(["https://example.com/duck"]))
        return httpx.Response(200, stream=Oversized())

    context = setup(kernel, monkeypatch, respond, key="key")
    result = await call(kernel, context)
    assert result["ok"] and result["partial"] and closed
    assert result["engine_status"][0]["category"] == "response_limit"


async def test_cancellation_closes_both_searches_and_is_not_a_completed_result(kernel, monkeypatch):
    started, cancelled = set(), set()
    both_started = asyncio.Event()

    async def respond(request):
        host = request.url.host
        started.add(host)
        if len(started) == 2:
            both_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.add(host)
            raise

    context = setup(kernel, monkeypatch, respond, key="key")
    pending = asyncio.create_task(call(kernel, context, engine="both"))
    await asyncio.wait_for(both_started.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert len(cancelled) == 2
    kinds = {e["kind"] for e in kernel.store.events()}
    assert "tool.cancelled" in kinds and "tool.completed" not in kinds


async def test_large_combined_results_keep_structured_metadata_within_registry_cap(kernel, monkeypatch):
    def respond(request):
        urls = [f"https://example.com/{request.url.host}/{i}?q=" + "x" * 1800 for i in range(10)]
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(200, text=html_results(urls).replace("Snippet &amp;", "x" * 1000))
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [{"url": url, "title": "t" * 300, "description": "d" * 1000} for url in urls]
                }
            },
        )

    context = setup(kernel, monkeypatch, respond, key="key")
    result = await call(kernel, context, engine="both", count=10)
    assert result["ok"] and result["truncated"] and result["engine_status"]
    assert len(result["results"]) < 20 and len(json.dumps(result)) < 60000
    assert {e for r in result["results"] for e in r["engines"]} == {"brave", "duckduckgo"}


async def test_per_bot_config_and_call_overrides_preserve_global_settings(kernel, monkeypatch):
    def respond(request):
        assert request.url.host == "html.duckduckgo.com"
        return httpx.Response(200, text=html_results([f"https://example.com/{i}" for i in range(8)]))

    context = setup(kernel, monkeypatch, respond, config={"engine": "brave", "count": 5})
    context.bot["plugin_config"]["web_search"] = {"engine": "duckduckgo", "count": 2}
    assert len((await call(kernel, context))["results"]) == 2
    assert len((await call(kernel, context, count=3))["results"]) == 3
    assert kernel.store.get("plugins", "web_search")["config"]["engine"] == "brave"
