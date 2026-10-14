"""Live tool state stays discoverable without rewriting advertised schemas."""

import json
from unittest.mock import AsyncMock

import pytest

from conftest import configured
from hortator.plugins import ToolContext
from hortator.research_assistant import ID
from support.background_jobs import finish
from support.research_assistant import call, setup, start
from support.research_fanout import worker


@pytest.mark.parametrize("name", ["memory", "global_memory"])
async def test_memory_writes_keep_schema_bytes_stable_and_help_and_errors_live(kernel, name):
    plugin = kernel.store.get("plugins", name)
    kernel.store.put("plugins", {**plugin, "enabled": True})
    bot = configured(kernel, enabled_plugins=[name], memory_char_limit=100, global_memory_char_limit=100)
    context = ToolContext(bot, "room", "memory-schema")
    schemas = json.dumps(kernel.registry.schemas(context))
    initial = await kernel.registry.call(name, {}, context, "initial-help")
    assert initial["usage_only"] and "0/100" in initial["usage"]["description"]

    saved = await kernel.registry.call(
        name, {"operation": "write", "key": "topic", "value": "é\x00" * 52}, context, "write"
    )
    assert saved["saved"] and saved["budget"]["used_chars"] == 104
    assert saved["budget"]["must_consolidate"]
    assert json.dumps(kernel.registry.schemas(context)) == schemas
    help_result = await kernel.registry.call(name, {}, context, "updated-help")
    assert help_result["usage_only"] and not help_result["executed"]
    assert "104/100" in help_result["usage"]["description"]
    assert "WARNING: 4 characters over budget" in help_result["usage"]["description"]
    assert help_result["usage"]["parameters"]["properties"]["value"]["maxLength"] == 8000

    rejected = await kernel.registry.call(
        name, {"operation": "write", "key": "extra", "value": "x"}, context, "over-budget"
    )
    assert not rejected["ok"] and "would use 105" in rejected["error"]
    assert "104/100" in rejected["usage"]["description"]
    invalid = await kernel.registry.call(
        name,
        {"operation": "write", "key": "topic", "value": 123, "unexpected": True},
        context,
        "invalid",
    )
    assert not invalid["ok"] and not invalid["executed"]
    assert all(field in json.dumps(invalid["errors"]) for field in ("value", "unexpected"))
    assert "104/100" in invalid["usage"]["description"]
    read = await kernel.registry.call(name, {"operation": "read"}, context, "read")
    assert read["budget"]["used_chars"] == 104
    assert read["notes"][0]["value"] == "é\x00" * 52

    deleted = await kernel.registry.call(name, {"operation": "delete", "key": "topic"}, context, "delete")
    assert deleted["budget"]["used_chars"] == 0
    assert json.dumps(kernel.registry.schemas(context)) == schemas


async def test_research_capacity_and_task_batches_refresh_without_changing_schema(kernel):
    context = setup(kernel)
    worker(kernel, AsyncMock(return_value={"report": "Saved findings"}))
    tools = kernel.registry.schemas(context)
    schemas = json.dumps(tools)
    initial = kernel.registry.prompt_status(context, tools)[ID]
    assert "dispatch batches: 0/3 used" in initial
    assert "outstanding researchers: 0/4" in initial and "available slots: 4" in initial

    jobs = [await call(kernel, context, **start(submission_id=f"job-{n}")) for n in range(4)]
    full = kernel.registry.prompt_status(context, tools)[ID]
    assert "dispatch batches: 1/3 used" in full and "2 new batches left" in full
    assert "outstanding researchers: 4/4" in full and "available slots: 0" in full
    assert json.dumps(kernel.registry.schemas(context)) == schemas
    await finish(kernel)
    assert kernel.jobs.capacity(context.bot["id"], ID)["pending_completions"] == 4
    assert kernel.registry.prompt_status(context, tools)[ID] == full
    help_result = await call(kernel, context)
    assert help_result["usage_only"] and "outstanding researchers: 4/4" in help_result["usage"]["description"]
    rejected = await call(kernel, context, **start(submission_id="overflow"))
    assert not rejected["ok"] and "4/4 outstanding" in rejected["error"]
    assert "available slots: 0" in rejected["usage"]["description"]

    await call(kernel, context, operation="read_result", job_id=jobs[0]["job_id"])
    after_read = kernel.registry.prompt_status(context, tools)[ID]
    assert "outstanding researchers: 3/4" in after_read and "available slots: 1" in after_read
    assert "dispatch batches: 1/3 used" in after_read
    assert json.dumps(kernel.registry.schemas(context)) == schemas


async def test_research_schema_preserves_configured_limits_and_validation(kernel):
    context = setup(kernel)
    plugin = kernel.store.get("plugins", ID)
    plugin["config"].update(max_output_tokens=4096, timeout_seconds=321, max_keyword=7, limit=9)
    kernel.store.put("plugins", plugin)
    tool = next(
        tool["function"] for tool in kernel.registry.schemas(context) if tool["function"]["name"] == ID
    )
    assert tool["parameters"]["properties"]["max_output_tokens"]["maximum"] == 4096
    assert "output ceiling: 4096 tokens" in tool["description"]
    assert "total deadline: 321 seconds" in tool["description"]
    assert "up to 7 queries" in tool["description"] and "up to 9 results" in tool["description"]
    assert "default completion notification: True" in tool["description"]
    assert "dispatch batches: 0/3" not in tool["description"]
    assert "outstanding researchers: 0/4" not in tool["description"]

    rejected = await call(kernel, context, **start(max_output_tokens=4097))
    assert not rejected["ok"] and not rejected["executed"]
    assert rejected["usage"]["parameters"]["properties"]["max_output_tokens"]["maximum"] == 4096
    assert "outstanding researchers: 0/4" in rejected["usage"]["description"]
    assert not kernel.jobs.tasks


@pytest.mark.parametrize("revoked", ["global", "saved_bot", "captured_bot", "model_tool"])
async def test_prompt_status_requires_advertised_tool_and_current_grants(kernel, revoked):
    context = setup(kernel)
    tools = kernel.registry.schemas(context)
    assert ID in kernel.registry.prompt_status(context, tools)
    assert kernel.registry.prompt_status(context, []) == {}
    assert kernel.registry.prompt_status(context, None) == {}
    assert (
        kernel.registry.prompt_status(context, [{"type": "function", "function": {"name": "end_cycle"}}])
        == {}
    )

    if revoked == "global":
        plugin = kernel.store.get("plugins", ID)
        kernel.store.put("plugins", {**plugin, "enabled": False})
    elif revoked == "saved_bot":
        bot = kernel.store.get("bots", context.bot["id"])
        kernel.store.put("bots", {**bot, "enabled_plugins": []})
    elif revoked == "captured_bot":
        context.bot = {**context.bot, "enabled_plugins": []}
    else:
        kernel.registry.specs[ID].model_tool = False
    assert kernel.registry.prompt_status(context, tools) == {}


async def test_unavailable_research_ancestry_is_live_status_and_not_schema_text(kernel):
    context = setup(kernel)
    tools = kernel.registry.schemas(context)
    completion = ToolContext(
        {**context.bot, "background_completion": {"job_id": "unclaimed"}},
        context.channel_id,
        "completion-without-claim",
    )
    status = kernel.registry.prompt_status(completion, tools)[ID]
    assert "New research dispatch unavailable" in status
    assert "without its own claimed research results" in status
    assert kernel.registry.schemas(completion) == tools
    help_result = await call(kernel, completion)
    assert "New research dispatch unavailable" in help_result["usage"]["description"]
