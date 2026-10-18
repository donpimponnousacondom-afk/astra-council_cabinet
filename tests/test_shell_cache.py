"""Cache routing across real sandbox calls, without package downloads."""

import errno
from pathlib import Path

import pytest

from hortator import shell_worker
from support.agentic_runtime import grant
from test_filesystem import require_runner


@pytest.mark.parametrize("storage", ["filesystem", "workspace", "disposable"])
async def test_cache_routing_preserves_saved_settings_and_obeys_task_lifetime(kernel, storage):
    runner = await require_runner(kernel)
    context = grant(kernel, "shell", *([] if storage == "disposable" else [storage]))
    scope = {"task": "cache-test"} if storage == "workspace" else {}
    if storage == "workspace":
        await kernel.registry.call("workspace", {"operation": "start", **scope}, context, "start")
    runner.begin_turn(context, owner_id="human")
    schemas = kernel.registry.schemas(context)

    async def run(command, label):
        result = await kernel.registry.call(
            "shell", {"operation": "run", **scope, "command": command}, context, label
        )
        assert result["status"] == "succeeded", result
        assert result["workspace_committed"] == (storage != "disposable")
        return result

    first = await run(
        """set -eu
python3 - <<'PY'
import os, stat
from pathlib import Path
assert os.environ['HOME'] == '/workspace'
assert os.environ['XDG_CACHE_HOME'] == '/packages/cache'
for key in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'MPLCONFIGDIR', 'WGETRC'):
    assert key not in os.environ, key
cache = Path(os.environ['XDG_CACHE_HOME'])
assert stat.S_IMODE(cache.stat().st_mode) == 0o700
(cache / 'task-cache').write_bytes(b'x' * (2 * 1024 * 1024))
Path('/workspace/.cache').mkdir()
Path('/workspace/.cache/old').write_text('existing saved cache')
Path('/workspace/.config/example').mkdir(parents=True)
Path('/workspace/.config/example/settings').write_text('saved settings')
Path('/workspace/.wgetrc').write_text('user_agent = saved-agent\\n')
PY
git config --global user.name saved-user
""",
        "seed",
    )
    assert first["scratch"]["used_bytes"] >= 2 * 1024 * 1024

    saved_checks = """set -eu
test "$(git config --get user.name)" = saved-user
test "$(cat ~/.config/example/settings)" = 'saved settings'
test "$(cat ~/.wgetrc)" = 'user_agent = saved-agent'
test "$(cat ~/.cache/old)" = 'existing saved cache'
test ! -e ~/.cache/task-cache
"""
    workspace_checks = saved_checks if storage != "disposable" else "set -eu\ntest ! -e /workspace/.cache\n"
    await run(
        workspace_checks
        + """test "$(stat -c %s "$XDG_CACHE_HOME/task-cache")" = 2097152
XDG_CACHE_HOME=/workspace/.cache python3 - <<'PY'
import os
from pathlib import Path
cache = Path(os.environ['XDG_CACHE_HOME'])
cache.mkdir(exist_ok=True)
(cache / 'explicit').write_text('chosen saved cache')
PY
""",
        "reuse",
    )
    runner.finish_turn(context)
    await run(
        workspace_checks
        + "test ! -e /packages/cache/task-cache\n"
        + (
            "test \"$(cat ~/.cache/explicit)\" = 'chosen saved cache'\n"
            if storage != "disposable"
            else "test ! -e /workspace/.cache/explicit\n"
        ),
        "after-task",
    )
    assert kernel.registry.schemas(context) == schemas


def test_failed_cache_preparation_does_not_continue_or_fall_back(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    original_mkdir = Path.mkdir

    def full(path, *args, **kwargs):
        if path == cache:
            raise OSError(errno.ENOSPC, "No space left on device")
        return original_mkdir(path, *args, **kwargs)

    prepare = shell_worker.scratch_directory
    paths = []

    def prepare_cache(path):
        paths.append(path)
        assert path == "/packages/cache"
        prepare(cache)

    monkeypatch.setattr(Path, "mkdir", full)
    monkeypatch.setattr(shell_worker, "scratch_directory", prepare_cache)
    with pytest.raises(RuntimeError, match="Bash did not start") as error:
        shell_worker.prepare_scratch()
    assert "No space left on device" in str(error.value)
    assert paths == ["/packages/cache"]
    assert not cache.exists()
