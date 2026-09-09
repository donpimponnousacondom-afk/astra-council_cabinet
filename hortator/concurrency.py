"""Structured task ownership, explicit failure isolation, and complete joins."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import time
import traceback


def error_text(error):
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(error_text(child) for child in error.exceptions)
    message = str(error)
    if not message:
        message = (
            "Operation timed out; no additional timeout detail supplied"
            if isinstance(error, TimeoutError)
            else "Operation cancelled by its owner"
            if isinstance(error, asyncio.CancelledError)
            else "No exception message supplied"
        )
    return f"{type(error).__name__}: {message}"


@asynccontextmanager
async def task_group():
    """Native TaskGroup semantics; preserve a sole domain error's public type.

    Multiple failures remain a group. Never throw away secondary exceptions.
    """
    try:
        async with asyncio.TaskGroup() as group:
            yield group
    except BaseExceptionGroup as error:
        while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
            error = error.exceptions[0]
        raise error


async def cancel_and_wait(*tasks):
    """Cancel every owned child, join all, and propagate any non-cancellation errors."""
    tasks = [task for task in tasks if task is not None and task is not asyncio.current_task()]
    for task in tasks:
        if not task.done():
            task.cancel()
    await join_tasks(*tasks)


async def join_tasks(*tasks):
    """Join existing owned tasks without cancelling a caller's in-flight operation."""
    tasks = [task for task in tasks if task is not None and task is not asyncio.current_task()]
    cancelling_at_entry = asyncio.current_task().cancelling()
    errors = []
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            # This function deliberately joins cancelled children. Cancellation
            # of the owner itself must still propagate after cleanup.
            if asyncio.current_task().cancelling() > cancelling_at_entry:
                raise
        except Exception as error:
            errors.append(error)
    if errors:
        raise ExceptionGroup("Errors while joining owned tasks", errors)


class BackgroundTasks:
    """One application-lifetime TaskGroup with an explicit service isolation boundary.

    Independent bot/services report unexpected exceptions immediately rather
    than cancelling unrelated clients. Related I/O uses task_group() directly.
    """

    def __init__(self, store):
        self.store = store
        self.group = None
        self.closing = False
        self.pending = set()
        self.failures = {}

    @asynccontextmanager
    async def lifetime(self):
        if self.group is not None:
            raise RuntimeError("Background task lifetime is already open")
        async with asyncio.TaskGroup() as group:
            self.group = group
            self.closing = False
            try:
                yield self
            finally:
                self.closing = True
                try:
                    await cancel_and_wait(*self.pending)
                finally:
                    self.group = None

    def spawn(self, coroutine, *, name, bot_id=None, turn_id=None):
        if self.group is None or self.closing:
            coroutine.close()
            raise RuntimeError("Open Kernel.lifetime() before scheduling background work")

        async def supervised():
            try:
                return await coroutine
            except asyncio.CancelledError:
                raise
            except Exception as error:
                detail = self.store.redact(error_text(error))[:4000]
                self.failures[name] = detail
                if len(self.failures) > 50:
                    self.failures.pop(next(iter(self.failures)))
                self.store.emit(
                    "runtime.task_failed",
                    {
                        "task": name,
                        "error": detail,
                        "reason": "Background task stopped; unrelated tasks are not cancelled",
                        "traceback": self.store.redact("".join(traceback.format_exception(error)))[-12000:],
                    },
                    bot_id=bot_id,
                    turn_id=turn_id,
                    level="error",
                )
                if turn_id and bot_id:
                    self.store.execute(
                        "UPDATE turns SET status='failed',error=?,ended_at=? WHERE id=? AND bot_id=? AND status='running'",
                        (detail, time.time(), turn_id, bot_id),
                    )
                return TaskFailure(name, detail)

        try:
            task = self.group.create_task(supervised(), name=name)
        except BaseException:
            coroutine.close()
            raise
        self.pending.add(task)

        def finished(completed):
            self.pending.discard(completed)
            # If cancelled before supervised() begins, its input coroutine
            # still needs closing (no abandoned "never awaited" coroutine).
            coroutine.close()

        task.add_done_callback(finished)
        return task

    def status(self):
        return {
            "running": sorted(t.get_name() for t in self.pending if not t.done()),
            "failed": dict(self.failures),
        }


@dataclass(frozen=True)
class TaskFailure:
    task: str
    error: str
