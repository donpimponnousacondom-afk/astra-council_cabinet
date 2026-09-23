import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.research_cycles import ResearchCycles
from support.background_handoff import configure
from support.background_jobs import finish
from support.provider import install_client
from support.research_assistant import ID, call, setup, start
from support.research_fanout import worker
from support.runtime import completion, settle
from support.runtime_feedback import reply, tool


def claim(kernel, context, job_id, turn_id):
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
    bot = {**context.bot, "background_completion": kernel.jobs.notification(kernel.jobs.get(job_id))}
    return ToolContext(bot, context.channel_id, turn_id)


def end(kernel, context):
    kernel.store.execute("UPDATE turns SET status='sent' WHERE id=?", (context.turn_id,))
    kernel.jobs.release_turn(context.turn_id)


async def test_three_batches_dispatch_sleep_evaluate_refine_and_stop_with_new_messages(kernel):
    configure(kernel, 4)
    entered = []

    async def research(job):
        entered.append(job["submission_id"])
        return {
            "report": "Limited initial coverage" if len(entered) <= 2 else "Refined findings",
            "complete": True,
        }

    worker(kernel, research)
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        n = len(requests)
        if n in (1, 3, 5):
            if n > 1:
                assert "research_cycle" in str(body["messages"])
                completed = kernel.store.one(
                    "SELECT id FROM background_jobs WHERE state='completed' ORDER BY created_at DESC"
                )
                read = [tool(ID, {"operation": "read_result", "job_id": completed["id"]}, f"read-{n}")]
            else:
                read = []
            return reply(
                *read,
                *[
                    tool(
                        ID,
                        start(submission_id=f"batch-{n}-{i}", task=f"Narrow question {n}-{i}"),
                        f"call-{n}-{i}",
                    )
                    for i in range(1 if n == 5 else 2)
                ],
            )
        if n in (2, 4, 6):
            assert not body.get("tools")
            assert len(entered) == {2: 0, 4: 2, 6: 4}[n]  # New workers wait for this acknowledgement.
            return completion("I dispatched refined research and will report back.")
        if n == 7:
            return reply(tool(ID, start(submission_id="over-budget"), "denied"))
        assert n == 8
        assert "batch budget exhausted: 3/3" in str(body["messages"])
        return completion("Here are the findings and the remaining gaps.")

    await install_client(kernel, respond)
    for _ in range(4):
        await kernel.engine.tick()
        await settle(kernel)
        await finish(kernel)
    assert len(requests) == 8 and len(entered) == 5
    batches = kernel.store.rows("SELECT * FROM research_batches ORDER BY batch_number")
    assert [b["batch_number"] for b in batches] == [1, 2, 3]
    assert len({b["root_turn_id"] for b in batches}) == 1
    assert kernel.engine.transport.send.await_count == 4
    assert kernel.engine.transport.edit.await_count == 0
    assert {t["status"] for t in kernel.store.rows("SELECT status FROM turns")} == {"sent"}
    await kernel.engine.tick()
    assert len(kernel.store.rows("SELECT * FROM turns")) == 4


async def test_sibling_waves_share_budget_and_initial_history_retention_does_not_refill_it(kernel):
    context = setup(kernel)
    gates = {"a": asyncio.Event(), "b": asyncio.Event()}

    async def research(job):
        if gate := gates.get(job["submission_id"]):
            await gate.wait()
        return {"report": "Findings"}

    worker(kernel, research)
    a = await call(kernel, context, **start(submission_id="a"))
    b = await call(kernel, context, **start(submission_id="b"))
    gates["a"].set()
    await kernel.jobs.tasks[a["job_id"]]
    first = claim(kernel, context, a["job_id"], "wave-a")
    c = await call(kernel, first, **start(submission_id="c"))
    duplicate = await call(kernel, first, **start(submission_id="c"))
    assert duplicate["job_id"] == c["job_id"]
    assert c["research_cycle"]["batches_used"] == 2
    end(kernel, first)
    gates["b"].set()
    await finish(kernel)
    second = claim(kernel, context, b["job_id"], "wave-b")
    d = await call(kernel, second, **start(submission_id="d"))
    assert d["research_cycle"]["batches_used"] == 3
    end(kernel, second)
    await finish(kernel)
    # Original jobs roll out, but descendants retain the original shared budget.
    kernel.store.execute(
        "UPDATE background_jobs SET updated_at=? WHERE origin_turn_id=?",
        (time.time() - 8 * 86400, context.turn_id),
    )
    kernel.jobs.cleanup()
    kernel.registry.research.cycles.cleanup()
    assert kernel.jobs.get(a["job_id"]) is None
    assert len(kernel.store.rows("SELECT * FROM research_batches")) == 3
    # Recreating the plugin budget reader does not reset SQLite accounting.
    kernel.registry.research.cycles = ResearchCycles(kernel.registry.research)
    third = claim(kernel, context, c["job_id"], "wave-c")
    plugin = kernel.store.get("plugins", ID)
    plugin["config"]["max_research_batches"] = 9
    kernel.store.put("plugins", plugin)
    denied = await call(kernel, third, **start(submission_id="e"))
    assert "3/3" in denied["error"]  # Increasing config applies only to new tasks.
    assert len(kernel.store.rows("SELECT * FROM research_batches")) == 3
    end(kernel, third)
    kernel.store.execute("DELETE FROM background_jobs")
    kernel.registry.research.cycles.cleanup()
    assert not kernel.store.rows("SELECT * FROM research_batches")


async def test_rejected_insert_rolls_back_budget_and_recovery_never_launches_paid_work(kernel):
    context = setup(kernel)
    work = AsyncMock(return_value={"report": "saved"})
    worker(kernel, work)
    kernel.store.execute(
        "CREATE TRIGGER reject_job BEFORE INSERT ON background_jobs BEGIN SELECT RAISE(ABORT, 'test insertion failure'); END"
    )
    denied = await call(kernel, context, **start())
    assert "test insertion failure" in denied["error"]
    assert not kernel.store.rows("SELECT * FROM research_batches") and not kernel.jobs.tasks
    kernel.store.execute("DROP TRIGGER reject_job")
    result = await call(kernel, context, **start())
    await finish(kernel)
    assert result["research_cycle"]["batches_used"] == 1
    kernel.jobs.recover()
    work.assert_awaited_once()
    assert len(kernel.store.rows("SELECT * FROM research_batches")) == 1


@pytest.mark.parametrize(
    "mode", ["disabled", "lowered", "wrong_channel", "wrong_bot", "forged_turn", "clean_slate", "legacy"]
)
async def test_followups_require_current_scope_real_claim_and_available_task_budget(kernel, mode):
    context = setup(kernel)
    work = AsyncMock(return_value={"report": "saved"})
    worker(kernel, work)
    plugin = kernel.store.get("plugins", ID)
    if mode == "disabled":
        plugin["config"]["max_research_batches"] = 1
        kernel.store.put("plugins", plugin)
    initial = await call(kernel, context, **start())
    await finish(kernel)
    followup = claim(kernel, context, initial["job_id"], "real-followup")
    if mode == "lowered":
        plugin["config"]["max_research_batches"] = 1
        kernel.store.put("plugins", plugin)
    elif mode == "wrong_channel":
        followup = ToolContext(followup.bot, "other-channel", followup.turn_id)
    elif mode == "wrong_bot":
        followup = ToolContext({**followup.bot, "id": "loki"}, followup.channel_id, followup.turn_id)
    elif mode == "forged_turn":
        followup = ToolContext(followup.bot, followup.channel_id, "forged-turn")
    elif mode == "clean_slate":
        kernel.store.execute(
            "INSERT INTO context_resets VALUES(?,?,?,?)", ("ada", context.channel_id, 999, time.time() + 1)
        )
    elif mode == "legacy":
        kernel.store.execute("DELETE FROM research_batches")
    denied = await call(kernel, followup, **start(submission_id="followup"))
    assert denied.get("error"), denied
    assert len(kernel.store.rows("SELECT * FROM background_jobs")) == 1
    work.assert_awaited_once()


@pytest.mark.parametrize("value", [0, -1, True, 3.0, 1.5, "3", None])
@pytest.mark.parametrize("per_bot", [False, True])
async def test_research_batch_setting_rejects_invalid_values(kernel, owner, value, per_bot):
    setup(kernel)
    field = {"max_research_batches": value}
    changes = {"plugin_config": {ID: field}} if per_bot else {"config": field}
    with pytest.raises(ControlError, match="max_research_batches"):
        await kernel.service.save(owner, "bots" if per_bot else "plugins", "ada" if per_bot else ID, changes)
