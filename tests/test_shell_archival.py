"""Archive real private job files; no provider calls or live council data."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.shell_runner import ShellRunner, _Active, limits
from hortator.store import Store


@pytest.fixture
def runner(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    instance = ShellRunner(tmp_path / "jobs", SimpleNamespace(store=store))
    yield instance
    store.close()


def seed(runner, number, *, bot="ada", status="succeeded", age=0, output=b"original output"):
    job_id = f"job_{number:032x}"
    directory = runner.root / job_id
    directory.mkdir(mode=0o700)
    for name, data in (("stdout", output), ("stderr", b"original stderr")):
        path = directory / f"{name}.log"
        path.write_bytes(data)
        path.chmod(0o600)
    metadata = dict(
        job_id=job_id,
        bot_id=bot,
        channel_id="room",
        turn_id=f"turn-{number}",
        status=status,
        started_at=time.time() - age,
        exit_code=0,
        stdout_bytes=len(output),
        stderr_bytes=15,
    )
    runner._save(metadata)
    return job_id


def context(number, bot="ada", channel="room"):
    return ToolContext({"id": bot}, channel, f"turn-{number}")


def running_turn(runner, number):
    runner.workspaces.store.execute(
        "INSERT INTO turns(id,bot_id,channel_id,profile_id,provider_id,model,started_at,status,trigger,revision) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (f"turn-{number}", "ada", "room", "p", "p", "model", time.time(), "running", "test", 1),
    )


def test_repeated_admission_never_hits_lifetime_count_and_keeps_evidence(runner):
    config = limits({"max_jobs_per_bot": 3})
    for number in range(1, 41):
        runner._reserve("ada", config)
        seed(runner, number, status="failed" if number % 2 else "succeeded")
    assert len(list(runner.root.glob("job_*/meta.json"))) == 3
    assert len(list(runner.archive.glob("*.json"))) == 37
    assert len(runner.inspect(limit=100)) == 40
    first = f"job_{1:032x}"
    assert runner.status(context(1), first)["archived"]
    assert runner.read(context(1), first)["text"] == "original output"
    assert runner.read(context(1), first, stream="stderr")["text"] == "original stderr"
    assert (runner.archive.stat().st_mode & 0o777) == 0o700
    assert ((runner.archive / f"{first}.json").stat().st_mode & 0o777) == 0o600
    archived = [e for e in runner.workspaces.store.events(limit=100) if e["kind"] == "job.archived"]
    assert len(archived) == 37 and all(e["level"] == "info" for e in archived)


def test_archive_retention_restart_lookup_and_scopes(runner):
    first = seed(runner, 1)
    runner._reserve("ada", limits({"max_jobs_per_bot": 1}))
    restarted = ShellRunner(runner.root, runner.workspaces)
    assert restarted.status(context(1), first)["archived"]
    assert restarted.read(context(1), first)["text"] == "original output"
    for wrong in (context(1, bot="loki"), context(1, channel="elsewhere"), context(2)):
        with pytest.raises(ControlError, match="another bot, channel, or turn"):
            restarted.status(wrong, first)
    metadata = restarted._load(first)
    metadata["started_at"] -= 8 * 86400
    restarted._save(metadata)
    restarted._reserve("ada", limits())
    assert not (runner.root / first).exists()
    assert restarted.status(context(1), first)["output_expired"]
    with pytest.raises(ControlError, match="expired"):
        restarted.read(context(1), first)
    # Archive history remains inspectable, but admission does not scan it.
    (runner.archive / "job_malformed.json").write_text("not consulted on admission")
    restarted._reserve("ada", limits())
    assert json.loads((runner.archive / f"{first}.json").read_text())["job_id"] == first


def test_protected_jobs_and_other_bots_are_never_archived_or_expired(runner):
    protected = seed(runner, 1, age=8 * 86400)
    active = seed(runner, 2, status="running", age=8 * 86400)
    other = seed(runner, 3, bot="loki", age=8 * 86400)
    eligible = seed(runner, 4, status="cancelled")
    running_turn(runner, 1)
    runner.active[active] = _Active(runner._load(active))
    runner._reserve("ada", limits({"max_jobs_per_bot": 3}))
    assert runner._load(eligible)["archived"]
    for job in (protected, active, other):
        assert not runner._load(job)["archived"]
        assert (runner.root / job / "stdout.log").exists()
    with pytest.raises(ControlError, match="running turns"):
        runner._reserve("ada", limits({"max_jobs_per_bot": 2}))
    assert len(list(runner.archive.glob("*.json"))) == 1


def test_archival_does_not_bypass_output_byte_quota(runner):
    first = seed(runner, 1, output=b"x" * 1024)
    runner._reserve("ada", limits({"max_jobs_per_bot": 1}))
    with pytest.raises(ControlError, match="output storage quota.*1024|output storage quota"):
        runner._reserve("ada", limits({"job_storage_bytes_per_bot": 1048576}))
    assert runner.read(context(1), first)["total_bytes"] == 1024


def test_failed_archive_rename_preserves_source_and_restart_recovers_committed_move(runner, monkeypatch):
    first = seed(runner, 1)
    original = Path.rename

    def fail_rename(path, target):
        raise OSError("fixture disk failure")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", fail_rename)
        with pytest.raises(OSError, match="disk failure"):
            runner._reserve("ada", limits({"max_jobs_per_bot": 1}))
    assert runner._load(first)["archived"] is False
    assert runner.read(context(1), first)["text"] == "original output"

    def fail_after_move(path, target):
        original(path, target)
        raise OSError("fixture crash after atomic rename")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", fail_after_move)
        with pytest.raises(OSError, match="after atomic rename"):
            runner._reserve("ada", limits({"max_jobs_per_bot": 1}))
    restarted = ShellRunner(runner.root, runner.workspaces)
    assert restarted._load(first)["archived"]
    assert restarted.read(context(1), first)["text"] == "original output"
    restarted._reserve("ada", limits({"max_jobs_per_bot": 1}))


@pytest.mark.parametrize(
    "unsafe", ["directory_symlink", "metadata_symlink", "metadata_hardlink", "duplicate"]
)
def test_archive_rejects_unsafe_paths_and_duplicate_records(runner, tmp_path, unsafe):
    first = seed(runner, 1)
    runner._reserve("ada", limits({"max_jobs_per_bot": 1}))
    path = runner.archive / f"{first}.json"
    sentinel = tmp_path / "sentinel"
    sentinel.write_text(path.read_text())
    if unsafe == "directory_symlink":
        directory = tmp_path / "other-archive"
        runner.archive.rename(directory)
        runner.archive.symlink_to(directory, target_is_directory=True)
    elif unsafe == "duplicate":
        (runner.root / first / "meta.json").write_text(sentinel.read_text())
    else:
        path.unlink()
        if unsafe == "metadata_symlink":
            path.symlink_to(sentinel)
        else:
            os.link(sentinel, path)
    with pytest.raises(ControlError):
        runner._load(first)
    assert json.loads(sentinel.read_text())["job_id"] == first
