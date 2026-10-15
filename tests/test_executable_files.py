"""Executable intent survives saved-file operations and real shell boundaries."""

import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from hortator import shell_worker
from hortator.models import ControlError
from support.agentic_runtime import grant
from test_filesystem import require_runner


@pytest.fixture(params=["filesystem", "workspace"])
async def saved(kernel, request):
    plugin = request.param
    context = grant(kernel, plugin, "shell")
    scope = {"task": "executables"} if plugin == "workspace" else {}
    backend = kernel.registry.workspaces if scope else kernel.registry.filesystems

    async def file(operation, **fields):
        return await kernel.registry.call(
            plugin, {"operation": operation, **scope, **fields}, context, operation
        )

    async def shell(command, **fields):
        return await kernel.registry.call(
            "shell", {"operation": "run", "command": command, **scope, **fields}, context, "shell"
        )

    def root():
        row = backend._workspace(context.bot["id"], context.channel_id, scope.get("task"))
        return backend.directory / row["id"] / row["generation"]

    if scope:
        await file("start")
    await file("mkdir", path="nested")
    return SimpleNamespace(
        file=file, shell=shell, root=root, backend=backend, context=context, task=scope.get("task")
    )


@pytest.mark.parametrize("source_mode", [0o600, 0o700, 0o777, 0o6711, 0o611])
async def test_commit_and_file_tools_preserve_only_owner_execute(saved, tmp_path, source_mode):
    await saved.file("write", path="script", content="old")
    prepared = saved.backend.prepare_job(saved.context, saved.task)
    stage = tmp_path / "stage"
    stage.mkdir()
    source = stage / "script"
    source.write_text("first")
    source.chmod(source_mode)
    try:
        saved.backend.finish_job(prepared["workspace_id"], stage)
    finally:
        saved.backend.release_job(prepared["workspace_id"])
    expected = 0o600 | (source_mode & 0o100)
    executable = bool(source_mode & 0o100)
    assert stat.S_IMODE((saved.root() / "script").stat().st_mode) == expected
    assert (await saved.file("stat", path="script"))["executable"] is executable
    assert (await saved.file("read", path="script"))["executable"] is executable
    assert (await saved.file("list", path="."))["files"][0]["executable"] is executable
    edited = await saved.file("edit", path="script", old_text="first", new_text="second")
    assert edited["executable"] is executable
    overwritten = await saved.file("write", path="script", content="third", overwrite=True)
    assert overwritten["executable"] is executable
    assert stat.S_IMODE((saved.root() / "script").stat().st_mode) == expected
    fresh = await saved.file("write", path="fresh", content="new")
    assert fresh["executable"] is False
    assert stat.S_IMODE((saved.root() / "fresh").stat().st_mode) == 0o600


async def test_permission_change_during_validation_keeps_previous_generation(saved, tmp_path, monkeypatch):
    await saved.file("write", path="script", content="old")
    original_root = saved.root()
    prepared = saved.backend.prepare_job(saved.context, saved.task)
    stage = tmp_path / "changing-stage"
    stage.mkdir()
    (stage / "script").write_text("new")
    (stage / "script").chmod(0o700)
    metadata = saved.backend._metadata

    def change_after_inspection(descriptor, path, info):
        result = metadata(descriptor, path, info)
        os.fchmod(descriptor, 0o600)
        return result

    monkeypatch.setattr(saved.backend, "_metadata", change_after_inspection)
    try:
        with pytest.raises(ControlError, match="changed during snapshot validation"):
            saved.backend.finish_job(prepared["workspace_id"], stage)
    finally:
        saved.backend.release_job(prepared["workspace_id"])
    assert saved.root() == original_root
    assert (original_root / "script").read_text() == "old"
    assert stat.S_IMODE((original_root / "script").stat().st_mode) == 0o600


async def test_real_binary_and_script_survive_calls_edits_and_removal(kernel, saved):
    await require_runner(kernel)
    first = await saved.shell(
        "cp /usr/bin/true native; printf '#!/bin/sh\\nprintf first' > script; "
        "chmod 4755 native; chmod 711 script; ./native && ./script"
    )
    assert first["status"] == "succeeded" and first["workspace_committed"], first
    assert first["stdout"]["text"] == "first"
    assert stat.S_IMODE((saved.root() / "native").stat().st_mode) == 0o700
    second = await saved.shell("./native && ./script")
    assert second["status"] == "succeeded" and second["stdout"]["text"] == "first", second
    await saved.file("edit", path="script", old_text="first", new_text="second")
    third = await saved.shell("./native && ./script; chmod u-x native; exit 7")
    assert third["status"] == "failed" and third["exit_code"] == 7 and third["workspace_committed"]
    assert third["stdout"]["text"] == "second"
    assert (await saved.file("stat", path="native"))["executable"] is False
    fourth = await saved.shell("./script; ./native")
    assert fourth["exit_code"] == 126 and "Permission denied" in fourth["stderr"]["text"], fourth
    assert fourth["stdout"]["text"] == "second"


@pytest.mark.parametrize("failure", ["invalid_tree", "timeout"])
async def test_real_failed_save_does_not_persist_chmod(kernel, saved, failure):
    await require_runner(kernel)
    await saved.file("write", path="script", content="original")
    command = "chmod u+x script; " + (
        "ln -s script invalid-link" if failure == "invalid_tree" else "sleep 10"
    )
    result = await saved.shell(command, **({"timeout_seconds": 1} if failure == "timeout" else {}))
    assert not result["workspace_committed"], result
    assert (await saved.file("stat", path="script"))["executable"] is False
    assert stat.S_IMODE((saved.root() / "script").stat().st_mode) == 0o600


@pytest.mark.parametrize("flag", [None, 1, "true", "missing"])
async def test_real_transport_rejects_invalid_executable_flags(kernel, tmp_path, monkeypatch, flag):
    await require_runner(kernel)
    context = grant(kernel, "filesystem", "shell")
    runner = kernel.registry.agentic.runner
    frame = {"type": "file", "path": "injected", "size": 0}
    if flag != "missing":
        frame["executable"] = flag
    frames = [
        {"type": "result", "status": "succeeded", "exit_code": 0, "elapsed_seconds": 0, "cwd": "."},
        frame,
        {"type": "endfile"},
        {"type": "complete"},
    ]
    worker = tmp_path / "invalid-worker.py"
    worker.write_text("print(" + repr("\n".join(json.dumps(frame) for frame in frames)) + ", flush=True)\n")
    original_command = runner._command

    def command(*args):
        value = original_command(*args)
        value[value.index(str(Path(shell_worker.__file__)))] = str(worker)
        return value

    monkeypatch.setattr(runner, "_command", command)
    result = await kernel.registry.call("shell", {"operation": "run", "command": "true"}, context, "bad")
    assert result["status"] == "failed" and not result["workspace_committed"], result
    assert "Invalid workspace executable flag" in result["error"]
    assert kernel.registry.filesystems.usage(context.bot["id"])["used_bytes"] == 0
