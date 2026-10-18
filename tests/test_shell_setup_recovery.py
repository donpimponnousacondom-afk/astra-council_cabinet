"""Real retained-scratch recovery and cwd receipts; no package/network requests."""

import asyncio
import os
import pytest

from hortator.shell_worker import scratch_directory
from test_shell_runner import read_file, sandbox as _sandbox

sandbox = _sandbox


@pytest.mark.parametrize("path", ["/packages/tmp", "/packages/tmp/runtime"])
@pytest.mark.parametrize("kind", ["file", "symlink", "socket"])
async def test_required_scratch_directories_recover_without_following_targets(sandbox, path, kind):
    runner, _, context = sandbox
    create = {
        "file": f"touch {path}",
        "symlink": f"ln -s /workspace {path}",
        "socket": f"python3 -c \"import socket; s=socket.socket(socket.AF_UNIX); s.bind('{path}')\"",
    }[kind]
    first = await runner.run(
        context,
        task="default",
        command=f"""set -eu
printf saved > /workspace/sentinel
printf retained > /packages/untouched
rm -rf {path}
{create}
""",
    )
    assert first["status"] == "succeeded", first
    second = await runner.run(
        context,
        task="default",
        command="""set -eu
[ "$(cat /workspace/sentinel)" = saved ]
[ "$(cat /packages/untouched)" = retained ]
python3 - <<'CHECK'
import os, stat
from pathlib import Path
assert os.environ['HOME'] == '/workspace'
assert os.environ['TMPDIR'] == '/packages/tmp'
assert os.environ['XDG_RUNTIME_DIR'] == '/packages/tmp/runtime'
for name in ('/packages/tmp', os.environ['XDG_RUNTIME_DIR']):
    info = Path(name).lstat()
    assert stat.S_ISDIR(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o700
    assert info.st_uid == os.getuid()
assert not Path('/workspace/runtime').exists()
CHECK
""",
    )
    assert second["status"] == "succeeded", second
    assert second["workspace_committed"]


async def test_existing_scratch_directories_keep_contents_and_repair_mode(sandbox):
    runner, _, context = sandbox
    first = await runner.run(
        context,
        command="""set -eu
printf temp > /tmp/preserved
printf runtime > "$XDG_RUNTIME_DIR/preserved"
chmod 0500 "$XDG_RUNTIME_DIR" /packages/tmp
""",
    )
    assert first["status"] == "succeeded", first
    second = await runner.run(
        context,
        command="""set -eu
[ "$(cat /tmp/preserved)" = temp ]
[ "$(cat "$XDG_RUNTIME_DIR/preserved")" = runtime ]
printf writable > "$XDG_RUNTIME_DIR/new"
""",
    )
    assert second["status"] == "succeeded", second


@pytest.mark.parametrize("kind", ["directory", "socket", "symlink"])
async def test_old_cwd_marker_is_unrelated_user_scratch(sandbox, kind):
    runner, _, context = sandbox
    create = {
        "directory": "mkdir /tmp/hortator-cwd; printf keep > /tmp/hortator-cwd/keep",
        "socket": "python3 -c \"import socket; s=socket.socket(socket.AF_UNIX); s.bind('/tmp/hortator-cwd')\"",
        "symlink": "ln -s /workspace/sentinel /tmp/hortator-cwd",
    }[kind]
    first = await runner.run(
        context,
        task="default",
        command=f"set -eu\nprintf saved > sentinel\nmkdir nested\n{create}\ncd nested",
    )
    assert first["status"] == "succeeded", first
    assert first["cwd"] == "nested"
    second = await runner.run(context, task="default", command="pwd; stat -c %F /tmp/hortator-cwd")
    assert second["status"] == "succeeded", second
    assert second["stdout"]["text"].startswith("/workspace/nested\n")
    assert (await read_file(runner.workspaces, context, "sentinel"))["content"] == "saved"
    if kind == "directory":
        kept = await runner.run(context, command="cat /tmp/hortator-cwd/keep")
        assert kept["stdout"]["text"] == "keep"


async def test_missing_venv_configuration_is_repaired_without_removing_packages(sandbox):
    runner, _, context = sandbox
    first = await runner.run(
        context,
        command="""set -eu
python3 - <<'CHECK'
import sysconfig
from pathlib import Path
Path(sysconfig.get_path('purelib'), 'retained_fixture.py').write_text('value = 37')
Path('/packages/venv/pyvenv.cfg').unlink()
CHECK
""",
    )
    assert first["status"] == "succeeded", first
    second = await runner.run(
        context,
        command="""set -eu
python3 - <<'CHECK'
import sys, retained_fixture
assert sys.prefix == '/packages/venv'
assert retained_fixture.value == 37
CHECK
python3 -m pip --version
""",
    )
    assert second["status"] == "succeeded", second


async def test_cwd_pipe_still_works_when_scratch_is_full(sandbox):
    runner, _, context = sandbox
    config = {"package_bytes": 67_108_864}
    full = await runner.run(
        context,
        task="default",
        configuration=config,
        command="""set -eu
mkdir nested
python3 - <<'CHECK'
import errno
with open('/packages/full', 'wb', buffering=0) as target:
    try:
        while True:
            target.write(b'x' * (1024 * 1024))
    except OSError as error:
        assert error.errno == errno.ENOSPC
CHECK
cd nested
""",
    )
    assert full["status"] == "succeeded", full
    assert full["scratch"]["available_bytes"] == 0
    assert full["cwd"] == "nested"
    recovered = await runner.run(
        context, task="default", configuration=config, command="set -eu; rm /packages/full; pwd"
    )
    assert recovered["status"] == "succeeded", recovered
    assert recovered["stdout"]["text"] == "/workspace/nested\n"


async def test_cwd_pipe_bounds_invalid_payload_and_allows_replaced_exit_trap(sandbox):
    runner, _, context = sandbox
    flooded = await runner.run(
        context,
        task="default",
        command="""set -eu
python3 - <<'CHECK'
import os, stat
pipes = []
for name in os.listdir('/proc/self/fd'):
    descriptor = int(name)
    try:
        if descriptor > 2 and stat.S_ISFIFO(os.fstat(descriptor).st_mode):
            pipes.append(descriptor)
    except OSError:
        pass
assert len(pipes) == 1, pipes
for _ in range(32):
    os.write(pipes[0], b'x' * 16384)
CHECK
mkdir nested
cd nested
""",
    )
    assert flooded["status"] == "succeeded", flooded
    assert flooded["cwd"] == "."  # Invalid/oversized cwd receipts cannot select a directory.
    replaced = await runner.run(
        context, task="default", command="set -eu; trap ':' EXIT; cd nested; printf complete"
    )
    assert replaced["status"] == "succeeded", replaced
    assert replaced["cwd"] == "."
    assert replaced["stdout"]["text"] == "complete"


async def test_cwd_pipe_does_not_leak_host_descriptors_after_timeout_or_cancel(sandbox):
    runner, _, context = sandbox
    warmup = await runner.run(context, command="true")
    assert warmup["status"] == "succeeded", warmup
    before = len(os.listdir("/proc/self/fd"))
    timed_out = await runner.run(context, command="exec 1>&- 2>&-; sleep 30", timeout_seconds=0.2)
    assert timed_out["status"] == "timed_out", timed_out
    async with asyncio.TaskGroup() as group:
        call = group.create_task(runner.run(context, command="printf waiting; sleep 30"))
        async with asyncio.timeout(5):
            while not any(
                (runner.root / job / "stdout.log").read_bytes() == b"waiting"
                for job in runner.active
                if (runner.root / job / "stdout.log").exists()
            ):
                await asyncio.sleep(0.01)
        call.cancel()
    assert call.cancelled()
    after = await runner.run(context, command="printf alive")
    assert after["status"] == "succeeded", after
    assert after["stdout"]["text"] == "alive"
    assert not runner.active
    assert len(os.listdir("/proc/self/fd")) == before
    assert not list(runner.root.glob("job_*/.execution"))


@pytest.mark.parametrize(
    "command, kind",
    [
        ("ln -s /packages/not-a-published-target invalid-entry", "symlink"),
        ("ln sentinel invalid-entry", "hardlinked regular file"),
        (
            "python3 -c \"import socket; s=socket.socket(socket.AF_UNIX); s.bind('invalid-entry')\"",
            "special file",
        ),
    ],
)
@pytest.mark.parametrize("task", ["default", None])
async def test_invalid_workspace_entries_report_path_and_type_without_link_target(
    sandbox, command, kind, task
):
    runner, workspace, context = sandbox
    first = await runner.run(context, task="default", command="printf before > sentinel")
    assert first["status"] == "succeeded", first
    rejected = await runner.run(context, task=task, command=f"set -eu; printf changed > sentinel; {command}")
    assert rejected["status"] == "failed", rejected
    assert not rejected["workspace_committed"]
    assert kind in rejected["error"]
    # Both names of a hardlink are invalid; scan order is intentionally unspecified.
    assert ("invalid-entry" in rejected["error"]) or (
        kind == "hardlinked regular file" and "sentinel" in rejected["error"]
    )
    assert "/packages/not-a-published-target" not in rejected["error"]
    assert "No workspace changes from this call will be saved" in rejected["error"]
    assert (await read_file(workspace, context, "sentinel"))["content"] == "before"


def test_failed_scratch_repair_names_the_path_and_unstarted_command(tmp_path):
    path = tmp_path / "missing-parent" / "tmp"
    with pytest.raises(RuntimeError) as error:
        scratch_directory(path)
    assert str(path) in str(error.value)
    assert "Bash did not start" in str(error.value)


async def test_cwd_pipe_cannot_choose_a_directory_symlink_outside_workspace(sandbox):
    runner, _, context = sandbox
    result = await runner.run(
        context,
        task="default",
        command=r"""set -eu
mkdir /packages/outside
ln -s /packages/outside /workspace/redirect
trap ':' EXIT
python3 - <<'CHECK'
import os, stat
for name in os.listdir('/proc/self/fd'):
    descriptor = int(name)
    try:
        is_pipe = descriptor > 2 and stat.S_ISFIFO(os.fstat(descriptor).st_mode)
    except OSError:
        continue
    if is_pipe:
        os.write(descriptor, b'/workspace/redirect\n')
CHECK
""",
    )
    assert result["status"] == "failed", result
    assert result["cwd"] == "."
    assert not result["workspace_committed"]
    assert "symlink" in result["error"]
    assert "redirect" in result["error"]


@pytest.mark.parametrize(
    "damage",
    [
        "rm /packages/venv/pyvenv.cfg; mkdir /packages/venv/pyvenv.cfg",
        "rm -rf /packages/venv; touch /packages/venv",
    ],
)
async def test_unrepairable_venv_fails_before_bash_and_recovers_when_human_task_ends(sandbox, damage):
    runner, _, context = sandbox
    damaged = await runner.run(
        context,
        command="set -eu; " + damage,
    )
    assert damaged["status"] == "succeeded", damaged
    blocked = await runner.run(context, command="printf command-must-not-start")
    assert blocked["status"] == "failed", blocked
    assert blocked["stdout"]["text"] == ""
    assert "/packages/venv" in blocked["error"]
    assert "Bash did not start" in blocked["error"]
    assert "Finish the current human task" in blocked["error"]
    assert not blocked["workspace_committed"]
    runner.finish_turn(context)
    fresh = await runner.run(context, command="printf fresh-task")
    assert fresh["status"] == "succeeded", fresh
    assert fresh["stdout"]["text"] == "fresh-task"
