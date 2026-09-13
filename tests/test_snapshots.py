import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from hortator.app import Kernel, create_app
from hortator.models import ControlError
from hortator.snapshots import PAUSE_FILE, Snapshots, schema

BUILD = {
    "commit": "a" * 40,
    "short_commit": "a" * 12,
    "dirty": False,
    "package_version": "0.1.0",
    "commit_title": "Snapshot test",
    "started_at": "2026-09-13T00:00:00Z",
}


@pytest.fixture
def snapshot_data(tmp_path):
    directory = tmp_path / "live"
    k = Kernel(directory)
    k.vault.put("provider/test/key", "test-only-private-value")
    k.store.context("ada", "channel-a")
    k.store.context("ada", "channel-b")
    k.store.context("socrates", "channel-a")
    for bot, channel, value in (
        ("ada", "channel-a", "old A"),
        ("ada", "channel-b", "old B"),
        ("socrates", "channel-a", "Socrates notes"),
    ):
        k.store.execute("INSERT INTO memories VALUES (?,?,?,?,?)", (bot, channel, "note", value, 1))
    k.store.execute(
        "CREATE TABLE IF NOT EXISTS global_memories (bot_id TEXT NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,source_channel_id TEXT,updated_at REAL NOT NULL,PRIMARY KEY(bot_id,key))"
    )
    k.store.execute("INSERT INTO global_memories VALUES ('ada','global','old global','channel-a',1)")
    fingerprint = schema(k.store.db)
    k.store.close()
    (directory / "images").mkdir(exist_ok=True)
    (directory / "images" / "test-image").write_bytes(b"test image bytes")
    manager = Snapshots(directory, BUILD)
    return manager, directory, fingerprint


def test_complete_snapshot_validated_immutable_and_incompatible(snapshot_data):
    manager, directory, fingerprint = snapshot_data
    manifest = manager.capture("Experiment baseline", "All current state")
    assert manager.verify(manifest["id"], fingerprint)["name"] == "Experiment baseline"
    assert manifest["files"]["images/test-image"]["bytes"] == 16
    assert (manager.path(manifest["id"]) / "data/master.key").stat().st_mode & 0o777 == 0o400
    assert manager.catalog(fingerprint)["snapshots"][0]["compatible"]
    other = Snapshots(directory, {**BUILD, "commit": "b" * 40})
    with pytest.raises(ControlError, match="exact source commit"):
        other.verify(manifest["id"], fingerprint)
    with pytest.raises(ControlError, match="Database schema differs"):
        manager.verify(manifest["id"], "different")
    assert not Snapshots(directory, {**BUILD, "dirty": True}).catalog(fingerprint)["snapshots"][0][
        "compatible"
    ]


def test_corruption_and_mismatching_key_are_refused(snapshot_data):
    manager, _, fingerprint = snapshot_data
    manifest = manager.capture("Valid")
    payload = manager.path(manifest["id"]) / "data"
    (payload / "images/test-image").chmod(0o600)
    (payload / "images/test-image").write_bytes(b"changed")
    with pytest.raises(ControlError, match="integrity check"):
        manager.verify(manifest["id"], fingerprint)
    from cryptography.fernet import Fernet

    (payload / "master.key").chmod(0o600)
    (payload / "master.key").write_bytes(Fernet.generate_key())
    with pytest.raises(ControlError, match="matching encryption key"):
        manager.validate_database(payload)


def test_snapshot_refuses_symlinks_and_path_escape(snapshot_data, tmp_path):
    manager, directory, fingerprint = snapshot_data
    (directory / "images" / "link").symlink_to(tmp_path)
    with pytest.raises(ControlError, match="symlink"):
        manager.capture("Invalid")
    assert not list(manager.root.glob(".creating-*"))
    with pytest.raises(ControlError, match="identifier"):
        manager.verify("../../outside", fingerprint)


def test_bot_notes_only_and_explicit_global_restore(snapshot_data):
    manager, directory, fingerprint = snapshot_data
    manifest = manager.capture("Baseline")
    with sqlite3.connect(directory / "council.sqlite3") as db:
        db.execute("UPDATE memories SET value='new' WHERE bot_id='ada'")
        db.execute("UPDATE contexts SET summary='new summary' WHERE bot_id='ada'")
        db.execute("UPDATE global_memories SET value='new global'")
    receipt = manager.restore(manifest["id"], fingerprint, scope="bot", bot_id="ada", channel_id="channel-a")
    assert receipt["paused"] and (directory / PAUSE_FILE).exists()
    recovery = receipt["recovery_snapshot_id"]
    manager.verify(recovery, fingerprint)
    with sqlite3.connect(directory / "council.sqlite3") as db:
        assert (
            db.execute("SELECT value FROM memories WHERE bot_id='ada' AND channel_id='channel-a'").fetchone()[
                0
            ]
            == "old A"
        )
        assert (
            db.execute("SELECT value FROM memories WHERE bot_id='ada' AND channel_id='channel-b'").fetchone()[
                0
            ]
            == "new"
        )
        assert (
            db.execute("SELECT value FROM memories WHERE bot_id='socrates'").fetchone()[0] == "Socrates notes"
        )
        assert db.execute("SELECT value FROM global_memories").fetchone()[0] == "new global"
        assert (
            db.execute("SELECT summary FROM contexts WHERE bot_id='ada' LIMIT 1").fetchone()[0]
            == "new summary"
        )
    manager.restore(
        manifest["id"],
        fingerprint,
        scope="bot",
        bot_id="ada",
        include_context=True,
        include_global_memory=True,
    )
    with sqlite3.connect(directory / "council.sqlite3") as db:
        assert db.execute("SELECT value FROM global_memories").fetchone()[0] == "old global"
        assert db.execute("SELECT summary FROM contexts WHERE bot_id='ada' LIMIT 1").fetchone()[0] == ""


def test_full_restore_preserves_recovery_and_lock(snapshot_data):
    manager, directory, fingerprint = snapshot_data
    lock = directory / "runtime.lock"
    lock.touch()
    inode = lock.stat().st_ino
    manifest = manager.capture("Baseline")
    with sqlite3.connect(directory / "council.sqlite3") as db:
        db.execute("UPDATE memories SET value='experiment'")
    (directory / "images/test-image").write_bytes(b"experiment pixels")
    (directory / "images/extra").write_bytes(b"extra")
    receipt = manager.restore(manifest["id"], fingerprint)
    assert lock.stat().st_ino == inode
    assert (directory / "images/test-image").read_bytes() == b"test image bytes"
    assert not (directory / "images/extra").exists()
    recovery = manager.path(receipt["recovery_snapshot_id"])
    assert (recovery / "data/images/extra").read_bytes() == b"extra"
    with sqlite3.connect(directory / "council.sqlite3") as db:
        assert (
            db.execute("SELECT value FROM memories WHERE bot_id='ada' AND channel_id='channel-a'").fetchone()[
                0
            ]
            == "old A"
        )


def test_snapshot_api_auth_lifecycle_pause_and_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("HORTATOR_ADMIN_PASSWORD", "test-password-only")
    monkeypatch.setattr("hortator.service.runtime_version", lambda: dict(BUILD))
    app = create_app(tmp_path / "live", start_runtime=False)
    with TestClient(app) as client:
        assert client.get("/api/snapshots").status_code == 401
        csrf = client.post("/api/auth/login", json={"password": "test-password-only"}).json()["csrf"]
        headers = {"x-csrf-token": csrf}
        before = app.state.kernel
        response = client.post("/api/snapshots", headers=headers, json={"name": "From dashboard"})
        assert response.status_code == 200, response.text
        identifier = response.json()["snapshot"]["id"]
        assert before.closed and app.state.kernel is not before
        assert client.get("/api/snapshots").json()["snapshots"][0]["compatible"]
        response = client.post(
            f"/api/snapshots/{identifier}/restore",
            headers=headers,
            json={"scope": "full", "confirmation": identifier},
        )
        assert response.status_code == 200, response.text
        assert response.json()["paused"]
        assert client.get("/api/status").status_code == 401
        csrf = client.post("/api/auth/login", json={"password": "test-password-only"}).json()["csrf"]
        headers = {"x-csrf-token": csrf}
        assert client.get("/api/status").json()["maintenance_pause"]
        assert client.post("/api/control", headers=headers, json={"action": "pause"}).status_code == 409
        assert client.post("/api/snapshots/resume", headers=headers).json() == {"paused": False}
        assert not client.get("/api/status").json()["maintenance_pause"]
        assert not (tmp_path / "live" / PAUSE_FILE).exists()


def test_pause_marker_survives_process_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HORTATOR_ADMIN_PASSWORD", "test-password-only")
    directory = tmp_path / "live"
    seed = Kernel(directory)
    seed.store.close()
    (directory / PAUSE_FILE).write_text(json.dumps({"note": "Restored"}))
    called = []
    monkeypatch.setattr(Kernel, "start", lambda self: called.append(True))
    with TestClient(create_app(directory, start_runtime=True)) as client:
        assert client.get("/api/health").status_code == 200
        assert not called


def test_restore_rejects_notes_above_current_bot_budget_without_mutation(snapshot_data):
    manager, directory, fingerprint = snapshot_data
    manifest = manager.capture("Original notes")
    with sqlite3.connect(directory / "council.sqlite3") as db:
        body = json.loads(
            db.execute("SELECT body FROM entities WHERE kind='bots' AND id='ada'").fetchone()[0]
        )
        body["memory_char_limit"] = 1
        db.execute("UPDATE entities SET body=? WHERE kind='bots' AND id='ada'", (json.dumps(body),))
    with pytest.raises(ControlError, match="current memory budget"):
        manager.restore(manifest["id"], fingerprint, scope="bot", bot_id="ada")
    assert len(manager.catalog(fingerprint)["snapshots"]) == 1
    assert not (directory / PAUSE_FILE).exists()


def test_failed_full_file_swap_rolls_back_displaced_state(snapshot_data, monkeypatch):
    manager, directory, fingerprint = snapshot_data
    manifest = manager.capture("Original")
    (directory / "images/test-image").write_bytes(b"experimental image")
    original_rename = type(directory).rename
    failed = False

    def break_install(path, target):
        nonlocal failed
        if not failed and path.parent.name.startswith(".hortator-restore-") and path.name == "master.key":
            failed = True
            raise OSError("Injected disk failure")
        return original_rename(path, target)

    monkeypatch.setattr(type(directory), "rename", break_install)
    with pytest.raises(OSError, match="Injected disk failure"):
        manager.restore(manifest["id"], fingerprint)
    assert (directory / "images/test-image").read_bytes() == b"experimental image"
    assert not manager.journal.exists()
    assert json.loads((directory / PAUSE_FILE).read_text())["restore_rolled_back"]
    assert manager.validate_database(directory) == fingerprint


def test_interrupted_swap_recovered_before_runtime_opens_store(snapshot_data, monkeypatch):
    manager, directory, fingerprint = snapshot_data
    manifest = manager.capture("Original")
    original_rename = type(directory).rename
    original_recover = manager.recover_pending
    failed = False

    def simulated_process_loss(path, target):
        nonlocal failed
        if not failed and path.parent.name.startswith(".hortator-restore-") and path.name == "master.key":
            failed = True
            raise OSError("Simulated process termination")
        return original_rename(path, target)

    monkeypatch.setattr(type(directory), "rename", simulated_process_loss)
    monkeypatch.setattr(manager, "recover_pending", lambda: None)
    with pytest.raises(OSError):
        manager.restore(manifest["id"], fingerprint)
    assert manager.journal.exists()
    monkeypatch.setattr(manager, "recover_pending", original_recover)
    monkeypatch.setenv("HORTATOR_ADMIN_PASSWORD", "test-password-only")
    with TestClient(create_app(directory, start_runtime=False)) as client:
        assert client.get("/api/health").status_code == 200
    assert not manager.journal.exists()
    assert json.loads((directory / PAUSE_FILE).read_text())["restore_rolled_back"]
    assert manager.validate_database(directory) == fingerprint


@pytest.mark.asyncio
async def test_snapshot_gate_drains_handlers_and_refuses_new_ones():
    import asyncio
    from hortator.snapshot_runtime import SnapshotGate

    gate = SnapshotGate()
    release = asyncio.Event()
    entered = asyncio.Event()
    exclusive_entered = asyncio.Event()

    async def reader():
        async with gate.enter():
            entered.set()
            await release.wait()

    async def maintenance():
        async with gate.enter(exclusive=True):
            exclusive_entered.set()

    async with asyncio.TaskGroup() as group:
        group.create_task(reader())
        await entered.wait()
        group.create_task(maintenance())
        await asyncio.sleep(0)
        assert gate.maintenance and not exclusive_entered.is_set()
        with pytest.raises(ControlError, match="maintenance"):
            async with gate.enter():
                pass
        release.set()
    assert exclusive_entered.is_set()
    assert not gate.maintenance and gate.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellations", [1, 3])
async def test_cancelled_filesystem_owner_joins_actual_copy_thread(cancellations):
    import asyncio
    import threading
    from hortator.snapshot_runtime import blocking

    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def worker():
        started.set()
        release.wait(timeout=5)
        finished.set()

    async with asyncio.TaskGroup() as group:
        task = group.create_task(blocking(worker))
        await asyncio.to_thread(started.wait, 2)
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
        release.set()
    assert finished.is_set() and task.cancelled()


def test_paused_incomplete_restore_cannot_initialize_empty_database(tmp_path):
    directory = tmp_path / "incomplete"
    directory.mkdir()
    (directory / PAUSE_FILE).write_text("{}")
    with pytest.raises(ExceptionGroup, match="TaskGroup"):
        with TestClient(create_app(directory, start_runtime=False)):
            pass
    assert not (directory / "council.sqlite3").exists()
    assert not (directory / "master.key").exists()


@pytest.mark.parametrize("replacement", [False, True])
def test_selective_context_refuses_missing_or_divergent_message_anchors(snapshot_data, replacement):
    manager, directory, fingerprint = snapshot_data
    older = manager.capture("Before message")
    with sqlite3.connect(directory / "council.sqlite3") as db:
        seq = db.execute(
            "INSERT INTO messages (discord_id,channel_id,author_id,author_name,content,at) VALUES ('original-message','channel-a','human','Human','hello',1)"
        ).lastrowid
        db.execute(
            "UPDATE contexts SET last_seen=?,checkpoint=? WHERE bot_id='ada' AND channel_id='channel-a'",
            (seq, seq),
        )
    newer = manager.capture("After message")
    manager.restore(older["id"], fingerprint)
    if replacement:
        with sqlite3.connect(directory / "council.sqlite3") as db:
            db.execute(
                "INSERT INTO messages (seq,discord_id,channel_id,author_id,author_name,content,at) VALUES (?,'different-message','channel-a','human','Human','different timeline',1)",
                (seq,),
            )
    with pytest.raises(ControlError, match="message anchor is missing or differs"):
        manager.restore(
            newer["id"], fingerprint, scope="bot", bot_id="ada", channel_id="channel-a", include_context=True
        )
    # The same snapshot's notes still restore without moving these checkpoints.
    manager.restore(newer["id"], fingerprint, scope="bot", bot_id="ada", channel_id="channel-a")
