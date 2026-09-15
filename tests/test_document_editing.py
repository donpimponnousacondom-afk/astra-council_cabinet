import json
from types import SimpleNamespace

import pytest

from hortator.documents import DocumentSites, PARAMETERS
from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.store import Store
from hortator.tool_feedback import feedback


@pytest.fixture
def documents(tmp_path):
    store = Store(tmp_path / "council.sqlite3")
    documents = DocumentSites(store, tmp_path, SimpleNamespace(redact=lambda value: value))
    yield documents
    store.close()


@pytest.fixture
def context():
    return ToolContext({"id": "dirac", "name": "Paul Dirac"}, "123456", "turn-sites")


async def call(documents, context, operation, **args):
    return await documents.call({"operation": operation, **args}, context, {})


def automatic(documents):
    documents.store.put(
        "plugins",
        {
            "id": "document_site",
            "enabled": True,
            "config": {"auto_publish": True, "public_base_url": "https://council.example.test"},
        },
    )


@pytest.mark.parametrize(
    "operation,args",
    [
        ("start", {}),
        ("edit", {}),
        ("status", {}),
        ("history", {}),
        ("publish", {}),
        ("read", {"path": "index.html"}),
        ("write", {"path": "index.html", "content": "test"}),
        ("append", {"path": "index.html", "content": "test", "expected_revision": 0}),
        ("replace", {"path": "index.html", "old_text": "old", "new_text": "new", "expected_revision": 0}),
        ("restore", {"path": "index.html", "revision": 1, "expected_revision": 0}),
        ("import_artifact", {"path": "image.png", "artifact_id": "art-test"}),
    ],
)
async def test_only_create_can_create_a_missing_site(documents, context, operation, args):
    with pytest.raises(ControlError, match="Please use create"):
        await call(documents, context, operation, site="missing", **args)
    assert documents.store.rows("SELECT * FROM document_sites") == []
    assert documents.store.rows("SELECT * FROM document_revisions") == []


async def test_create_refuses_collision_and_resume_preserves_identity(documents, context):
    first = await call(documents, context, "create", site="summary", title="First title")
    assert first["document_task_started"]
    with pytest.raises(ControlError, match="already taken"):
        await call(documents, context, "create", site="summary", title="Replacement title")
    for operation in ("start", "edit"):
        result = await call(documents, context, operation, site="summary")
        assert result["title"] == "First title"
        assert result["document_task_started"] and result["revision"] == 0
    assert first["local_url"].endswith("/sites/dirac/summary/")


async def test_hortator_has_no_cross_bot_override_and_retired_names_stay_owned(documents, context):
    await call(documents, context, "create", site="summary")
    await call(documents, context, "write", site="summary", path="index.html", content="Dirac's page")
    await call(documents, context, "publish", site="summary")
    hortator = ToolContext(
        {"id": "hortator", "name": "Paul Dirac"}, context.channel_id, context.turn_id, True
    )
    with pytest.raises(ControlError, match="Site not found"):
        await call(documents, hortator, "edit", site="summary")
    own = await call(documents, hortator, "create", site="summary")
    assert own["bot_id"] == "hortator" and own["revision"] == 0
    documents.store.execute("INSERT INTO entity_tombstones VALUES('bots','dirac',1)")
    for operation in ("create", "edit", "list"):
        with pytest.raises(ControlError, match="retired"):
            await call(documents, context, operation, site="summary")
    assert documents.resolve_published("dirac", "summary", "index.html")[0] == b"Dirac's page"


def test_paged_schema_returns_all_errors_and_full_usage_together():
    result = feedback(
        "document_site",
        {
            "operation": "read",
            "site": "",
            "offset": -1,
            "length": 12001,
            "revision": "one",
            "bot_id": "hortator",
        },
        PARAMETERS,
    )
    assert result["error_count"] == 6
    assert result["usage"]["parameters"] == PARAMETERS
    assert {error["rule"] for error in result["errors"]} == {
        "pattern",
        "minimum",
        "maximum",
        "type",
        "required",
        "additionalProperties",
    }
    append = feedback("document_site", {"operation": "append", "encoding": "base64"}, PARAMETERS)
    assert append["error_count"] == 5
    assert append["executed"] is False
    assert (
        feedback("document_site", {"operation": "delete", "site": "summary"}, PARAMETERS)["executed"] is False
    )


def test_path_errors_are_reported_with_other_invalid_fields_without_running_handler():
    result = feedback(
        "document_site",
        {
            "operation": "write",
            "site": "../foreign",
            "path": "report.PHP8.HTML",
            "content": 4,
            "expected_revision": "1",
        },
        PARAMETERS,
    )
    assert result["error_count"] == 4
    assert {error["path"] for error in result["errors"]} == {
        "$.site",
        "$.path",
        "$.content",
        "$.expected_revision",
    }
    for path in ("assets/chart.svg", "index.HTML", "reports/report notes.txt", "style.css"):
        assert (
            feedback(
                "document_site",
                {"operation": "write", "site": "summary", "path": path, "content": "test"},
                PARAMETERS,
            )
            is None
        )


async def test_read_pages_are_bounded_in_characters_and_pin_the_original_revision(documents, context):
    await call(documents, context, "create", site="summary")
    original = "é🐈中文" * 2501
    await call(documents, context, "write", site="summary", path="report.txt", content=original)
    first = await call(documents, context, "read", site="summary", path="report.txt")
    assert first["returned_chars"] == 4000 and first["bytes"] == len(original.encode())
    assert first["total_chars"] == len(original)
    assert first["next_read"]["revision"] == first["revision"] == 1
    await call(documents, context, "write", site="summary", path="report.txt", content="new current text")
    parts = [first["content"]]
    page = first
    while page["next_read"]:
        page = await documents.call(page["next_read"], context, {})
        assert page["revision"] == 1 and page["current_revision"] == 2
        assert page["returned_chars"] <= 4000
        parts.append(page["content"])
    assert "".join(parts) == original and not page["has_more"]
    eof = await call(
        documents, context, "read", site="summary", path="report.txt", revision=1, offset=len(original)
    )
    assert eof["content"] == "" and not eof["next_read"]
    with pytest.raises(ControlError, match="beyond this file"):
        await call(documents, context, "read", site="summary", path="report.txt", offset=1000)


async def test_binary_read_stays_metadata_only(documents, context):
    await call(documents, context, "create", site="summary")
    await call(
        documents, context, "write", site="summary", path="image.png", content="/wAA", encoding="base64"
    )
    result = await call(documents, context, "read", site="summary", path="image.png")
    assert result["bytes"] == 3 and result["content"] is None
    assert result["total_chars"] is None and result["next_read"] is None


async def test_append_and_unique_replace_reject_stale_or_ambiguous_edits(documents, context):
    await call(documents, context, "create", site="summary")
    await call(documents, context, "write", site="summary", path="report.txt", content="First line\n")
    await call(
        documents,
        context,
        "append",
        site="summary",
        path="report.txt",
        content="Second line\n",
        expected_revision=1,
    )
    for operation, args in (
        ("append", {"content": "lost update"}),
        ("replace", {"old_text": "First", "new_text": "Altered"}),
        ("write", {"content": "replacement"}),
    ):
        with pytest.raises(ControlError, match="now at revision 2"):
            await call(
                documents, context, operation, site="summary", path="report.txt", expected_revision=1, **args
            )
    with pytest.raises(ControlError, match="occurs 2 times"):
        await call(
            documents,
            context,
            "replace",
            site="summary",
            path="report.txt",
            old_text="line",
            new_text="row",
            expected_revision=2,
        )
    assert documents._site("dirac", "summary")["revision"] == 2
    await call(
        documents,
        context,
        "replace",
        site="summary",
        path="report.txt",
        old_text="Second line",
        new_text="Last line",
        expected_revision=2,
    )
    assert documents.read_file("dirac", "summary", "report.txt")[0] == b"First line\nLast line\n"
    assert documents.read_file("dirac", "summary", "report.txt", 1)[0] == b"First line\n"
    with pytest.raises(ControlError, match="File not found"):
        await call(
            documents,
            context,
            "append",
            site="summary",
            path="missing.txt",
            content="no create",
            expected_revision=3,
        )


async def test_history_pages_and_file_restore_keep_every_other_current_asset(documents, context):
    automatic(documents)
    await call(documents, context, "create", site="summary")
    await call(documents, context, "write", site="summary", path="index.html", content="Original")
    await call(
        documents, context, "write", site="summary", path="assets/style.css", content="body { color: green; }"
    )
    await call(documents, context, "write", site="summary", path="index.html", content="Changed")
    page = await call(documents, context, "history", site="summary", limit=2)
    assert [row["revision"] for row in page["revisions"]] == [3, 2]
    previous = await call(
        documents, context, "history", site="summary", limit=2, before_revision=page["next_before_revision"]
    )
    assert [row["revision"] for row in previous["revisions"]] == [1]
    assert not previous["has_more"]
    restored = await call(
        documents, context, "restore", site="summary", path="index.html", revision=1, expected_revision=3
    )
    assert restored["revision"] == restored["published_revision"] == restored["sync"]["revision"] == 4
    assert documents.resolve_published("dirac", "summary", "index.html")[0] == b"Original"
    assert documents.resolve_published("dirac", "summary", "assets/style.css")[0] == b"body { color: green; }"
    assert documents.read_file("dirac", "summary", "index.html", 3)[0] == b"Changed"
    assert len(documents.store.rows("SELECT * FROM document_revisions")) == 4
    restored_event = documents.store.one("SELECT data FROM events WHERE kind='document.restored'")
    assert json.loads(restored_event["data"])["source_revision"] == 1


async def test_auto_publish_saves_revision_local_pointer_and_queue_in_one_transaction(
    documents, context, monkeypatch
):
    automatic(documents)
    await call(documents, context, "create", site="summary")
    first = await call(documents, context, "write", site="summary", path="index.html", content="First")
    assert first["revision"] == first["published_revision"] == first["sync"]["revision"] == 1
    assert first["auto_publish"] and first["local_ready"]

    def unavailable(*args):
        raise RuntimeError("Queue unavailable")

    monkeypatch.setattr(documents, "_queue", unavailable)
    with pytest.raises(RuntimeError, match="Queue unavailable"):
        await call(documents, context, "write", site="summary", path="index.html", content="Must not publish")
    site = documents._site("dirac", "summary")
    assert site["revision"] == site["published_revision"] == 1
    assert documents.resolve_published("dirac", "summary", "index.html")[0] == b"First"
    assert len(documents.store.rows("SELECT * FROM document_revisions")) == 1
    assert len(documents.store.rows("SELECT * FROM document_sync_queue")) == 1


async def test_auto_publish_never_queues_a_snapshot_with_a_damaged_other_asset(documents, context):
    automatic(documents)
    await call(documents, context, "create", site="summary")
    await call(documents, context, "write", site="summary", path="index.html", content="First")
    entry = documents._manifest(documents._site("dirac", "summary"))["index.html"]
    documents._blob("dirac", entry["blob"]).write_bytes(b"other")
    with pytest.raises(ControlError, match="integrity"):
        await call(documents, context, "write", site="summary", path="style.css", content="body {}")
    assert documents._site("dirac", "summary")["revision"] == 1
    assert len(documents.store.rows("SELECT * FROM document_sync_queue")) == 1


async def test_publishing_is_idempotent_after_delivery_and_pending_edits_are_explicit(documents, context):
    automatic(documents)
    documents.set_remote_state(lambda: {"enabled": True, "configured": True, "status": "ready"})
    await call(documents, context, "create", site="summary")
    first = await call(documents, context, "write", site="summary", path="index.html", content="First")
    assert first["remote_status"] == "queued" and first["public_url"] is None
    assert first["synced_revision"] == 0 and not first["delivery_current"]
    documents.store.execute("ALTER TABLE document_sync_queue ADD COLUMN snapshot_commit TEXT")
    documents.store.execute(
        "UPDATE document_sync_queue SET status='delivered',snapshot_commit='snapshot-1',updated_at=123"
    )
    repeated = await call(documents, context, "publish", site="summary")
    assert repeated["sync"]["status"] == "delivered"
    assert repeated["sync"]["updated_at"] == 123 and repeated["sync"]["snapshot_commit"] == "snapshot-1"
    assert repeated["public_url"] == "https://council.example.test/dirac/summary/"
    assert repeated["synced_revision"] == 1 and repeated["delivery_current"]
    second = await call(documents, context, "write", site="summary", path="index.html", content="Second")
    assert second["remote_status"] == "queued" and second["synced_revision"] == 1
    assert second["public_url"] == repeated["public_url"] and not second["delivery_current"]
    assert [
        row["status"]
        for row in documents.store.rows("SELECT status FROM document_sync_queue ORDER BY revision")
    ] == ["delivered", "queued"]


async def test_bot_config_cannot_redirect_or_suppress_global_automatic_publication(documents, context):
    automatic(documents)
    override = {
        "auto_publish": False,
        "public_base_url": "https://other.example.test",
        "remote_enabled": True,
    }
    await documents.call({"operation": "create", "site": "summary"}, context, override)
    result = await documents.call(
        {"operation": "write", "site": "summary", "path": "index.html", "content": "test"}, context, override
    )
    assert result["auto_publish"] and result["local_ready"]
    assert result["sync"]["target_url"] == "https://council.example.test/dirac/summary/"
    assert result["remote_status"] == "disabled"  # A config field alone does not instantiate a worker.


async def test_list_is_bounded_and_does_not_dump_each_sites_file_manifest(documents, context):
    for slug in ("first", "second", "third"):
        await call(documents, context, "create", site=slug)
    first = await call(documents, context, "list", limit=2)
    assert len(first["sites"]) == 2 and first["has_more"]
    assert all("files" not in site and site["file_count"] == 0 for site in first["sites"])
    second = await call(documents, context, "list", limit=2, offset=first["next_offset"])
    assert len(second["sites"]) == 1 and not second["has_more"]


@pytest.mark.parametrize(
    "first,second",
    [
        ("assets.css", "assets.css/page.html"),
        ("assets.css/page.html", "assets.css"),
    ],
)
async def test_files_cannot_replace_directories_or_become_parent_directories(
    documents, context, first, second
):
    await call(documents, context, "create", site="summary")
    await call(documents, context, "write", site="summary", path=first, content="First")
    with pytest.raises(ControlError, match="conflicts with an existing file or folder"):
        await call(documents, context, "write", site="summary", path=second, content="Second")
    assert documents._site("dirac", "summary")["revision"] == 1


async def test_missing_history_revision_is_a_repairable_tool_error(documents, context):
    await call(documents, context, "create", site="summary")
    await call(documents, context, "write", site="summary", path="index.html", content="First")
    with pytest.raises(ControlError, match="Revision 99 does not exist"):
        await call(documents, context, "read", site="summary", path="index.html", revision=99)


async def test_import_published_copies_only_public_bytes_into_an_explicitly_created_owned_site(
    documents, context
):
    source = ToolContext({"id": "ada"}, context.channel_id, "turn-source")
    await call(documents, source, "create", site="original")
    await call(documents, source, "write", site="original", path="index.html", content="Published source")
    await call(documents, source, "write", site="original", path="assets/style.css", content="body {}")
    await call(documents, source, "publish", site="original")
    await call(documents, source, "write", site="original", path="index.html", content="SECRET PRIVATE DRAFT")
    await call(documents, source, "write", site="original", path="private.txt", content="SECRET PRIVATE FILE")
    documents.store.execute("INSERT INTO entity_tombstones VALUES('bots','ada',1)")
    listing = await call(
        documents, context, "published_files", source_bot="ada", source_site="original", limit=1
    )
    assert listing["source_revision"] == 2 and listing["has_more"]
    page = await documents.call(listing["next_page"], context, {})
    assert {row["path"] for row in listing["files"] + page["files"]} == {"index.html", "assets/style.css"}
    assert "SECRET" not in json.dumps(listing) + json.dumps(page)
    arguments = {
        "source_bot": "ada",
        "source_site": "original",
        "source_path": "index.html",
        "path": "index.html",
        "source_revision": 2,
    }
    with pytest.raises(ControlError, match="Please use create"):
        await call(documents, context, "import_published", site="copy", **arguments)
    await call(documents, context, "create", site="copy")
    automatic(documents)
    copied = await call(documents, context, "import_published", site="copy", expected_revision=0, **arguments)
    assert copied["local_ready"] and copied["sync"]["revision"] == 1
    assert copied["imported_source"] == {
        "bot_id": "ada",
        "site": "original",
        "path": "index.html",
        "revision": 2,
    }
    assert documents.read_file("dirac", "copy", "index.html")[0] == b"Published source"
    assert documents.read_file("ada", "original", "index.html")[0] == b"SECRET PRIVATE DRAFT"
    assert (
        documents._manifest(documents._site("dirac", "copy"))["index.html"]["blob"]
        == documents._manifest(documents._site("ada", "original"), 2)["index.html"]["blob"]
    )
    assert documents._bot_dir("dirac") != documents._bot_dir("ada")
    event = documents.store.one("SELECT data FROM events WHERE kind='document.imported_public'")
    assert json.loads(event["data"])["source"] == copied["imported_source"]
    for operation in ("published_files", "import_published"):
        args = {"source_bot": "ada", "source_site": "original", "source_revision": 3}
        if operation == "import_published":
            args.update(site="copy", path="private.txt", source_path="index.html")
        with pytest.raises(ControlError, match="was not published"):
            await call(documents, context, operation, **args)
    with pytest.raises(ControlError, match="File not found in the published"):
        await call(
            documents,
            context,
            "import_published",
            site="copy",
            path="private.txt",
            source_bot="ada",
            source_site="original",
            source_path="private.txt",
        )


async def test_published_listing_pins_prior_publication_when_source_publishes_again(documents, context):
    await call(documents, context, "create", site="summary")
    await call(documents, context, "write", site="summary", path="a.txt", content="first")
    await call(documents, context, "write", site="summary", path="b.txt", content="second")
    await call(documents, context, "publish", site="summary")
    listing = await call(
        documents, context, "published_files", source_bot="dirac", source_site="summary", limit=1
    )
    await call(documents, context, "write", site="summary", path="c.txt", content="third")
    await call(documents, context, "publish", site="summary")
    second_page = await documents.call(listing["next_page"], context, {})
    assert second_page["source_revision"] == 2
    assert [file["path"] for file in second_page["files"]] == ["b.txt"]
    assert not second_page["has_more"]


async def test_new_publication_supersedes_obsolete_failure_but_preserves_active_transfer(documents, context):
    automatic(documents)
    await call(documents, context, "create", site="summary")
    for revision in (1, 2):
        await call(documents, context, "write", site="summary", path="index.html", content=str(revision))
    documents.store.execute("UPDATE document_sync_queue SET status='failed' WHERE revision=1")
    documents.store.execute("UPDATE document_sync_queue SET status='syncing' WHERE revision=2")
    await call(documents, context, "write", site="summary", path="index.html", content="3")
    assert [
        row["status"]
        for row in documents.store.rows("SELECT status FROM document_sync_queue ORDER BY revision")
    ] == ["superseded", "syncing", "queued"]


@pytest.mark.parametrize("status", ["queued", "failed"])
async def test_new_publication_preserves_attempted_job_for_remote_receipt_recovery(
    documents, context, status
):
    automatic(documents)
    documents.store.execute("ALTER TABLE document_sync_queue ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
    await call(documents, context, "create", site="summary")
    await call(documents, context, "write", site="summary", path="index.html", content="first")
    documents.store.execute("UPDATE document_sync_queue SET status=?,attempts=1", (status,))
    await call(documents, context, "write", site="summary", path="index.html", content="second")
    assert [
        row["status"]
        for row in documents.store.rows("SELECT status FROM document_sync_queue ORDER BY revision")
    ] == [status, "queued"]


async def test_verified_remote_ahead_evidence_prevents_stale_current_delivery_claim(documents, context):
    automatic(documents)
    documents.set_remote_state(lambda: {"enabled": True, "configured": True, "status": "enabled"})
    await call(documents, context, "create", site="summary")
    for revision in (1, 2, 3):
        await call(documents, context, "write", site="summary", path="index.html", content=str(revision))
    documents.store.execute("UPDATE document_sync_queue SET status='delivered' WHERE revision=3")
    legacy = await call(documents, context, "status", site="summary")
    assert legacy["delivery_current"] and not legacy["remote_currentness_verified"]
    assert legacy["observed_remote_revision"] is None
    for column, kind in (
        ("remote_current_revision", "INTEGER"),
        ("remote_delivery_current", "INTEGER"),
        ("remote_observed_at", "REAL"),
    ):
        documents.store.execute(f"ALTER TABLE document_sync_queue ADD COLUMN {column} {kind}")
    documents.store.execute(
        "UPDATE document_sync_queue SET remote_current_revision=3,remote_delivery_current=1,remote_observed_at=50,updated_at=999 WHERE revision=3"
    )
    # A retry of an old job reveals that the restored local database is behind
    # remote history. Its observation is newer, despite its older job revision.
    documents.store.execute(
        "UPDATE document_sync_queue SET status='failed',remote_current_revision=5,remote_delivery_current=0,remote_observed_at=100,updated_at=100 WHERE revision=1"
    )
    ahead = await call(documents, context, "status", site="summary")
    assert not ahead["delivery_current"] and ahead["remote_currentness_verified"]
    assert ahead["synced_revision"] == 3 and ahead["observed_remote_revision"] == 5
    assert ahead["remote_observed_at"] == 100
    assert ahead["sync"]["remote_current_revision"] == 3  # Latest job is distinct from latest observation.
    assert ahead["sync"]["remote_delivery_current"] == 1
    assert ahead["sync"]["remote_observed_at"] == 50
    documents.store.execute("UPDATE document_sync_queue SET remote_observed_at=200 WHERE revision=3")
    newer = await call(documents, context, "status", site="summary")
    assert newer["delivery_current"] and newer["observed_remote_revision"] == 3
