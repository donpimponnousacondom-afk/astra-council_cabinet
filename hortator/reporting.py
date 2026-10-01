"""Detached reporting queries and a bounded, cancellation-joined read worker."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading
import time

from .concurrency import task_group
from .diagnostics import read_diagnostics
from .models import ControlError
from .store import Store, event_columns


def request_stats(store, hours=24):
    since = time.time() - hours * 3600
    fields = """count(*) AS requests, sum(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failures,
      sum(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancellations,
      sum(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
      sum(input_tokens) AS input_tokens,sum(output_tokens) AS output_tokens,
      sum(reasoning_tokens) AS reasoning_tokens,sum(cached_tokens) AS cached_tokens,sum(cost) AS cost,
      sum(CASE WHEN cost IS NOT NULL THEN 1 ELSE 0 END) AS cost_known_requests,
      sum(CASE WHEN cost_source='estimated' THEN 1 ELSE 0 END) AS estimated_cost_requests,
      sum(CASE WHEN input_tokens IS NOT NULL THEN 1 ELSE 0 END) AS usage_known_requests,
      avg(ttft_ms) AS avg_ttft_ms,avg(duration_ms) AS avg_duration_ms"""
    total = store.one(f"SELECT {fields} FROM requests WHERE started_at>=?", (since,))
    grouped = {}
    for group in ("bot_id", "provider_id", "profile_id", "model", "purpose"):
        grouped[group] = store.rows(
            f"SELECT {group} AS id,{fields} FROM requests WHERE started_at>=? GROUP BY {group}", (since,)
        )
    grouped["configuration"] = store.rows(
        f"SELECT profile_id || '@r' || coalesce(m.profile_revision,0) AS id,{fields} FROM requests LEFT JOIN request_metadata m ON m.request_id=requests.id WHERE started_at>=? GROUP BY profile_id,coalesce(m.profile_revision,0)",
        (since,),
    )
    latency = store.rows(
        "SELECT ttft_ms,duration_ms FROM requests WHERE started_at>=? AND status='completed' ORDER BY started_at DESC LIMIT 10000",
        (since,),
    )
    for key in ("ttft_ms", "duration_ms"):
        vals = sorted(r[key] for r in latency if r[key] is not None)
        total["p95_" + key] = vals[min(len(vals) - 1, int(len(vals) * 0.95))] if vals else None
    total["latency_sample_size"] = len(latency)
    history = store.rows(
        f"SELECT CAST(started_at/3600 AS INTEGER)*3600 AS at,{fields} FROM requests WHERE started_at>=? GROUP BY CAST(started_at/3600 AS INTEGER) ORDER BY at",
        (since,),
    )
    turns = store.rows(
        "SELECT status,count(*) AS count FROM turns WHERE started_at>=? GROUP BY status", (since,)
    )
    tools = store.rows(
        "SELECT json_extract(data,'$.name') AS name,kind,count(*) AS count,avg(json_extract(data,'$.duration_ms')) AS avg_duration_ms FROM events WHERE at>=? AND kind IN ('tool.completed','tool.failed') GROUP BY name,kind",
        (since,),
    )
    return {
        "hours": hours,
        "total": total,
        "by": grouped,
        "history": history,
        "turns": turns,
        "tools": tools,
    }


def turn_detail(store, turn_id, *, include_diagnostics=False, summary=False):
    turn = store.one("SELECT * FROM turns WHERE id=?", (turn_id,))
    if not turn:
        raise ControlError("Turn not found", 404)
    columns = (
        "id,turn_id,bot_id,provider_id,profile_id,model,purpose,started_at,ended_at,status,ttft_ms,duration_ms,input_tokens,output_tokens,cost,error,"
        "(SELECT updated_at FROM request_diagnostics d WHERE d.request_id=requests.id) AS diagnostics_updated_at"
        if summary
        else "*"
    )
    requests = store.rows(f"SELECT {columns} FROM requests WHERE turn_id=? ORDER BY started_at", (turn_id,))
    for request in [] if summary else requests:
        for key in ("body", "context", "usage", "response"):
            request[key] = json.loads(request[key]) if request[key] else None
        if include_diagnostics:
            request["diagnostics"] = read_diagnostics(store, request["id"])
    outbox = store.rows("SELECT * FROM outbox WHERE turn_id=? ORDER BY created_at", (turn_id,))
    # Detail includes the full turn. Global history remains cursor-paginated.
    events = store.rows(
        f"SELECT {event_columns(summary)} FROM events WHERE turn_id=? ORDER BY seq", (turn_id,)
    )
    for event in events:
        event["data"] = json.loads(event["data"])
    result = {"turn": turn, "requests": requests, "events": events, "outbox": outbox}
    if summary:
        result["summary"] = True
    return result


def event_detail(store, seq):
    rows = store.events(after=seq - 1, before=seq + 1, limit=1)
    if not rows:
        raise ControlError("Event not found", 404)
    return rows[0]


class ReportingReader:
    """One worker, at most four admitted reads, no shared SQLite/vault objects.

    Connections are opened and closed by their worker. A cancelled owner interrupts
    its SQL and joins the actual worker before returning, including during snapshot
    maintenance. Callbacks accept only detached arguments and this read-only store.
    """

    def __init__(self, path):
        self.path = path
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="council-reporting")
        self.pending = 0
        self.closed = False
        self.idle = asyncio.Event()
        self.idle.set()

    def _read(self, cancel, function, args, kwargs):
        if cancel.is_set():
            return None
        reader = Store.__new__(Store)
        reader.path = self.path
        reader.db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
        reader.db.row_factory = sqlite3.Row
        try:
            reader.db.set_progress_handler(lambda: int(cancel.is_set()), 1000)
            reader.db.execute("BEGIN")
            return function(reader, *args, **kwargs)
        except sqlite3.OperationalError:
            if cancel.is_set():
                return None
            raise
        finally:
            reader.db.close()

    async def run(self, function, *args, **kwargs):
        if self.closed:
            raise ControlError("Reporting reader is stopping; retry after maintenance", 503)
        if self.pending >= 4:
            raise ControlError("Reporting reads are busy; retry shortly", 503)
        self.pending += 1
        self.idle.clear()
        cancel = threading.Event()
        try:
            future = asyncio.get_running_loop().run_in_executor(
                self.executor, self._read, cancel, function, args, kwargs
            )

            async def join():
                # Return a worker error to the owner after joining. Letting the
                # child and its awaiting parent both raise duplicates the same
                # domain error in TaskGroup and would turn a 404 into a 500.
                try:
                    return await future, None
                except Exception as error:
                    return None, error

            async with task_group() as group:
                task = group.create_task(join(), name="reporting-read")
                cancelled = None
                while True:
                    try:
                        result, error = await asyncio.shield(task)
                        break
                    except asyncio.CancelledError as error:
                        cancelled = error
                        cancel.set()
                        if task.cancelled():
                            raise
                if cancelled is not None:
                    raise cancelled
                if error is not None:
                    raise error
                return result
        finally:
            self.pending -= 1
            if not self.pending:
                self.idle.set()

    async def close(self):
        self.closed = True
        cancelled = None
        while not self.idle.is_set():
            try:
                await self.idle.wait()
            except asyncio.CancelledError as error:
                cancelled = error
        # Every submitted call has joined before reaching shutdown.
        self.executor.shutdown(wait=True)
        if cancelled is not None:
            raise cancelled
