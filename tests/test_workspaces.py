import base64
import hashlib
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.store import Store
from hortator.tool_feedback import feedback
from hortator.vision import MAX_IMAGE_BYTES, validate_image
from hortator.workspaces import (
    DEFAULTS,
    DESCRIPTION,
    EXPORT_LIMIT,
    PARAMETERS,
    Workspaces,
    safe_path,
    validate_config,
)


@pytest.fixture
def workspaces(tmp_path):
    store = Store(tmp_path / "council.sqlite3")
    vault = SimpleNamespace(
        redact=lambda value: value.replace("test-secret", "[REDACTED]") if isinstance(value, str) else value
    )

    def artifact(data, mime, suffix, context):
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir(exist_ok=True)
        artifact_id = "art_" + str(len(store.rows("SELECT * FROM artifacts")))
        (artifacts / (artifact_id + suffix)).write_bytes(data)
        store.execute(
            "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
            (artifact_id, context.bot["id"], context.turn_id, artifact_id + suffix, mime, len(data), 1),
        )
        return {"artifact_id": artifact_id, "mime": mime, "bytes": len(data)}

    service = Workspaces(store, tmp_path, vault, artifact=artifact)
    yield service
    store.close()


@pytest.fixture
def context():
    return ToolContext({"id": "ada"}, "123", "turn-files")


async def call(workspaces, context, operation, config=None, **args):
    return await workspaces.call({"operation": operation, **args}, context, config)


async def test_durable_scopes_atomic_write_edit_and_current_turn_export(workspaces, context):
    started = await call(workspaces, context, "start", task="notes")
    assert started["workspace_task_started"]
    first = await call(
        workspaces, context, "write", task="notes", path="notes/log.txt", content="first line\nsecond line\n"
    )
    assert first["bytes"] == 23
    assert first["sha256"] == hashlib.sha256(b"first line\nsecond line\n").hexdigest()
    with pytest.raises(ControlError, match="overwrite"):
        await call(workspaces, context, "write", task="notes", path="notes/log.txt", content="no")
    with pytest.raises(ControlError, match="changed"):
        await call(
            workspaces,
            context,
            "edit",
            task="notes",
            path="notes/log.txt",
            old_text="first",
            new_text="new",
            expected_sha256="0" * 64,
        )
    await call(
        workspaces,
        context,
        "edit",
        task="notes",
        path="notes/log.txt",
        old_text="first",
        new_text="new",
        expected_sha256=first["sha256"],
    )
    assert (await call(workspaces, context, "read", task="notes", path="notes/log.txt"))[
        "content"
    ] == "new line\nsecond line\n"
    for foreign in (
        ToolContext({"id": "curie"}, context.channel_id, context.turn_id),
        ToolContext(context.bot, "456", context.turn_id),
    ):
        assert (await call(workspaces, foreign, "list"))["workspaces"] == []
        with pytest.raises(ControlError, match="not found"):
            await call(workspaces, foreign, "read", task="notes", path="notes/log.txt")
    later = ToolContext(context.bot, context.channel_id, "later-turn")
    exported = await call(workspaces, later, "export", task="notes", path="notes/log.txt")
    artifact = workspaces.store.one("SELECT * FROM artifacts WHERE id=?", (exported["artifact_id"],))
    assert artifact["bot_id"] == "ada" and artifact["turn_id"] == "later-turn"
    assert (
        workspaces.directory.parent / "artifacts" / artifact["filename"]
    ).read_text() == "new line\nsecond line\n"
    restarted = Workspaces(workspaces.store, workspaces.directory.parent, workspaces.vault)
    assert restarted.read_file("ada", "123", "notes", "notes/log.txt")["content"] == "new line\nsecond line\n"


async def test_directory_bounded_binary_read_and_unicode_byte_cursors(workspaces, context):
    await call(workspaces, context, "start", task="text")
    await call(workspaces, context, "mkdir", task="text", path="a/b")
    original = "abc😀é\n中文" * 60
    await call(workspaces, context, "write", task="text", path="a/b/note.txt", content=original)
    offset, chunks = 0, []
    while True:
        part = await call(
            workspaces, context, "read", task="text", path="a/b/note.txt", offset=offset, limit_bytes=11
        )
        chunks.append(part["content"])
        assert part["offset"] == offset and part["end_offset"] > offset
        if part["eof"]:
            assert part["next_offset"] is None
            break
        offset = part["next_offset"]
    assert "".join(chunks) == original
    end = await call(
        workspaces, context, "read", task="text", path="a/b/note.txt", offset=len(original.encode())
    )
    assert end["content"] == "" and end["eof"]
    with pytest.raises(ControlError, match="beyond"):
        await call(
            workspaces, context, "read", task="text", path="a/b/note.txt", offset=len(original.encode()) + 1
        )
    with pytest.raises(ControlError, match="UTF-8"):
        await call(workspaces, context, "read", task="text", path="a/b/note.txt", offset=4)
    with pytest.raises(ControlError, match="at least 4"):
        await call(workspaces, context, "read", task="text", path="a/b/note.txt", offset=3, limit_bytes=1)
    binary = b"\x00\xffhello"
    await call(
        workspaces,
        context,
        "write",
        task="text",
        path="binary.bin",
        content=base64.b64encode(binary).decode(),
        encoding="base64",
    )
    assert (await call(workspaces, context, "read", task="text", path="binary.bin", encoding="base64"))[
        "content"
    ] == base64.b64encode(binary).decode()
    listed = await call(workspaces, context, "list", task="text", limit=1)
    assert len(listed["files"]) == 1 and listed["truncated"]
    assert workspaces.inspect_files("ada", "123", "text", "a/b")["files"][0]["path"] == "a/b/note.txt"
    directory = await call(workspaces, context, "stat", task="text", path="a")
    assert directory["kind"] == "directory" and directory["bytes"] == len(original.encode())
    assert (await call(workspaces, context, "stat", task="text", path="."))["entries"] == 4
    with pytest.raises(ControlError, match="Directory not found"):
        workspaces.inspect_files("ada", "123", "text", "missing")


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "../x", "x/../../y", "a//b", "a/./b", "a\\b", "a\0b", "a\nb", "", "a/" * 33 + "x"]
)
def test_reject_unsafe_paths(path):
    with pytest.raises(ControlError):
        safe_path(path)
    report = feedback(
        "workspace",
        {"operation": "write", "task": "task", "path": path, "content": "x"},
        PARAMETERS,
        DESCRIPTION,
    )
    assert report and not report["executed"]


def test_path_schema_reports_depth_and_independent_required_errors_together():
    shallow = "/".join(["x"] * 32)
    assert safe_path(shallow) == ["x"] * 32
    assert (
        feedback(
            "workspace",
            {"operation": "write", "task": "task", "path": shallow, "content": "x"},
            PARAMETERS,
            DESCRIPTION,
        )
        is None
    )
    errors = feedback(
        "workspace", {"operation": "import_attachment", "path": shallow + "/x"}, PARAMETERS, DESCRIPTION
    )
    assert {error["path"] for error in errors["errors"]} >= {"$", "$.path"}
    assert errors["error_count"] >= 4


async def test_symlinks_hardlinks_fifos_and_workspace_root_escape_fail_closed(workspaces, context, tmp_path):
    await call(workspaces, context, "start", task="links")
    prepared = workspaces.prepare_job(context, "links")
    root = prepared["path"]
    workspaces.release_job(prepared["workspace_id"])
    sentinel = tmp_path / "outside-private"
    sentinel.write_text("cannot see this")
    (root / "escape").symlink_to(sentinel)
    with pytest.raises(ControlError):
        await call(workspaces, context, "read", task="links", path="escape")
    with pytest.raises(ControlError):
        await call(workspaces, context, "write", task="links", path="other.txt", content="x")
    (root / "escape").unlink()
    os.link(sentinel, root / "hardlink")
    with pytest.raises(ControlError, match="single-link"):
        await call(workspaces, context, "export", task="links", path="hardlink")
    (root / "hardlink").unlink()
    os.mkfifo(root / "pipe")
    with pytest.raises(ControlError, match="regular"):
        await call(workspaces, context, "read", task="links", path="pipe")
    (root / "pipe").unlink()
    (root / "folder").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ControlError):
        await call(workspaces, context, "read", task="links", path="folder/outside-private")
    assert sentinel.read_text() == "cannot see this"


async def test_job_pin_atomic_copyback_cwd_and_unsafe_output_rejection(workspaces, context, tmp_path):
    await call(workspaces, context, "start", task="script")
    await call(workspaces, context, "write", task="script", path="input.txt", content="original")
    prepared = workspaces.prepare_job(context, "script")
    with pytest.raises(ControlError, match="busy"):
        await call(workspaces, context, "read", task="script", path="input.txt")
    with pytest.raises(ControlError, match="busy"):
        workspaces.prepare_job(context, "script")
    stage = tmp_path / "sandbox-output"
    stage.mkdir()
    (stage / "nested").mkdir()
    (stage / "input.txt").write_text("original")
    (stage / "nested" / "result.txt").write_text("pipeline output")
    finished = workspaces.finish_job(prepared["workspace_id"], stage, "nested")
    assert finished["workspace_updated"] and not prepared["path"].exists()
    workspaces.release_job(prepared["workspace_id"])
    assert workspaces.read_file("ada", "123", "script", "nested/result.txt")["content"] == "pipeline output"
    next_job = workspaces.prepare_job(context, "script")
    assert next_job["cwd"] == "nested"
    (stage / "escape").symlink_to(tmp_path)
    with pytest.raises(ControlError, match="unsafe"):
        workspaces.finish_job(next_job["workspace_id"], stage)
    workspaces.release_job(next_job["workspace_id"])
    assert workspaces.read_file("ada", "123", "script", "input.txt")["content"] == "original"


async def test_real_large_image_cache_import_metadata_compress_copy_and_export(workspaces, context, tmp_path):
    # A valid PNG with deliberately uncompressed IDAT proves encoded bytes, not a guessed model size.
    buffer = io.BytesIO()
    with Image.new("RGB", (2048, 1800), (25, 100, 175)) as picture:
        picture.save(buffer, format="PNG", compress_level=0)
    original = buffer.getvalue()
    assert 8 * 1024 * 1024 < len(original) < MAX_IMAGE_BYTES
    digest = hashlib.sha256(original).hexdigest()
    workspaces.images.root.mkdir()
    workspaces.images.path(digest).write_bytes(original)
    attachment = {
        "id": "987",
        "filename": "original.png",
        "content_type": "image/png",
        "size": len(original),
        "url": "https://cdn.discordapp.com/attachments/123/987/expired.png?ex=old",
        "vision": {"status": "ready", "sha256": digest, **validate_image(original)},
    }
    workspaces.store.ingest(
        discord_id="456",
        channel_id="123",
        author_id="owner",
        author_name="Owner",
        content="compress my image",
        attachments=[attachment],
    )
    workspaces.images.capture = AsyncMock(
        side_effect=AssertionError("cached pixels must not be redownloaded")
    )
    await call(workspaces, context, "start", task="compress")
    imported = await call(
        workspaces,
        context,
        "import_attachment",
        task="compress",
        path="original.png",
        message_id="456",
        attachment_id="987",
    )
    assert imported["cached"] and imported["original_protected"]
    assert (imported["width"], imported["height"], imported["bytes"]) == (2048, 1800, len(original))
    with pytest.raises(ControlError, match=str(EXPORT_LIMIT)):
        await call(workspaces, context, "export", task="compress", path="original.png")
    with pytest.raises(ControlError, match="originals are protected"):
        await call(
            workspaces, context, "write", task="compress", path="original.png", content="bad", overwrite=True
        )
    prepared = workspaces.prepare_job(context, "compress")
    staged = tmp_path / "compressed-workspace"
    staged.mkdir()
    (staged / "original.png").write_bytes(original)
    with Image.open(io.BytesIO(original)) as picture:
        picture.save(staged / "compressed.png", format="PNG", optimize=True)
    assert (staged / "compressed.png").stat().st_size < EXPORT_LIMIT
    workspaces.finish_job(prepared["workspace_id"], staged)
    workspaces.release_job(prepared["workspace_id"])
    exported = await call(workspaces, context, "export", task="compress", path="compressed.png")
    assert exported["bytes"] < EXPORT_LIMIT and exported["turn_id"] == context.turn_id
    assert (await call(workspaces, context, "stat", task="compress", path="original.png"))["sha256"] == digest
    # This local Pillow fixture tests workspace import/copy/export; real isolated Bash is tested separately.
    next_job = workspaces.prepare_job(context, "compress")
    (staged / "original.png").unlink()
    with pytest.raises(ControlError, match="imported original"):
        workspaces.finish_job(next_job["workspace_id"], staged)
    workspaces.release_job(next_job["workspace_id"])


async def test_attachment_scope_deleted_and_expired_recovery(workspaces, context):
    await call(workspaces, context, "start", task="imports")
    args = dict(task="imports", path="image.png", message_id="456", attachment_id="987")
    attachment = {
        "id": "987",
        "filename": "image.png",
        "content_type": "image/png",
        "url": "https://cdn.discordapp.com/attachments/123/987/image.png",
    }
    workspaces.store.ingest(
        discord_id="456",
        channel_id="456",
        author_id="owner",
        author_name="Owner",
        content="other channel",
        attachments=[attachment],
    )
    with pytest.raises(ControlError, match="authorized channel"):
        await call(workspaces, context, "import_attachment", **args)
    workspaces.store.execute("UPDATE messages SET channel_id='123',deleted=1 WHERE discord_id='456'")
    with pytest.raises(ControlError, match="authorized channel"):
        await call(workspaces, context, "import_attachment", **args)
    workspaces.store.execute("UPDATE messages SET deleted=0 WHERE discord_id='456'")
    workspaces.images.capture = AsyncMock(
        return_value={
            **attachment,
            "vision": {
                "status": "unavailable",
                "error": "Discord image download returned HTTP 403; reattach if expired",
            },
        }
    )
    with pytest.raises(ControlError, match="403.*refreshed authenticated"):
        await call(workspaces, context, "import_attachment", **args)
    assert not workspaces._active
    data = io.BytesIO()
    Image.new("RGB", (8, 8)).save(data, format="PNG")
    digest = hashlib.sha256(data.getvalue()).hexdigest()
    workspaces.images.root.mkdir()
    workspaces.images.path(digest).write_bytes(data.getvalue())
    workspaces.images.capture = AsyncMock(
        return_value={
            **attachment,
            "vision": {"status": "ready", "sha256": digest, **validate_image(data.getvalue())},
        }
    )
    assert (await call(workspaces, context, "import_attachment", **args))["bytes"] == len(data.getvalue())
    assert (
        json.loads(
            workspaces.store.one("SELECT attachments FROM messages WHERE discord_id='456'")["attachments"]
        )[0]["vision"]["sha256"]
        == digest
    )


async def test_retention_protects_jobs_and_active_turn_references(workspaces, context):
    await call(workspaces, context, "start", task="retention")
    prepared = workspaces.prepare_job(context, "retention")
    workspaces.store.execute("UPDATE workspaces SET expires_at=1")
    assert workspaces.prune_expired(now=2)["removed"] == []
    workspaces.release_job(prepared["workspace_id"])
    workspaces.store.execute(
        "INSERT INTO turns(id,bot_id,channel_id,profile_id,provider_id,model,started_at,status,revision,trigger) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (context.turn_id, "ada", "123", "test", "test", "test", 1, "running", 1, "test"),
    )
    assert workspaces.prune_expired(now=2)["removed"] == []
    workspaces.store.execute("UPDATE turns SET ended_at=2,status='completed'")
    assert workspaces.prune_expired(now=3)["removed"] == [prepared["workspace_id"]]
    assert not prepared["path"].exists()
    assert not workspaces.store.rows("SELECT * FROM workspace_turn_refs")


async def test_quota_limits_and_atomic_failed_replace(workspaces, context, monkeypatch, tmp_path):
    config = {**DEFAULTS, "max_files": 2, "max_workspaces": 1}
    await call(workspaces, context, "start", task="quota", config=config)
    await call(workspaces, context, "write", task="quota", path="a.txt", content="old", config=config)
    with pytest.raises(ControlError, match="files/directories"):
        await call(
            workspaces, context, "write", task="quota", path="nested/file.txt", content="x", config=config
        )
    with pytest.raises(ControlError, match="limit of 1"):
        await call(workspaces, context, "start", task="second", config=config)
    original_replace = os.replace

    def fail_replace(*args, **kwargs):
        raise OSError("test disk failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(ControlError):
        await call(workspaces, context, "write", task="quota", path="a.txt", content="new", overwrite=True)
    monkeypatch.setattr(os, "replace", original_replace)
    assert (await call(workspaces, context, "read", task="quota", path="a.txt"))["content"] == "old"
    prepared = workspaces.prepare_job(context, "quota")
    stage = tmp_path / "oversize"
    stage.mkdir()
    with (stage / "huge.bin").open("wb") as handle:
        handle.truncate(DEFAULTS["max_file_bytes"] + 1)
    with pytest.raises(ControlError, match="exceeds"):
        workspaces.finish_job(prepared["workspace_id"], stage)
    workspaces.release_job(prepared["workspace_id"])
    assert (await call(workspaces, context, "read", task="quota", path="a.txt"))["content"] == "old"


def test_complete_usage_validation_and_strict_configuration():
    usage = feedback("workspace", {}, PARAMETERS, DESCRIPTION)
    assert usage["usage_only"] and not usage["executed"]
    invalid = feedback(
        "workspace",
        {
            "operation": "import_attachment",
            "task": "Bad!",
            "offset": -1,
            "limit_bytes": "9",
            "surprise": True,
        },
        PARAMETERS,
        DESCRIPTION,
    )
    assert invalid["error_count"] >= 7 and invalid["usage"]["example"]["operation"] == "import_attachment"
    assert all(
        field in invalid["usage"]["example"] for field in ("message_id", "attachment_id", "path", "task")
    )
    assert feedback("workspace", {"operation": "read_result"}, PARAMETERS, DESCRIPTION)["usage"]["example"][
        "result_id"
    ]
    invalid_path = feedback(
        "workspace",
        {"operation": "write", "path": "../outside", "encoding": "base64", "content": "!!!"},
        PARAMETERS,
        DESCRIPTION,
    )
    assert invalid_path["error_count"] >= 3
    assert (
        feedback("workspace", {"operation": "list", "path": "a"}, PARAMETERS, DESCRIPTION)["error_count"] >= 1
    )
    with pytest.raises(ControlError) as error:
        validate_config(
            {"max_files": True, "max_read_bytes": "12000", "max_file_bytes": 8_000_000, "unknown": 1}
        )
    assert "unknown" in str(error.value) and "integer" in str(error.value)
    with pytest.raises(ControlError, match="max_file_bytes.*max_workspace_bytes"):
        validate_config({"max_file_bytes": 64 * 1024 * 1024, "max_workspace_bytes": 32 * 1024 * 1024})


async def test_secret_text_redaction_does_not_change_offsets_in_original_read(workspaces, context):
    await call(workspaces, context, "start", task="redaction")
    await call(workspaces, context, "write", task="redaction", path="notes.txt", content="test-secret value")
    assert (await call(workspaces, context, "read", task="redaction", path="notes.txt"))[
        "content"
    ] == "[REDACTED] value"


async def test_snapshot_commit_failure_keeps_old_tree_and_cleans_staging(
    workspaces, context, tmp_path, monkeypatch
):
    await call(workspaces, context, "start", task="snapshot")
    await call(workspaces, context, "write", task="snapshot", path="file.txt", content="old")
    prepared = workspaces.prepare_job(context, "snapshot")
    stage = tmp_path / "finished-sandbox"
    stage.mkdir()
    (stage / "file.txt").write_text("new")
    execute = workspaces.store.execute

    def fail_pointer(sql, args=()):
        if "SET generation=" in sql:
            raise RuntimeError("simulated pointer commit failure")
        return execute(sql, args)

    monkeypatch.setattr(workspaces.store, "execute", fail_pointer)
    with pytest.raises(RuntimeError, match="pointer commit failure"):
        workspaces.finish_job(prepared["workspace_id"], stage)
    monkeypatch.setattr(workspaces.store, "execute", execute)
    workspaces.release_job(prepared["workspace_id"])
    assert workspaces.read_file("ada", "123", "snapshot", "file.txt")["content"] == "old"
    assert list(prepared["path"].parent.iterdir()) == [prepared["path"]]
    obsolete = prepared["path"].parent / ("files_" + "0" * 20)
    obsolete.mkdir()
    (obsolete / "partial.txt").write_text("interrupted snapshot")
    unknown = prepared["path"].parent / "operator-recovery"
    unknown.mkdir()
    assert (await call(workspaces, context, "start", task="snapshot"))["resumed"]
    assert not obsolete.exists() and unknown.exists()
    assert workspaces.read_file("ada", "123", "snapshot", "file.txt")["content"] == "old"


async def test_attachment_removal_during_download_does_not_resurrect_source(workspaces, context, monkeypatch):
    await call(workspaces, context, "start", task="download")
    workspaces.store.ingest(
        discord_id="456",
        channel_id="123",
        author_id="owner",
        author_name="Owner",
        content="file",
        attachments=[
            {
                "id": "987",
                "filename": "notes.txt",
                "content_type": "text/plain",
                "url": "https://cdn.discordapp.com/attachments/123/987/notes.txt",
            }
        ],
    )

    async def download(attachment):
        with pytest.raises(ControlError, match="busy"):
            workspaces.prepare_job(context, "download")
        workspaces.store.execute("UPDATE messages SET deleted=1 WHERE discord_id='456'")
        return b"must not import this removed attachment"

    monkeypatch.setattr(workspaces, "_download_attachment", download)
    with pytest.raises(ControlError, match="observation changed"):
        await call(
            workspaces,
            context,
            "import_attachment",
            task="download",
            path="notes.txt",
            message_id="456",
            attachment_id="987",
        )
    assert not workspaces._active
    assert workspaces.inspect_files("ada", "123", "download")["files"] == []
