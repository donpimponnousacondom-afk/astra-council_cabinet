"""Capacity-derived limits, old-config migration and large saved shell files."""

import json

import pytest

from hortator.app import Kernel
from hortator.filesystem import CONFIG_SCHEMA, DEFAULTS, MIB, storage_config, validate_config
from hortator.models import ControlError
from hortator.workspaces import validate_config as workspace_config
from support.agentic_runtime import grant
from test_filesystem import call, require_runner, shell


@pytest.mark.parametrize("capacity", [20 * MIB, 20 * MIB + 1, 256 * MIB, 512 * MIB, 1024 * MIB])
def test_file_limit_tracks_half_capacity(capacity):
    config = validate_config({"max_bytes": capacity})
    assert storage_config(config)["max_file_bytes"] == capacity // 2
    assert "max_file_bytes" not in config
    assert "max_file_bytes" not in CONFIG_SCHEMA["properties"]
    assert "max_file_bytes" not in DEFAULTS


def test_retired_setting_rejected_and_private_workspace_limit_unchanged():
    with pytest.raises(ControlError, match="automatic"):
        validate_config({"max_file_bytes": 32 * MIB})
    assert workspace_config()["max_file_bytes"] == 32 * MIB
    with pytest.raises(ControlError, match="Invalid workspace"):
        workspace_config({"max_file_bytes": 128 * MIB, "max_workspace_bytes": 256 * MIB})


async def test_effective_limits_follow_global_and_bot_capacity_and_recheck_on_save(kernel, owner, tmp_path):
    context = grant(kernel, "filesystem")
    await kernel.service.save(owner, "plugins", "filesystem", {"config": {"max_bytes": 512 * MIB}})
    filesystem = kernel.registry.filesystems
    assert filesystem.usage(context.bot["id"])["max_file_bytes"] == 256 * MIB
    inherited = filesystem.prepare_job(context)
    assert inherited["max_file_bytes"] == 256 * MIB
    filesystem.release_job(inherited["workspace_id"])
    await kernel.service.save(
        owner, "bots", context.bot["id"], {"plugin_config": {"filesystem": {"max_bytes": 256 * MIB}}}
    )
    await call(kernel, context, "write", path="note.txt", content="keep")
    prepared = filesystem.prepare_job(context)
    assert prepared["max_file_bytes"] == 128 * MIB
    assert filesystem.validate_storage_config(filesystem.effective_config(context.bot["id"]))
    with pytest.raises(ControlError, match="derive from capacity"):
        filesystem.validate_storage_config(
            {**filesystem.effective_config(context.bot["id"]), "max_file_bytes": MIB}
        )
    stage = tmp_path / "staged"
    stage.mkdir()
    (stage / "note.txt").write_text("changed")
    with (stage / "big.bin").open("wb") as handle:
        handle.truncate(11 * MIB)
    # Represent a capacity change while the job is owned, without cancellation
    # masking the final current-limit recheck being exercised here.
    bot = kernel.store.get("bots", context.bot["id"])
    bot["plugin_config"]["filesystem"]["max_bytes"] = 20 * MIB
    kernel.store.put("bots", bot)
    try:
        with pytest.raises(ControlError, match="exceeds"):
            filesystem.finish_job(prepared["workspace_id"], stage)
    finally:
        filesystem.release_job(prepared["workspace_id"])
    assert (await call(kernel, context, "read", path="note.txt"))["content"] == "keep"
    assert filesystem.usage(context.bot["id"])["max_file_bytes"] == 10 * MIB


async def test_half_size_boundary_and_total_capacity_are_both_enforced(kernel, owner, tmp_path):
    context = grant(kernel, "filesystem")
    await kernel.service.save(owner, "plugins", "filesystem", {"config": {"max_bytes": 20 * MIB}})
    filesystem = kernel.registry.filesystems
    staged = tmp_path / "staged-boundaries"
    staged.mkdir()
    for name in ("first.bin", "second.bin"):
        with (staged / name).open("wb") as handle:
            handle.truncate(10 * MIB)
    prepared = filesystem.prepare_job(context)
    try:
        saved = filesystem.finish_job(prepared["workspace_id"], staged)
        assert saved["bytes"] == 20 * MIB
    finally:
        filesystem.release_job(prepared["workspace_id"])
    # Two half-capacity files may fill the quota; half is not reserved space.
    prepared = filesystem.prepare_job(context)
    try:
        (staged / "extra.bin").write_bytes(b"x")
        with pytest.raises(ControlError, match="Workspace exceeds"):
            filesystem.finish_job(prepared["workspace_id"], staged)
        (staged / "extra.bin").unlink()
        (staged / "second.bin").unlink()
        with (staged / "first.bin").open("ab") as handle:
            handle.write(b"x")
        with pytest.raises(ControlError, match="exceeds"):
            filesystem.finish_job(prepared["workspace_id"], staged)
    finally:
        filesystem.release_job(prepared["workspace_id"])
    assert filesystem.usage(context.bot["id"])["used_bytes"] == 20 * MIB
    assert (await call(kernel, context, "stat", path="second.bin"))["bytes"] == 10 * MIB


def seed_legacy(kernel):
    plugin = kernel.store.get("plugins", "filesystem")
    plugin["config"].update(max_bytes=512 * MIB, max_file_bytes=32 * MIB)
    kernel.store.put("plugins", plugin)
    bot = kernel.store.get("bots", "ada")
    bot["plugin_config"].update(
        filesystem={"max_bytes": 256 * MIB, "max_file_bytes": 64 * MIB},
        workspace={"max_file_bytes": 32 * MIB},
    )
    kernel.store.put("bots", bot)
    return kernel.store.get("plugins", "filesystem"), kernel.store.get("bots", "ada")


def receipts(kernel):
    return [
        json.loads(row["data"])
        for row in kernel.store.rows("SELECT data FROM events WHERE kind='config.migrated' ORDER BY seq")
        if "filesystem" in json.loads(row["data"]).get("reason", "")
    ]


async def test_migration_preserves_files_other_configuration_and_is_idempotent(tmp_path, owner):
    first = Kernel(tmp_path)
    async with first.lifetime():
        context = grant(first, "filesystem")
        await call(first, context, "write", path="note.txt", content="unchanged")
        filesystem_rows = first.store.rows("SELECT * FROM filesystems")
        old_plugin, old_bot = seed_legacy(first)
    second = Kernel(tmp_path)
    async with second.lifetime():
        plugin, bot = second.store.get("plugins", "filesystem"), second.store.get("bots", "ada")
        for old, new, field in ((old_plugin, plugin, "config"), (old_bot, bot, "plugin_config")):
            assert new["revision"] == old["revision"] + 1
            assert {k: v for k, v in new.items() if k not in (field, "revision")} == {
                k: v for k, v in old.items() if k not in (field, "revision")
            }
        assert plugin["config"] == {k: v for k, v in old_plugin["config"].items() if k != "max_file_bytes"}
        assert bot["plugin_config"] == {
            "filesystem": {"max_bytes": 256 * MIB},
            "workspace": {"max_file_bytes": 32 * MIB},
        }
        assert second.store.rows("SELECT * FROM filesystems") == filesystem_rows
        assert second.registry.filesystems.read_file("ada", None, None, "note.txt")["content"] == "unchanged"
        assert second.registry.filesystems.effective_config("ada")["max_file_bytes"] == 128 * MIB
        audit = receipts(second)
        assert len(audit) == 2
        second.service.migrate_filesystem_file_limit()
        assert receipts(second) == audit
        assert second.store.get("bots", "ada") == bot
        await second.service.save(owner, "plugins", "filesystem", {"name": "Saved files"})
        with pytest.raises(ControlError, match="automatic"):
            await second.service.save(
                owner, "bots", "ada", {"plugin_config": {"filesystem": {"max_file_bytes": 1}}}
            )


def test_migration_rolls_back_records_and_receipts_together(kernel, monkeypatch):
    seed_legacy(kernel)
    entities = kernel.store.rows("SELECT * FROM entities ORDER BY kind,id")
    events = kernel.store.rows("SELECT * FROM events ORDER BY seq")
    emit = kernel.store.emit
    count = 0

    def fail_second(kind, *args, **kwargs):
        nonlocal count
        if kind == "config.migrated":
            count += 1
            if count == 2:
                raise RuntimeError("audit failure")
        return emit(kind, *args, **kwargs)

    monkeypatch.setattr(kernel.store, "emit", fail_second)
    with pytest.raises(RuntimeError, match="audit failure"):
        kernel.service.migrate_filesystem_file_limit()
    assert count == 2
    assert kernel.store.rows("SELECT * FROM entities ORDER BY kind,id") == entities
    assert kernel.store.rows("SELECT * FROM events ORDER BY seq") == events


async def test_real_shell_saves_104_mib_binary_and_rejects_over_half_without_losing_it(kernel):
    context = grant(kernel, "filesystem", "shell")
    await require_runner(kernel)
    result = await shell(kernel, context, "truncate -s 109051904 engine.bin; printf kept > note.txt")
    assert result["workspace_committed"] and result["status"] == "succeeded", result
    assert result["usage"]["max_file_bytes"] == 128 * MIB
    stat = await call(kernel, context, "stat", path="engine.bin")
    assert stat["bytes"] == 104 * MIB
    page = await call(kernel, context, "read", path="engine.bin", encoding="base64", limit_bytes=4)
    assert page["content"] == "AAAAAA==" and page["next_offset"] == 4
    bad = await shell(kernel, context, "truncate -s 134217729 engine.bin; printf wrong > note.txt")
    assert not bad["workspace_committed"] and bad["status"] == "failed", bad
    assert (await call(kernel, context, "stat", path="engine.bin"))["bytes"] == 104 * MIB
    assert (await call(kernel, context, "read", path="note.txt"))["content"] == "kept"
