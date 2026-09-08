"""These jobs execute real Bash/Python inside bubblewrap, never mocked subprocesses."""

import asyncio
import base64
import json
import os
from pathlib import Path
import select
import sys
import time
from types import SimpleNamespace

import pytest

from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.shell_runner import ShellRunner, validate_config
from hortator.store import Store
from hortator.workspaces import Workspaces


@pytest.fixture
async def sandbox(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    workspace = Workspaces(store, tmp_path, SimpleNamespace(redact=lambda value: value))
    runner = ShellRunner(tmp_path / "jobs", workspace)
    context = ToolContext({"id": "ada"}, "123456", "turn-shell")
    await workspace.call({"operation": "start", "task": "default"}, context)
    ready = await runner.readiness()
    if not ready["ready"]:
        store.close()
        if os.environ.get("HORTATOR_REQUIRE_SANDBOX") == "1":
            pytest.fail("Required sandbox unavailable: " + ready["error"])
        pytest.skip("Real namespace execution unavailable: " + ready["error"])
    yield runner, workspace, context
    await runner.close()
    store.close()


async def read_file(workspace, context, path, task="default"):
    return await workspace.call({"operation": "read", "task": task, "path": path}, context)


async def test_real_pipeline_scripts_cwd_and_nonzero_exit_persist(sandbox):
    runner, workspace, context = sandbox
    first = await runner.run(context, command="mkdir nested; cd nested; printf 'z\\na\\n' | sort > names.txt")
    assert first["status"] == "succeeded", first
    assert first["workspace_committed"] and first["cwd"] == "nested"
    second = await runner.run(context, command="pwd; cat names.txt; printf 'why\\n' >&2; exit 7")
    assert second["status"] == "failed" and second["exit_code"] == 7
    assert second["workspace_committed"]
    assert second["stdout"]["text"] == "/workspace/nested\na\nz\n"
    assert second["stderr"]["text"] == "why\n"
    assert (await read_file(workspace, context, "nested/names.txt"))["content"] == "a\nz\n"
    third = await runner.run(
        context, command="printf '#!/bin/bash\\nprintf script-ok' > check.sh; bash check.sh"
    )
    assert third["stdout"]["text"] == "script-ok"
    assert not list(runner.root.glob("job_*/.execution"))


async def test_real_stdout_stderr_concurrent_byte_paging_and_limit(sandbox):
    runner, _, context = sandbox
    job = await runner.run(
        context,
        command="python3 - <<'PY'\nimport os\nfor _ in range(100):\n os.write(1, '雪\\n'.encode()*50)\n os.write(2, b'error\\n'*50)\nPY",
    )
    assert job["status"] == "succeeded", job
    for stream, expected in (("stdout", ("雪\n" * 5000).encode()), ("stderr", b"error\n" * 5000)):
        offset = 0
        content = bytearray()
        while True:
            page = runner.read(context, job["job_id"], stream, offset=offset, limit=101)
            content += base64.b64decode(page["data_base64"])
            assert page["start"] == offset
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        assert content == expected
        assert runner.read(context, job["job_id"], stream, len(expected))["text"] == ""
    limited = await runner.run(
        context,
        command='python3 -c \'import os; os.write(1,b"x"*200000); os.write(2,b"y"*200000)\'',
        configuration={"output_bytes": 4096},
    )
    assert limited["status"] == "output_limited", limited
    assert limited["stdout_bytes"] + limited["stderr_bytes"] <= 4096
    assert not limited["workspace_committed"]


def namespace_pids(namespace):
    found = []
    for item in Path("/proc").iterdir():
        if item.name.isdigit():
            try:
                if os.readlink(item / "ns/pid") == namespace:
                    found.append(int(item.name))
            except FileNotFoundError, PermissionError, ProcessLookupError:
                pass
    return found


async def test_real_timeout_cancel_background_cleanup_and_closed_streams(sandbox):
    runner, workspace, context = sandbox
    detached = "python3 - <<'PY'\nimport subprocess\nsubprocess.Popen(['python3','-c','import time; time.sleep(30)'],start_new_session=True)\nprint('spawned')\nPY"
    job = await runner.run(context, command=detached, timeout_seconds=1)
    assert job["status"] == "succeeded", job
    assert not namespace_pids(job["pid_namespace"])
    timeout = await runner.run(
        context, command="echo scratch > lost.txt; exec 1>&- 2>&-; sleep 30", timeout_seconds=0.2
    )
    assert timeout["status"] == "timed_out", timeout
    assert timeout["elapsed_seconds"] < 3
    assert not timeout["workspace_committed"]
    assert not namespace_pids(timeout["pid_namespace"])
    with pytest.raises(ControlError):
        await read_file(workspace, context, "lost.txt")
    task = asyncio.create_task(runner.run(context, command=detached + "\nsleep 30"))
    while not runner.active or next(iter(runner.active.values())).process is None:
        await asyncio.sleep(0.01)
    job_id = next(iter(runner.active))
    started = time.monotonic()
    await runner.cancel(context, job_id)
    cancelled = await task
    assert time.monotonic() - started < 3
    assert cancelled["status"] == "cancelled"
    assert not cancelled["workspace_committed"]
    assert not runner.active and not workspace._active
    task = asyncio.create_task(runner.run(context, command="sleep 30"))
    while not runner.active or next(iter(runner.active.values())).process is None:
        await asyncio.sleep(0.01)
    job_id = next(iter(runner.active))
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner.status(context, job_id)["status"] == "cancelled"
    assert time.monotonic() - started < 3
    assert not runner.active and not workspace._active


async def test_real_files_environment_network_and_supervisor_isolation(sandbox, tmp_path, monkeypatch):
    runner, _, context = sandbox
    sentinel = tmp_path / "outside-sentinel"
    sentinel.write_text("unshared-private-fixture")
    monkeypatch.setenv("HORTATOR_TEST_PRIVATE", "environment-sentinel")
    received = []

    async def handler(reader, writer):
        received.append(True)
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    script = f"""python3 - <<'PY'
import os, socket, ctypes, pathlib
assert os.getuid() == 1000
assert os.environ.get('HORTATOR_TEST_PRIVATE') is None
for p in [{str(sentinel)!r}, '/home/codexy/.ssh', '/home/codexy/.local/share/hortator', '/proc/1/root{str(sentinel)}', '/sys/fs/cgroup', '/run', '/bin/sudo', '/bin/node', '/bin/python3.12', '/bin/python3.13']:
 try:
  assert not pathlib.Path(p).exists(), p
 except PermissionError:
  pass
assert 'CapEff:\\t0000000000000000' in pathlib.Path('/proc/self/status').read_text()
with socket.create_connection(('127.0.0.1', {port}), 1):
 pass
libc = ctypes.CDLL(None, use_errno=True)
assert libc.unshare(0x10000000) == -1
try:
 os.open('/proc/1/fd/1', os.O_WRONLY)
 raise AssertionError('supervisor pipe exposed')
except PermissionError:
 pass
print('isolated')
PY"""
    try:
        result = await runner.run(context, command=script)
    finally:
        server.close()
        await server.wait_closed()
    assert result["status"] == "succeeded", result
    assert result["stdout"]["text"] == "isolated\n"
    assert received
    assert sentinel.read_text() == "unshared-private-fixture"


async def test_real_memory_process_and_tmpfs_limits(sandbox):
    runner, workspace, context = sandbox
    script = """python3 - <<'PY'
import resource, subprocess
try:
 bytearray(512*1024*1024)
 raise AssertionError('address-space limit bypassed')
except MemoryError:
 print('memory-bounded')
try:
 resource.setrlimit(resource.RLIMIT_AS, (-1,-1))
 raise AssertionError('hard limit raised')
except ValueError:
 pass
children=[]
try:
 for _ in range(40):
  children.append(subprocess.Popen(['sleep','20']))
 raise AssertionError('process limit bypassed')
except BlockingIOError:
 print('processes-bounded',len(children))
finally:
 for child in children: child.kill()
 for child in children: child.wait()
PY"""
    result = await runner.run(
        context, command=script, configuration={"memory_bytes_per_process": 67_108_864, "process_limit": 8}
    )
    assert result["status"] == "succeeded", result
    assert "memory-bounded" in result["stdout"]["text"]
    assert "processes-bounded" in result["stdout"]["text"]
    # /tmp's independent hard 16 MiB tmpfs limit includes separate files.
    result = await runner.run(
        context,
        command="python3 - <<'PY'\nimport errno\ntry:\n for i in range(8):\n  with open('/tmp/f'+str(i),'wb') as f: f.write(b'x'*4194304)\n raise AssertionError('tmpfs byte cap bypassed')\nexcept OSError as e:\n assert e.errno == errno.ENOSPC,e\n print('tmpfs-bounded')\nPY",
    )
    assert result["status"] == "succeeded", result
    assert "tmpfs-bounded" in result["stdout"]["text"]
    result = await runner.run(
        context,
        command="python3 - <<'PY'\nimport pathlib,time\nfor i in range(2000):\n pathlib.Path('/workspace/f'+str(i)).touch()\ntime.sleep(3)\nPY",
    )
    assert result["status"] == "resource_limited", result
    assert not result["workspace_committed"]
    assert not workspace._active


@pytest.mark.parametrize("stage", ["setup", "running"])
async def test_real_parent_sigkill_cleans_namespace_and_restart_marks_interrupted(sandbox, tmp_path, stage):
    # This is a separate trusted test controller, not an API/Discord runtime.
    # SIGKILL exercises the boundary's parent-death path with no Python finally.
    runner, _, _ = sandbox
    isolated = tmp_path / ("parent-death-" + stage)
    program = """import asyncio,sys
from pathlib import Path
from types import SimpleNamespace
from hortator.store import Store
from hortator.workspaces import Workspaces
from hortator.shell_runner import ShellRunner
from hortator.plugins import ToolContext
async def main():
 root=Path(sys.argv[1]);root.mkdir()
 store=Store(root/'test.sqlite3')
 workspace=Workspaces(store,root,SimpleNamespace(redact=lambda x:x))
 context=ToolContext({'id':'ada'},'channel','parent-turn')
 await workspace.call({'operation':'start','task':'default'},context)
 runner=ShellRunner(root/'jobs',workspace)
 await runner.run(context,command="echo child-ready; python3 -c 'import time;time.sleep(30)'")
asyncio.run(main())
"""
    controller = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        program,
        str(isolated),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": os.environ["PATH"]},
    )
    pidfd = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            infos = list((isolated / "jobs").glob("job_*/.execution/namespace.json"))
            if infos:
                try:
                    info = json.loads(infos[0].read_text())
                    job_directory = infos[0].parent.parent
                    ready = stage == "setup" or b"child-ready" in (job_directory / "stdout.log").read_bytes()
                    if ready:
                        pidfd = os.pidfd_open(info["child-pid"])
                        break
                except ValueError, OSError:
                    pass
            if controller.returncode is not None:
                pytest.fail((await controller.stderr.read()).decode())
            await asyncio.sleep(0.002)
        assert pidfd is not None, "Test controller never entered its isolated job"
        started = time.monotonic()
        controller.kill()
        await controller.wait()
        while not select.select([pidfd], [], [], 0)[0] and time.monotonic() - started < 3:
            await asyncio.sleep(0.01)
        assert select.select([pidfd], [], [], 0)[0], "Namespace PID 1 survived its controller's SIGKILL"
        recovered = ShellRunner(isolated / "jobs")
        context = ToolContext({"id": "ada"}, "channel", "parent-turn")
        metadata = recovered.status(context, job_directory.name)
        assert metadata["status"] == "interrupted"
        assert not metadata["workspace_committed"]
        if stage == "running":
            assert "child-ready" in recovered.read(context, job_directory.name)["text"]
        assert not list(recovered.root.glob("job_*/.execution"))
        await recovered.close()
    finally:
        if controller.returncode is None:
            controller.kill()
            await controller.wait()
        if pidfd is not None:
            os.close(pidfd)
    assert not runner.active


async def test_close_waits_for_actual_tree_cleanup_and_workspace_release(sandbox):
    runner, workspace, context = sandbox
    task = asyncio.create_task(runner.run(context, command="sleep 30"))
    while not runner.active or next(iter(runner.active.values())).process is None:
        await asyncio.sleep(0.01)
    started = time.monotonic()
    await runner.close()
    result = await task
    assert result["status"] == "cancelled"
    assert time.monotonic() - started < 3
    assert not runner.active and not workspace._active


async def test_real_workspace_byte_and_export_file_limits(sandbox, monkeypatch):
    from hortator.workspaces import validate_config as workspace_limits

    runner, workspace, context = sandbox
    config = workspace_limits({"max_workspace_bytes": 20 * 1024 * 1024, "max_file_bytes": 20 * 1024 * 1024})
    monkeypatch.setattr(workspace, "effective_config", lambda bot: config)
    result = await runner.run(context, command="truncate -s 21M too-big")
    assert result["status"] == "failed" and not result["workspace_committed"]
    assert "per-file" in result["error"]
    result = await runner.run(context, command="dd if=/dev/zero of=full bs=1M count=21; rm -f full")
    assert result["status"] == "succeeded", result
    assert "No space left" in result["stderr"]["text"]
    assert result["workspace_committed"]


async def test_job_retention_preserves_running_turn_and_quota_is_explicit(sandbox):
    runner, workspace, context = sandbox
    job = await runner.run(context, command="echo retained-output")
    metadata = runner._load(job["job_id"])
    metadata["started_at"] -= 8 * 86_400
    runner._save(metadata)
    workspace.store.execute(
        "INSERT INTO turns(id,bot_id,channel_id,profile_id,provider_id,model,started_at,status,trigger,revision) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            context.turn_id,
            "ada",
            context.channel_id,
            "profile",
            "provider",
            "model",
            time.time(),
            "running",
            "test",
            1,
        ),
    )
    await runner.run(context, command="true")
    assert runner.read(context, job["job_id"])["text"] == "retained-output\n"
    workspace.store.execute("UPDATE turns SET status='completed' WHERE id=?", (context.turn_id,))
    await runner.run(context, command="true")
    with pytest.raises(ControlError, match="expired"):
        runner.read(context, job["job_id"])
    with pytest.raises(ControlError, match="count quota"):
        await runner.run(context, command="echo should-not-run", configuration={"max_jobs_per_bot": 1})
    assert not workspace._active
    kinds = [event["kind"] for event in workspace.store.events(limit=100)]
    assert "job.started" in kinds and "job.completed" in kinds
    assert len([kind for kind in kinds if kind == "job.progress"]) == 3


async def test_job_storage_rejects_symlinks_and_hardlinks(sandbox, tmp_path):
    runner, _, context = sandbox
    root_link = tmp_path / "linked-jobs"
    root_link.symlink_to(runner.root, target_is_directory=True)
    with pytest.raises(ControlError, match="symlink"):
        ShellRunner(root_link)
    job = await runner.run(context, command="echo safe")
    log = runner.root / job["job_id"] / "stdout.log"
    sentinel = tmp_path / "private-output"
    sentinel.write_text("do-not-read")
    log.unlink()
    log.symlink_to(sentinel)
    with pytest.raises(ControlError, match="regular files"):
        runner.read(context, job["job_id"])
    log.unlink()
    os.link(sentinel, log)
    with pytest.raises(ControlError, match="regular files"):
        runner.read(context, job["job_id"])
    assert sentinel.read_text() == "do-not-read"


async def test_job_ownership_rejected_paths_unsafe_copyback_and_restart_metadata(sandbox):
    runner, workspace, context = sandbox
    for path in ("/home/codexy", "../other", "x/../../out"):
        with pytest.raises(ControlError):
            await runner.run(context, command="true", cwd=path)
    job = await runner.run(context, command="echo okay > normal.txt")
    for unauthorized in (
        ToolContext({"id": "curie"}, context.channel_id, context.turn_id),
        ToolContext(context.bot, "other-channel", context.turn_id),
        ToolContext(context.bot, context.channel_id, "later-turn"),
    ):
        with pytest.raises(ControlError):
            runner.status(unauthorized, job["job_id"])
        with pytest.raises(ControlError):
            runner.read(unauthorized, job["job_id"])
        with pytest.raises(ControlError):
            await runner.cancel(unauthorized, job["job_id"])
    bad = await runner.run(
        context, command='python3 -c \'import os;os.symlink("/source/normal.txt","link.txt")\''
    )
    assert bad["status"] == "failed" and not bad["workspace_committed"]
    assert "symlink" in bad["error"]
    assert (await read_file(workspace, context, "normal.txt"))["content"] == "okay\n"
    path = runner.root / job["job_id"] / "meta.json"
    metadata = json.loads(path.read_text())
    metadata["status"] = "running"
    path.write_text(json.dumps(metadata))
    restarted = ShellRunner(runner.root, workspace)
    assert restarted.status(context, job["job_id"])["status"] == "interrupted"
    assert restarted.read(context, job["job_id"])["text"] == ""
    await restarted.close()


async def test_unavailable_isolation_fails_closed(tmp_path, monkeypatch):
    runner = ShellRunner(tmp_path / "jobs")
    monkeypatch.setattr("hortator.shell_runner.shutil.which", lambda *args, **kwargs: None)
    ready = await runner.readiness()
    assert not ready["ready"] and "bubblewrap" in ready["error"]
    assert "No unrestricted-host fallback" in ready["setup"]
    with pytest.raises(ControlError, match="bubblewrap"):
        await runner.run(ToolContext({"id": "ada"}, "channel", "turn"), command="echo forbidden")
    assert not list(runner.root.glob("job_*"))


@pytest.mark.parametrize(
    "config",
    [
        {"unknown": 1},
        {"timeout_seconds": float("nan")},
        {"process_limit": True},
        {"memory_bytes_per_process": 1},
    ],
)
def test_invalid_configuration_never_coerced(config):
    with pytest.raises(ControlError):
        validate_config(config)


@pytest.mark.parametrize("relation", ["match", "mismatch", "disappeared"])
def test_pidfd_pins_process_before_parent_check_and_closes_rejected_handles(tmp_path, monkeypatch, relation):
    if not hasattr(os, "pidfd_open"):
        pytest.skip("Linux pidfd support is unavailable")
    info = tmp_path / "namespace.json"
    info.write_text(json.dumps({"child-pid": os.getpid()}))
    active = SimpleNamespace(
        init_pidfd=None,
        info_path=info,
        process=SimpleNamespace(pid=os.getppid() if relation != "mismatch" else -1),
    )
    opened = []
    original_open, original_read = os.pidfd_open, Path.read_text

    def open_pidfd(pid):
        descriptor = original_open(pid)
        opened.append(descriptor)
        return descriptor

    def read_text(path, *args, **kwargs):
        if path == Path(f"/proc/{os.getpid()}/stat"):
            assert opened, "The numeric PID must be pinned before its parent is validated"
            os.fstat(opened[-1])
            if relation == "disappeared":
                raise FileNotFoundError("Process exited during parent validation")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(os, "pidfd_open", open_pidfd)
    monkeypatch.setattr(Path, "read_text", read_text)
    try:
        ShellRunner._capture_init(active)
        assert len(opened) == 1
        if relation == "match":
            assert active.init_pidfd == opened[0]
            os.fstat(active.init_pidfd)
        else:
            assert active.init_pidfd is None
            with pytest.raises(OSError):
                os.fstat(opened[0])
    finally:
        if active.init_pidfd is not None:
            os.close(active.init_pidfd)


async def test_real_expanded_toolchain_and_python314_aliases(sandbox):
    runner, _, context = sandbox
    command = """set -eu
for tool in curl wget git jq perl ps pgrep id whoami ss netstat ip chmod ln dd uv micromamba pkg; do
 command -v "$tool"
done
! command -v node
for python_name in python python3 python3.14; do
 "$python_name" -c 'import sys; assert sys.version_info[:2] == (3,14); assert sys.prefix == "/packages/venv"'
done
[ "$(whoami)" = agent ]; [ "$(id -u)" = 1000 ]
ps -eo pid,comm; pgrep bash; ss -ltn > /packages/ss; netstat -ltn > /packages/netstat; ip -brief address
printf '{"value":42}' | jq -e '.value == 42'
perl -MJSON::PP -MDigest::SHA=sha256_hex -e 'print encode_json({ok => sha256_hex("abc")})'
git init -q /packages/repo
git -C /packages/repo -c user.name=Test -c user.email=test@example.invalid commit --allow-empty -qm test
git -C /packages/repo log --oneline
python3.14 -m venv /packages/second
/packages/second/bin/python3.14 -m pip --version
/bin/python3.14 -m pip install --no-index example 2> /packages/system-pip-error && exit 9
cat /packages/system-pip-error
"""
    result = await runner.run(context, command=command)
    assert result["status"] == "succeeded", result
    assert "externally-managed-environment" in result["stdout"]["text"]
    assert result["workspace_committed"]


async def test_real_python_package_install_and_disposable_environments(sandbox):
    runner, workspace, context = sandbox
    # A local synthetic wheel exercises real uv/pip without relying on an index.
    command = """set -eu
python3.14 - <<'WHEEL'
from zipfile import ZipFile
with ZipFile('/packages/sandbox_fixture-1.0-py3-none-any.whl','w') as w:
 w.writestr('sandbox_fixture.py', 'VALUE = 42')
 w.writestr('sandbox_fixture-1.0.dist-info/METADATA', 'Metadata-Version: 2.1\\nName: sandbox-fixture\\nVersion: 1.0\\n')
 w.writestr('sandbox_fixture-1.0.dist-info/WHEEL', 'Wheel-Version: 1.0\\nGenerator: fixture\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n')
 w.writestr('sandbox_fixture-1.0.dist-info/RECORD', '')
WHEEL
uv pip install --no-index /packages/sandbox_fixture-1.0-py3-none-any.whl
python3.14 -c 'import sandbox_fixture; assert sandbox_fixture.VALUE == 42'
uv venv --python python3.14 --system-site-packages /packages/uv-env
/packages/uv-env/bin/python3.14 -m pip install --no-index /packages/sandbox_fixture-1.0-py3-none-any.whl
/packages/uv-env/bin/python3.14 -c 'import sandbox_fixture; assert sandbox_fixture.VALUE == 42'
printf saved > deliverable.txt
"""
    result = await runner.run(context, command=command)
    assert result["status"] == "succeeded", result
    assert result["workspace_committed"]
    assert (await read_file(workspace, context, "deliverable.txt"))["content"] == "saved"
    next_job = await runner.run(
        context,
        command="python3.14 -c 'import importlib.util; assert importlib.util.find_spec(\"sandbox_fixture\") is None'; test ! -e /packages/uv-env; cat deliverable.txt",
    )
    assert next_job["status"] == "succeeded", next_job
    assert next_job["stdout"]["text"] == "saved"


async def test_real_package_storage_byte_and_entry_limits(sandbox):
    runner, _, context = sandbox
    result = await runner.run(
        context,
        command="dd if=/dev/zero of=/packages/full bs=1M count=70; rm -f /packages/full; echo retained > result.txt",
        configuration={"package_bytes": 67_108_864},
    )
    assert result["status"] == "succeeded" and result["workspace_committed"], result
    assert "No space left" in result["stderr"]["text"]
    result = await runner.run(
        context,
        command="python3.14 -c 'from pathlib import Path; import time; [Path(f\"/packages/f{i}\").touch() for i in range(2000)]; time.sleep(3)'",
        configuration={"package_entries": 1000},
    )
    assert result["status"] == "resource_limited" and not result["workspace_committed"], result


@pytest.mark.skipif(
    os.environ.get("HORTATOR_TEST_PACKAGE_NETWORK") != "1", reason="Opt-in public package download"
)
async def test_real_public_https_and_native_package_install(sandbox):
    runner, _, context = sandbox
    result = await runner.run(
        context,
        command="set -eu; curl -fsS https://pypi.org/simple/six/ -o /packages/curl-index; wget -q https://pypi.org/simple/six/ -O /packages/wget-index; uv pip install six==1.17.0; python3.14 -c 'import six; assert six.__version__ == \"1.17.0\"'; git ls-remote --exit-code https://github.com/mamba-org/micromamba-releases.git refs/tags/2.9.0-0 > /packages/git-refs; pkg install zstd; zstd --version; pkg install zstd; echo installed > native-result.txt",
    )
    assert result["status"] == "succeeded" and result["workspace_committed"], result
    assert "Zstandard" in result["stdout"]["text"]
