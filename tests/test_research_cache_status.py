"""Live research accounting stays current without rewriting advertised schemas."""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from hortator.plugins import ToolContext
from support.background_jobs import finish
from support.provider import install_client
from support.research_assistant import ID, call, setup, start
from support.research_fanout import worker
from support.runtime import completion, settle
from support.runtime_feedback import reply, tool


def schema(kernel, context):
    return next(item for item in kernel.registry.schemas(context) if item["function"]["name"] == ID)


def claim(kernel, context, job_id):
    turn_id = "research-cache-followup"
    kernel.store.execute(
        "INSERT INTO turns(id,bot_id,channel_id,profile_id,provider_id,model,started_at,status,trigger,revision) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            turn_id,
            context.bot["id"],
            context.channel_id,
            "balanced",
            "openrouter",
            "test",
            time.time(),
            "running",
            "background_completion",
            1,
        ),
    )
    assert kernel.jobs.claim(kernel.jobs.get(job_id), turn_id)
    return ToolContext(
        {**context.bot, "background_completion": kernel.jobs.notification(kernel.jobs.get(job_id))},
        context.channel_id,
        turn_id,
    )


async def test_research_schema_stays_stable_across_admissions_and_completion_reads(kernel):
    context = setup(kernel)
    release = asyncio.Event()

    async def research(job):
        await release.wait()
        return {"report": "Saved findings"}

    worker(kernel, research)
    before = schema(kernel, context)
    help_before = await call(kernel, context)
    status_before = kernel.registry.spec_for(ID, context).prompt_status
    assert "0/3 used" in status_before and "available slots: 4" in status_before

    jobs = [await call(kernel, context, **start(submission_id=f"cache-{n}")) for n in range(4)]
    assert all(job.get("job_id") for job in jobs)
    assert schema(kernel, context) == before
    status_full = kernel.registry.spec_for(ID, context).prompt_status
    assert "1/3 used" in status_full and "available slots: 0" in status_full
    assert "outstanding researchers: 4/4" in status_full
    help_full = await call(kernel, context)
    assert help_full["usage_only"] and help_full != help_before
    assert "outstanding researchers: 4/4" in str(help_full)

    denied = await call(kernel, context, **start(submission_id="over-capacity"))
    assert "4/4 outstanding" in denied["error"]
    assert len(kernel.store.rows("SELECT * FROM background_jobs")) == 4
    assert len(kernel.store.rows("SELECT * FROM research_batches")) == 1

    release.set()
    await finish(kernel)
    assert schema(kernel, context) == before
    assert "available slots: 0" in kernel.registry.spec_for(ID, context).prompt_status
    await call(kernel, context, operation="read_result", job_id=jobs[0]["job_id"])
    assert schema(kernel, context) == before
    assert "available slots: 1" in kernel.registry.spec_for(ID, context).prompt_status
    assert "outstanding researchers: 3/4" in str(await call(kernel, context))


async def test_research_schema_preserves_contextual_settings_and_output_validation(kernel):
    context = setup(kernel)
    bot = kernel.store.get("bots", context.bot["id"])
    bot["plugin_config"][ID] = {
        "max_output_tokens": 1024,
        "timeout_seconds": 45,
        "max_keyword": 2,
        "limit": 9,
        "notify_on_completion": False,
    }
    kernel.store.put("bots", bot)
    context = ToolContext(bot, context.channel_id, context.turn_id)
    advertised = schema(kernel, context)["function"]
    assert advertised["parameters"]["properties"]["max_output_tokens"]["maximum"] == 1024
    for text in (
        "output ceiling: 1024 tokens",
        "total deadline: 45 seconds",
        "up to 2 queries",
        "up to 9 results",
        "completion notification: False",
        "runtime facts",
    ):
        assert text in advertised["description"]
    assert "0/3 used" not in advertised["description"]
    assert "available slots: 4" not in advertised["description"]

    denied = await call(kernel, context, **start(max_output_tokens=1025))
    assert denied.get("error") and "1024" in str(denied)
    assert "outstanding researchers: 0/4" in str(denied)
    assert not kernel.store.rows("SELECT * FROM background_jobs")
    assert not kernel.store.rows("SELECT * FROM research_batches")


async def test_research_exhausted_batch_and_unavailable_ancestry_leave_schema_stable(kernel):
    context = setup(kernel)
    plugin = kernel.store.get("plugins", ID)
    plugin["config"]["max_research_batches"] = 1
    kernel.store.put("plugins", plugin)
    work = AsyncMock(return_value={"report": "Findings"})
    worker(kernel, work)
    before = schema(kernel, context)
    initial = await call(kernel, context, **start())
    await finish(kernel)
    followup = claim(kernel, context, initial["job_id"])
    assert schema(kernel, followup) == before
    status = kernel.registry.spec_for(ID, followup).prompt_status
    assert "1/1 used" in status and "0 new batches left" in status
    denied = await call(kernel, followup, **start(submission_id="exhausted"))
    assert "batch budget exhausted: 1/1" in denied["error"]

    unclaimed = ToolContext(followup.bot, followup.channel_id, "unclaimed-followup")
    assert schema(kernel, unclaimed) == before
    status = kernel.registry.spec_for(ID, unclaimed).prompt_status
    assert "New research dispatch unavailable" in status
    assert "without its own claimed research results" in status
    assert "New research dispatch unavailable" in str(await call(kernel, unclaimed))
    denied = await call(kernel, unclaimed, **start(submission_id="unclaimed"))
    assert "without its own claimed research results" in denied["error"]
    assert len(kernel.store.rows("SELECT * FROM background_jobs")) == 1
    assert len(kernel.store.rows("SELECT * FROM research_batches")) == 1
    work.assert_awaited_once()


async def test_research_runtime_status_is_sent_late_and_removed_from_tool_free_round(kernel):
    context = setup(kernel)
    kernel.store.put(
        "bots",
        {
            **context.bot,
            "interval_seconds": 1,
            "max_tool_rounds": 1,
            "allow_silence": False,
        },
    )
    transport = AsyncMock()
    transport.send.return_value = "888888888888888888"
    kernel.engine.transport = transport
    bodies = []

    def facts(body):
        marker = "Runtime facts (trusted): "
        content = next(
            m["content"]
            for m in body["messages"]
            if isinstance(m.get("content"), str) and marker in m["content"]
        )
        return json.JSONDecoder().raw_decode(content.split(marker, 1)[1])[0]

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            runtime = facts(body)
            status = runtime["tool_status"][ID]
            assert "0/3 used" in status and "available slots: 4" in status
            advertised = next(t["function"] for t in body["tools"] if t["function"]["name"] == ID)
            assert "0/3 used" not in advertised["description"]
            assert "available slots: 4" not in advertised["description"]
            assert "tool_status" not in body["messages"][0]["content"]
            return reply(tool(ID, {}))
        assert len(bodies) == 2
        assert not body.get("tools")
        assert "tool_status" not in facts(body)
        assert any(m["role"] == "tool" for m in body["messages"])
        return completion("Ready to research when needed.")

    await install_client(kernel, respond)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(bodies) == 2
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    assert not kernel.store.rows("SELECT * FROM background_jobs")
    transport.send.assert_awaited_once()


@pytest.mark.parametrize("revocation", ["plugin", "bot"])
async def test_research_status_respects_current_grants_and_round_tool_availability(kernel, revocation):
    context = setup(kernel)
    captured_tools = kernel.registry.schemas(context)
    assert ID in kernel.registry.prompt_status(context, captured_tools)
    assert kernel.registry.prompt_status(context, []) == {}

    if revocation == "plugin":
        plugin = kernel.store.get("plugins", ID)
        kernel.store.put("plugins", {**plugin, "enabled": False})
    else:
        bot = kernel.store.get("bots", context.bot["id"])
        kernel.store.put("bots", {**bot, "enabled_plugins": []})
    # Stale schemas and context cannot retain live status after current grants change.
    assert kernel.registry.prompt_status(context, captured_tools) == {}
