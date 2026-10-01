import asyncio
import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from hortator.app import create_app
from hortator.models import ControlError
from hortator.reporting import event_detail, request_stats, turn_detail
from hortator.store import Store


def seed(store, request_id="request", revision=7, input_tokens=100):
    store.execute(
        "INSERT OR IGNORE INTO turns VALUES('turn','ada','channel','balanced','openrouter','fixture',?,NULL,'running',NULL,NULL,'test',1)",
        (time.time(),),
    )
    context = json.dumps(
        {"profile_revision": revision, "estimated_tokens": 50, "prompt_layers": ["x" * 100000]}
    )
    store.execute(
        "INSERT INTO requests(id,turn_id,bot_id,provider_id,profile_id,model,purpose,started_at,status,body,context,input_tokens,output_tokens,ttft_ms,duration_ms) "
        "VALUES(?,'turn','ada','openrouter','balanced','fixture','generation',?,'completed',?,?,?,10,5,20)",
        (request_id, time.time(), json.dumps({"messages": ["x" * 100000]}), context, input_tokens),
    )
    return context


def test_metadata_migrates_legacy_rows_and_tracks_context_without_altering_evidence(tmp_path):
    path = tmp_path / "council.sqlite3"
    store = Store(path)
    original = seed(store)
    store.execute("DROP TRIGGER request_metadata_insert")
    store.execute("DROP TRIGGER request_metadata_update")
    store.execute("DROP TABLE request_metadata")
    store.close()
    store = Store(path)
    try:
        assert store.one("SELECT * FROM request_metadata") == {
            "request_id": "request",
            "profile_revision": 7,
            "estimated_tokens": 50,
        }
        assert store.one("SELECT context FROM requests")["context"] == original
        updated = json.dumps({"profile_revision": 8, "estimated_tokens": 99})
        store.execute("UPDATE requests SET context=?", (updated,))
        assert store.one("SELECT * FROM request_metadata")["estimated_tokens"] == 99
        store.execute("DELETE FROM requests")
        assert not store.rows("SELECT * FROM request_metadata")
    finally:
        store.close()


async def test_statistics_and_calibration_do_not_read_context_payloads(kernel):
    seed(kernel.store)
    seed(kernel.store, "older-profile", revision=6, input_tokens=50)
    seed(kernel.store, "unknown-profile", revision=None, input_tokens=None)
    reads = []

    def authorize(action, table, column, *_):
        if action == sqlite3.SQLITE_READ:
            reads.append((table, column))
            if table == "requests" and column in {"context", "body"}:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    kernel.store.db.set_authorizer(authorize)
    try:
        result = request_stats(kernel.store)
        measured = kernel.store.one(
            "SELECT input_tokens,m.estimated_tokens FROM requests JOIN request_metadata m ON m.request_id=requests.id "
            "WHERE bot_id=? AND profile_id=? AND model=? AND m.profile_revision=? "
            "AND purpose='generation' AND input_tokens IS NOT NULL ORDER BY started_at DESC LIMIT 1",
            ("ada", "balanced", "fixture", 7),
        )
    finally:
        kernel.store.db.set_authorizer(None)
    assert measured == {"input_tokens": 100, "estimated_tokens": 50}
    assert result["total"]["requests"] == 3
    assert result["total"]["input_tokens"] == 150
    assert {r["id"]: r["input_tokens"] for r in result["by"]["configuration"]} == {
        "balanced@r0": None,
        "balanced@r6": 50,
        "balanced@r7": 100,
    }
    assert result == await kernel.reporting.run(request_stats)


async def test_summaries_omit_large_data_but_preserve_full_evidence(kernel):
    context = seed(kernel.store)
    event = kernel.store.emit("context.assembled", json.loads(context), turn_id="turn")
    small = kernel.store.emit("tool.completed", {"name": "memory"}, turn_id="turn")
    rows = await kernel.reporting.run(Store.events, summary=True)
    assert next(r for r in rows if r["seq"] == event["seq"])["data_omitted"]
    assert next(r for r in rows if r["seq"] == event["seq"])["data"] == {}
    assert next(r for r in rows if r["seq"] == small["seq"])["data"] == {"name": "memory"}
    full = await kernel.reporting.run(event_detail, event["seq"])
    assert full["data"] == json.loads(context)
    summary = await kernel.reporting.run(turn_detail, "turn", summary=True)
    assert "body" not in summary["requests"][0]
    assert "context" not in summary["requests"][0]
    detail = await kernel.reporting.run(turn_detail, "turn", include_diagnostics=True)
    assert detail["requests"][0]["context"] == json.loads(context)
    assert detail["requests"][0]["diagnostics"]["capture"] == "unavailable"
    with pytest.raises(ControlError, match="not found"):
        await kernel.reporting.run(event_detail, 999999)
    with pytest.raises(ControlError, match="not found"):
        await kernel.reporting.run(turn_detail, "absent")


async def test_reader_runs_off_loop_is_readonly_and_preserves_live_writes(kernel):
    owner = threading.get_ident()
    entered, release = threading.Event(), threading.Event()

    def read(store):
        entered.set()
        assert release.wait(5)
        assert threading.get_ident() != owner
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store.execute("DELETE FROM entities")
        return store.one("SELECT count(*) AS n FROM events WHERE kind='test.during_read'")["n"]

    async with asyncio.TaskGroup() as group:
        task = group.create_task(kernel.reporting.run(read))
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        kernel.store.emit("test.during_read")
        release.set()
        assert await task == 1


async def test_cancel_and_close_join_actual_worker_and_bound_admission(kernel):
    entered, release = threading.Event(), threading.Event()

    def held(store):
        entered.set()
        assert release.wait(5)
        return store.one("SELECT 1 AS n")

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(kernel.reporting.run(held)) for _ in range(4)]
        async with asyncio.timeout(3):
            while not entered.is_set() or kernel.reporting.pending != 4:
                await asyncio.sleep(0.001)
        with pytest.raises(ControlError, match="busy"):
            await kernel.reporting.run(held)
        tasks[0].cancel()
        await asyncio.sleep(0.01)
        tasks[0].cancel()
        await asyncio.sleep(0.01)
        assert not tasks[0].done() and kernel.reporting.pending == 4
        close = group.create_task(kernel.reporting.close())
        await asyncio.sleep(0.01)
        assert not close.done()
        with pytest.raises(ControlError, match="stopping"):
            await kernel.reporting.run(held)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
    assert kernel.reporting.pending == 0 and close.done()


async def test_cancel_interrupts_long_sql_and_reader_can_be_reused(kernel):
    entered = threading.Event()

    def long_query(store):
        entered.set()
        return store.one(
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) SELECT sum(x) FROM n"
        )

    async with asyncio.TaskGroup() as group:
        task = group.create_task(kernel.reporting.run(long_query))
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    assert await kernel.reporting.run(lambda s: s.one("SELECT 1 AS n")) == {"n": 1}


def test_reporting_api_auth_summary_detail_and_export(tmp_path, monkeypatch):
    monkeypatch.setenv("HORTATOR_ADMIN_PASSWORD", "test-only-reporting-password")
    app = create_app(tmp_path, start_runtime=False)
    with TestClient(app) as client:
        assert client.get("/api/events/1").status_code == 401
        assert client.get("/api/stats").status_code == 401
        client.post("/api/auth/login", json={"password": "test-only-reporting-password"})

        def setup():
            k = app.state.kernel
            original = seed(k.store)
            return k.store.emit("context.assembled", json.loads(original), turn_id="turn")["seq"]

        seq = client.portal.call(setup)
        assert client.get("/api/stats").json()["total"]["requests"] == 1
        brief = client.get("/api/trajectory/turn?summary=true").json()
        assert "body" not in brief["requests"][0]
        full = client.get("/api/trajectory/turn/export").json()
        assert full["requests"][0]["body"]["messages"] == ["x" * 100000]
        assert client.get(f"/api/events/{seq}").json()["data"]["profile_revision"] == 7
        rows = client.get("/api/events?summary=true").json()
        assert next(r for r in rows if r["seq"] == seq)["data_omitted"]
        assert client.get("/api/events/999999").status_code == 404


async def test_reader_transaction_keeps_one_snapshot_while_writer_progresses(kernel):
    entered, release = threading.Event(), threading.Event()

    def read(store):
        before = store.one("SELECT count(*) AS n FROM events")["n"]
        entered.set()
        assert release.wait(5)
        after = store.one("SELECT count(*) AS n FROM events")["n"]
        return before, after

    async with asyncio.TaskGroup() as group:
        task = group.create_task(kernel.reporting.run(read))
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        kernel.store.emit("test.concurrent_writer")
        release.set()
        before, after = await task
    assert before == after
    assert kernel.store.one("SELECT count(*) AS n FROM events")["n"] == after + 1


async def test_summary_tracks_live_private_diagnostic_updates_without_loading_body(kernel):
    seed(kernel.store)
    before = await kernel.reporting.run(turn_detail, "turn", summary=True)
    assert before["requests"][0]["diagnostics_updated_at"] is None
    kernel.store.execute(
        "INSERT INTO request_diagnostics VALUES(?,?,?)",
        ("request", json.dumps({"reasoning_content": "private progress"}), 100),
    )
    first = await kernel.reporting.run(turn_detail, "turn", summary=True)
    kernel.store.execute("UPDATE request_diagnostics SET updated_at=101")
    second = await kernel.reporting.run(turn_detail, "turn", summary=True)
    assert first != second and "private progress" not in json.dumps(second)
    assert second["requests"][0]["diagnostics_updated_at"] == 101


async def test_sse_closes_cleanly_when_reporting_admission_is_full(kernel, monkeypatch, tmp_path):
    app = create_app(tmp_path, start_runtime=False)
    endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", None) == "/api/events/stream")

    class Request:
        headers = {}
        cookies = {}

        async def is_disconnected(self):
            return False

    async def busy(*args, **kwargs):
        raise ControlError("Reporting reads are busy", 503)

    monkeypatch.setattr(kernel.auth, "session", lambda token: {})
    monkeypatch.setattr(kernel.reporting, "run", busy)
    response = await endpoint(Request(), after=3, summary=True, actor=None, k=kernel)
    assert "connected" in await anext(response.body_iterator)
    with pytest.raises(StopAsyncIteration):
        await anext(response.body_iterator)
