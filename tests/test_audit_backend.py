import asyncio
import json

import httpx
import pytest

from conftest import configured
from support.background_jobs import setup as setup_jobs
from support.provider import Fragments, call_args, install_client
from support.research_assistant import call, setup as setup_research, start
from support.background_jobs import finish


@pytest.mark.parametrize("stream", [True, False])
async def test_completion_exposes_same_private_safe_metrics_as_event(kernel, stream):
    bot = configured(kernel)
    args = call_args(kernel, bot)
    args["profile"]["stream"] = stream
    private = "Private reasoning must not enter numeric measurements."

    def respond(request):
        packet = {
            "choices": [
                {
                    "delta" if stream else "message": {"content": "Report", "reasoning_content": private},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }
        if not stream:
            return httpx.Response(200, json=packet)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=Fragments(("data: " + json.dumps(packet) + "\n\ndata: [DONE]\n\n").encode()),
        )

    await install_client(kernel, respond)
    result = await kernel.pool.complete(**args)
    data = json.loads(
        kernel.store.one(
            "SELECT data FROM events WHERE request_id=? AND kind='request.completed'", (result.request_id,)
        )["data"]
    )
    assert result.metrics and all(data[key] == value for key, value in result.metrics.items())
    assert result.metrics["token_counts"]["reasoning"] == {"value": 0, "source": "reported"}
    assert private not in json.dumps(result.metrics)
    assert (result.metrics["tps"] is not None) == stream
    assert result.metrics["tps_source"] == ("reported" if stream else "unavailable")
    assert "metrics" not in result.message()


async def test_research_metrics_survive_absent_completion_event(kernel, monkeypatch):
    context = setup_research(kernel, stream=False)
    await install_client(
        kernel,
        lambda request: httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Report"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            },
        ),
    )
    original_emit = kernel.store.emit

    def without_completion_event(kind, *args, **kwargs):
        if kind != "request.completed":
            return original_emit(kind, *args, **kwargs)

    monkeypatch.setattr(kernel.store, "emit", without_completion_event)
    submitted = await call(kernel, context, **start())
    await finish(kernel)
    job = kernel.jobs.get(submitted["job_id"])
    assert job["state"] == "completed", job["error"]
    metrics = json.loads(job["result"])["metrics"]
    assert metrics["tokens"]["total"]["value"] == 30
    assert metrics["duration_ms"] >= 0
    assert metrics["tps"] is None and metrics["tps_source"] == "unavailable"


async def test_cancel_before_first_worker_step_removes_task_and_settles_row(kernel):
    entered = False

    async def work(job):
        nonlocal entered
        entered = True

    context = setup_jobs(kernel, work)
    submitted = kernel.jobs.submit(context, "memory", "pre-entry", {}, seconds=60)
    job = kernel.jobs.get(submitted["job_id"])
    await kernel.jobs.cancel_job(job, "Immediate cancellation")
    assert not entered
    assert not kernel.jobs.tasks
    assert kernel.jobs.get(job["id"])["state"] == "cancelled"


async def test_cancel_job_tolerates_retention_cleanup_during_join(kernel):
    entered = asyncio.Event()

    async def work(job):
        entered.set()
        await asyncio.Event().wait()

    context = setup_jobs(kernel, work)
    submitted = kernel.jobs.submit(context, "memory", "retention", {}, seconds=60)
    job = kernel.jobs.get(submitted["job_id"])
    await entered.wait()

    def expire_settled_row(task):
        kernel.store.execute("UPDATE background_jobs SET updated_at=0 WHERE id=?", (job["id"],))
        kernel.jobs.cleanup()

    kernel.jobs.tasks[job["id"]].add_done_callback(expire_settled_row)
    await kernel.jobs.cancel_job(job, "Cancelled during retention")
    assert kernel.jobs.get(job["id"]) is None
    assert not kernel.jobs.tasks


async def test_new_batch_gate_does_not_reblock_prior_workers(kernel):
    entered = []

    async def work(job):
        entered.append(job["submission_id"])
        return {"report": "done"}

    context = setup_jobs(kernel, work)
    kernel.jobs.limits["memory"] = lambda _: 2
    kernel.store.execute(
        "INSERT INTO turns(id,bot_id,channel_id,profile_id,provider_id,model,started_at,status,trigger,revision) "
        "VALUES(?,?,?,?,?,?,0,'running','scheduled',1)",
        (context.turn_id, context.bot["id"], context.channel_id, "balanced", "openrouter", "test-model"),
    )
    kernel.jobs.begin_batch(context.turn_id)
    first = kernel.jobs.submit(context, "memory", "first-batch", {}, seconds=60)
    first_task = kernel.jobs.tasks[first["job_id"]]
    kernel.jobs.release_batch(context.turn_id)
    kernel.jobs.begin_batch(context.turn_id)
    second = kernel.jobs.submit(context, "memory", "second-batch", {}, seconds=60)
    second_task = kernel.jobs.tasks[second["job_id"]]
    await first_task
    assert entered == ["first-batch"]
    assert not second_task.done()
    kernel.jobs.release_batch(context.turn_id)
    await second_task
    assert entered == ["first-batch", "second-batch"]
