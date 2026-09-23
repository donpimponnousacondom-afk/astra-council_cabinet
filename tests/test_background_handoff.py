import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from hortator.background_jobs import JobResultError
from hortator.plugins import ToolContext
from support.background_jobs import finish
from support.provider import install_client
from support.research_assistant import ID, start
from support.research_fanout import worker
from support.runtime import completion, settle
from support.runtime_feedback import reply, tool
from support.background_handoff import configure


def calls(count, *, notify=True):
    return [
        tool(ID, start(submission_id=f"handoff-{n}", notify_on_completion=notify), f"call-{n}")
        for n in range(count)
    ]


@pytest.mark.parametrize("first_notifies", [False, True])
async def test_mixed_notification_batch_cannot_take_provider_slot_before_ack(kernel, first_notifies):
    configure(kernel, 2)
    work = AsyncMock(return_value={"report": "saved"})
    worker(kernel, work)
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply(
                *[
                    tool(ID, start(submission_id=f"mixed-{n}", notify_on_completion=notifies), f"call-{n}")
                    for n, notifies in enumerate([first_notifies, not first_notifies])
                ]
            )
        work.assert_not_awaited()
        assert body.get("tools", []) == []
        return completion("One follow-up requested; the other result stays available.")

    await install_client(kernel, respond)
    await kernel.engine.tick()
    await settle(kernel)
    await finish(kernel)
    assert work.await_count == 2 and len(requests) == 2
    assert (
        kernel.store.one("SELECT count(*) AS n FROM background_jobs WHERE notification='pending'")["n"] == 1
    )


async def test_malformed_ack_does_not_execute_more_tools_or_lose_workers(kernel):
    configure(kernel, 4)
    work = AsyncMock(return_value={"report": "saved"})
    worker(kernel, work)
    await install_client(kernel, lambda _: reply(*calls(1)))
    await kernel.engine.tick()
    await settle(kernel)
    await finish(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "failed"
    assert len(kernel.store.rows("SELECT * FROM requests")) == 2
    assert len(kernel.store.rows("SELECT * FROM background_jobs")) == 1
    assert kernel.jobs.pending(kernel.store.get("bots", "ada")) is not None
    work.assert_awaited_once()
    kernel.engine.transport.send.assert_not_awaited()


@pytest.mark.parametrize("phase", ["background_handoff", "background_completion"])
async def test_full_receipts_appear_once_in_default_prompt_layers(kernel, phase):
    configure(kernel, 4)
    bot = kernel.store.get("bots", "ada")
    profile, _ = kernel.engine.configuration(bot)
    bot[phase] = {
        "origin_turn_id": "origin",
        "progress": {"total": 2},
        "jobs": [{"job_id": "first"}, {"job_id": "second", "assignment": "UNIQUE_ASSIGNMENT_MARKER"}],
    }
    layers = kernel.engine.contexts.dynamic_layers(bot, profile, "222222222222222222", 1, 100)
    assert "\n".join(layer["content"] for layer in layers).count("UNIQUE_ASSIGNMENT_MARKER") == 1


@pytest.mark.parametrize("count", [5, 16])
async def test_dispatch_ack_ends_typing_and_workers_survive_for_one_followup(kernel, count):
    configure(kernel, count)
    release = asyncio.Event()
    all_started = asyncio.Event()
    entered = set()
    typing_started, typing_stopped = asyncio.Event(), asyncio.Event()

    async def typing(*_, **__):
        typing_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            typing_stopped.set()

    kernel.engine.transport.typing.side_effect = typing

    async def run(job):
        entered.add(job["submission_id"])
        if len(entered) == count:
            all_started.set()
        await release.wait()
        return {"report": "Saved independent report"}

    worker(kernel, run)
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply(*calls(count))
        if len(requests) == 2:
            assert not entered  # Paid child work waits for the dispatch acknowledgement.
            assert not typing_started.is_set() or typing_stopped.is_set()
            assert body.get("tools", []) == []
            # Large native exchanges may be omitted at the configured 6k
            # working-set bound; durable control receipts must survive intact.
            for row in kernel.store.rows("SELECT id FROM background_jobs"):
                assert row["id"] in json.dumps(body["messages"])
            assert "background_handoff" in json.dumps(body["messages"])
            return completion("Research started; I will report when it completes.")
        assert "background_completion" in json.dumps(body["messages"])
        return completion("The research batch is complete.")

    await install_client(kernel, respond)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(kernel.jobs.tasks) == count
    assert not typing_started.is_set() or typing_stopped.is_set()
    await asyncio.wait_for(all_started.wait(), timeout=2)
    assert len(entered) == count
    assert len(requests) == 2
    assert len(kernel.store.rows("SELECT * FROM background_jobs")) == count
    assert {row["state"] for row in kernel.store.rows("SELECT state FROM background_jobs")} <= {
        "queued",
        "running",
    }
    assert kernel.store.one("SELECT trigger,status,error FROM turns")["status"] == "sent", kernel.store.one(
        "SELECT trigger,status,error FROM turns"
    )
    assert len(kernel.store.rows("SELECT * FROM events WHERE kind='background_job.handoff'")) == 1
    release.set()
    await finish(kernel)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(requests) == 3
    turns = kernel.store.rows("SELECT trigger,status FROM turns ORDER BY started_at")
    assert [turn["trigger"] for turn in turns] == ["timer", "background_completion"]
    assert all(turn["status"] == "sent" for turn in turns)
    assert {row["notification"] for row in kernel.store.rows("SELECT notification FROM background_jobs")} == {
        "claimed"
    }
    await kernel.engine.tick()
    assert len(kernel.store.rows("SELECT * FROM turns")) == 2
    assert kernel.engine.transport.send.await_count == 2
    assert kernel.engine.transport.edit.await_count == 0


async def test_partial_waves_coalesce_and_failed_sibling_is_reported_once(kernel):
    configure(kernel, 5)
    gates = [asyncio.Event() for _ in range(5)]

    async def run(job):
        number = int(job["submission_id"].split("-")[-1])
        await gates[number].wait()
        if number == 4:
            raise JobResultError("Researcher failed", {"complete": False, "report": "partial"})
        return {"report": f"Saved {number}"}

    worker(kernel, run)
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply(*calls(5))
        if len(requests) == 2:
            assert body.get("tools", []) == []
            return completion("Research started.")
        return completion(f"Wave {len(requests) - 2} received.")

    await install_client(kernel, respond)
    await kernel.engine.tick()
    await settle(kernel)
    for gate in gates[:2]:
        gate.set()
    first_ids = [
        row["id"]
        for row in kernel.store.rows("SELECT id,submission_id FROM background_jobs")
        if row["submission_id"] in {"handoff-0", "handoff-1"}
    ]
    await asyncio.gather(*(kernel.jobs.tasks[job_id] for job_id in first_ids))
    await kernel.engine.tick()
    await settle(kernel)
    first = json.dumps(requests[-1]["messages"])
    assert "handoff-0" in first and "handoff-1" in first
    assert "handoff-2" not in first or "pending_notifications" in first
    assert len(requests) == 3
    for gate in gates[2:]:
        gate.set()
    await finish(kernel)
    await kernel.engine.tick()
    await settle(kernel)
    second = json.dumps(requests[-1]["messages"])
    assert "handoff-2" in second and "handoff-3" in second and "handoff-4" in second
    assert "failed" in second
    assert len(requests) == 4
    assert [row["trigger"] for row in kernel.store.rows("SELECT trigger FROM turns ORDER BY started_at")] == [
        "timer",
        "background_completion",
        "background_completion",
    ]
    await kernel.engine.tick()
    assert len(requests) == 4


async def test_no_handoff_for_notify_false_or_recovery_from_older_origin(kernel):
    configure(kernel, 5)
    gate = asyncio.Event()

    async def run(job):
        await gate.wait()
        return {"report": "Saved"}

    worker(kernel, run)
    old_bot = kernel.store.get("bots", "ada")
    older = ToolContext(old_bot, "222222222222222222", "older-origin")
    prior = await kernel.registry.call(ID, start(submission_id="handoff-0"), older, "older-call")
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply(
                *calls(1), tool(ID, start(submission_id="quiet", notify_on_completion=False), "quiet")
            )
        assert any(item["function"]["name"] == ID for item in body["tools"])
        return completion("Recovered earlier work and started a quiet job.")

    await install_client(kernel, respond)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(requests) == 2
    rows = kernel.store.rows("SELECT submission_id,origin_turn_id,notify FROM background_jobs")
    assert len(rows) == 2
    assert (
        next(row for row in rows if row["submission_id"] == "handoff-0")["origin_turn_id"] == "older-origin"
    )
    assert kernel.jobs.get(prior["job_id"])["id"] == prior["job_id"]
    assert not kernel.store.rows("SELECT * FROM events WHERE kind='background_job.handoff'")
    gate.set()
    await finish(kernel)
    # The quiet job never queues a completion; the older job belongs to its old origin.
    assert (
        next(
            row
            for row in kernel.store.rows("SELECT submission_id,notification FROM background_jobs")
            if row["submission_id"] == "quiet"
        )["notification"]
        == "none"
    )
