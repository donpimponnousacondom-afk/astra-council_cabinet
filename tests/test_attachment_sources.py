"""Direct attachments preserve storage grants and create an all-or-nothing selection."""

import asyncio
from dataclasses import replace

import pytest

from hortator.attachments import Attachments
from hortator.filesystem import MIB
from hortator.plugins import ToolContext
from support.agentic_runtime import grant
from conftest import configured


async def source_call(kernel, context, source, operation, **fields):
    result = await kernel.registry.call(source, {"operation": operation, **fields}, context, operation)
    assert result.get("ok") is not False, result
    return result


async def saved_file(kernel, context, path="report.md", content="saved report"):
    return await source_call(kernel, context, "filesystem", "write", path=path, content=content)


def saved_root(kernel, context):
    service = kernel.registry.filesystems
    row = service.lookup(context.bot["id"])
    return service.directory / row["id"] / row["generation"]


@pytest.mark.parametrize("path", ["report.md", "/workspace/report.md"])
async def test_saved_file_attaches_without_export_and_snapshot_survives_edit(kernel, path):
    context = grant(kernel, "filesystem")
    await saved_file(kernel, context)
    result = await Attachments(kernel.registry).prepare({"files": [path]}, context)
    assert result["ok"] is True
    artifact = kernel.registry.resolve_artifact(result["artifact_ids"][0], context)
    assert artifact.read_bytes() == b"saved report"
    await source_call(
        kernel, context, "filesystem", "write", path="report.md", content="changed", overwrite=True
    )
    assert artifact.read_bytes() == b"saved report"
    assert not kernel.store.rows("SELECT * FROM outbox")


async def test_workspace_and_pinned_document_revision_use_existing_sources(kernel):
    context = grant(kernel, "workspace", "document_site")
    await source_call(kernel, context, "workspace", "start", task="analysis")
    await source_call(
        kernel, context, "workspace", "write", task="analysis", path="result.txt", content="task result"
    )
    await source_call(kernel, context, "document_site", "create", site="briefing")
    first = await source_call(
        kernel, context, "document_site", "write", site="briefing", path="index.html", content="first"
    )
    await source_call(
        kernel, context, "document_site", "write", site="briefing", path="index.html", content="second"
    )
    result = await Attachments(kernel.registry).prepare(
        {
            "files": [
                {"source": "workspace", "task": "analysis", "path": "result.txt"},
                {
                    "source": "document_site",
                    "site": "briefing",
                    "path": "index.html",
                    "revision": first["revision"],
                },
            ]
        },
        context,
    )
    assert result["ok"] is True
    assert [
        kernel.registry.resolve_artifact(item, context).read_bytes() for item in result["artifact_ids"]
    ] == [
        b"task result",
        b"first",
    ]
    assert not kernel.store.rows("SELECT * FROM document_sync_queue")


async def test_generated_artifacts_and_saved_files_can_be_selected_together(kernel):
    context = grant(kernel, "filesystem")
    await saved_file(kernel, context)
    generated = kernel.registry.artifact(b"generated", "text/plain", ".txt", context)
    result = await Attachments(kernel.registry).prepare(
        {"files": ["report.md"], "artifact_ids": [generated["artifact_id"]]}, context
    )
    assert result["ok"] is True and len(result["artifact_ids"]) == 2
    assert generated["artifact_id"] in result["artifact_ids"]
    assert len(kernel.store.rows("SELECT * FROM artifacts")) == 2


@pytest.mark.parametrize("revoke", ["global", "bot"])
async def test_revoked_source_grant_rejects_saved_file(kernel, revoke):
    context = grant(kernel, "filesystem")
    await saved_file(kernel, context)
    if revoke == "global":
        plugin = kernel.store.get("plugins", "filesystem")
        kernel.store.put("plugins", {**plugin, "enabled": False})
    else:
        kernel.store.put("bots", {**context.bot, "enabled_plugins": []})
    result = await Attachments(kernel.registry).prepare({"files": ["report.md"]}, context)
    assert result["ok"] is False and result["executed"] is False
    assert "not enabled" in result["errors"][0]["message"]
    assert not kernel.store.rows("SELECT * FROM artifacts")
    assert (saved_root(kernel, context) / "report.md").read_bytes() == b"saved report"


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../report.md",
        "/workspace/../report.md",
        "/workspace//report.md",
        "nested/../report.md",
    ],
)
async def test_saved_paths_never_resolve_outside_bot_filesystem(kernel, path):
    context = grant(kernel, "filesystem")
    await saved_file(kernel, context)
    result = await Attachments(kernel.registry).prepare({"files": [path]}, context)
    assert result["ok"] is False and result["errors"][0]["path"].startswith("$.files[0]")
    assert not kernel.store.rows("SELECT * FROM artifacts")


async def test_other_bot_file_and_other_channel_task_are_not_accessible(kernel):
    context = grant(kernel, "filesystem", "workspace", "document_site")
    await saved_file(kernel, context)
    await source_call(kernel, context, "workspace", "start", task="private")
    await source_call(
        kernel, context, "workspace", "write", task="private", path="private.txt", content="private"
    )
    await source_call(kernel, context, "document_site", "create", site="private")
    await source_call(
        kernel, context, "document_site", "write", site="private", path="private.txt", content="private"
    )
    other_bot = configured(kernel, "socrates", enabled_plugins=["filesystem", "document_site"])
    foreign = ToolContext(other_bot, context.channel_id, context.turn_id)
    denied = await Attachments(kernel.registry).prepare(
        {"files": ["report.md", {"source": "document_site", "site": "private", "path": "private.txt"}]},
        foreign,
    )
    assert denied["ok"] is False and denied["error_count"] == 2
    other_channel = replace(context, channel_id="another-channel")
    denied = await Attachments(kernel.registry).prepare(
        {"files": [{"source": "workspace", "task": "private", "path": "private.txt"}]}, other_channel
    )
    assert denied["ok"] is False
    assert not kernel.store.rows("SELECT * FROM artifacts")


async def test_cross_turn_and_foreign_artifact_ids_are_all_reported(kernel):
    context = grant(kernel, "filesystem")
    foreign = replace(context, bot={**context.bot, "id": "another-bot"})
    previous = replace(context, turn_id="previous-turn")
    foreign_id = kernel.registry.artifact(b"foreign", "text/plain", ".txt", foreign)["artifact_id"]
    previous_id = kernel.registry.artifact(b"previous", "text/plain", ".txt", previous)["artifact_id"]
    result = await Attachments(kernel.registry).prepare(
        {"artifact_ids": [foreign_id, previous_id, "art_missing"]}, context
    )
    assert result["ok"] is False and result["error_count"] == 3
    assert {error["path"] for error in result["errors"]} == {
        "$.artifact_ids[0]",
        "$.artifact_ids[1]",
        "$.artifact_ids[2]",
    }
    assert len(kernel.store.rows("SELECT * FROM artifacts")) == 2


async def test_missing_artifact_bytes_are_reported_before_delivery(kernel):
    context = grant(kernel, "filesystem")
    artifact_id = kernel.registry.artifact(b"gone", "text/plain", ".txt", context)["artifact_id"]
    kernel.registry.resolve_artifact(artifact_id, context).unlink()
    result = await Attachments(kernel.registry).prepare({"artifact_ids": [artifact_id]}, context)
    assert result["ok"] is False and "data is missing" in result["errors"][0]["message"]


async def test_failure_aggregates_errors_and_removes_only_new_snapshots(kernel):
    context = grant(kernel, "filesystem")
    await saved_file(kernel, context)
    existing = kernel.registry.artifact(b"keep", "text/plain", ".txt", context)["artifact_id"]
    result = await Attachments(kernel.registry).prepare(
        {"files": ["report.md", "missing.md"], "artifact_ids": [existing, "art_bad"], "message": 9},
        context,
        errors=[{"path": "$.reply_to", "message": "Reply target is not in context"}],
    )
    assert result["ok"] is False and result["executed"] is False
    assert {error["path"] for error in result["errors"]} == {
        "$.reply_to",
        "$.message",
        "$.files[1]",
        "$.artifact_ids[1]",
    }
    assert result["error_count"] == 4
    assert [row["id"] for row in kernel.store.rows("SELECT * FROM artifacts")] == [existing]
    assert kernel.registry.resolve_artifact(existing, context).read_bytes() == b"keep"
    assert len(list(kernel.registry.directory.iterdir())) == 1


async def test_cancelled_selection_discards_its_snapshots_and_propagates(kernel, monkeypatch):
    context = grant(kernel, "filesystem")
    await saved_file(kernel, context)
    original = kernel.registry.call

    async def interrupt(name, args, context, call_id):
        if args.get("path") == "cancel.md":
            raise asyncio.CancelledError
        return await original(name, args, context, call_id)

    monkeypatch.setattr(kernel.registry, "call", interrupt)
    with pytest.raises(asyncio.CancelledError):
        await Attachments(kernel.registry).prepare({"files": ["report.md", "cancel.md"]}, context)
    assert not kernel.store.rows("SELECT * FROM artifacts")
    assert not list(kernel.registry.directory.iterdir())


async def test_source_revoked_during_preparation_discards_all_snapshots(kernel, monkeypatch):
    context = grant(kernel, "filesystem")
    await saved_file(kernel, context)
    original = kernel.registry.call

    async def revoke_after_export(name, args, context, call_id):
        result = await original(name, args, context, call_id)
        kernel.store.put("bots", {**context.bot, "enabled_plugins": []})
        return result

    monkeypatch.setattr(kernel.registry, "call", revoke_after_export)
    result = await Attachments(kernel.registry).prepare({"files": ["report.md"]}, context)
    assert result["ok"] is False and "disabled before" in result["errors"][0]["message"]
    assert not kernel.store.rows("SELECT * FROM artifacts")


async def test_attachment_ceiling_is_enforced_by_existing_file_export(kernel):
    context = grant(kernel, "filesystem")
    await saved_file(kernel, context)
    with (saved_root(kernel, context) / "large.bin").open("wb") as handle:
        handle.truncate(8_000_001)
    result = await Attachments(kernel.registry).prepare({"files": ["large.bin"]}, context)
    assert result["ok"] is False and "attachment delivery limit" in result["errors"][0]["message"]
    assert not kernel.store.rows("SELECT * FROM artifacts")


async def test_lowered_bot_storage_limit_is_read_from_current_configuration(kernel):
    context = grant(kernel, "filesystem")
    await saved_file(kernel, context)
    with (saved_root(kernel, context) / "large.bin").open("wb") as handle:
        handle.truncate(11 * MIB)
    kernel.store.put("bots", {**context.bot, "plugin_config": {"filesystem": {"max_bytes": 20 * MIB}}})
    result = await Attachments(kernel.registry).prepare({"files": ["large.bin"]}, context)
    assert result["ok"] is False and str(10 * MIB) in result["errors"][0]["message"]
    assert not kernel.store.rows("SELECT * FROM artifacts")


@pytest.mark.parametrize("args", [{}, {"files": []}, {"artifact_ids": []}, {"message": "No files"}])
async def test_empty_selection_is_not_a_send(kernel, args):
    context = grant(kernel, "filesystem")
    result = await Attachments(kernel.registry).prepare(args, context)
    assert result["ok"] is False and result["executed"] is False
    assert not kernel.store.rows("SELECT * FROM artifacts")


async def test_combined_attachment_limit_rejects_without_snapshot_side_effect(kernel):
    context = grant(kernel, "filesystem")
    await saved_file(kernel, context)
    result = await Attachments(kernel.registry).prepare(
        {"files": ["report.md"] * 4, "artifact_ids": ["art_missing"]}, context
    )
    assert result["ok"] is False and "combined" in result["errors"][0]["message"]
    assert not kernel.store.rows("SELECT * FROM artifacts")
