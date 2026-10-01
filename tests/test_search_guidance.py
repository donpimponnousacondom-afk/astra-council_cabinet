import copy
import json

import httpx
import pytest
from conftest import configured

from hortator.plugins import ToolContext
from hortator.tool_feedback import errors_for, full_schema
from hortator.web_search import MODES
from support.web_search import setup


def advertised(kernel, context):
    return next(
        tool["function"]
        for tool in kernel.registry.schemas(context)
        if tool["function"]["name"] == "web_search"
    )


@pytest.mark.parametrize(
    "global_config,override,engine,count,hosts",
    [
        ({}, {}, "auto", 5, ["api.search.brave.com", "html.duckduckgo.com"]),
        ({"engine": "brave", "count": 8}, {}, "brave", 8, ["api.search.brave.com"]),
        (
            {"engine": "both", "count": 5},
            {"engine": "ollama", "count": 2},
            "ollama",
            2,
            ["ollama.com"],
        ),
        (
            {"engine": "duckduckgo", "count": 10},
            {"count": 3},
            "duckduckgo",
            3,
            ["html.duckduckgo.com"],
        ),
    ],
)
async def test_advertised_defaults_and_help_example_match_actual_routing(
    kernel, monkeypatch, global_config, override, engine, count, hosts
):
    seen = []

    def respond(request):
        seen.append(request.url.host)
        if request.url.host == "ollama.com":
            assert json.loads(request.content)["max_results"] == count
            return httpx.Response(200, json={"results": []})
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(200, text='<div class="no-results">No results</div>')
        assert int(request.url.params["count"]) == count
        return httpx.Response(200, json={"web": {"results": []}})

    context = setup(kernel, monkeypatch, respond, key="brave-fixture-key")
    plugin = kernel.store.get("plugins", "web_search")
    plugin["config"] = global_config
    kernel.store.put("plugins", plugin)
    context.bot["plugin_config"]["web_search"] = override
    if engine == "ollama":
        provider = {**kernel.store.get("providers", "openrouter"), "base_url": "https://ollama.com/v1"}
        kernel.store.put("providers", provider)
        kernel.vault.put("provider/openrouter/api_key", "ollama-fixture-key")
        context.bot["plugin_config"]["web_search"]["ollama_provider_id"] = "openrouter"

    tool = advertised(kernel, context)
    properties = tool["parameters"]["properties"]
    assert properties["engine"]["default"] == engine
    assert properties["count"]["default"] == count
    assert properties["engine"]["enum"] == MODES
    assert f"engine={engine}, count={count}" in tool["description"]
    assert '"engine":"both"' not in tool["description"]

    # Discovery and repair must describe the same effective configuration,
    # without network access or resolving any credential.
    original_get = kernel.vault.get

    def no_credential(_):
        raise AssertionError("Guidance must not resolve credentials")

    monkeypatch.setattr(kernel.vault, "get", no_credential)
    help_result = await kernel.registry.call("web_search", {}, context, "help")
    invalid = await kernel.registry.call("web_search", {"count": 0}, context, "invalid")
    syntax = await kernel.registry.call_raw("web_search", "{", context, "syntax")
    for result in (help_result, invalid, syntax):
        usage = result["usage"]
        assert usage["parameters"] == full_schema(tool["parameters"])
        assert usage["description"] in tool["description"]
        assert usage["example"] == {"query": "Python documentation"}
        assert not errors_for(usage["parameters"], usage["example"])
    assert not seen
    monkeypatch.setattr(kernel.vault, "get", original_get)
    result = await kernel.registry.call("web_search", help_result["usage"]["example"], context, "example")
    assert result["ok"] and result["mode"] == engine
    assert seen == hosts


async def test_guidance_isolated_between_bots_and_refreshes_global_defaults(kernel, monkeypatch):
    context = setup(kernel, monkeypatch, lambda _: pytest.fail("No network for guidance"))
    context.bot["plugin_config"]["web_search"] = {"engine": "ollama", "count": 2}
    other = configured(kernel, "socrates", enabled_plugins=["web_search"])
    other_context = ToolContext(other, "channel", "other-turn")
    base = copy.deepcopy(kernel.registry.specs["web_search"].parameters)
    first = advertised(kernel, context)
    plugin = kernel.store.get("plugins", "web_search")
    plugin["config"].update(engine="brave", count=9)
    kernel.store.put("plugins", plugin)
    second = advertised(kernel, other_context)
    assert second["parameters"]["properties"]["engine"]["default"] == "brave"
    assert second["parameters"]["properties"]["count"]["default"] == 9
    assert advertised(kernel, context) == first
    assert kernel.registry.specs["web_search"].parameters == base
    assert "default" not in base["properties"]["engine"]


@pytest.mark.parametrize("operation", ["search", "read_result"])
async def test_operation_specific_help_has_valid_examples(kernel, monkeypatch, operation):
    context = setup(kernel, monkeypatch, lambda _: pytest.fail("No network for invalid arguments"))
    result = await kernel.registry.call("web_search", {"operation": operation}, context, "invalid")
    assert not result["ok"] and not result["executed"]
    usage = result["usage"]
    assert usage["example"]["operation"] == operation
    assert not errors_for(usage["parameters"], usage["example"])
    assert "engine" not in usage["example"]
