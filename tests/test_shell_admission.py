"""Admission uses real job records/leases and controlled, TaskGroup-owned workers."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.shell_runner import ShellRunner
from hortator.store import Store
from hortator.workspaces import Workspaces


@dataclass
class Command:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    cleanup_entered: asyncio.Event = field(default_factory=asyncio.Event)
    cleanup_release: asyncio.Event | None = None
    failure: bool = False
    input_text: str | None = None
    config: dict | None = None


class AdmissionScenario:
    def __init__(self, tmp_path, monkeypatch, *, limit=None):
        self.store = Store(tmp_path / "test.sqlite3")
        self.workspace = Workspaces(self.store, tmp_path, SimpleNamespace(redact=lambda value: value))
        self.limit = limit
        options = {} if limit is None else {"concurrency_limit": lambda: self.limit}
        self.runner = ShellRunner(tmp_path / "jobs", self.workspace, **options)
        self.commands = {}
        self.started = []
        self.scratch_calls = []
        self.contexts = {}

        async def readiness():
            return {"ready": True}

        async def scratch(context, config):
            self.scratch_calls.append(context.turn_id)
            return SimpleNamespace(facts=lambda: {})

        monkeypatch.setattr(self.runner, "readiness", readiness)
        monkeypatch.setattr(self.runner, "_task_scratch", scratch)
        monkeypatch.setattr(self.runner, "_execute", self.execute)

    async def context(self, bot, name, *, channel="room"):
        context = ToolContext({"id": bot}, channel, name)
        await self.workspace.call({"operation": "start", "task": "default"}, context)
        self.contexts[name] = context
        self.commands[name] = Command()
        return context

    async def execute(self, directory, source, config, active, scratch):
        name = config["command"]
        command = self.commands[name]
        command.config = config
        prior = source / "result.txt"
        command.input_text = prior.read_text() if prior.exists() else None
        for stream in ("stdout", "stderr"):
            (directory / f"{stream}.log").touch()
        self.started.append(name)
        command.entered.set()
        await command.release.wait()
        command.cleanup_entered.set()
        if command.cleanup_release is not None:
            await command.cleanup_release.wait()
        if active.cancelled:
            active.metadata.update(status="cancelled", exit_code=-9)
            return None
        if command.failure:
            raise RuntimeError("controlled worker failure")
        active.metadata.update(status="succeeded", exit_code=0)
        staged = directory / ".execution" / "staged"
        staged.mkdir(parents=True)
        (staged / "result.txt").write_text(name)
        return staged

    def launch(self, group, name, **kwargs):
        requested = asyncio.Event()

        async def call():
            requested.set()
            return await self.runner.run(self.contexts[name], task="default", command=name, **kwargs)

        return group.create_task(call()), requested

    async def entered(self, name):
        async with asyncio.timeout(2):
            await self.commands[name].entered.wait()

    def release(self, *names):
        for name in names:
            self.commands[name].release.set()

    def recorded(self):
        return {item["turn_id"] for item in self.runner.inspect(limit=100)}

    def leased(self):
        return {item["context"].turn_id for item in self.workspace._active.values()}


@pytest.fixture
async def scenarios(tmp_path, monkeypatch):
    values = []

    def create(*, limit=None):
        root = tmp_path / str(len(values))
        root.mkdir()
        value = AdmissionScenario(root, monkeypatch, limit=limit)
        values.append(value)
        return value

    yield create
    for value in values:
        await value.runner.close()
        value.store.close()


async def test_default_four_bots_run_together_and_fifth_waits_without_job_or_lease(scenarios):
    scenario = scenarios()
    names = [f"job-{index}" for index in range(5)]
    for index, name in enumerate(names):
        await scenario.context(f"bot-{index}", name)
    async with asyncio.TaskGroup() as group:
        tasks = []
        for name in names:
            task, requested = scenario.launch(group, name)
            tasks.append(task)
            await requested.wait()
        for name in names[:4]:
            await scenario.entered(name)
        assert scenario.started == names[:4]
        assert scenario.recorded() == scenario.leased() == set(names[:4])
        assert scenario.scratch_calls == names[:4]
        scenario.release(names[0])
        await scenario.entered(names[4])
        assert len(scenario.runner.active) == 4
        scenario.release(*names)
    assert all(task.result()["workspace_committed"] for task in tasks)
    assert not scenario.runner.active and not scenario.workspace._active


async def test_same_bot_serializes_through_export_while_other_bot_bypasses_it(scenarios):
    scenario = scenarios(limit=2)
    await scenario.context("ada", "first")
    await scenario.context("ada", "next")
    await scenario.context("sun", "independent")
    scenario.commands["first"].cleanup_release = asyncio.Event()
    async with asyncio.TaskGroup() as group:
        first, _ = scenario.launch(group, "first")
        await scenario.entered("first")
        next_call, requested = scenario.launch(group, "next")
        await requested.wait()
        independent, _ = scenario.launch(group, "independent")
        await scenario.entered("independent")
        assert scenario.started == ["first", "independent"]
        assert scenario.recorded() == scenario.leased() == {"first", "independent"}
        scenario.release("first")
        await scenario.commands["first"].cleanup_entered.wait()
        # Command execution has finished, but snapshot export/cleanup still owns the bot.
        assert not scenario.commands["next"].entered.is_set()
        assert "first" in scenario.leased()
        scenario.commands["first"].cleanup_release.set()
        await scenario.entered("next")
        assert scenario.commands["next"].input_text == "first"
        scenario.release("next", "independent")
    assert all(task.result()["workspace_committed"] for task in (first, next_call, independent))
    assert not scenario.workspace._active


async def test_global_queue_starts_eligible_bots_in_arrival_order(scenarios):
    scenario = scenarios(limit=1)
    names = ["first", "second", "third"]
    for name in names:
        await scenario.context(name, name)
    async with asyncio.TaskGroup() as group:
        tasks = []
        for name in names:
            task, requested = scenario.launch(group, name)
            tasks.append(task)
            await requested.wait()
        await scenario.entered("first")
        assert scenario.started == ["first"]
        scenario.release("first")
        await scenario.entered("second")
        assert scenario.started == ["first", "second"]
        scenario.release("second")
        await scenario.entered("third")
        scenario.release("third")
    assert all(task.result()["status"] == "succeeded" for task in tasks)


async def test_cancelled_waiter_has_no_job_lease_or_scratch_and_does_not_block_successor(scenarios):
    scenario = scenarios(limit=1)
    for name in ("first", "cancelled", "next"):
        await scenario.context(name, name)
    async with asyncio.TaskGroup() as group:
        first, _ = scenario.launch(group, "first")
        await scenario.entered("first")
        cancelled, requested = scenario.launch(group, "cancelled")
        await requested.wait()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert scenario.recorded() == scenario.leased() == {"first"}
        assert scenario.scratch_calls == ["first"]
        next_call, requested = scenario.launch(group, "next")
        await requested.wait()
        scenario.release("first")
        await scenario.entered("next")
        scenario.release("next")
    assert first.result()["status"] == next_call.result()["status"] == "succeeded"
    assert scenario.recorded() == {"first", "next"}


async def test_cancelling_running_caller_releases_slot_after_worker_cleanup(scenarios):
    scenario = scenarios(limit=1)
    await scenario.context("ada", "cancelled")
    await scenario.context("sun", "next")
    async with asyncio.TaskGroup() as group:
        cancelled, _ = scenario.launch(group, "cancelled")
        await scenario.entered("cancelled")
        next_call, requested = scenario.launch(group, "next")
        await requested.wait()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await scenario.entered("next")
        assert scenario.leased() == {"next"}
        scenario.release("next")
    assert next_call.result()["status"] == "succeeded"
    cancelled_record = next(item for item in scenario.runner.inspect() if item["turn_id"] == "cancelled")
    assert cancelled_record["status"] == "cancelled" and not cancelled_record["workspace_committed"]
    assert not scenario.runner.active and not scenario.workspace._active


async def test_close_cancels_waiters_and_joins_active_cleanup_without_starting_them(scenarios, monkeypatch):
    scenario = scenarios(limit=1)
    for name in ("active", "queued", "after-close"):
        await scenario.context(name, name)
    scenario.commands["active"].cleanup_release = asyncio.Event()
    execute = scenario.execute

    async def process_execution(directory, source, config, active, scratch):
        active.process = SimpleNamespace(returncode=None)
        try:
            return await execute(directory, source, config, active, scratch)
        finally:
            active.process.returncode = 0

    async def terminate(active):
        active.process.returncode = -9
        scenario.release(active.metadata["turn_id"])

    monkeypatch.setattr(scenario.runner, "_execute", process_execution)
    monkeypatch.setattr(scenario.runner, "_terminate", terminate)
    async with asyncio.TaskGroup() as group:
        active, _ = scenario.launch(group, "active")
        await scenario.entered("active")
        queued, requested = scenario.launch(group, "queued")
        await requested.wait()
        closing = group.create_task(scenario.runner.close())
        async with asyncio.timeout(2):
            await scenario.commands["active"].cleanup_entered.wait()
        assert not closing.done()
        assert scenario.recorded() == scenario.leased() == {"active"}
        assert not scenario.commands["queued"].entered.is_set()
        scenario.commands["active"].cleanup_release.set()
        async with asyncio.timeout(2):
            await closing
        assert active.done() and queued.cancelled()
    assert active.result()["status"] == "cancelled"
    assert scenario.recorded() == {"active"}
    assert not scenario.runner.active and not scenario.workspace._active
    with pytest.raises(ControlError, match="shutting down"):
        await scenario.runner.run(scenario.contexts["after-close"], task="default", command="after-close")


async def test_superseded_waiter_rechecks_before_start_without_creating_a_job(scenarios):
    scenario = scenarios(limit=1)
    for name in ("first", "superseded", "next"):
        await scenario.context(name, name)
    current = True
    checked = asyncio.Event()

    def before_start():
        checked.set()
        if not current:
            raise ControlError("new human input superseded this command")

    async def queued():
        with pytest.raises(ControlError, match="superseded"):
            await scenario.runner.run(
                scenario.contexts["superseded"],
                task="default",
                command="superseded",
                before_start=before_start,
            )

    async with asyncio.TaskGroup() as group:
        first, _ = scenario.launch(group, "first")
        await scenario.entered("first")
        superseded = group.create_task(queued())
        await checked.wait()
        current = False
        async with asyncio.timeout(1):
            await superseded
        assert scenario.recorded() == scenario.leased() == {"first"}
        assert scenario.scratch_calls == ["first"]
        next_call, requested = scenario.launch(group, "next")
        await requested.wait()
        scenario.release("first")
        await scenario.entered("next")
        scenario.release("next")
    assert first.result()["status"] == next_call.result()["status"] == "succeeded"


async def test_increased_capacity_wakes_waiter_without_a_completed_job(scenarios):
    scenario = scenarios(limit=1)
    for name in ("first", "second"):
        await scenario.context(name, name)
    async with asyncio.TaskGroup() as group:
        first, _ = scenario.launch(group, "first")
        await scenario.entered("first")
        second, requested = scenario.launch(group, "second")
        await requested.wait()
        assert scenario.started == ["first"]
        scenario.limit = 2
        async with asyncio.timeout(0.75):
            await scenario.commands["second"].entered.wait()
        assert not first.done() and len(scenario.runner.active) == 2
        scenario.release("first", "second")
    assert first.result()["status"] == second.result()["status"] == "succeeded"


async def test_reduced_capacity_drains_active_jobs_without_cancelling_them(scenarios):
    scenario = scenarios(limit=2)
    for name in ("first", "second", "third"):
        await scenario.context(name, name)
    async with asyncio.TaskGroup() as group:
        first, _ = scenario.launch(group, "first")
        second, _ = scenario.launch(group, "second")
        await scenario.entered("first")
        await scenario.entered("second")
        scenario.limit = 1
        third, requested = scenario.launch(group, "third")
        await requested.wait()
        scenario.release("first")
        await first
        # Even after a cap refresh, the remaining active job occupies the one slot.
        await asyncio.sleep(0.3)
        assert scenario.started == ["first", "second"]
        assert not second.done()
        assert scenario.leased() == {"second"}
        scenario.release("second")
        await scenario.entered("third")
        scenario.release("third")
    assert all(task.result()["status"] == "succeeded" for task in (first, second, third))


@pytest.mark.parametrize("stage", ["prepare", "execute", "commit"])
async def test_admitted_failure_releases_capacity_and_storage_for_next_waiter(scenarios, monkeypatch, stage):
    scenario = scenarios(limit=1)
    for name in ("first", "failure", "next"):
        await scenario.context(name, name)
    prepare = scenario.workspace.prepare_job
    commit = scenario.workspace.finish_job

    def prepare_job(context, *args):
        if stage == "prepare" and context.turn_id == "failure":
            raise ControlError("controlled prepare failure")
        return prepare(context, *args)

    def finish_job(workspace_id, path, cwd):
        if stage == "commit" and (Path(path) / "result.txt").read_text() == "failure":
            raise ControlError("controlled commit failure")
        return commit(workspace_id, path, cwd)

    monkeypatch.setattr(scenario.workspace, "prepare_job", prepare_job)
    monkeypatch.setattr(scenario.workspace, "finish_job", finish_job)
    scenario.commands["failure"].failure = stage == "execute"
    scenario.release("failure")

    async def failing():
        if stage == "prepare":
            with pytest.raises(ControlError, match="controlled prepare failure"):
                await scenario.runner.run(scenario.contexts["failure"], task="default", command="failure")
        else:
            result = await scenario.runner.run(
                scenario.contexts["failure"], task="default", command="failure"
            )
            assert result["status"] == "failed" and not result["workspace_committed"]
            assert f"controlled {'worker' if stage == 'execute' else stage} failure" in result["error"]

    async with asyncio.TaskGroup() as group:
        first, _ = scenario.launch(group, "first")
        await scenario.entered("first")
        failure = group.create_task(failing())
        # Start failure's admission first; it should pass its slot to the next waiter.
        await asyncio.sleep(0)
        next_call, requested = scenario.launch(group, "next")
        await requested.wait()
        scenario.release("first")
        await scenario.entered("next")
        await failure
        assert scenario.leased() == {"next"}
        scenario.release("next")
    assert first.result()["status"] == next_call.result()["status"] == "succeeded"
    assert not scenario.runner.active and not scenario.workspace._active


async def test_queue_wait_does_not_start_job_clock_or_consume_command_budget(scenarios):
    scenario = scenarios(limit=1)
    await scenario.context("ada", "first")
    await scenario.context("sun", "queued")
    async with asyncio.TaskGroup() as group:
        first, _ = scenario.launch(group, "first")
        await scenario.entered("first")
        queued, requested = scenario.launch(group, "queued", timeout_seconds=0.1)
        await requested.wait()
        await asyncio.sleep(0.2)
        assert not queued.done() and scenario.recorded() == {"first"}
        released_at = time.time()
        scenario.release("first")
        await scenario.entered("queued")
        scenario.release("queued")
    result = queued.result()
    assert first.result()["status"] == result["status"] == "succeeded"
    assert result["started_at"] >= released_at
    assert scenario.commands["queued"].config["timeout_seconds"] == 0.1
