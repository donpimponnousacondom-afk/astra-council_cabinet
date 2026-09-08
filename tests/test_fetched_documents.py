import hashlib
import json
import time
from unittest.mock import AsyncMock

import pytest

from conftest import configured
from hortator.fetched_documents import (
    CONFIG_SCHEMA,
    DEFAULTS,
    DESCRIPTION,
    PARAMETERS,
    FetchedDocuments,
    validate_config,
)
from hortator.models import ControlError
from hortator.plugins import PluginSpec, ToolContext
from hortator.security import Vault
from hortator.store import Store, dumps
from hortator.tool_feedback import feedback


@pytest.fixture
def fetched(tmp_path):
    store = Store(tmp_path / "council.sqlite3")
    vault = Vault(store, tmp_path)
    documents = FetchedDocuments(store, tmp_path, vault)
    yield documents
    store.close()


def context(bot="ada", channel="channel-a", turn="turn-fetch"):
    return ToolContext({"id": bot}, channel, turn)


def response(documents, data, content_type="text/plain; charset=utf-8", url="https://example.com/final"):
    documents.fetcher = AsyncMock(return_value=(data, content_type, url))
    return documents.fetcher


async def call(documents, args, ctx=None, config=None):
    return await documents.call(args, ctx or context(), config or DEFAULTS, "unused-key-never-forwarded")


async def test_near_megabyte_html_unicode_marker_and_complete_pagination_survive_restart(fetched, tmp_path):
    plain = "abé🙂\n" * 109_000
    marker = "UNIQUE_MARKER_PAST_FIRST_CHUNK"
    plain = plain[:31_001] + marker + plain[31_001:]
    data = ("<html><body><p>" + plain + "</p><script>hidden()</script></body></html>").encode()
    assert 950_000 < len(data) < 1_000_000
    expected = "\n" + plain
    network = response(fetched, data, "text/html; charset=utf-8")
    first = current = await call(fetched, {"url": "https://example.com/source"})
    assert first["downloaded_bytes"] == len(data)
    assert first["stored_bytes"] == len(expected.encode())
    assert first["content_sha256"] == hashlib.sha256(expected.encode()).hexdigest()
    assert first["total_chars"] == len(expected)
    assert first["text"] == expected[:18_000] and marker not in first["text"]
    assert first["url"] == "https://example.com/final"
    assert first["fetched_at"].endswith("+00:00")
    assert "untrusted" in first["trust"]
    found = await call(fetched, {"operation": "search", "document_id": first["document_id"], "query": marker})
    assert found["matches"][0]["match_range"]["start"] == expected.index(marker) > 18_000
    assert marker in found["matches"][0]["text"]
    assert fetched.store.one("SELECT count(*) AS n FROM fetched_documents")["n"] == 1
    fetched = FetchedDocuments(fetched.store, tmp_path, fetched.vault, fetcher=network)
    pieces, offset = [], 0
    while True:
        assert current["range"]["start"] == offset
        assert current["text"] == expected[offset : current["range"]["end"]]
        assert len(dumps(current)) < 60_000
        pieces.append(current["text"])
        offset = current["range"]["end"]
        if current["next"] is None:
            break
        assert current["next"]["offset"] == offset
        current = await call(fetched, current["next"])
    assert offset == len(expected) and "".join(pieces) == expected
    assert not current["truncated"]
    assert network.await_count == 1
    assert first["_working_set"]["read"]["document_id"] == first["document_id"]


@pytest.mark.parametrize("plain", ["α🙂e\u0301\r\n終\n", "", "abc"])
async def test_character_ranges_and_exact_end_have_no_duplicate_or_missing_text(fetched, plain):
    network = response(fetched, plain.encode())
    first = current = await call(fetched, {"url": "https://example.com", "length": 3})
    chunks = [current["text"]]
    while current["next"]:
        current = await call(fetched, current["next"])
        chunks.append(current["text"])
    assert "".join(chunks) == plain
    end = await call(
        fetched, {"operation": "read", "document_id": first["document_id"], "offset": len(plain), "length": 3}
    )
    assert end["text"] == "" and end["next"] is None and not end["truncated"]
    assert end["range"] == {"start": len(plain), "end": len(plain)}
    assert network.await_count == 1
    with pytest.raises(ControlError, match="offset.*Unicode"):
        await call(
            fetched, {"operation": "read", "document_id": first["document_id"], "offset": len(plain) + 1}
        )


async def test_search_has_literal_unicode_offsets_context_and_bounded_continuation(fetched):
    plain = "\n🙂 before [MARK] after\n" * 40
    response(fetched, plain.encode())
    first = await call(fetched, {"url": "https://example.com"})
    args = {
        "operation": "search",
        "document_id": first["document_id"],
        "query": "[mark]",
        "limit": 3,
        "context_chars": 8,
    }
    starts = []
    while args:
        result = await call(fetched, args)
        assert len(result["matches"]) <= 3
        for match in result["matches"]:
            start, end = match["range"]["start"], match["range"]["end"]
            assert match["text"] == plain[start:end]
            assert plain[match["match_range"]["start"] : match["match_range"]["end"]] == "[MARK]"
            starts.append(match["match_range"]["start"])
        args = result["next"]
    expected = [offset for offset in range(len(plain)) if plain.startswith("[MARK]", offset)]
    assert starts == expected
    sensitive = await call(
        fetched,
        {
            "operation": "search",
            "document_id": first["document_id"],
            "query": "[mark]",
            "case_sensitive": True,
        },
    )
    assert sensitive["matches"] == [] and sensitive["next"] is None
    small = await call(
        fetched,
        {"operation": "search", "document_id": first["document_id"], "query": "MARK", "context_chars": 500},
        config={**DEFAULTS, "chunk_chars": 256},
    )
    assert sum(len(match["text"]) for match in small["matches"]) <= 256
    assert small["next"] is not None


async def test_snapshot_ownership_is_bot_and_channel_not_turn_or_unguessable_id(fetched):
    response(fetched, b"private snapshot")
    first = await call(fetched, {"url": "https://example.com"})
    for other in (context(bot="socrates"), context(channel="channel-b")):
        for operation in ("read", "search", "refetch"):
            with pytest.raises(ControlError, match="missing or unavailable"):
                await call(
                    fetched,
                    {"operation": operation, "document_id": first["document_id"], "query": "private"},
                    other,
                )
    later = await call(
        fetched, {"operation": "read", "document_id": first["document_id"]}, context(turn="later-turn")
    )
    assert later["text"] == "private snapshot"
    assert fetched.list_documents("socrates", "channel-a") == []
    assert fetched.list_documents("ada", "channel-b") == []
    with pytest.raises(ControlError, match="missing or unavailable"):
        fetched.read_document("ada", "channel-b", first["document_id"])


async def test_refetch_is_explicit_new_snapshot_and_old_snapshot_remains_stable(fetched):
    fetched.fetcher = AsyncMock(
        side_effect=[
            (b"original", "text/plain", "https://example.com/one"),
            (b"changed", "text/plain", "https://example.com/two"),
        ]
    )
    first = await call(fetched, {"url": "https://example.com/requested"})
    second = await call(fetched, {"operation": "refetch", "document_id": first["document_id"]})
    assert second["document_id"] != first["document_id"]
    assert second["replaces_document_id"] == first["document_id"]
    assert second["text"] == "changed"
    assert (await call(fetched, {"operation": "read", "document_id": first["document_id"]}))[
        "text"
    ] == "original"
    assert fetched.fetcher.await_count == 2
    assert all(item.args == ("https://example.com/requested",) for item in fetched.fetcher.await_args_list)


async def test_retention_protects_running_references_then_expires_and_refetches(fetched):
    now = time.time()
    fetched.store.execute(
        """INSERT INTO turns(id,bot_id,channel_id,profile_id,provider_id,model,started_at,status,trigger,revision)
        VALUES('turn-fetch','ada','channel-a','p','provider','m',?,'running','test',1)""",
        (now,),
    )
    response(fetched, b"kept while a turn references it")
    first = await call(fetched, {"url": "https://example.com"})
    row = fetched.store.one("SELECT * FROM fetched_documents WHERE id=?", (first["document_id"],))
    path = fetched._path(row)
    fetched.store.execute("UPDATE fetched_documents SET expires_at=?", (now - 1,))
    assert fetched.cleanup()["expired_snapshots"] == 0 and path.exists()
    assert (await call(fetched, {"operation": "read", "document_id": first["document_id"]}))["text"]
    fetched.store.execute("UPDATE turns SET status='completed',ended_at=?", (now,))
    # Dashboard inspection reports expiry but must not mutate the file or metadata.
    assert fetched.list_documents("ada")[0]["status"] == "expired"
    assert path.exists()
    with pytest.raises(ControlError, match="expired.*refetch"):
        fetched.read_document("ada", "channel-a", first["document_id"])
    assert path.exists()
    assert fetched.cleanup()["expired_snapshots"] == 1 and not path.exists()
    second = await call(fetched, {"operation": "refetch", "document_id": first["document_id"]})
    assert second["document_id"] != first["document_id"]
    assert fetched.fetcher.await_count == 2


async def test_quota_is_per_bot_across_channels_and_does_not_evict_unexpired_snapshots(fetched):
    response(fetched, b"a" * 800)
    config = {**DEFAULTS, "storage_quota_bytes": 1024}
    first = await call(fetched, {"url": "https://example.com/one"}, config=config)
    with pytest.raises(ControlError, match="1024.*800.*800"):
        await call(fetched, {"url": "https://example.com/two"}, context(channel="channel-b"), config)
    assert (await call(fetched, {"operation": "read", "document_id": first["document_id"]}))[
        "text"
    ] == "a" * 800
    other = await call(fetched, {"url": "https://example.com/three"}, context(bot="socrates"), config)
    assert other["text"] == "a" * 800
    assert len(fetched.list_documents()) == 2


async def test_orphan_files_consume_quota_without_automatic_data_deletion(fetched):
    response(fetched, b"a" * 600)
    first = await call(fetched, {"url": "https://example.com"})
    path = next(fetched.directory.rglob("*.txt"))
    orphan = path.parent / "tmp-interrupted-write"
    orphan.write_bytes(b"x" * 1000)
    with pytest.raises(ControlError, match="1600 bytes are retained"):
        await call(
            fetched, {"url": "https://example.com/new"}, config={**DEFAULTS, "storage_quota_bytes": 2000}
        )
    assert orphan.read_bytes() == b"x" * 1000
    assert fetched.read_document("ada", "channel-a", first["document_id"])["text"] == "a" * 600


async def test_expired_metadata_is_bounded_without_removing_live_snapshots(fetched, monkeypatch):
    import hortator.fetched_documents as module

    monkeypatch.setattr(module, "MAX_EXPIRED_METADATA", 2)
    response(fetched, b"snapshot")
    for index in range(5):
        await call(fetched, {"url": f"https://example.com/{index}"})
    fetched.store.execute("UPDATE fetched_documents SET expires_at=?", (time.time() - 1,))
    assert fetched.cleanup()["expired_snapshots"] == 5
    assert len(fetched.list_documents()) == 2
    assert not list(fetched.directory.rglob("*.txt"))
    latest = await call(fetched, {"url": "https://example.com/new"})
    assert len(fetched.list_documents()) == 3
    assert fetched.read_document("ada", "channel-a", latest["document_id"])["text"] == "snapshot"


@pytest.mark.parametrize("mime,data", [("image/png", b"PNG"), ("application/pdf", b"%PDF"), ("", b"binary")])
async def test_binary_responses_explain_attachment_import_without_persistence(fetched, mime, data):
    response(fetched, data, mime)
    with pytest.raises(ControlError, match="workspace attachment import"):
        await call(fetched, {"url": "https://example.com"})
    assert fetched.list_documents() == []


async def test_false_text_binary_limit_and_invalid_encoding_have_actionable_failures(fetched):
    response(fetched, b"bad\x00content")
    with pytest.raises(ControlError, match="binary control.*attachment import"):
        await call(fetched, {"url": "https://example.com"})
    response(fetched, b"x" * 1025)
    with pytest.raises(ControlError, match="1024.*smaller source"):
        await call(fetched, {"url": "https://example.com"}, config={**DEFAULTS, "max_download_bytes": 1024})
    response(fetched, b"text", "text/plain; charset=invalid-charset")
    with pytest.raises(ControlError, match="unsupported character encoding"):
        await call(fetched, {"url": "https://example.com"})
    assert fetched.list_documents() == []


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/",
        "https://example.com:8000",
        "https://u:p@example.com",
    ],
)
async def test_existing_public_url_boundary_is_used_even_with_injected_fetcher(fetched, url):
    network = response(fetched, b"must not fetch")
    with pytest.raises(ControlError):
        await call(fetched, {"url": url})
    network.assert_not_awaited()


@pytest.mark.parametrize("scenario", ["success", "private_redirect", "download_limit"])
async def test_real_fetch_helper_keeps_redirect_socket_deadline_and_environment_safety(
    fetched, monkeypatch, scenario
):
    import hortator.plugins as module

    requests = []
    sessions = []
    monkeypatch.setenv("HTTP_PROXY", "http://synthetic-proxy-credential@127.0.0.1:18118")
    monkeypatch.setenv("HTTPS_PROXY", "http://synthetic-proxy-credential@127.0.0.1:18118")

    class Stream:
        async def iter_chunked(self, size):
            assert size == 65536
            yield b"a" * 800
            if scenario == "download_limit":
                yield b"b" * 300

    class Response:
        def __init__(self, url):
            self.url = url
            self.status = 302 if url.endswith("/redirect") else 200
            self.headers = {"Content-Type": "text/plain"}
            if self.status == 302:
                self.headers["Location"] = (
                    "http://127.0.0.1/private" if scenario == "private_redirect" else "/final"
                )
            self.content = Stream()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Session:
        def __init__(self, **kwargs):
            sessions.append(kwargs)
            self.connector = kwargs["connector"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.connector.close()

        def get(self, url, **kwargs):
            requests.append((url, kwargs))
            return Response(url)

    monkeypatch.setattr(module.aiohttp, "ClientSession", Session)
    if scenario == "private_redirect":
        with pytest.raises(ControlError, match="Private addresses"):
            await call(fetched, {"url": "https://example.com/redirect"})
        assert len(requests) == 1
    elif scenario == "download_limit":
        with pytest.raises(ControlError, match="1024.*smaller source"):
            await call(
                fetched,
                {"url": "https://example.com/redirect"},
                config={**DEFAULTS, "max_download_bytes": 1024},
            )
    else:
        result = await call(fetched, {"url": "https://example.com/redirect"})
        assert result["url"] == "https://example.com/final" and result["text"] == "a" * 800
    assert sessions[0]["trust_env"] is False
    assert isinstance(sessions[0]["cookie_jar"], module.aiohttp.DummyCookieJar)
    assert isinstance(sessions[0]["connector"]._resolver, module.PublicResolver)
    assert sessions[0]["timeout"].total == 25
    for _, kwargs in requests:
        assert kwargs["allow_redirects"] is False
        assert kwargs["headers"] == {"User-Agent": "Hortator/0.1"}
        assert "auth" not in kwargs


async def test_redaction_precedes_persistence_offsets_hash_and_does_not_forward_vault_keys(fetched):
    secret = "synthetic-provider-key-for-test"
    fetched.vault.put("provider/example", secret)
    data = ("before " + secret + " after").encode()
    network = response(fetched, data)
    first = await call(fetched, {"url": "https://example.com"})
    expected = "before [REDACTED] after"
    assert first["text"] == expected and first["total_chars"] == len(expected)
    assert first["content_sha256"] == hashlib.sha256(expected.encode()).hexdigest()
    assert secret not in str(fetched.store.events())
    path = next(fetched.directory.rglob("*.txt"))
    assert path.read_text() == expected
    assert network.await_args.kwargs == {"limit": 1_000_000}
    with pytest.raises(ControlError, match="protected credential"):
        await call(fetched, {"url": "https://example.com/?key=" + secret})
    assert network.await_count == 1


async def test_text_encoding_and_storage_integrity_are_explicit(fetched):
    plain = "café\r\nfin"
    response(fetched, plain.encode("latin-1"), "text/plain; charset=iso-8859-1")
    first = await call(fetched, {"url": "https://example.com"})
    assert first["text"] == plain and first["encoding"] == "iso8859-1"
    path = next(fetched.directory.rglob("*.txt"))
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_bytes(b"changed!")
    with pytest.raises(ControlError, match="integrity.*refetch"):
        fetched.read_document("ada", "channel-a", first["document_id"])


async def test_read_rejects_symlink_and_missing_blob(fetched, tmp_path):
    response(fetched, b"private snapshot")
    first = await call(fetched, {"url": "https://example.com"})
    path = next(fetched.directory.rglob("*.txt"))
    path.unlink()
    with pytest.raises(ControlError, match="file is missing.*refetch"):
        fetched.read_document("ada", "channel-a", first["document_id"])
    outside = tmp_path / "outside-sentinel"
    outside.write_text("private host file")
    path.symlink_to(outside)
    with pytest.raises(ControlError, match="symbolic link"):
        fetched.read_document("ada", "channel-a", first["document_id"])


def test_configuration_validates_all_limits_and_rejects_coercion():
    assert validate_config({}) == DEFAULTS
    config = {
        "max_download_bytes": "1000000",
        "chunk_chars": 18_001,
        "storage_quota_bytes": True,
        "retention_seconds": 0,
    }
    report = feedback("configuration", config, CONFIG_SCHEMA)
    assert report["error_count"] == 4
    with pytest.raises(ControlError) as caught:
        validate_config(config)
    assert all(field in str(caught.value) for field in config)
    with pytest.raises(ControlError, match="Additional properties"):
        validate_config({"unknown_limit": 50})


async def test_custom_chunk_read_limit_and_readonly_dashboard_do_not_change_snapshot(fetched):
    response(fetched, b"a" * 1024)
    first = await call(fetched, {"url": "https://example.com"}, config={**DEFAULTS, "chunk_chars": 256})
    assert len(first["text"]) == 256
    with pytest.raises(ControlError, match="configured 256 character"):
        await call(
            fetched,
            {"operation": "read", "document_id": first["document_id"], "length": 257},
            config={**DEFAULTS, "chunk_chars": 256},
        )
    before = fetched.store.db.total_changes
    metadata = fetched.list_documents("ada", "channel-a")
    result = fetched.read_document("ada", "channel-a", metadata[0]["document_id"], 512, 5)
    assert result["text"] == "aaaaa" and result["range"] == {"start": 512, "end": 517}
    assert fetched.store.db.total_changes == before


async def test_registry_complete_usage_errors_and_global_and_bot_revocation(kernel, tmp_path):
    documents = FetchedDocuments(kernel.store, tmp_path, kernel.vault)
    network = response(documents, b"test source")
    kernel.registry.specs["web_fetch"] = PluginSpec(
        "web_fetch", "Web fetch", DESCRIPTION, PARAMETERS, documents.call, DEFAULTS
    )
    bot = configured(kernel, enabled_plugins=["web_fetch"])
    ctx = ToolContext(bot, "channel-a", "turn-fetch")
    help_result = await kernel.registry.call("web_fetch", {}, ctx, "help")
    assert help_result["usage_only"] and not help_result["executed"]
    assert help_result["usage"]["example"] == {"url": "https://example.com"}
    invalid = await kernel.registry.call(
        "web_fetch",
        {"operation": "search", "query": 17, "offset": -1, "limit": 100, "extra": True},
        ctx,
        "invalid",
    )
    assert not invalid["executed"] and invalid["error_count"] >= 5
    assert invalid["usage"]["example"]["operation"] == "search"
    assert "document_id" in invalid["usage"]["example"]
    malformed = await kernel.registry.call_raw("web_fetch", '{"url":', ctx, "malformed")
    assert malformed["errors"][0]["rule"] == "json" and "usage" in malformed
    network.assert_not_awaited()
    started = await kernel.registry.call("web_fetch", {"operation": "start"}, ctx, "start")
    assert started["web_task_started"] and documents.list_documents() == []
    network.assert_not_awaited()
    fetched_result = await kernel.registry.call("web_fetch", {"url": "https://example.com"}, ctx, "fetch")
    document_id = fetched_result["document_id"]
    kernel.store.put("bots", {**bot, "enabled_plugins": []})
    bot_denied = await kernel.registry.call(
        "web_fetch",
        {"operation": "read", "document_id": document_id},
        ToolContext({**bot, "enabled_plugins": []}, "channel-a", "turn-fetch"),
        "bot-denied",
    )
    assert not bot_denied["ok"]
    kernel.store.put("bots", bot)
    plugin = kernel.store.get("plugins", "web_fetch")
    kernel.store.put("plugins", {**plugin, "enabled": False})
    denied = await kernel.registry.call(
        "web_fetch", {"operation": "read", "document_id": document_id}, ctx, "denied"
    )
    assert not denied["ok"] and "not enabled" in denied["error"]
    assert documents.read_document("ada", "channel-a", document_id)["text"] == "test source"


@pytest.mark.parametrize(
    "arguments",
    [
        {"operation": "start", "url": "https://example.com"},
        {"operation": "read", "url": "https://example.com"},
        {"document_id": "fetch_0123456789abcdef0123"},
        {"operation": "read_result", "result_id": False, "length": "10", "offset": -1},
    ],
)
def test_operation_schema_rejects_ignored_fields_and_incomplete_calls(arguments):
    result = feedback("web_fetch", arguments, PARAMETERS, DESCRIPTION)
    assert result and not result["ok"] and not result["executed"]
    assert "usage" in result and result["error_count"] >= 1
    json.dumps(result)
