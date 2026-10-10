"""Simple filesystem contracts: real files, grants, shell mounts and recovery."""

import asyncio
import hashlib
import json
import os

import pytest
from jsonschema import Draft202012Validator

from hortator.filesystem import Filesystems, MIB, validate_config
from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.tool_feedback import full_schema
from support.agentic_runtime import grant


async def call(kernel, context, operation, **fields):
    return await kernel.registry.call("filesystem", {"operation": operation, **fields}, context, operation)


async def shell(kernel, context, command, **fields):
    return await kernel.registry.call(
        "shell", {"operation": "run", "command": command, **fields}, context, "shell"
    )


async def require_runner(kernel):
    runner = kernel.registry.agentic.runner
    ready = await runner.readiness()
    if not ready["ready"]:
        if os.environ.get("HORTATOR_REQUIRE_SANDBOX") == "1":
            pytest.fail(ready["error"])
        pytest.skip(ready["error"])
    return runner


async def test_discovery_single_directory_schema_and_complete_errors(kernel):
    assert not kernel.store.get("plugins", "filesystem")["enabled"]
    context = grant(kernel, "filesystem", "shell")
    result = await kernel.registry.call("filesystem", {}, context, "help")
    assert result["usage_only"] and not result["executed"]
    schema = result["usage"]["parameters"]
    assert "task" not in schema["properties"] and "start" not in schema["properties"]["operation"]["enum"]
    assert not kernel.store.rows("SELECT * FROM filesystems")
    assert "organized" not in result["usage"]["description"]
    errors = await call(kernel, context, "write", content=5, unexpected="x")
    assert errors["executed"] is False and len(errors["errors"]) >= 3
    assert not kernel.store.rows("SELECT * FROM filesystems")
    parameters = full_schema(kernel.registry.spec_for("shell", context).parameters)
    assert "task" not in parameters["properties"]
    validator = Draft202012Validator(parameters)
    assert validator.is_valid({"operation": "run", "command": "pwd"})
    assert not validator.is_valid({"operation": "run", "command": "pwd", "task": "invented"})


async def test_files_shared_across_channels_rename_and_reconstruction_but_not_bots(kernel):
    context = grant(kernel, "filesystem")
    assert (await call(kernel, context, "write", path="notes/USAGE.md", content="saved"))["bytes"] == 5
    other_channel = ToolContext(context.bot, "other-channel", "another-turn")
    assert (await call(kernel, other_channel, "read", path="notes/USAGE.md"))["content"] == "saved"
    updated = {**context.bot, "name": "Renamed bot"}
    kernel.store.put("bots", updated)
    renamed = ToolContext(updated, context.channel_id, context.turn_id)
    assert (await call(kernel, renamed, "read", path="notes/USAGE.md"))["content"] == "saved"
    filesystem = kernel.registry.filesystems
    reopened = Filesystems(kernel.store, filesystem.directory.parent, kernel.vault)
    assert reopened.read_file(context.bot["id"], None, None, "notes/USAGE.md")["content"] == "saved"
    assert reopened.prune_expired(now=10**15)["removed"] == []
    foreign_bot = {**context.bot, "id": "another-bot"}
    kernel.store.put("bots", foreign_bot)
    foreign = ToolContext(foreign_bot, context.channel_id, context.turn_id)
    assert (await call(kernel, foreign, "list"))["files"] == []
    assert (await call(kernel, foreign, "read", path="notes/USAGE.md"))["ok"] is False
    assert not kernel.store.rows("SELECT * FROM workspaces")


async def test_directory_paging_edit_move_delete_and_usage(kernel):
    context = grant(kernel, "filesystem")
    await call(kernel, context, "mkdir", path="notes")
    for name in ("a", "b", "c"):
        await call(kernel, context, "write", path=f"notes/{name}.txt", content=name)
    first = await call(kernel, context, "list", path="notes", limit=2)
    assert [item["path"] for item in first["files"]] == ["notes/a.txt", "notes/b.txt"]
    second = await kernel.registry.call("filesystem", first["next"], context, "page")
    assert [item["path"] for item in second["files"]] == ["notes/c.txt"] and second["next"] is None
    changed = await call(kernel, context, "edit", path="notes/a.txt", old_text="a", new_text="updated")
    assert changed["usage"]["used_bytes"] == 9
    stale = await call(
        kernel,
        context,
        "edit",
        path="notes/a.txt",
        old_text="updated",
        new_text="wrong",
        expected_sha256="0" * 64,
    )
    assert stale["ok"] is False
    await call(kernel, context, "move", path="notes/a.txt", destination="moved.txt")
    assert (await call(kernel, context, "read", path="moved.txt"))["content"] == "updated"
    await call(kernel, context, "delete", path="notes", recursive=True)
    assert (await call(kernel, context, "status"))["usage"]["used_bytes"] == 7
    assert (await call(kernel, context, "delete", path="."))["ok"] is False
    await call(kernel, context, "delete", path="moved.txt")
    status = await call(kernel, context, "status")
    assert status["usage"]["display"] == "0/256 MiB (0%)"
    usage = kernel.registry.filesystems.usage(context.bot["id"], {"bytes": 182 * MIB, "entries": 6})
    assert usage["display"] == "182/256 MiB (71%)" and type(usage["percent"]) is int


async def test_cleanup_over_entry_quota_and_imported_originals_remain_protected(kernel):
    context = grant(kernel, "filesystem")
    for name in ("a", "b", "c"):
        await call(kernel, context, "write", path=name, content=name)
    plugin = kernel.store.get("plugins", "filesystem")
    kernel.store.put("plugins", {**plugin, "config": {**plugin["config"], "max_files": 1}})
    assert (await call(kernel, context, "write", path="d", content="d"))["ok"] is False
    assert len((await call(kernel, context, "list"))["files"]) == 3
    assert (await call(kernel, context, "delete", path="a"))["freed_bytes"] == 1
    assert (await call(kernel, context, "delete", path="b"))["freed_bytes"] == 1
    row = kernel.registry.filesystems.lookup(context.bot["id"])
    kernel.store.execute(
        "INSERT INTO filesystem_imports VALUES(?,?,?,?,?)",
        (row["id"], "c", hashlib.sha256(b"c").hexdigest(), "1", "2"),
    )
    for operation, fields in (
        ("delete", {}),
        ("move", {"destination": "new"}),
        ("write", {"content": "different", "overwrite": True}),
    ):
        result = await call(kernel, context, operation, path="c", **fields)
        assert result["ok"] is False and "protected" in result["error"]


async def test_attachment_intake_still_uses_actual_channel(kernel):
    context = grant(kernel, "filesystem")
    kernel.store.ingest(
        discord_id="123",
        channel_id="different-channel",
        room_id="council",
        author_id="1",
        author_name="Human",
        content="attachment",
        attachments=[
            {
                "id": "456",
                "filename": "note.txt",
                "url": "https://cdn.discordapp.com/attachments/1/2/note.txt",
            }
        ],
    )
    result = await call(
        kernel, context, "import_attachment", path="note.txt", message_id="123", attachment_id="456"
    )
    assert result["ok"] is False and "authorized channel" in result["error"]


@pytest.mark.parametrize("global_disable", [False, True])
async def test_real_shell_mount_disable_reenable_and_transitive_evidence(kernel, global_disable):
    context = grant(kernel, "filesystem", "shell", "web_fetch")
    await require_runner(kernel)
    await call(kernel, context, "write", path="note.txt", content="from-file-tool")
    result = await shell(kernel, context, "cat note.txt; printf from-shell > other.txt")
    assert result["storage"] == "filesystem" and result["workspace_committed"], result
    assert result["stdout"]["text"] == "from-file-tool"
    assert (await call(kernel, context, "read", path="other.txt"))["content"] == "from-shell"
    page = await kernel.registry.call(
        "web_fetch", {"operation": "read_result", "result_id": result["result_id"]}, context, "page"
    )
    if global_disable:
        entity = kernel.store.get("plugins", "filesystem")
        kernel.store.put("plugins", {**entity, "enabled": False})
    else:
        entity = kernel.store.get("bots", context.bot["id"])
        kernel.store.put("bots", {**entity, "enabled_plugins": ["shell", "web_fetch"]})
    assert not kernel.registry.allowed("filesystem", context)
    assert (await kernel.registry.call("filesystem", {}, context, "help-off"))["ok"] is False
    facts = kernel.registry.filesystems.mount_facts(context.bot)
    assert not facts["mounted"] and "still exist" in facts["notice"] and "owner" in facts["notice"]
    fresh = await shell(
        kernel,
        context,
        "test ! -e note.txt; test ! -e other.txt; echo scratch > temporary.txt; echo unmounted",
    )
    assert fresh["stdout"]["text"] == "unmounted\n" and fresh["storage"] == "disposable"
    for identifier in (result["result_id"], page["result_id"]):
        denied = await kernel.registry.call(
            "web_fetch", {"operation": "read_result", "result_id": identifier}, context, "denied"
        )
        assert denied["ok"] is False and "grant" in denied["error"]
    for operation in ("status", "read", "cancel"):
        denied = await kernel.registry.call(
            "shell", {"operation": operation, "job_id": result["job_id"]}, context, "denied"
        )
        assert denied["ok"] is False
    kernel.store.put("plugins" if global_disable else "bots", entity)
    restored = await shell(kernel, context, "test ! -e temporary.txt; cat note.txt other.txt")
    assert restored["stdout"]["text"] == "from-file-toolfrom-shell" and restored["workspace_committed"]


async def test_inflight_disable_discards_changes_and_busy_scope_is_per_bot(kernel):
    context = grant(kernel, "filesystem", "shell")
    runner = await require_runner(kernel)
    await call(kernel, context, "write", path="note.txt", content="before")
    async with asyncio.TaskGroup() as group:
        pending = group.create_task(shell(kernel, context, "echo changed > note.txt; sleep 0.4"))
        async with asyncio.timeout(10):
            while not runner.active or not kernel.registry.filesystems._active:
                await asyncio.sleep(0.01)
        elsewhere = ToolContext(context.bot, "another-channel", "another-turn")
        busy = await call(kernel, elsewhere, "read", path="note.txt")
        assert busy["ok"] is False and "busy" in busy["error"]
        plugin = kernel.store.get("plugins", "filesystem")
        kernel.store.put("plugins", {**plugin, "enabled": False})
    assert pending.result()["ok"] is False
    assert (
        kernel.registry.filesystems.read_file(context.bot["id"], None, None, "note.txt")["content"]
        == "before"
    )
    assert not runner.active and not kernel.registry.filesystems._active


async def test_coexistence_explicit_private_task_and_default_simple_filesystem(kernel):
    context = grant(kernel, "filesystem", "workspace", "shell")
    await require_runner(kernel)
    await kernel.registry.call("workspace", {"operation": "start", "task": "legacy"}, context, "start")
    old = await shell(kernel, context, "echo old > legacy.txt", task="legacy")
    new = await shell(kernel, context, "test ! -e legacy.txt; echo new > simple.txt")
    assert old["storage"] == "workspace" and new["storage"] == "filesystem"
    assert (await shell(kernel, context, "test ! -e simple.txt; cat legacy.txt", task="legacy"))["stdout"][
        "text"
    ] == "old\n"


async def test_runtime_notice_and_usage_are_tail_facts_and_do_not_change_schema(kernel):
    context = grant(kernel, "filesystem", "shell")
    profile = kernel.store.get("profiles", context.bot["model_profile_id"])
    initial_schema = kernel.registry.spec_for("filesystem", context).parameters
    await call(kernel, context, "write", path="notes.txt", content="saved")
    layers = kernel.engine.contexts.dynamic_layers(context.bot, profile, context.channel_id, 0, 100)
    text = json.dumps(layers)
    assert "filesystem_mount" in text and "0/256 MiB (0%)" in text
    assert "same directory" in text
    assert kernel.registry.spec_for("filesystem", context).parameters == initial_schema
    plugin = kernel.store.get("plugins", "filesystem")
    kernel.store.put("plugins", {**plugin, "enabled": False})
    text = json.dumps(kernel.engine.contexts.dynamic_layers(context.bot, profile, context.channel_id, 1, 100))
    assert "still exist" in text and "cannot access" in text


async def test_invalid_shell_snapshot_and_timeout_preserve_last_saved_files(kernel):
    context = grant(kernel, "filesystem", "shell")
    await require_runner(kernel)
    await call(kernel, context, "write", path="note", content="original")
    bad = await shell(kernel, context, "echo wrong > note; ln -s note link")
    assert bad["status"] == "failed" and not bad["workspace_committed"]
    assert "symlink" in bad["error"]
    timeout = await shell(kernel, context, "echo wrong > note; sleep 5", timeout_seconds=0.1)
    assert timeout["status"] == "timed_out" and not timeout["workspace_committed"]
    assert (await call(kernel, context, "read", path="note"))["content"] == "original"
    assert not list(kernel.registry.agentic.runner.root.glob("job_*/.execution"))


async def test_export_registers_current_turn_artifact_without_sending(kernel):
    context = grant(kernel, "filesystem")
    await call(kernel, context, "write", path="output.txt", content="result")
    exported = await call(kernel, context, "export", path="output.txt")
    assert exported["artifact_id"] and exported["turn_id"] == context.turn_id
    assert "not yet sent" in exported["delivery"]


async def test_full_snapshot_contains_filesystem_bytes_and_database(kernel):
    from hortator.snapshots import Snapshots, schema

    context = grant(kernel, "filesystem")
    await call(kernel, context, "write", path="notes.txt", content="saved filesystem")
    filesystem = kernel.registry.filesystems
    row = filesystem.lookup(context.bot["id"])
    manager = Snapshots(filesystem.directory.parent, {"commit": "a" * 40, "dirty": False})
    fingerprint = schema(kernel.store.db)
    manifest = manager.capture("Filesystem fixture")
    relative = f"filesystems/{row['id']}/{row['generation']}/notes.txt"
    assert manifest["files"][relative]["sha256"] == hashlib.sha256(b"saved filesystem").hexdigest()
    assert (manager.path(manifest["id"]) / "data" / relative).read_text() == "saved filesystem"
    assert manager.verify(manifest["id"], fingerprint)["id"] == manifest["id"]


def test_configuration_rejects_unknown_and_inconsistent_limits():
    assert validate_config()["max_bytes"] == 256 * MIB
    for config in ({"max_bytes": True}, {"retention_days": 1}, {"max_bytes": 20 * MIB}):
        with pytest.raises(ControlError):
            validate_config(config)
