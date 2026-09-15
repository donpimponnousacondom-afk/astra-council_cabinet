"""Single-owner kernel replacement with drained HTTP access and offline snapshots."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager

from .models import ControlError
from .concurrency import task_group, error_text
from .snapshots import PAUSE_FILE, Snapshots, schema
from .version import iso_now


class SnapshotGate:
    """Ordinary handlers may overlap; maintenance rejects new work and drains existing handlers."""

    def __init__(self):
        self.condition = asyncio.Condition()
        self.active = 0
        self.maintenance = False

    @asynccontextmanager
    async def enter(self, exclusive=False):
        async with self.condition:
            if self.maintenance:
                raise ControlError(
                    "Snapshot maintenance is in progress; retry after the operation completes", 503
                )
            if exclusive:
                self.maintenance = True
                try:
                    await self.condition.wait_for(lambda: self.active == 0)
                except BaseException:
                    self.maintenance = False
                    self.condition.notify_all()
                    raise
            else:
                self.active += 1
        try:
            yield
        finally:
            async with self.condition:
                if exclusive:
                    self.maintenance = False
                else:
                    self.active -= 1
                self.condition.notify_all()


async def blocking(function, *args, **kwargs):
    """Finish an offline filesystem operation before owner cancellation can release its lock."""
    # TaskGroup owns the worker; explicitly wait on cancellation because cancelling
    # asyncio.to_thread cannot stop the underlying filesystem writes.
    async with task_group() as group:
        task = group.create_task(asyncio.to_thread(function, *args, **kwargs))
        cancelled = None
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError as error:
                # Repeated Ctrl-C/cancellation must not cancel the to_thread
                # handle: the actual worker still owns an offline transaction.
                cancelled = error
                if task.cancelled():
                    raise
        if cancelled is not None:
            raise cancelled
        return result


class SnapshotRuntime:
    def __init__(self, app, directory, kernel_factory, *, start_runtime, console=None):
        self.app, self.directory, self.factory = app, directory, kernel_factory
        self.start_runtime, self.console = start_runtime, console
        self.queue = asyncio.Queue()
        self.ready = asyncio.Event()
        self.busy = False
        self.operation = None
        self.build = None
        self.snapshots = None
        self.database_schema = None
        self.paused = (directory / PAUSE_FILE).exists()
        self.last_restore = None
        if self.paused:
            try:
                self.last_restore = json.loads((directory / PAUSE_FILE).read_text())
            except OSError, ValueError:
                self.last_restore = {"note": "Runtime pause marker requires operator inspection"}

    async def run(self):
        command = result = error = None
        try:
            # A killed process may have left a partially swapped store. Roll it
            # back under the existing runtime lock before constructing SQLite.
            recovery = Snapshots(self.directory, {})
            if recovery.journal.exists():
                await blocking(recovery.recover_pending)
                self.paused = True
                self.last_restore = json.loads((self.directory / PAUSE_FILE).read_text())
            while True:
                if self.paused and (
                    not (self.directory / "council.sqlite3").is_file()
                    or not (os.getenv("HORTATOR_MASTER_KEY") or (self.directory / "master.key").is_file())
                ):
                    raise ControlError(
                        "Restored state is incomplete; refusing to initialize a new database or key. Recover the matching snapshot before startup."
                    )
                kernel = self.factory(self.directory)
                if self.build is None:
                    self.build = kernel.service.version()
                    self.snapshots = Snapshots(self.directory, self.build)
                else:
                    kernel.service._version = {**self.build, "started_at": iso_now()}
                self.database_schema = schema(kernel.store.db)
                kernel.service.snapshot_maintenance = False
                self.app.state.kernel = kernel
                async with kernel.lifetime():
                    if self.console:
                        self.console.bind(kernel)
                    if self.start_runtime and not self.paused:
                        kernel.start()
                    self.busy, self.operation = False, None
                    self.ready.set()
                    if command:
                        if error:
                            kernel.store.emit(
                                "snapshot.failed",
                                {
                                    "operation": command[0],
                                    "error": error_text(error),
                                    "paused": self.paused,
                                    "consequence": "Operation failed; inspect recovery state before retrying",
                                },
                                level="error",
                            )
                        else:
                            kind = {
                                "capture": "snapshot.created",
                                "restore": "snapshot.restored",
                                "resume": "snapshot.resumed",
                            }[command[0]]
                            kernel.store.emit(
                                kind,
                                {
                                    "snapshot_id": result.get("snapshot_id")
                                    or result.get("snapshot", {}).get("id"),
                                    "recovery_snapshot_id": result.get("recovery_snapshot_id"),
                                    "scope": result.get("scope"),
                                    "bot_id": result.get("bot_id"),
                                    "channel_id": result.get("channel_id"),
                                    "paused": self.paused,
                                },
                            )
                        future = command[2]
                        if not future.done():
                            if error:
                                future.set_exception(error)
                            else:
                                future.set_result(result)
                    command = await self.queue.get()
                    self.busy, self.operation = True, command[0]
                    kernel.service.snapshot_maintenance = True
                    kernel.store.emit(
                        "snapshot.started",
                        {
                            "operation": command[0],
                            "active_bots": sorted(kernel.engine.tasks),
                            "note": "Stopping all runtime services and cancelling active work before offline state capture; active generations are not resumed halfway",
                        },
                    )
                    if self.console:
                        self.console.stop()
                    # A kernel closes ALL services, bot turns, tool jobs and gateway
                    # children before its lifetime owner closes clients and SQLite.
                try:
                    result = await blocking(self.execute, command[0], command[1])
                    error = None
                except Exception as exc:
                    result, error = None, exc
                self.paused = (self.directory / PAUSE_FILE).exists()
                if self.paused:
                    self.last_restore = json.loads((self.directory / PAUSE_FILE).read_text())
        finally:
            if command and not command[2].done():
                command[2].set_exception(
                    ControlError(
                        "Server stopped during snapshot operation; inspect the snapshot catalog before retrying",
                        503,
                    )
                )
            self.app.state.kernel = None

    def execute(self, action, data):
        if action == "capture":
            manifest = self.snapshots.capture(data["name"], data.get("note", ""))
            return {"snapshot": self.snapshots.public(manifest, self.database_schema), "paused": self.paused}
        if action == "restore":
            self.last_restore = self.snapshots.restore(data.pop("identifier"), self.database_schema, **data)
            # Session tokens must never rewind into a different authenticated state.
            if self.last_restore["scope"] == "full":
                import sqlite3

                with sqlite3.connect(self.directory / "council.sqlite3") as db:
                    db.execute("DELETE FROM auth_sessions")
            return self.last_restore
        if action == "resume":
            (self.directory / PAUSE_FILE).unlink(missing_ok=True)
            return {"paused": False}
        raise ControlError("Unknown snapshot operation")

    async def request(self, action, data):
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((action, dict(data), future))
        # Disconnecting the initiating browser must not abandon halfway through
        # a restore; its operation remains owned by the application lifecycle.
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:

            def observe(done):
                if not done.cancelled():
                    done.exception()

            future.add_done_callback(observe)
            raise

    async def catalog(self):
        return await blocking(self.snapshots.catalog, self.database_schema) | {
            "paused": self.paused,
            "busy": self.busy,
            "operation": self.operation,
            "last_restore": self.last_restore,
        }
