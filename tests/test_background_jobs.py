import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from conftest import configured, ingest
from hortator.models import ControlError
from hortator.plugins import ToolContext
from test_provider import install_client
from test_runtime import completion, settle


def setup(k, run):
    bot = configured(k, interval_seconds=0, enabled_plugins=["memory"])
    ingest(k)
    k.jobs.register("memory", run=run, check=lambda _: None)
    k.engine.transport = AsyncMock()
    k.engine.transport.send.return_value = "777777777777777777"
    return ToolContext(bot, "222222222222222222", "origin")


async def finish(k):
    tasks = list(k.jobs.tasks.values())
    for task in tasks:
        await task


async def test_job_survives_submitter_and_completion_runs_once_with_timer_off(kernel):
    gate = asyncio.Event()

    async def work(job):
        await gate.wait()
        return {"report": "Saved research", "metrics": {"tps": 12}}

    context = setup(kernel, work)
    result = kernel.jobs.submit(context, "memory", "a", {"task": "Research"}, seconds=60)
    assert result["state"] == "queued" and not kernel.engine.tasks
    assert (
        kernel.jobs.submit(context, "memory", "a", {"task": "Research"}, seconds=60)["job_id"]
        == result["job_id"]
    )
    with pytest.raises(ControlError, match="already belongs"):
        kernel.jobs.submit(context, "memory", "a", {"task": "Different"}, seconds=60)
    gate.set()
    await finish(kernel)
    await install_client(kernel, lambda _: completion("Research is ready"))
    kernel.store.execute("UPDATE bot_runtime SET next_at=? WHERE bot_id='ada'", (time.time() + 3600,))
    await kernel.engine.tick()
    await settle(kernel)
    turn = kernel.store.one("SELECT * FROM turns")
    assert turn["trigger"] == "background_completion" and turn["status"] == "sent"
    request = kernel.store.one("SELECT body FROM requests")
    assert result["job_id"] in request["body"] and "Research" in request["body"]
    await kernel.engine.tick()
    assert len(kernel.store.rows("SELECT * FROM turns")) == 1


async def test_cancellation_joins_work_and_revokes_notification(kernel):
    entered, exited = asyncio.Event(), asyncio.Event()

    async def work(job):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    context = setup(kernel, work)
    result = kernel.jobs.submit(context, "memory", "a", {}, seconds=60)
    await entered.wait()
    await kernel.engine.cancel(["ada"], "Paused")
    assert exited.is_set() and not kernel.jobs.tasks
    job = kernel.jobs.get(result["job_id"])
    assert job["state"] == "cancelled" and job["notification"] != "pending"


async def test_scope_deadline_restart_and_no_recursive_followups(kernel):
    async def work(job):
        await asyncio.sleep(10)

    context = setup(kernel, work)
    result = kernel.jobs.submit(context, "memory", "a", {}, seconds=0.01)
    await finish(kernel)
    job = kernel.jobs.get(result["job_id"])
    assert job["state"] == "failed" and "deadline" in job["error"]
    other = ToolContext(context.bot, "other", "other")
    with pytest.raises(ControlError, match="scope"):
        kernel.jobs.owned(job["id"], other, "memory")
    context.bot["background_completion"] = {"job_id": job["id"]}
    with pytest.raises(ControlError, match="follow-up"):
        kernel.jobs.submit(context, "memory", "b", {}, seconds=60)
    kernel.store.execute("UPDATE background_jobs SET state='running' WHERE id=?", (job["id"],))
    kernel.jobs.recover()
    assert kernel.jobs.get(job["id"])["state"] == "interrupted"
    assert not kernel.jobs.tasks


async def test_completed_job_revoked_by_clean_slate_and_no_context_leak(kernel):
    context = setup(kernel, AsyncMock(return_value={"report": "old"}))
    result = kernel.jobs.submit(context, "memory", "a", {}, seconds=60)
    await finish(kernel)
    kernel.store.execute(
        "INSERT INTO context_resets VALUES(?,?,?,?)", ("ada", context.channel_id, 999, time.time() + 1)
    )
    assert kernel.jobs.pending(context.bot) is None
    assert kernel.jobs.get(result["job_id"])["notification"] == "revoked"
    with pytest.raises(ControlError, match="clean-slate"):
        kernel.jobs.owned(result["job_id"], context, "memory")


async def test_completed_results_are_in_sqlite_and_retention_rolls(kernel):
    context = setup(kernel, AsyncMock(return_value={"report": "saved"}))
    result = kernel.jobs.submit(context, "memory", "a", {}, seconds=60, notify=False)
    await finish(kernel)
    assert json.loads(kernel.jobs.get(result["job_id"])["result"])["report"] == "saved"
    kernel.store.execute("UPDATE background_jobs SET updated_at=?", (time.time() - 8 * 86400,))
    kernel.jobs.cleanup()
    assert kernel.jobs.get(result["job_id"]) is None


async def test_cancel_before_worker_enters_and_redact_assignment(kernel):
    work = AsyncMock(return_value={})
    context = setup(kernel, work)
    kernel.vault.put("provider/test/api_key", "private-test-key")
    result = kernel.jobs.submit(context, "memory", "a", {"task": "private-test-key"}, seconds=60)
    await kernel.engine.cancel(["ada"], "Paused")
    job = kernel.jobs.get(result["job_id"])
    assert job["state"] == "cancelled" and not kernel.jobs.tasks
    assert "private-test-key" not in job["payload"]
    work.assert_not_called()


async def test_human_priority_preserves_job_then_failed_followup_is_not_replayed(kernel):
    from test_addressing import message

    context = setup(kernel, AsyncMock(return_value={"report": "Ready"}))
    result = kernel.jobs.submit(context, "memory", "a", {"task": "Research"}, seconds=60)
    await finish(kernel)
    await kernel.connector.receive("ada", message(555555555555555556, mentions=[context.bot]))
    await install_client(kernel, lambda _: completion("Human answer"))
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.store.one("SELECT trigger FROM turns")["trigger"] == "human_mention"
    assert kernel.jobs.get(result["job_id"])["notification"] == "pending"
    kernel.engine.transport.send.side_effect = ControlError("Fixture delivery failed")
    # Personal cooldown remains authoritative, but need not delay this test.
    kernel.store.execute("UPDATE bot_runtime SET last_sent=0 WHERE bot_id='ada'")
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.jobs.get(result["job_id"])["notification"] == "claimed"
    turns = kernel.store.rows("SELECT * FROM turns ORDER BY started_at")
    assert len(turns) == 2 and turns[-1]["status"] == "failed"
    await kernel.engine.tick()
    assert len(kernel.store.rows("SELECT * FROM turns")) == 2


def test_background_owner_api_paging_and_cancel_require_auth_and_csrf(tmp_path):
    from fastapi.testclient import TestClient
    from hortator.app import create_app

    app = create_app(tmp_path, start_runtime=False)

    async def seed():
        k = app.state.kernel
        context = setup(k, AsyncMock(return_value={"report": "x" * 8000}))
        result = k.jobs.submit(context, "memory", "api", {"task": "Private assignment"}, seconds=60)
        await finish(k)
        return result["job_id"]

    with TestClient(app) as client:
        job_id = client.portal.call(seed)
        path = f"/api/background-jobs/{job_id}"
        assert client.get(path).status_code == 401
        assert client.get("/api/background-jobs").status_code == 401
        client.post("/api/auth/login", json={"password": (tmp_path / "initial-password").read_text().strip()})
        assert client.get("/api/background-jobs?bot_id=ada").json()[0]["id"] == job_id
        assert client.get("/api/background-jobs?bot_id=socrates").json() == []
        detail = client.get(path, params={"length": 30}).json()
        assert len(detail["content"]) == 30 and detail["next_offset"] == 30
        assert detail["assignment"] == "Private assignment"
        assert client.get(path, params={"length": 99999}).status_code == 422
        assert client.post(path + "/cancel").status_code == 403
        csrf = client.get("/api/auth/session").json()["csrf"]
        response = client.post(path + "/cancel", headers={"X-CSRF-Token": csrf})
        assert response.status_code == 200 and response.json()["notification"] == "revoked"
