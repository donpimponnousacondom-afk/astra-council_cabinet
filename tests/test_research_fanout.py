import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from hortator.models import ControlError
from hortator.research_assistant import ID
from test_background_jobs import finish, setup as background_setup
from test_provider import install_client
from test_research_assistant import call, setup, start
from test_runtime import completion, settle
from test_runtime_feedback import reply, responses, tool


def set_limit(kernel, value, *, bot=False):
    kind, entity_id = ("bots", "ada") if bot else ("plugins", ID)
    entity = kernel.store.get(kind, entity_id)
    target = entity.setdefault("plugin_config", {}).setdefault(ID, {}) if bot else entity["config"]
    target["max_parallel_jobs"] = value
    kernel.store.put(kind, entity)


def worker(kernel, run):
    # Keep real registry admission and grant/profile checks while replacing only
    # paid inference in lifecycle/admission tests.
    _, check = kernel.jobs.handlers[ID]
    kernel.jobs.handlers[ID] = (run, check)


@pytest.mark.parametrize("count", [4, 16])
async def test_configured_research_fanout_and_unread_completions_share_allowance(kernel, count):
    context = setup(kernel)
    if count != 4:
        set_limit(kernel, count, bot=True)
    entered, release = asyncio.Event(), asyncio.Event()
    running = []

    async def run(job):
        running.append(job["id"])
        if len(running) == count:
            entered.set()
        await release.wait()
        return {"report": "Saved research"}

    worker(kernel, run)
    jobs = [await call(kernel, context, **start(submission_id=f"job-{n}")) for n in range(count)]
    async with asyncio.timeout(2):
        await entered.wait()
    assert len(running) == count  # No hidden two-worker or eight-job ceiling.
    assert jobs[-1]["capacity"]["available_slots"] == 0
    assert {job["state"] for job in kernel.jobs.inventory(bot_id="ada")} == {"running"}
    repeat = await call(kernel, context, **start(submission_id="job-0"))
    assert repeat["job_id"] == jobs[0]["job_id"]  # Recover while full; never duplicate paid work.
    denied = await call(kernel, context, **start(submission_id="overflow"))
    assert f"{count}/{count} outstanding" in denied["error"]
    assert len(kernel.jobs.tasks) == count
    release.set()
    await finish(kernel)
    capacity = kernel.jobs.capacity("ada", ID)
    assert capacity["active_jobs"] == 0 and capacity["pending_completions"] == count
    assert capacity["available_slots"] == 0
    denied = await call(kernel, context, **start(submission_id="still-full"))
    assert "pending completions" in denied["error"]
    read = await call(kernel, context, operation="read_result", job_id=jobs[0]["job_id"])
    assert read["notification"] == "read" and read["capacity"]["available_slots"] == 1
    result = await call(kernel, context, **start(submission_id="replacement"))
    assert result["capacity"]["available_slots"] == 0
    await finish(kernel)
    queued = kernel.store.rows("SELECT data FROM events WHERE kind='background_job.queued'")
    assert len(queued) == count + 1
    assert json.loads(queued[count - 1]["data"])["outstanding_jobs"] == count


async def test_background_service_defaults_to_one_and_plugin_allowance_is_independent(kernel):
    context = background_setup(kernel, AsyncMock(return_value={}))
    first = kernel.jobs.submit(context, "memory", "first", {}, seconds=60)
    with pytest.raises(ControlError, match="1/1 outstanding"):
        kernel.jobs.submit(context, "memory", "second", {}, seconds=60)
    assert first["capacity"]["max_parallel_jobs"] == 1
    await finish(kernel)


async def test_current_global_or_bot_limit_applies_to_existing_jobs_without_replay(kernel):
    context = setup(kernel)
    worker(kernel, AsyncMock(return_value={}))
    set_limit(kernel, 16)
    jobs = [await call(kernel, context, **start(submission_id=f"job-{n}")) for n in range(6)]
    await finish(kernel)
    assert kernel.jobs.capacity("ada", ID)["max_parallel_jobs"] == 16
    set_limit(kernel, 4, bot=True)
    # Admission resolves current settings even if a caller holds an older bot snapshot.
    capacity = kernel.jobs.capacity("ada", ID)
    assert capacity["outstanding_jobs"] == 6 and capacity["available_slots"] == 0
    denied = await call(kernel, context, **start(submission_id="over-lowered-limit"))
    assert "6/4 outstanding" in denied["error"]
    for job in jobs[:3]:
        await call(kernel, context, operation="read_result", job_id=job["job_id"])
    assert kernel.jobs.capacity("ada", ID)["available_slots"] == 1
    accepted = await call(kernel, context, **start(submission_id="new"))
    assert accepted["capacity"]["max_parallel_jobs"] == 4
    await finish(kernel)
    assert len(kernel.store.rows("SELECT * FROM background_jobs")) == 7


@pytest.mark.parametrize("value", [0, -1, 3, True, 4.0, 4.5, "8"])
@pytest.mark.parametrize("bot", [False, True])
async def test_parallel_job_configuration_rejects_non_integer_or_under_four(kernel, owner, value, bot):
    setup(kernel)
    kind, entity_id = ("bots", "ada") if bot else ("plugins", ID)
    changes = (
        {"plugin_config": {ID: {"max_parallel_jobs": value}}}
        if bot
        else {"config": {"max_parallel_jobs": value}}
    )
    with pytest.raises(ControlError, match="max_parallel_jobs"):
        await kernel.service.save(owner, kind, entity_id, changes)


async def test_operator_limits_above_previous_caps_and_all_outstanding_jobs_remain_visible(kernel, owner):
    context = setup(kernel)
    config = kernel.store.get("plugins", ID)["config"]
    await kernel.service.save(owner, "plugins", ID, {"config": {**config, "max_parallel_jobs": 64}})
    worker(kernel, AsyncMock(return_value={}))
    jobs = [await call(kernel, context, **start(submission_id=f"job-{n}")) for n in range(60)]
    await finish(kernel)
    for job in jobs:
        assert job["job_id"]
    inventory = kernel.jobs.inventory(bot_id="ada", plugin=ID)
    listing = await call(kernel, context, operation="list")
    assert len(inventory) == len(listing["jobs"]) == 60
    assert listing["capacity"]["available_slots"] == 4
    for job in jobs[:35]:
        await call(kernel, context, operation="read_result", job_id=job["job_id"])
    listing = await call(kernel, context, operation="list")
    assert len(listing["jobs"]) == 25 + 30  # All pending + bounded recent settled history.
    assert len(kernel.jobs.inventory(bot_id="ada")) == 60
    for job in jobs[35:]:
        await call(kernel, context, operation="read_result", job_id=job["job_id"])
    assert len(kernel.jobs.inventory(bot_id="ada")) == 50
    assert len((await call(kernel, context, operation="list"))["jobs"]) == 30


@pytest.mark.parametrize("change", ["pause", "plugin", "allowance"])
async def test_configuration_cancellation_joins_all_workers(kernel, owner, change):
    context = setup(kernel)
    entered, exited, ready = [], [], asyncio.Event()

    async def run(job):
        entered.append(job["id"])
        if len(entered) == 4:
            ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.append(job["id"])

    worker(kernel, run)
    for n in range(4):
        await call(kernel, context, **start(submission_id=f"job-{n}"))
    async with asyncio.timeout(2):
        await ready.wait()
    if change == "pause":
        await kernel.service.save(owner, "bots", "ada", {"enabled": False})
    elif change == "plugin":
        await kernel.service.save(owner, "plugins", ID, {"enabled": False})
    else:
        await kernel.service.save(owner, "bots", "ada", {"plugin_config": {ID: {"max_parallel_jobs": 8}}})
    assert sorted(entered) == sorted(exited) and not kernel.jobs.tasks
    assert {job["state"] for job in kernel.jobs.inventory(bot_id="ada")} == {"cancelled"}
    assert kernel.jobs.capacity("ada", ID)["outstanding_jobs"] == 0


async def test_restart_marks_entire_fanout_interrupted_and_each_notification_claims_once(kernel):
    context = setup(kernel)
    run = AsyncMock(return_value={})
    worker(kernel, run)
    for n in range(4):
        await call(kernel, context, **start(submission_id=f"job-{n}"))
    await finish(kernel)
    kernel.store.execute("UPDATE background_jobs SET state='running',result=NULL,notification='none'")
    kernel.jobs.recover()  # Simulate durable state after a crash, without restarting any worker.
    assert not kernel.jobs.tasks and run.await_count == 4
    for n in range(4):
        job = kernel.jobs.pending(context.bot)
        assert job["state"] == "interrupted"
        assert kernel.jobs.claim(job, f"followup-{n}")
        assert not kernel.jobs.claim(job, f"duplicate-{n}")
    assert kernel.jobs.pending(context.bot) is None
    assert kernel.jobs.capacity("ada", ID)["available_slots"] == 4


async def test_fanout_keeps_provider_concurrency_authoritative(kernel):
    context = setup(kernel, stream=False)
    provider = kernel.store.get("providers", "openrouter")
    kernel.store.put("providers", {**provider, "max_concurrency": 2})
    entered, finished, peak = [], [], 0
    pair, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        nonlocal peak
        entered.append(request)
        peak = max(peak, len(entered) - len(finished))
        if len(entered) == 2:
            pair.set()
        await release.wait()
        finished.append(request)
        return completion("Research result")

    await install_client(kernel, respond)
    for n in range(4):
        await call(kernel, context, **start(submission_id=f"job-{n}"))
    async with asyncio.timeout(2):
        await pair.wait()
    assert len(entered) == 2 and len(kernel.jobs.tasks) == 4
    release.set()
    await finish(kernel)
    assert len(entered) == 4 and peak == 2
    assert {job["state"] for job in kernel.jobs.inventory(bot_id="ada")} == {"completed"}


async def test_provider_queue_wait_cannot_extend_a_research_job_deadline(kernel):
    context = setup(kernel, stream=False)
    provider = kernel.store.get("providers", "openrouter")
    kernel.store.put("providers", {**provider, "max_concurrency": 1})
    plugin = kernel.store.get("plugins", ID)
    kernel.store.put("plugins", {**plugin, "config": {**plugin["config"], "timeout_seconds": 1}})
    entered, exited = [], []

    async def respond(request):
        entered.append(request)
        try:
            await asyncio.Event().wait()
        finally:
            exited.append(request)

    await install_client(kernel, respond)
    for n in range(4):
        await call(kernel, context, **start(submission_id=f"job-{n}"))
    async with asyncio.timeout(3):
        await finish(kernel)
    assert len(entered) == len(exited) == 1
    assert not kernel.jobs.tasks and kernel.pool.active["openrouter"] == 0
    jobs = kernel.jobs.inventory(bot_id="ada")
    assert len(jobs) == 4
    assert all(job["state"] == "failed" and "total deadline exceeded" in job["error"] for job in jobs)


@pytest.mark.parametrize("calls_per_round,requested,expected", [(4, 4, 4), (4, 5, 0)])
async def test_engine_charges_one_call_per_researcher_and_preserves_round_budget(
    kernel, calls_per_round, requested, expected
):
    setup(kernel)
    bot = kernel.store.get("bots", "ada")
    kernel.store.put(
        "bots",
        {
            **bot,
            "interval_seconds": 1,
            "max_tool_rounds": 1,
            "max_calls_per_round": calls_per_round,
            "tool_working_set_tokens": 24000,
        },
    )
    set_limit(kernel, 16)
    kernel.engine.transport = AsyncMock()
    kernel.engine.transport.send.return_value = "777777777777777777"
    worker(kernel, AsyncMock(return_value={}))
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply(*(tool(ID, start(submission_id=f"job-{n}"), f"call-{n}") for n in range(requested)))
        reports = responses(body)
        assert len(reports) == requested
        if expected:
            assert all(report["job_id"] for report in reports)
        else:
            assert all("maximum 4 per round" in report["error"] for report in reports)
        assert ID not in [item["function"]["name"] for item in body["tools"]]
        return completion("Research requested")

    await install_client(kernel, respond)
    await kernel.engine.tick()
    await settle(kernel)
    await finish(kernel)
    assert len(requests) == 2
    turn = kernel.store.one("SELECT * FROM turns")
    assert turn["status"] == "sent", turn
    assert len(kernel.store.rows("SELECT * FROM background_jobs")) == expected
    assert len(kernel.store.rows("SELECT * FROM events WHERE kind='tool.started'")) == expected
    assert not kernel.store.one("SELECT 1 FROM events WHERE kind='tool.task_started'")


async def test_fanout_does_not_allow_completion_recursion_or_paused_one_shots(kernel):
    context = setup(kernel)
    worker(kernel, AsyncMock(return_value={}))
    context.bot["background_completion"] = {"job_id": "completed"}
    denied = await call(kernel, context, **start())
    assert "follow-up cannot launch" in denied["error"]
    assert not kernel.jobs.tasks
    context.bot.pop("background_completion")
    bot = kernel.store.get("bots", "ada")
    kernel.store.put("bots", {**bot, "enabled": False})
    denied = await call(kernel, context, **start())
    assert "paused" in denied["error"] and not kernel.jobs.tasks
    assert kernel.jobs.capacity("ada", ID)["available_slots"] == 4
