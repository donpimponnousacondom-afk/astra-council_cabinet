"""Real namespace-backed task scratch; no provider or network traffic."""

import errno
import os

import pytest

from hortator.plugins import ToolContext
from test_shell_runner import sandbox as _sandbox

sandbox = _sandbox


async def test_tmp_and_packages_share_capacity_and_survive_calls(sandbox):
    runner, _, context = sandbox
    config = {"package_bytes": 67_108_864}
    first = await runner.run(
        context,
        configuration=config,
        command="""set -eu
python3 - <<'PY'
from pathlib import Path
Path('/tmp/large').write_bytes(b'x' * (20 * 1024 * 1024))
Path('/packages/large').write_bytes(b'y' * (20 * 1024 * 1024))
PY
""",
    )
    assert first["status"] == "succeeded", first
    assert first["scratch"]["used_bytes"] >= 40 * 1024 * 1024
    second = await runner.run(
        context,
        configuration=config,
        command="""set -eu
python3 - <<'PY'
from pathlib import Path
import errno, os
assert Path('/tmp/large').stat().st_size == 20 * 1024 * 1024
assert os.stat('/tmp').st_dev == os.stat('/packages').st_dev
try:
    Path('/tmp/overflow').write_bytes(b'z' * (30 * 1024 * 1024))
except OSError as error:
    assert error.errno == errno.ENOSPC
    Path('/tmp/overflow').unlink()
else:
    raise AssertionError('shared quota bypassed')
PY
""",
    )
    assert second["status"] == "succeeded", second
    assert runner.runtime_facts("ada", "123456", config)["scratch"]["allocated"]


async def test_same_human_handoff_keeps_scratch_but_not_job_access(sandbox):
    runner, _, context = sandbox
    runner.begin_turn(context, owner_id="human")
    first = await runner.run(context, command="printf receipt > /tmp/receipt")
    state = runner._scratch_turns[("ada", "123456", "turn-shell")]["namespace"]
    descriptors = [state.user_fd, state.mount_fd, state.data_fd]
    runner.finish_turn(context, preserve_for="human")
    replacement = ToolContext(context.bot, context.channel_id, "replacement")
    runner.begin_turn(replacement, owner_id="human")
    next_job = await runner.run(replacement, command="cat /tmp/receipt")
    assert next_job["status"] == "succeeded", next_job
    assert next_job["stdout"]["text"] == "receipt"
    from hortator.models import ControlError

    with pytest.raises(ControlError, match="another response cycle"):
        runner.read(replacement, first["job_id"])
    runner.finish_turn(replacement)
    for descriptor in descriptors:
        with pytest.raises(OSError) as error:
            os.fstat(descriptor)
        assert error.value.errno == errno.EBADF
    assert not state.path.exists()
    third = await runner.run(replacement, command="test ! -e /tmp/receipt")
    assert third["status"] == "succeeded", third


@pytest.mark.parametrize("owner,channel", [("other", "123456"), ("human", "different"), (None, "123456")])
async def test_scratch_does_not_transfer_to_other_people_or_conversations(sandbox, owner, channel):
    runner, _, context = sandbox
    runner.begin_turn(context, owner_id="human")
    first = await runner.run(context, command="printf private > /packages/private")
    assert first["status"] == "succeeded", first
    runner.finish_turn(context, preserve_for="human")
    replacement = ToolContext(context.bot, channel, "replacement")
    runner.begin_turn(replacement, owner_id=owner)
    next_job = await runner.run(replacement, command="test ! -e /packages/private")
    assert next_job["status"] == "succeeded", next_job
    assert not runner._scratch_pending


async def test_settings_change_replaces_old_scratch_and_shutdown_releases_all(sandbox):
    runner, _, context = sandbox
    first = await runner.run(context, command="printf private > /tmp/private")
    assert first["status"] == "succeeded", first
    state = runner._scratch_turns[("ada", "123456", "turn-shell")]["namespace"]
    second = await runner.run(
        context, configuration={"package_bytes": 67_108_864}, command="test ! -e /tmp/private"
    )
    assert second["status"] == "succeeded", second
    assert not state.path.exists()
    await runner.close()
    assert not runner._scratch_turns
    assert not list(runner.root.glob("scratch-*"))
