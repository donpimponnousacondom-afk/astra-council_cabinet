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
        assert body["tools"] == [{"type": "web_search", "force_search": True, "max_keyword": 3, "limit": 5}]
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
    assert result["complete"]
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


@pytest.mark.parametrize(
    "saved,override,expected",
    [
        ({}, {}, (3, 5)),
        ({"max_keyword": 2}, {}, (2, 5)),
        ({"max_keyword": 50, "limit": 50}, {}, (50, 50)),
        ({"max_keyword": 50, "limit": 50}, {"max_keyword": 1, "limit": 7}, (1, 7)),
    ],
)
async def test_search_settings_inherit_and_reach_the_native_request(kernel, saved, override, expected):
    context = setup(kernel, stream=False)
    plugin = kernel.store.get("plugins", ID)
    # Older saved configurations may omit the new result allowance entirely.
    plugin["config"] = {"profile_id": "research", "system_prompt": "Keep my custom prompt.", **saved}
    kernel.store.put("plugins", plugin)
    bot = kernel.store.get("bots", "ada")
    bot["plugin_config"][ID] = override
    kernel.store.put("bots", bot)
    context = ToolContext(kernel.store.get("bots", "ada"), context.channel_id, context.turn_id)
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Findings"}, "finish_reason": "stop"}]}
        )

    await install_client(kernel, respond)
    started = await call(kernel, context, **start())
    await finish(kernel)
    job = kernel.jobs.get(started["job_id"])
    assert job["state"] == "completed", job["error"]
    payload = json.loads(job["payload"])
    assert (payload["max_keyword"], payload["limit"]) == expected
    assert len(bodies) == 1  # More results never add a local research loop.
    assert bodies[0]["tools"] == [
        {"type": "web_search", "force_search": True, "max_keyword": expected[0], "limit": expected[1]}
    ]
    assert bodies[0]["messages"][0]["content"] == "Keep my custom prompt."
    assert kernel.store.get("plugins", ID)["config"] == plugin["config"]


@pytest.mark.parametrize("field", ["max_keyword", "limit"])
@pytest.mark.parametrize("value", [0, 51, True, None, 1.5, "5"])
@pytest.mark.parametrize("per_bot", [False, True])
async def test_search_settings_reject_invalid_limits(kernel, owner, field, value, per_bot):
    setup(kernel)
    kind, entity_id = ("bots", "ada") if per_bot else ("plugins", ID)
    patch = {"plugin_config": {ID: {field: value}}} if per_bot else {"config": {field: value}}
    with pytest.raises(ControlError, match=field):
        await kernel.service.save(owner, kind, entity_id, patch)
    assert not kernel.jobs.tasks


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
    with pytest.raises(ControlError, match="Still referenced"):
        await kernel.service.delete(owner, "profiles", "research")


async def test_research_uses_selected_provider_key_and_marks_partial_reports(kernel):
    context = setup(kernel, stream=False)
    kernel.vault.put("bot/ada/provider_key", "MAIN-BOT-SECRET")
    kernel.vault.put("provider/openrouter/api_key", "RESEARCH-PROVIDER-SECRET")

    def respond(request):
        assert request.headers["authorization"] == "Bearer RESEARCH-PROVIDER-SECRET"
        assert "MAIN-BOT-SECRET" not in str(request.headers)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Partial report"}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 12},
            },
        )

    await install_client(kernel, respond)
    schema = kernel.registry.spec_for(ID, context)
    assert schema.parameters["properties"]["max_output_tokens"]["maximum"] == 16384
    response = await call(kernel, context, **start())
    await finish(kernel)
    job = kernel.jobs.get(response["job_id"])
    assert job["state"] == "completed", job["error"]
    report = json.loads(job["result"])
    assert not report["complete"] and report["finish_reason"] == "length"
    assert report["metrics"]["search_cost"] is None


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize(
    "content,native_calls,issue",
    [
        (
            "<tool_call><function=web_search><parameter=query>model prices</parameter></function></tool_call>"
            "<tool_call><function=web_search><parameter=query>benchmarks</parameter></function></tool_call>",
            False,
            "literal tool-call markup",
        ),
        ("", True, "unhandled tool calls"),
        ("   ", False, "no visible report"),
        (
            "Example syntax:\n```xml\n<tool_call><function=web_search></function></tool_call>\n```",
            False,
            None,
        ),
        ("A page mentions `<tool_call>` markup. No evidence found for its claim.", False, None),
    ],
)
async def test_report_validation_keeps_evidence_without_executing_or_retrying(
    kernel, stream, content, native_calls, issue
):
    context = setup(kernel, stream=stream)
    bodies = []
    annotation = {"url": "https://example.com/source", "title": "Source"}

    def respond(request):
        bodies.append(request.content)
        message = {"content": content, "annotations": [annotation], "reasoning_content": "PRIVATE TRACE"}
        if native_calls:
            message["tool_calls"] = [
                {"id": "native", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}
            ]
            if stream:
                message["tool_calls"][0]["index"] = 0
        usage = {"prompt_tokens": 40, "completion_tokens": 20, "web_search_usage": {"tool_usage": 3}}
        finish_reason = "tool_calls" if native_calls else "stop"
        if stream:
            packet = {
                "choices": [{"index": 0, "delta": message, "finish_reason": finish_reason}],
                "usage": usage,
            }
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=Fragments(("data: " + json.dumps(packet) + "\n\ndata: [DONE]\n\n").encode()),
            )
        return httpx.Response(
            200, json={"choices": [{"message": message, "finish_reason": finish_reason}], "usage": usage}
        )

    await install_client(kernel, respond)
    reply = await call(kernel, context, **start())
    await finish(kernel)
    job = kernel.jobs.get(reply["job_id"])
    saved = json.loads(job["result"])
    assert job["state"] == ("failed" if issue else "completed")
    assert saved["complete"] is (issue is None)
    assert saved["report"] == content.strip() and saved["sources"] == [annotation]
    assert saved["search_usage"] == {"tool_usage": 3}
    assert saved["metrics"]["attempts"] == 1 and "PRIVATE TRACE" not in job["result"]
    request = kernel.store.one("SELECT * FROM requests WHERE id=?", (saved["request_id"],))
    assert request["status"] == "completed"  # Transport did finish; validation is a separate job outcome.
    assert kernel.store.health("openrouter")["consecutive_failures"] == 0
    notification = kernel.jobs.notification(job)
    assert notification["state"] == job["state"]
    if issue:
        assert saved["kind"] == "invalid_researcher_output"
        assert issue in saved["validation_error"] and issue in notification["error"]
        event = kernel.store.one("SELECT * FROM events WHERE kind='background_job.failed'")
        assert event["level"] == "warning" and saved["request_id"] in event["data"]
    read = await call(kernel, context, operation="read_result", job_id=job["id"], length=18000)
    assert json.loads(read["content"]) == saved
    assert kernel.jobs.get(job["id"])["notification"] == "read"
    await call(kernel, context, **start())  # Same submission ID never replays the paid request.
    assert len(bodies) == 1
    assert not kernel.store.one(
        "SELECT 1 FROM events WHERE kind='tool.started' AND json_extract(data,'$.name')='web_search'"
    )


async def test_cancelled_partial_tool_markup_remains_cancellation(kernel):
    context = setup(kernel)
    received = asyncio.Event()

    class Partial(httpx.AsyncByteStream):
        async def __aiter__(self):
            packet = {"choices": [{"delta": {"content": "<tool_call><function=web_search>"}}]}
            yield ("data: " + json.dumps(packet) + "\n\n").encode()
            received.set()
            await asyncio.Event().wait()

    await install_client(
        kernel, lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Partial())
    )
    reply = await call(kernel, context, **start())
    await received.wait()
    await kernel.jobs.cancel_job(kernel.jobs.get(reply["job_id"]), "Owner stopped research")
    job = kernel.jobs.get(reply["job_id"])
    assert job["state"] == "cancelled" and job["notification"] != "pending"
    assert job["result"] is None
    assert kernel.store.one("SELECT status FROM requests")["status"] == "cancelled"
    assert not kernel.store.one("SELECT 1 FROM events WHERE kind='background_job.failed'")
