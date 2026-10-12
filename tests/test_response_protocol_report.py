import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from hortator.response_protocol_report import parse_until, response_protocol_report


END = parse_until("2026-10-12T12:00:00Z")
CALLS = [{"id": "call", "function": {"name": "memory", "arguments": "private tool arguments"}}]


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "council.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE requests(id TEXT PRIMARY KEY,bot_id TEXT,provider_id TEXT,profile_id TEXT,"
            "model TEXT,started_at REAL,status TEXT,purpose TEXT,context TEXT,response TEXT)"
        )

    def add(identifier, *, content=None, calls=None, finish="stop", at=None, **changes):
        values = {
            "id": identifier,
            "bot_id": "bot-original",
            "provider_id": "provider-original",
            "profile_id": "profile-original",
            "model": "model-original",
            "started_at": END - 10 if at is None else at,
            "status": "completed",
            "purpose": "generation",
            "context": json.dumps({"profile_revision": 3, "provider_revision": 5}),
            "response": json.dumps({"content": content, "tool_calls": calls, "finish_reason": finish}),
            **changes,
        }
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)",
                tuple(
                    values[key]
                    for key in (
                        "id",
                        "bot_id",
                        "provider_id",
                        "profile_id",
                        "model",
                        "started_at",
                        "status",
                        "purpose",
                        "context",
                        "response",
                    )
                ),
            )

    return path, add


def read(path, **changes):
    return response_protocol_report(path, until=END, **changes)


def test_shapes_ignore_unicode_whitespace_and_separate_interrupted_finishes(ledger):
    path, add = ledger
    add("text", content="Answer")
    add("tools", content="\t\n\u2003", calls=CALLS, finish="tool_calls")
    add("mixed", content="Checking", calls=CALLS, finish="tool_calls")
    add("empty", content="\u2003\n")
    add("cut", content="Partial", calls=CALLS, finish="length")
    add("filter", content="Partial", calls=CALLS, finish="content_filter")
    add("invalid", response="not JSON containing private prose")
    report = read(path)
    counts = report["counts"]
    assert counts["requests"] == 7
    assert counts["non_interrupted"] == {"text_only": 1, "tool_only": 1, "text_and_tools": 1, "empty": 1}
    assert counts["interrupted"]["text_and_tools"] == 2
    assert counts["unreadable"] == 1
    assert report["groups"][0]["mixed_output_evidence"] == "observed"
    serialized = json.dumps(report)
    assert "Checking" not in serialized
    assert "Partial" not in serialized
    assert "private prose" not in serialized
    assert "private tool arguments" not in serialized


def test_historical_identity_and_both_revisions_define_independent_groups(ledger):
    path, add = ledger
    add("original", content="mixed", calls=CALLS)
    for key in ("bot_id", "provider_id", "profile_id", "model"):
        add(key, content="text", **{key: "different"})
    add("profile-revision", context=json.dumps({"profile_revision": 4, "provider_revision": 5}))
    add("provider-revision", context=json.dumps({"profile_revision": 3, "provider_revision": 6}))
    add("no-revisions", context="{}")
    report = read(path)
    assert len(report["groups"]) == 8
    observed = [group for group in report["groups"] if group["mixed_output_evidence"] == "observed"]
    assert len(observed) == 1
    assert observed[0]["model"] == "model-original"
    assert observed[0]["profile_revision"] == 3
    assert observed[0]["provider_revision"] == 5
    assert any(
        group["profile_revision"] is None and group["provider_revision"] is None for group in report["groups"]
    )
    # There is deliberately no bots/profiles/providers configuration table to join.


def test_no_observations_are_unknown_and_interrupted_only_is_distinct(ledger):
    path, add = ledger
    report = read(path)
    assert report["sampled_rows"] == 0
    assert report["groups"] == []
    assert report["sample_first_at"] is None and report["sample_last_at"] is None
    add("tools", calls=CALLS, finish="tool_calls")
    assert read(path)["groups"][0]["mixed_output_evidence"] == "unknown"
    add("cut", content="Partial", calls=CALLS, finish="length")
    assert read(path)["groups"][0]["mixed_output_evidence"] == "observed_interrupted_only"


def test_window_excludes_other_statuses_and_purposes_and_caps_newest_rows(ledger):
    path, add = ledger
    add("outside", at=END - 3601, content="text")
    add("start", at=END - 3600, content="text")
    add("middle", at=END - 30, content="text")
    add("newest", at=END - 1, content="mixed", calls=CALLS)
    add("end-exclusive", at=END, content="text")
    add("failed", at=END - 0.5, content="text", status="failed")
    add("compaction", at=END - 0.5, content="text", purpose="compaction")
    add("research", at=END - 0.5, content="text", purpose="research")
    report = read(path, hours=1, max_rows=2)
    assert report["sampled_rows"] == 2
    assert report["max_rows"] == 2 and report["truncated"] is True
    assert report["sample_first_at"] == "2026-10-12T11:59:30Z"
    assert report["sample_last_at"] == "2026-10-12T11:59:59Z"
    assert report["counts"]["non_interrupted"]["text_and_tools"] == 1
    complete = read(path, hours=1, max_rows=3)
    assert complete["sampled_rows"] == 3 and complete["truncated"] is False
    assert complete["sample_first_at"] == "2026-10-12T11:00:00Z"


@pytest.mark.parametrize(
    "changes",
    [{"hours": 0}, {"hours": 721}, {"hours": 1.5}, {"max_rows": 0}, {"max_rows": 10001}, {"max_rows": True}],
)
def test_invalid_limits_are_rejected_before_opening_database(tmp_path, changes):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(ValueError):
        read(path, **changes)
    assert not path.exists()


@pytest.mark.parametrize("value", ["2026-10-12T12:00:00", "yesterday", "", "2026-13-12T12:00:00Z"])
def test_until_requires_a_valid_explicit_timezone(value):
    with pytest.raises(ValueError, match="explicit UTC offset"):
        parse_until(value)


def test_until_normalizes_offsets():
    assert parse_until("2026-10-12T14:00:00+02:00") == END


def test_report_connection_rejects_writes_and_database_is_unchanged(ledger, monkeypatch):
    path, add = ledger
    add("mixed", content="Check", calls=CALLS)
    original = sqlite3.connect
    before = hashlib.sha256(path.read_bytes()).digest()

    def checked_connect(*args, **kwargs):
        db = original(*args, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("CREATE TABLE forbidden_write(value)")
        return db

    monkeypatch.setattr(sqlite3, "connect", checked_connect)
    assert read(path)["sampled_rows"] == 1
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_live_wal_reads_committed_rows_without_initializing_or_locking_runtime(ledger):
    path, add = ledger
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        add("committed", content="Check", calls=CALLS)
        assert read(path)["sampled_rows"] == 1
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("DELETE FROM requests")
        assert read(path)["sampled_rows"] == 1
        writer.rollback()
    assert read(path)["sampled_rows"] == 1


def command(directory, *args):
    # -S excludes site packages: successful reporting cannot depend on Kernel,
    # Vault, cryptography, Pydantic, provider clients or application imports.
    root = Path(__file__).resolve().parents[1]
    program = (
        "import sys; sys.path.insert(0, sys.argv.pop(1)); "
        "from hortator.cli import main; main(); "
        "assert 'hortator.app' not in sys.modules; "
        "assert 'hortator.security' not in sys.modules"
    )
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            program,
            str(root),
            "--data-dir",
            str(directory),
            "response-protocol-report",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_cli_reads_existing_ledger_without_vault_initialization_or_runtime_lock(ledger):
    path, add = ledger
    add("mixed", content="private prose", calls=CALLS)
    before = sorted(entry.name for entry in path.parent.iterdir())
    result = command(path.parent, "--until", "2026-10-12T12:00:00Z")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["sampled_rows"] == 1
    assert "private prose" not in result.stdout
    assert result.stderr == ""
    assert sorted(entry.name for entry in path.parent.iterdir()) == before
    assert not (path.parent / "master.key").exists()
    assert not (path.parent / "runtime.lock").exists()


def test_cli_does_not_initialize_a_missing_data_directory(tmp_path):
    directory = tmp_path / "missing"
    result = command(directory)
    assert result.returncode == 2
    assert "no data or credentials were initialized" in result.stderr
    assert result.stdout == ""
    assert not directory.exists()
