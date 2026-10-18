"""Stale scratch cleanup belongs only to exclusive ASGI runtime startup."""

import fcntl
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

import hortator.app as application
from hortator.shell_runner import ShellRunner
from hortator.shell_scratch import recover_scratch_mountpoints
from test_shell_runner import sandbox as _sandbox

sandbox = _sandbox


def test_recovery_removes_only_empty_real_scratch_directories(tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "scratch-stale").mkdir()
    occupied = jobs / "scratch-occupied"
    occupied.mkdir()
    (occupied / "keep").write_text("not empty")
    (jobs / "scratch-file").write_text("not a mountpoint")
    other = tmp_path / "unrelated"
    other.mkdir()
    (jobs / "scratch-link").symlink_to(other, target_is_directory=True)
    (jobs / "job_unrelated").mkdir()
    assert recover_scratch_mountpoints(jobs) == 1
    assert not (jobs / "scratch-stale").exists()
    assert (occupied / "keep").read_text() == "not empty"
    assert (jobs / "scratch-file").read_text() == "not a mountpoint"
    assert (jobs / "scratch-link").is_symlink()
    assert other.is_dir()
    assert (jobs / "job_unrelated").is_dir()
    assert recover_scratch_mountpoints(jobs) == 0


def test_recovery_does_not_follow_jobs_root_symlink_or_create_missing_root(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "scratch-keep").mkdir()
    link = tmp_path / "jobs-link"
    link.symlink_to(target, target_is_directory=True)
    assert recover_scratch_mountpoints(link) == 0
    assert (target / "scratch-keep").is_dir()
    absent = tmp_path / "not-created"
    assert recover_scratch_mountpoints(absent) == 0
    assert not absent.exists()


def test_asgi_recovers_after_lock_and_before_kernel_construction(tmp_path, monkeypatch):
    events = []
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    stale = jobs / "scratch-stale"
    stale.mkdir()
    original_kernel = application.Kernel

    def recover(root):
        assert root == jobs
        with (tmp_path / "runtime.lock").open("a") as competing:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
        events.append("locked-recovery")
        return recover_scratch_mountpoints(root)

    def kernel(directory):
        assert events == ["locked-recovery"]
        assert not stale.exists()
        events.append("kernel")
        return original_kernel(directory)

    monkeypatch.setattr(application, "recover_scratch_mountpoints", recover)
    monkeypatch.setattr(application, "Kernel", kernel)
    with TestClient(application.create_app(tmp_path, start_runtime=False)):
        assert events == ["locked-recovery", "kernel"]


def test_second_runtime_cannot_sweep_first_runtimes_empty_mountpoints(tmp_path, monkeypatch):
    recover = Mock(wraps=recover_scratch_mountpoints)
    monkeypatch.setattr(application, "recover_scratch_mountpoints", recover)
    with TestClient(application.create_app(tmp_path, start_runtime=False)):
        current = tmp_path / "jobs" / "scratch-current-owner"
        current.mkdir()
        assert recover.call_count == 1
        with pytest.raises(RuntimeError, match="Another Hortator process"):
            with TestClient(application.create_app(tmp_path, start_runtime=False)):
                pytest.fail("Second runtime acquired the same data directory")
        assert recover.call_count == 1
        assert current.is_dir()
        current.rmdir()


def test_startup_cleanup_error_releases_runtime_lock(tmp_path, monkeypatch):
    def failed_recovery(_):
        raise PermissionError("isolated fixture recovery failure")

    monkeypatch.setattr(application, "recover_scratch_mountpoints", failed_recovery)
    with pytest.raises(PermissionError, match="isolated fixture"):
        with TestClient(application.create_app(tmp_path, start_runtime=False)):
            pytest.fail("Startup continued after recovery failed")
    with (tmp_path / "runtime.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock, fcntl.LOCK_UN)


async def test_constructing_an_inspection_runner_does_not_delete_live_scratch(sandbox):
    runner, workspace, context = sandbox
    first = await runner.run(context, command="printf retained > /tmp/retained")
    assert first["status"] == "succeeded", first
    namespace = runner._scratch_turns[(context.bot["id"], context.channel_id, context.turn_id)]["namespace"]
    inspection = ShellRunner(runner.root, workspace)
    try:
        assert namespace.path.is_dir()
        second = await runner.run(context, command="cat /tmp/retained")
        assert second["status"] == "succeeded", second
        assert second["stdout"]["text"] == "retained"
    finally:
        await inspection.close()
