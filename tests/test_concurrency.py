import asyncio
import ast
import inspect
from pathlib import Path

import pytest

from conftest import configured, ingest
from hortator.concurrency import TaskFailure, cancel_and_wait, error_text, task_group


async def test_related_failure_cancels_and_joins_sibling_before_propagation():
    ready, cleaned = asyncio.Event(), asyncio.Event()

    async def sibling():
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    async def broken():
        await ready.wait()
        raise ValueError("collector failed")

    with pytest.raises(ValueError, match="collector failed"):
        async with task_group() as group:
            task = group.create_task(sibling())
            group.create_task(broken())
    assert cleaned.is_set() and task.done()


async def test_multiple_child_failures_are_all_preserved():
    async def broken(message):
        raise ValueError(message)

    with pytest.raises(ExceptionGroup) as caught:
        async with task_group() as group:
            group.create_task(broken("stdout failed"))
            group.create_task(broken("stderr failed"))
    assert "stdout failed" in error_text(caught.value)
    assert "stderr failed" in error_text(caught.value)


async def test_background_failure_is_visible_redacted_and_does_not_cancel_other_bots(kernel):
    kernel.vault.put("plugin/test/api_key", "task-test-secret")
    running = asyncio.Event()

    async def sibling():
        running.set()
        await asyncio.Event().wait()

    async def broken():
        await running.wait()
        raise RuntimeError("unexpected task-test-secret")

    other = kernel.background.spawn(sibling(), name="bot:socrates:test", bot_id="socrates")
    failed = kernel.background.spawn(broken(), name="bot:ada:test", bot_id="ada")
    outcome = await failed
    assert isinstance(outcome, TaskFailure) and not other.done()
    event = next(e for e in kernel.store.events() if e["kind"] == "runtime.task_failed")
    assert event["bot_id"] == "ada" and "RuntimeError" in event["data"]["traceback"]
    assert "task-test-secret" not in str(event) and "task-test-secret" not in outcome.error
    assert "bot:ada:test" in kernel.service.status()["background_tasks"]["failed"]
    await cancel_and_wait(other)


async def test_unexpected_turn_wrapper_failure_does_not_leave_running_turn(kernel, monkeypatch):
    bot = configured(kernel)
    ingest(kernel)

    async def broken(*args):
        raise RuntimeError("outside ordinary turn handler")

    monkeypatch.setattr(kernel.engine, "run", broken)
    turn = kernel.engine.launch(bot, "222222222222222222")
    outcome = await kernel.engine.tasks[bot["id"]]
    assert isinstance(outcome, TaskFailure)
    assert kernel.store.one("SELECT status FROM turns WHERE id=?", (turn,))["status"] == "failed"


async def test_scope_closes_cancelled_before_start_coroutine_and_rejects_late_spawn(kernel):
    async def never():
        await asyncio.Event().wait()

    coroutine = never()
    task = kernel.background.spawn(coroutine, name="never-started")
    await cancel_and_wait(task)
    await asyncio.sleep(0)
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
    from hortator.concurrency import BackgroundTasks

    inactive = BackgroundTasks(kernel.store)
    late = never()
    with pytest.raises(RuntimeError, match="lifetime"):
        inactive.spawn(late, name="late")
    assert inspect.getcoroutinestate(late) == inspect.CORO_CLOSED


async def test_kernel_lifetime_joins_task_finalizers_before_closing_sqlite(tmp_path):
    from hortator.app import Kernel

    kernel = Kernel(tmp_path)
    started = asyncio.Event()

    async def work():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            kernel.store.emit("test.finalizer")

    async with kernel.lifetime():
        task = kernel.background.spawn(work(), name="finalizer-test")
        await started.wait()
    assert task.done() and kernel.closed and not kernel.background.pending


async def test_shutdown_attempts_every_component_and_joins_after_cleanup_failure(tmp_path, monkeypatch):
    from hortator.app import Kernel

    kernel = Kernel(tmp_path)
    closed, finalized = [], asyncio.Event()
    ready = asyncio.Event()

    def closer(name, fail=False):
        async def close():
            closed.append(name)
            if fail:
                raise RuntimeError(f"{name} cleanup failed")

        return close

    async def pending():
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            kernel.store.emit("test.cleanup_after_failure")
            finalized.set()

    for name, component in (
        ("publishing", kernel.publishing),
        ("engine", kernel.engine),
        ("agentic", kernel.registry.agentic),
        ("discord", kernel.connector),
    ):
        monkeypatch.setattr(component, "close", closer(name, name == "publishing"))
    with pytest.raises(ExceptionGroup, match="unhandled errors") as caught:
        async with kernel.lifetime():
            kernel.background.spawn(pending(), name="pending-on-close")
            await ready.wait()
    assert "publishing cleanup failed" in error_text(caught.value)
    assert closed == ["publishing", "engine", "agentic", "discord"]
    assert finalized.is_set() and kernel.closed and not kernel.background.pending


async def test_owner_cancellation_propagates_after_related_children_cleanup():
    ready, finished = asyncio.Event(), asyncio.Event()

    async def child():
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            finished.set()

    async def operation():
        async with task_group() as group:
            group.create_task(child())
            await asyncio.Event().wait()

    async with asyncio.TaskGroup() as owner:
        task = owner.create_task(operation())
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set()


def test_application_has_no_unowned_task_creation_or_discarded_gather_errors():
    for path in (Path(__file__).resolve().parents[1] / "hortator").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            func = node.func
            if isinstance(func.value, ast.Name) and func.value.id == "asyncio":
                assert func.attr not in {"create_task", "ensure_future"}, path.name
                assert not (
                    func.attr == "gather"
                    and any(
                        kw.arg == "return_exceptions"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value
                        for kw in node.keywords
                    )
                ), path.name
