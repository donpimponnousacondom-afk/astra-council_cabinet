import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator

from hortator.documents import DocumentSites, PARAMETERS, safe_path
from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.store import Store


@pytest.fixture
def documents(tmp_path):
    store = Store(tmp_path / "council.sqlite3")
    vault = SimpleNamespace(
        redact=lambda value: (
            value.replace("secret-test-value", "[REDACTED]") if isinstance(value, str) else value
        )
    )
    service = DocumentSites(store, tmp_path, vault)
    yield service
    store.close()


@pytest.fixture
def context():
    return ToolContext({"id": "ada"}, "123456", "turn-documents")


async def call(documents, context, operation, **args):
    return await documents.call(
        {"operation": operation, **args}, context, {"public_base_url": "http://council.example.test"}
    )


async def test_publish_is_local_only_atomic_persistent_and_draft_does_not_leak(documents, context):
    started = await call(documents, context, "create", site="summary", title="Meeting")
    assert started["document_task_started"] is True
    assert not started["local_ready"]
    await call(
        documents, context, "write", site="summary", path="index.html", content="<h1>Revision one</h1>"
    )
    assert not documents.store.rows("SELECT * FROM document_sync_queue")
    with pytest.raises(ControlError, match="not been published"):
        documents.resolve_published("ada", "summary", "index.html")
    published = await call(documents, context, "publish", site="summary")
    assert published["local_ready"] is True
    assert published["remote_status"] == "disabled"
    assert published["sync"]["status"] == "queued"
    assert published["sync"]["target_url"] == "http://council.example.test/ada/summary/"
    assert documents.resolve_published("ada", "summary", "index.html")[0] == b"<h1>Revision one</h1>"
    await call(
        documents, context, "write", site="summary", path="index.html", content="<h1>Private draft two</h1>"
    )
    assert documents.resolve_published("ada", "summary", "index.html")[0] == b"<h1>Revision one</h1>"
    assert documents.read_file("ada", "summary", "index.html")[0] == b"<h1>Private draft two</h1>"
    restarted = DocumentSites(documents.store, documents.directory.parent, documents.vault)
    assert restarted.resolve_published("ada", "summary", "index.html")[0] == b"<h1>Revision one</h1>"
    second = await call(restarted, context, "publish", site="summary")
    assert second["published_revision"] == 2
    assert [
        row["status"]
        for row in documents.store.rows("SELECT status FROM document_sync_queue ORDER BY revision")
    ] == ["superseded", "queued"]
    again = await call(documents, context, "publish", site="summary")
    assert again["sync"]["id"] == second["sync"]["id"]
    assert len(documents.store.rows("SELECT * FROM document_sync_queue")) == 2


async def test_publish_transaction_rolls_back_manifest_pointer_when_queue_fails(
    documents, context, monkeypatch
):
    await call(documents, context, "create", site="summary")
    await call(documents, context, "write", site="summary", path="index.html", content="x")

    def fail(*args):
        raise RuntimeError("disk problem")

    monkeypatch.setattr(documents, "_queue", fail)
    with pytest.raises(RuntimeError):
        await call(documents, context, "publish", site="summary")
    assert documents._site("ada", "summary")["published_revision"] == 0
    assert not documents.store.rows("SELECT * FROM document_sync_queue")


async def test_bot_isolation_scoped_import_and_secret_redaction(documents, context):
    await call(documents, context, "create", site="summary")
    await call(documents, context, "write", site="summary", path="index.html", content="secret-test-value")
    assert documents.read_file("ada", "summary", "index.html")[0] == b"[REDACTED]"
    foreign = ToolContext({"id": "curie"}, context.channel_id, context.turn_id)
    assert (await call(documents, foreign, "list"))["sites"] == []
    with pytest.raises(ControlError, match="not found"):
        await call(documents, foreign, "read", site="summary", path="index.html")
    artifacts = documents.directory.parent / "artifacts"
    artifacts.mkdir()
    (artifacts / "art_test.png").write_bytes(b"testimage")
    documents.store.execute(
        "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
        ("art_test", "ada", context.turn_id, "art_test.png", "image/png", 9, 1),
    )
    await call(
        documents, context, "import_artifact", site="summary", path="assets/image.png", artifact_id="art_test"
    )
    assert documents.read_file("ada", "summary", "assets/image.png")[0] == b"testimage"
    with pytest.raises(ControlError, match="current turn"):
        await call(
            documents,
            ToolContext(context.bot, context.channel_id, "later-turn"),
            "import_artifact",
            site="summary",
            path="image.png",
            artifact_id="art_test",
        )
    await call(
        documents,
        context,
        "write",
        site="summary",
        path="test.txt",
        content=base64.b64encode(b"secret-test-value").decode(),
        encoding="base64",
    )
    assert documents.read_file("ada", "summary", "test.txt")[0] == b"[REDACTED]"


@pytest.mark.parametrize(
    "path",
    [
        "../master.key",
        "/tmp/page.html",
        "assets/../../x.html",
        ".env",
        "assets/.htaccess",
        "index.php",
        "x\\page.html",
        "a//b.html",
        "page.html\n",
        "assets/link?.js",
        "report.php.html",
        "report.PHP8.HTML",
        "assets/test.py.svg",
        "page.shtml.txt",
    ],
)
def test_path_validation_rejects_traversal_and_server_programs(path):
    with pytest.raises(ControlError):
        safe_path(path)


async def test_symlink_and_corrupt_blob_cannot_escape_or_be_published(documents, context, tmp_path):
    await call(documents, context, "create", site="summary")
    await call(documents, context, "write", site="summary", path="index.html", content="hello")
    manifest = documents._manifest(documents._site("ada", "summary"))
    blob = documents._blob("ada", manifest["index.html"]["blob"])
    blob.unlink()
    target = tmp_path / "private.txt"
    target.write_text("do not expose")
    blob.symlink_to(target)
    with pytest.raises(ControlError, match="symbolic link"):
        documents.read_file("ada", "summary", "index.html")
    blob.unlink()
    blob.write_text("corrupt")
    with pytest.raises(ControlError, match="integrity"):
        documents.read_file("ada", "summary", "index.html")


async def test_file_count_size_and_empty_publish_fail_before_mutation(documents, context, monkeypatch):
    import hortator.documents as module

    await call(documents, context, "create", site="summary")
    with pytest.raises(ControlError, match="at least one"):
        await call(documents, context, "publish", site="summary")
    monkeypatch.setattr(module, "MAX_FILE", 5)
    with pytest.raises(ControlError, match="8 MB"):
        await call(documents, context, "write", site="summary", path="index.html", content="123456")
    await call(documents, context, "write", site="summary", path="index.html", content="short")
    monkeypatch.setattr(module, "MAX_FILES", 1)
    with pytest.raises(ControlError, match="100 files"):
        await call(documents, context, "write", site="summary", path="second.txt", content="x")
    assert documents._site("ada", "summary")["revision"] == 1


def test_operation_schema_reports_all_missing_and_wrong_fields():
    errors = list(
        Draft202012Validator(PARAMETERS).iter_errors(
            {"operation": "import_attachment", "site": 4, "path": 42, "unexpected": True}
        )
    )
    message = " ".join(error.message for error in errors)
    assert "message_id" in message and "attachment_id" in message
    assert message.count("not of type 'string'") == 2
    assert "unexpected" in message


async def test_attachment_scope_and_malformed_url_never_fetch(documents, context, monkeypatch):
    await call(documents, context, "create", site="summary")
    documents.store.ingest(
        discord_id="987654",
        channel_id="another-channel",
        room_id=None,
        author_id="owner",
        author_name="Owner",
        content="upload",
        attachments=[{"id": "123", "url": "http://127.0.0.1/secret", "size": 10}],
    )
    with pytest.raises(ControlError, match="current channel"):
        await call(
            documents,
            context,
            "import_attachment",
            site="summary",
            path="image.png",
            message_id="987654",
            attachment_id="123",
        )
    documents.store.execute(
        "UPDATE messages SET channel_id=? WHERE discord_id=?", (context.channel_id, "987654")
    )
    with pytest.raises(ControlError, match="Discord CDN"):
        await call(
            documents,
            context,
            "import_attachment",
            site="summary",
            path="image.png",
            message_id="987654",
            attachment_id="123",
        )


async def test_revoked_grant_cannot_commit_download_result(documents, context, monkeypatch):
    await call(documents, context, "create", site="summary")
    monkeypatch.setattr(documents, "_attachment", AsyncMock(return_value=b"downloaded"))
    with pytest.raises(ControlError, match="grant was removed"):
        await call(
            documents,
            context,
            "import_attachment",
            site="summary",
            path="image.png",
            message_id="987654",
            attachment_id="123",
        )
    assert documents._site("ada", "summary")["revision"] == 0


async def test_cached_attachment_import_survives_expired_cdn_url_without_network(
    documents, context, monkeypatch
):
    import hashlib
    import hortator.documents as module

    await call(documents, context, "create", site="summary")
    data = b"\x89PNG\r\n\x1a\nlocal-cache-test"
    digest = hashlib.sha256(data).hexdigest()
    images = documents.directory.parent / "images"
    images.mkdir()
    (images / digest).write_bytes(data)
    documents.store.ingest(
        discord_id="987654",
        channel_id=context.channel_id,
        room_id=None,
        author_id="owner",
        author_name="Owner",
        content="upload",
        attachments=[
            {"id": "123", "url": "expired://no-network", "vision": {"status": "ready", "sha256": digest}}
        ],
    )
    documents.store.put("bots", {"id": "ada", "enabled": True, "enabled_plugins": ["document_site"]})
    documents.store.put("plugins", {"id": "document_site", "enabled": True})

    def no_network(*args, **kwargs):
        raise AssertionError("Cached images must not need the expired CDN URL")

    monkeypatch.setattr(module.aiohttp, "ClientSession", no_network)
    await call(
        documents,
        context,
        "import_attachment",
        site="summary",
        path="image.png",
        message_id="987654",
        attachment_id="123",
    )
    assert documents.read_file("ada", "summary", "image.png")[0] == data


async def test_remote_enabled_config_cannot_activate_transfer(documents, context, monkeypatch):
    import hortator.documents as module

    def no_network(*args, **kwargs):
        raise AssertionError("Remote transfer is not implemented")

    monkeypatch.setattr(module.aiohttp, "ClientSession", no_network)
    config = {"remote_enabled": True, "public_base_url": "https://council.example.test"}
    for args in (
        {"operation": "create", "site": "summary"},
        {"operation": "write", "site": "summary", "path": "index.html", "content": "local"},
        {"operation": "publish", "site": "summary"},
    ):
        result = await documents.call(args, context, config)
    assert result["local_ready"] is True
    assert result["remote_status"] == "disabled"
    assert result["sync"]["status"] == "queued"


@pytest.mark.parametrize("damage", ["missing", "corrupt", "symlink"])
async def test_publish_never_claims_ready_or_queues_damaged_snapshot(documents, context, damage, tmp_path):
    await call(documents, context, "create", site="summary")
    await call(documents, context, "write", site="summary", path="index.html", content="valid")
    entry = documents._manifest(documents._site("ada", "summary"))["index.html"]
    blob = documents._blob("ada", entry["blob"])
    blob.unlink()
    if damage == "corrupt":
        blob.write_text("other")
    elif damage == "symlink":
        outside = tmp_path / "private.txt"
        outside.write_text("valid")
        blob.symlink_to(outside)
    with pytest.raises(ControlError):
        await call(documents, context, "publish", site="summary")
    assert documents._site("ada", "summary")["published_revision"] == 0
    assert not documents.store.rows("SELECT * FROM document_sync_queue")


async def test_list_preserves_per_bot_local_url_and_global_remote_url_with_document_only_entrypoint(
    documents, context
):
    documents.store.put(
        "plugins",
        {
            "id": "document_site",
            "config": {
                "public_base_url": "https://remote.example.test",
                "local_base_url": "https://local.example.test",
            },
        },
    )
    documents.store.put(
        "bots",
        {"id": "ada", "plugin_config": {"document_site": {"local_base_url": "https://ada.example.test"}}},
    )
    await call(documents, context, "create", site="summary")
    await call(documents, context, "write", site="summary", path="reports/meeting notes.txt", content="notes")
    await call(documents, context, "publish", site="summary")
    listed = documents.list_sites()[0]
    assert listed["published_entrypoint"] == "reports/meeting notes.txt"
    assert listed["local_url"] == "https://ada.example.test/sites/ada/summary/reports/meeting%20notes.txt"
    assert listed["planned_public_url"] == "https://remote.example.test/ada/summary/"
    assert (await call(documents, context, "list"))["sites"][0][
        "planned_public_url"
    ] == "https://remote.example.test/ada/summary/"
    await call(documents, context, "write", site="summary", path="index.html", content="private landing page")
    assert documents.list_sites()[0]["published_entrypoint"] == "reports/meeting notes.txt"
    await call(documents, context, "publish", site="summary")
    assert documents.list_sites()[0]["local_url"] == "https://ada.example.test/sites/ada/summary/"
