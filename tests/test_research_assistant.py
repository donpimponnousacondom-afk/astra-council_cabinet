import asyncio
import json

import httpx
import pytest

from conftest import configured, ingest
from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.research_assistant import DEFAULTS, ID
from test_provider import Fragments, install_client
from test_background_jobs import finish


def setup(k, *, stream=True):
    bot = configured(k, interval_seconds=0, enabled_plugins=[ID])
    ingest(k)
    p = k.store.get("profiles", "balanced")
    p.update(
        id="research",
        model="mimo-v2.6-flash",
        stream=stream,
        request_json={"thinking": {"type": "disabled"}, "max_tokens": 17},
    )
    p.pop("revision")
    k.store.put("profiles", p)
    plugin = k.store.get("plugins", ID)
    plugin.update(enabled=True, config={**DEFAULTS, "profile_id": "research"})
    plugin.pop("revision")
    k.store.put("plugins", plugin)
    return ToolContext(bot, "222222222222222222", "research-origin")


async def call(k, context, **args):
    return await k.registry.call(ID, args, context, "research-call")


def start(**changes):
    return {
        "operation": "start",
        "submission_id": "research-test",
        "task": "Find official prices",
        "max_output_tokens": 2048,
        **changes,
    }


@pytest.mark.parametrize("stream", [True, False])
async def test_real_adapter_preserves_sources_usage_and_private_reasoning(kernel, stream):
    context = setup(kernel, stream=stream)
    requests = []
    annotation = {
        "url": "https://example.com/pricing",
        "title": "Pricing",
        "summary": "Costs",
        "publish_time": "2026-09-22",
    }

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == "mimo-v2.6-flash"
        assert body["max_completion_tokens"] == 2048 and "max_tokens" not in body
        assert body["tools"] == [{"type": "web_search", "force_search": True, "max_keyword": 1, "limit": 1}]
        assert body["messages"][-1]["content"] == "Find official prices"
        assert "Hello council" not in str(body)
        usage = {
            "prompt_tokens": 90,
            "completion_tokens": 30,
            "total_tokens": 120,
            "completion_tokens_details": {"reasoning_tokens": 10},
            "web_search_usage": {"tool_usage": 1, "page_usage": 2},
        }
        message = {
            "content": "Official price report",
            "reasoning_content": "PRIVATE TRACE",
            "annotations": [annotation],
            "error_message": "One source unavailable",
        }
        if stream:
            packets = [
                {"choices": [{"index": 0, "delta": {"annotations": [annotation]}}]},
                {"choices": [{"index": 0, "delta": message, "finish_reason": "stop"}]},
                {"choices": [], "usage": usage},
            ]
            wire = "".join("data: " + json.dumps(p) + "\n\n" for p in packets) + "data: [DONE]\n\n"
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Fragments(wire.encode())
            )
        return httpx.Response(
            200, json={"choices": [{"message": message, "finish_reason": "stop"}], "usage": usage}
        )

    await install_client(kernel, respond)
    reply = await call(kernel, context, **start())
    assert reply["state"] == "queued"
    assert context.bot["model_profile_id"] == "balanced"
    await finish(kernel)
    job = kernel.jobs.get(reply["job_id"])
    assert job["state"] == "completed", job["error"]
    result = json.loads(job["result"])
    assert result["sources"] == [annotation]
    assert result["search_errors"] == ["One source unavailable"]
    assert result["search_usage"] == {"tool_usage": 1, "page_usage": 2}
    assert result["metrics"]["tokens"]["reasoning"]["value"] == 10
    assert (result["metrics"]["ttft_ms"] is not None) == stream
    assert "PRIVATE TRACE" not in job["result"]
    evidence = kernel.store.one("SELECT body FROM request_diagnostics")
    assert "PRIVATE TRACE" in evidence["body"]
    assert kernel.store.one("SELECT purpose FROM requests")["purpose"] == "research"
    read = await call(kernel, context, operation="read_result", job_id=job["id"], length=30)
    assert len(read["content"]) == 30 and read["next"]["offset"] == 30
    assert kernel.jobs.get(job["id"])["notification"] == "read"
    await call(kernel, context, **start())
    assert len(requests) == 1


async def test_discovery_validation_grants_and_slash_scope(kernel):
    context = setup(kernel)
    help_result = await call(kernel, context)
    assert help_result["usage_only"] and not kernel.jobs.tasks
    invalid = await call(kernel, context, **start(max_output_tokens=99999))
    assert invalid.get("error") and not kernel.jobs.tasks
    other = ToolContext(context.bot, "slash:ada:owner", "slash-turn")
    denied = await call(kernel, other, **start())
    assert "conversational channel" in str(denied) and not kernel.jobs.tasks
    plugin = kernel.store.get("plugins", ID)
    plugin["enabled"] = False
    kernel.store.put("plugins", plugin)
    assert ID not in [s["function"]["name"] for s in kernel.registry.schemas(context)]


async def test_retry_uses_same_assignment_and_upstream_error_is_saved(kernel):
    context = setup(kernel, stream=False)
    provider = kernel.store.get("providers", "openrouter")
    provider.update(retry_count=1, retry_delay_seconds=0.01)
    kernel.store.put("providers", provider)
    bodies = []

    def respond(request):
        bodies.append(request.content)
        return httpx.Response(503, json={"error": "busy"})

    await install_client(kernel, respond)
    result = await call(kernel, context, **start())
    await finish(kernel)
    job = kernel.jobs.get(result["job_id"])
    assert job["state"] == "failed" and "503" in job["error"]
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert job["notification"] == "pending"


async def test_secondary_profile_change_cancels_owned_request(kernel, owner):
    context = setup(kernel)
    entered, closed = asyncio.Event(), asyncio.Event()

    async def respond(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    await install_client(kernel, respond)
    result = await call(kernel, context, **start())
    await entered.wait()
    await kernel.service.save(owner, "profiles", "research", {"name": "Changed research profile"})
    assert closed.is_set() and kernel.jobs.get(result["job_id"])["state"] == "cancelled"


async def test_operator_configuration_validates_ranges(kernel, owner):
    setup(kernel)
    with pytest.raises(ControlError):
        await kernel.service.save(owner, "plugins", ID, {"config": {"max_output_tokens": 0}})
    with pytest.raises(ControlError):
        await kernel.service.save(owner, "bots", "ada", {"plugin_config": {ID: {"profile_id": "absent"}}})
