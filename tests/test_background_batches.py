import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from hortator.plugins import ToolContext
from hortator.store import dumps
from support.background_jobs import finish, setup


def context_for(base, *, channel=None, origin=None):
    return ToolContext(base.bot, channel or base.channel_id, origin or base.turn_id)


async def test_claim_only_coalesces_same_bot_channel_plugin_and_origin(kernel):
    base = setup(kernel, AsyncMock(return_value={"report": "saved"}))
    kernel.jobs.limits["memory"] = lambda _: 5
    room = kernel.store.get("rooms", "council")
    room.pop("revision")
    kernel.store.put("rooms", {**room, "id": "other-room", "channel_id": "222222222222222223"})
    bot = kernel.store.get("bots", "ada")
    kernel.store.put("bots", {**bot, "room_ids": [*bot["room_ids"], "other-room"]})
    kernel.store.ingest(
        discord_id="555555555555555557",
        channel_id="222222222222222223",
        room_id="other-room",
        author_id="1482143139828596916",
        author_name="The Boss",
        content="Other channel",
    )
    kernel.store.context("ada", "222222222222222223")
    same = [
        kernel.jobs.submit(base, "memory", f"same-{n}", {"task": f"Same {n}"}, seconds=60) for n in range(2)
    ]
    other_origin = kernel.jobs.submit(
        context_for(base, origin="another-origin"),
        "memory",
        "other-origin",
        {"task": "Other origin"},
        seconds=60,
    )
    other_channel = kernel.jobs.submit(
        context_for(base, channel="222222222222222223"),
        "memory",
        "other-channel",
        {"task": "Other channel"},
        seconds=60,
    )
    await finish(kernel)
    first = kernel.jobs.get(same[0]["job_id"])
    assert kernel.jobs.claim(first, "continuation")
    notice = kernel.jobs.notification(first)
    assert {job["job_id"] for job in notice["jobs"]} == {job["job_id"] for job in same}
    assert notice["progress"]["total"] == 2
    assert {kernel.jobs.get(job["job_id"])["notification"] for job in same} == {"claimed"}
    assert kernel.jobs.get(other_origin["job_id"])["notification"] == "pending"
    assert kernel.jobs.get(other_channel["job_id"])["notification"] == "pending"


async def test_large_batch_notice_stays_bounded_and_leaves_overflow_pending(kernel):
    base = setup(kernel, AsyncMock(return_value={"report": "saved"}))
    kernel.jobs.limits["memory"] = lambda _: 75
    for number in range(65):
        kernel.jobs.submit(
            base,
            "memory",
            f"job-{number}",
            {"task": f"Assignment {number} " + "detail " * 90},
            seconds=60,
        )
    await finish(kernel)
    first = kernel.jobs.pending(base.bot)
    assert kernel.jobs.claim(first, "first-notice")
    notice = kernel.jobs.notification(first)
    assert notice["progress"]["total"] == 65
    assert len(dumps(notice["jobs"])) <= 12000
    claimed = kernel.store.one("SELECT count(*) AS n FROM background_jobs WHERE notification='claimed'")["n"]
    pending = kernel.store.one("SELECT count(*) AS n FROM background_jobs WHERE notification='pending'")["n"]
    assert 0 < claimed < 65 and pending == 65 - claimed
    assert kernel.jobs.pending(base.bot) is not None
    notices = 1
    while next_job := kernel.jobs.pending(base.bot):
        notices += 1
        assert notices <= 65
        assert kernel.jobs.claim(next_job, f"notice-{notices}")
    assert kernel.jobs.pending(base.bot) is None
    assert (
        kernel.store.one("SELECT count(DISTINCT continuation_turn_id) AS n FROM background_jobs")["n"]
        == notices
    )


async def test_cleanup_retains_old_claimed_sibling_while_worker_or_continuation_active(kernel):
    second_gate = asyncio.Event()

    async def run(job):
        if job["submission_id"] == "second":
            await second_gate.wait()
        return {"report": "saved"}

    base = setup(kernel, run)
    kernel.jobs.limits["memory"] = lambda _: 2
    first = kernel.jobs.submit(base, "memory", "first", {"task": "First"}, seconds=60)
    second = kernel.jobs.submit(base, "memory", "second", {"task": "Second"}, seconds=60, notify=False)
    await kernel.jobs.tasks[first["job_id"]]
    assert kernel.jobs.claim(kernel.jobs.get(first["job_id"]), "active-followup")
    kernel.store.execute(
        "INSERT INTO turns(id,bot_id,channel_id,profile_id,provider_id,model,started_at,status,trigger,revision) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "active-followup",
            "ada",
            base.channel_id,
            "balanced",
            "openrouter",
            "test-model",
            time.time(),
            "running",
            "background_completion",
            1,
        ),
    )
    kernel.store.execute(
        "UPDATE background_jobs SET updated_at=? WHERE id=?", (time.time() - 8 * 86400, first["job_id"])
    )
    kernel.jobs.cleanup()
    assert kernel.jobs.get(first["job_id"]) is not None
    second_gate.set()
    await kernel.jobs.tasks[second["job_id"]]
    kernel.jobs.cleanup()
    assert kernel.jobs.get(first["job_id"]) is not None
    kernel.store.execute("UPDATE turns SET status='sent' WHERE id='active-followup'")
    kernel.jobs.cleanup()
    assert kernel.jobs.get(first["job_id"]) is None
    assert kernel.jobs.get(second["job_id"]) is not None


async def test_dispatch_gate_deadline_never_starts_paid_worker(kernel):
    work = AsyncMock(return_value={"report": "must not run"})
    base = setup(kernel, work)
    kernel.store.execute(
        "INSERT INTO turns(id,bot_id,channel_id,profile_id,provider_id,model,started_at,status,trigger,revision) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            base.turn_id,
            "ada",
            base.channel_id,
            "balanced",
            "openrouter",
            "test-model",
            time.time(),
            "running",
            "timer",
            1,
        ),
    )
    result = kernel.jobs.submit(base, "memory", "deadline", {"task": "Test"}, seconds=0.02)
    await finish(kernel)
    work.assert_not_awaited()
    job = kernel.jobs.get(result["job_id"])
    assert job["state"] == "failed" and "total deadline exceeded" in job["error"]
    assert kernel.jobs.pending(base.bot) is None
    kernel.store.execute("UPDATE turns SET status='failed' WHERE id=?", (base.turn_id,))
    kernel.jobs.release_turn(base.turn_id)
    assert not kernel.jobs.origin_gates
    assert kernel.jobs.pending(base.bot)["id"] == job["id"]


async def test_failed_turn_insert_rolls_back_entire_notification_claim(kernel, monkeypatch):
    base = setup(kernel, AsyncMock(return_value={"report": "saved"}))
    kernel.jobs.limits["memory"] = lambda _: 4
    for n in range(4):
        kernel.jobs.submit(base, "memory", f"item-{n}", {}, seconds=60)
    await finish(kernel)
    job = kernel.jobs.pending(base.bot)
    execute = kernel.store.execute

    def fail_insert(sql, args=()):
        if sql.startswith("INSERT INTO turns"):
            raise RuntimeError("test: disk write failed")
        return execute(sql, args)

    monkeypatch.setattr(kernel.store, "execute", fail_insert)
    with pytest.raises(RuntimeError, match="disk write failed"):
        kernel.engine.launch(base.bot, base.channel_id, completed_job=job)
    assert not kernel.store.db.in_transaction
    assert not kernel.engine.tasks
    assert all(
        row["notification"] == "pending" and row["continuation_turn_id"] is None
        for row in kernel.store.rows("SELECT * FROM background_jobs")
    )
    assert not kernel.store.one("SELECT 1 FROM events WHERE kind='background_job.notification_claimed'")
