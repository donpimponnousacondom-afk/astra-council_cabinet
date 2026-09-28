import asyncio
import json

import httpx
import pytest

from hortator.models import ControlError
from hortator.web_search import MAX_BYTES
from support.web_search import call, setup


def ollama_provider(kernel, provider_id="ollama", *, key="ollama-fixture-key"):
    provider = {
        **kernel.store.get("providers", "openrouter"),
        "id": provider_id,
        "base_url": "https://ollama.com/v1",
    }
    kernel.store.put("providers", provider)
    if key:
        kernel.vault.put(f"provider/{provider_id}/api_key", key)
    return provider


async def test_ollama_uses_named_provider_key_and_preserves_full_paged_evidence(kernel, monkeypatch):
    content = "Useful content " * 300 + " THE-END"

    def respond(request):
        assert request.method == "POST"
        assert str(request.url) == "https://ollama.com/api/web_search"
        assert json.loads(request.content) == {"query": "test search", "max_results": 2}
        assert request.headers["Authorization"] == "Bearer ollama-fixture-key"
        assert "X-Subscription-Token" not in request.headers
        return httpx.Response(
            200,
            json={"results": [{"title": "Title", "url": "https://example.com/source", "content": content}]},
        )

    context = setup(
        kernel, monkeypatch, respond, key="brave-fixture-key", config={"ollama_provider_id": "ollama"}
    )
    ollama_provider(kernel)
    kernel.vault.put("bot/ada/provider_key", "unrelated-conversation-key")
    kernel.vault.put("bot/ada/plugin:web_search", "bot-brave-key")
    result = await call(kernel, context, engine="ollama", count=2)
    assert result["ok"] and not result["partial"] and not result["fallback_used"]
    assert result["results"][0]["engines"] == ["ollama"]
    assert len(result["results"][0]["description"]) == 1000
    status = result["engine_status"][0]
    assert status["http_status"] == 200 and status["duration_ms"] > 0
    read = await kernel.registry.call("web_search", status["http_response"]["read_response"], context, "read")
    assert json.loads(json.loads(read["text"])["body"])["results"][0]["content"] == content
    assert "ollama-fixture-key" not in json.dumps(kernel.store.events())


@pytest.mark.parametrize("mode", ["auto", "both", "brave", "duckduckgo"])
async def test_existing_modes_never_read_or_send_ollama_credential(kernel, monkeypatch, mode):
    seen = []

    def respond(request):
        seen.append(request.url.host)
        assert "Authorization" not in request.headers
        if request.url.host == "html.duckduckgo.com":
            assert "X-Subscription-Token" not in request.headers
            return httpx.Response(200, text='<div class="no-results">No results</div>')
        assert request.headers["X-Subscription-Token"] == "brave-fixture-key"
        return httpx.Response(200, json={"web": {"results": []}})

    context = setup(
        kernel, monkeypatch, respond, key="brave-fixture-key", config={"ollama_provider_id": "ollama"}
    )
    original = kernel.vault.get

    def get(scope):
        assert scope != "provider/ollama/api_key"
        return original(scope)

    monkeypatch.setattr(kernel.vault, "get", get)
    result = await call(kernel, context, engine=mode)
    assert result["ok"] and "ollama.com" not in seen


@pytest.mark.parametrize("case", ["unset", "missing-provider", "missing-key", "wrong-host"])
async def test_ollama_configuration_failure_is_actionable_and_never_falls_back(kernel, monkeypatch, case):
    def respond(request):
        pytest.fail("Misconfigured search must not contact any endpoint")

    context = setup(
        kernel, monkeypatch, respond, config={"ollama_provider_id": "" if case == "unset" else "ollama"}
    )
    if case in {"missing-key", "wrong-host"}:
        provider = ollama_provider(kernel, key="" if case == "missing-key" else "fixture-key")
        if case == "wrong-host":
            provider["base_url"] = "https://other-provider.test/v1"
            kernel.store.put("providers", provider)
    result = await call(kernel, context, engine="ollama")
    assert result["ok"] is False
    assert result["engine_status"][0]["category"] == "configuration"
    assert len(result["engine_status"]) == 1
    assert result["usage"]


@pytest.mark.parametrize(
    "status,payload,category",
    [
        (200, {"results": []}, None),
        (200, {"error": "search unavailable"}, "upstream_error"),
        (200, {}, "response_format"),
        (200, {"results": None}, "response_format"),
        (200, {"results": [{"url": "javascript:alert(1)", "title": "x", "content": "x"}]}, "response_format"),
        (
            200,
            {"results": [{"url": "https://example.com", "title": "x", "content": None}]},
            "response_format",
        ),
        (429, {"results": []}, "http_status"),
        (500, {"error": "ollama-fixture-key"}, "http_status"),
        (302, {"results": []}, "http_status"),
    ],
)
async def test_ollama_empty_error_and_redirect_responses_keep_truthful_evidence(
    kernel, monkeypatch, status, payload, category
):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            status, json=payload, headers={"Location": "https://elsewhere.test/", "Retry-After": "10"}
        )

    context = setup(kernel, monkeypatch, respond, config={"ollama_provider_id": "ollama"})
    ollama_provider(kernel)
    result = await call(kernel, context, engine="ollama")
    evidence = result["engine_status"][0]
    assert result["ok"] is (category is None)
    assert evidence.get("category") == category
    assert evidence["http_status"] == status
    assert evidence["http_response"]["read_response"]["operation"] == "read_result"
    assert "ollama-fixture-key" not in json.dumps(result)
    assert "ollama-fixture-key" not in json.dumps(kernel.store.events())
    assert len(calls) == 1


async def test_ollama_download_limit_retains_partial_evidence(kernel, monkeypatch):
    context = setup(
        kernel,
        monkeypatch,
        lambda request: httpx.Response(200, content=b"x" * (MAX_BYTES + 1)),
        config={"ollama_provider_id": "ollama"},
    )
    ollama_provider(kernel)
    result = await call(kernel, context, engine="ollama")
    status = result["engine_status"][0]
    assert status["category"] == "response_limit"
    assert not status["http_response"]["capture_complete"]
    assert status["http_response"]["body_bytes"] == MAX_BYTES


async def test_ollama_cancellation_reaches_http_request(kernel, monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def respond(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    context = setup(kernel, monkeypatch, respond, config={"ollama_provider_id": "ollama"})
    ollama_provider(kernel)
    async with asyncio.TaskGroup() as group:
        task = group.create_task(call(kernel, context, engine="ollama"))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
    assert task.cancelled() and cancelled.is_set()
    assert "tool.cancelled" in {event["kind"] for event in kernel.store.events()}


async def test_ollama_config_references_guard_provider_deletion_and_origin_edits(kernel, owner):
    provider = ollama_provider(kernel)
    plugin = kernel.store.get("plugins", "web_search")
    await kernel.service.save(
        owner, "plugins", "web_search", {"config": {**plugin["config"], "ollama_provider_id": "ollama"}}
    )
    with pytest.raises(ControlError, match="Still referenced"):
        await kernel.service.delete(owner, "providers", "ollama")
    with pytest.raises(ControlError, match="official https://ollama.com"):
        await kernel.service.save(
            owner, "providers", "ollama", {"base_url": "https://other-provider.test/v1"}
        )
    assert kernel.store.get("providers", "ollama")["base_url"] == provider["base_url"]
    with pytest.raises(ControlError, match="official https://ollama.com"):
        await kernel.service.save(
            owner, "plugins", "web_search", {"config": {"ollama_provider_id": "missing"}}
        )
    bot = kernel.store.get("bots", "ada")
    await kernel.service.save(
        owner,
        "bots",
        "ada",
        {"plugin_config": {**bot["plugin_config"], "web_search": {"ollama_provider_id": "ollama"}}},
    )
    await kernel.service.save(owner, "plugins", "web_search", {"config": {}})
    with pytest.raises(ControlError, match="bots/ada/web_search"):
        await kernel.service.delete(owner, "providers", "ollama")
