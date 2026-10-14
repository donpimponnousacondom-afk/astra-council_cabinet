"""Durable prompt-memory snapshots are separate from canonical note storage."""

import json
import sqlite3
from copy import deepcopy

import pytest

from conftest import configured
from hortator.app import Kernel
from hortator.memory_epochs import FORMAT_VERSION, MemoryEpochs
from hortator.snapshots import Snapshots, schema
from hortator.store import Store


CHANNEL = "222222222222222222"
BUILD = {
    "commit": "a" * 40,
    "short_commit": "a" * 12,
    "dirty": False,
    "package_version": "0.1.0",
    "commit_title": "Memory epoch fixture",
    "started_at": "2026-10-14T00:00:00Z",
}


@pytest.fixture
def epochs(tmp_path):
    store = Store(tmp_path / "epochs.sqlite3")
    try:
        yield MemoryEpochs(store)
    finally:
        store.close()


def candidate(epochs, *, bot_id="ada", channel_id=CHANNEL, basis_hash="basis", notes=None, layers=None):
    return epochs.candidate(
        bot_id,
        channel_id,
        basis_hash,
        notes
        if notes is not None
        else {"memory": {"a": {"key": "a", "value": "é\x00before"}}, "global_memory": {}},
        layers
        if layers is not None
        else [
            {
                "id": "memory",
                "template_id": "runtime-memory",
                "revision": 2,
                "variables": ["notes"],
                "role": "user",
                "content": "Exact rendered note\nincluding braces {notes}",
            }
        ],
    )


def canonical_notes(store, bot_id, channel_id):
    return {
        "memory": {
            row["key"]: row
            for row in store.rows(
                "SELECT key,value FROM memories WHERE bot_id=? AND channel_id=? ORDER BY key",
                (bot_id, channel_id),
            )
        },
        "global_memory": {
            row["key"]: row
            for row in store.rows(
                "SELECT key,value,source_channel_id,updated_at FROM global_memories WHERE bot_id=? ORDER BY key",
                (bot_id,),
            )
        },
    }


def seed_scopes(store):
    epochs = MemoryEpochs(store)
    result = {}
    for bot_id, channel_id in (("ada", CHANNEL), ("ada", "other"), ("socrates", CHANNEL)):
        store.context(bot_id, channel_id)
        store.execute(
            "INSERT INTO memories VALUES(?,?,?,?,?)",
            (bot_id, channel_id, "note", f"{bot_id}/{channel_id}", 1),
        )
        result[bot_id, channel_id] = epochs.save(candidate(epochs, bot_id=bot_id, channel_id=channel_id))
    store.execute(
        "INSERT INTO global_memories VALUES(?,?,?,?,?)", ("ada", "global", "canonical global", CHANNEL, 1)
    )
    return result


def test_candidates_and_loaded_values_are_detached_without_implicit_writes(epochs):
    notes = {"memory": {"a": {"key": "a", "value": "original"}}, "global_memory": {}}
    layers = [{"id": "memory", "role": "system", "content": "original render"}]
    snapshot = candidate(epochs, notes=notes, layers=layers)
    notes["memory"]["a"]["value"] = "mutated source"
    layers[0]["content"] = "mutated render"
    assert snapshot["notes"]["memory"]["a"]["value"] == "original"
    assert snapshot["layers"][0]["content"] == "original render"
    assert epochs.load("ada", CHANNEL) is None
    returned = epochs.save(snapshot)
    returned["notes"]["memory"]["a"]["value"] = "mutated result"
    loaded = epochs.load("ada", CHANNEL)
    loaded["layers"][0]["content"] = "mutated load"
    assert epochs.load("ada", CHANNEL) == snapshot


def test_select_preserves_the_original_epoch_and_rendering_after_live_writes_and_store_reopen(epochs):
    original = epochs.select(candidate(epochs))
    changed = candidate(epochs)
    changed["notes"]["memory"]["a"]["value"] = "new live value"
    changed["layers"][0]["content"] = "new live rendering"
    reopened = Store(epochs.store.path)
    try:
        again = MemoryEpochs(reopened)
        assert again.select(changed) == original
        assert again.net_changes(original, changed["notes"])["memory"] == [
            {"operation": "replace", "note": {"key": "a", "value": "new live value"}}
        ]
    finally:
        reopened.close()


def test_explicit_save_participates_in_the_callers_transaction(epochs):
    original = epochs.select(candidate(epochs))
    next_epoch = candidate(epochs)
    epochs.store.execute("BEGIN IMMEDIATE")
    try:
        epochs.save(next_epoch)
        assert epochs.load("ada", CHANNEL) == next_epoch
    finally:
        epochs.store.execute("ROLLBACK")
    assert epochs.load("ada", CHANNEL) == original


@pytest.mark.parametrize("change", ["basis", "version", "scope", "invalid_layer"])
def test_incompatible_stored_snapshot_is_recaptured_only_for_its_own_scope(epochs, change):
    original = epochs.select(candidate(epochs))
    other = epochs.select(candidate(epochs, channel_id="other"))
    fresh = candidate(epochs)
    if change == "basis":
        fresh["basis_hash"] = "changed-template-basis"
    elif change == "version":
        epochs.store.execute(
            "UPDATE memory_snapshots SET format_version=? WHERE channel_id=?", (FORMAT_VERSION + 1, CHANNEL)
        )
    elif change == "scope":
        fresh["notes"] = {}
        fresh["layers"] = []
    else:
        epochs.store.execute("UPDATE memory_snapshots SET layers_json='[{}]' WHERE channel_id=?", (CHANNEL,))
    assert epochs.select(fresh) == fresh
    assert fresh["epoch_id"] != original["epoch_id"]
    assert epochs.load("ada", "other") == other


def test_net_changes_replace_complete_records_delete_explicitly_and_keep_empty_values(epochs):
    snapshot = candidate(epochs)
    current = {
        "memory": {"z": {"key": "z", "value": ""}},
        "global_memory": {
            "g": {"key": "g", "value": "new", "source_channel_id": "elsewhere", "updated_at": 4}
        },
    }
    changes = epochs.net_changes(snapshot, current)
    assert changes == {
        "memory": [
            {"operation": "delete", "key": "a"},
            {"operation": "replace", "note": current["memory"]["z"]},
        ],
        "global_memory": [{"operation": "replace", "note": current["global_memory"]["g"]}],
    }
    current["memory"]["z"]["value"] = "later"
    assert changes["memory"][1]["note"]["value"] == ""
    assert epochs.net_changes(snapshot, snapshot["notes"]) == {"memory": [], "global_memory": []}
    assert epochs.load("ada", CHANNEL) is None


def test_repeated_changes_coalesce_and_global_metadata_changes_are_retained(epochs):
    notes = {
        "memory": {},
        "global_memory": {
            "g": {"key": "g", "value": "same text", "source_channel_id": "first", "updated_at": 1}
        },
    }
    snapshot = candidate(epochs, notes=notes, layers=[])
    current = deepcopy(notes)
    current["global_memory"]["g"]["value"] = "intermediate write"
    current["global_memory"]["g"]["value"] = "same text"
    assert epochs.net_changes(snapshot, current) == {"memory": [], "global_memory": []}
    current["global_memory"]["g"].update(source_channel_id="second", updated_at=2)
    assert epochs.net_changes(snapshot, current)["global_memory"] == [
        {"operation": "replace", "note": current["global_memory"]["g"]}
    ]
    current["global_memory"].clear()
    assert epochs.net_changes(snapshot, current)["global_memory"] == [{"operation": "delete", "key": "g"}]


def test_source_fingerprint_ignores_clock_epoch_and_mapping_order_but_detects_content_changes(epochs):
    first = candidate(epochs)
    second = candidate(epochs)
    second["created_at"] += 100
    second["notes"] = {"global_memory": {}, "memory": {"a": {"value": "é\x00before", "key": "a"}}}
    assert first["epoch_id"] != second["epoch_id"]
    assert epochs.source_fingerprint(first) == epochs.source_fingerprint(second)
    for field in ("basis_hash", "notes", "layers"):
        changed = deepcopy(second)
        if field == "basis_hash":
            changed[field] = "new basis"
        elif field == "notes":
            changed[field]["memory"]["a"]["value"] += "!"
        else:
            changed[field][0]["content"] += " "
        assert epochs.source_fingerprint(first) != epochs.source_fingerprint(changed)
    metadata = epochs.metadata(first)
    assert metadata["note_counts"] == {"memory": 1, "global_memory": 0}
    assert "Exact rendered note" not in json.dumps(metadata)
    assert metadata["rendered_chars"] == len(first["layers"][0]["content"])


@pytest.mark.parametrize(
    "notes",
    [
        {"unknown": {}},
        {"memory": []},
        {"memory": {"a": {"key": "wrong", "value": "text"}}},
        {"memory": {"a": {"key": "a", "value": None}}},
    ],
)
def test_invalid_candidate_data_never_persists(epochs, notes):
    with pytest.raises(ValueError):
        candidate(epochs, notes=notes)
    assert epochs.load("ada", CHANNEL) is None


@pytest.mark.parametrize("column", ["notes_json", "layers_json"])
def test_malformed_storage_json_fails_explicitly_without_replacing_evidence(epochs, column):
    original = epochs.save(candidate(epochs))
    epochs.store.execute(f"UPDATE memory_snapshots SET {column}='malformed'")
    with pytest.raises(json.JSONDecodeError):
        epochs.select(candidate(epochs))
    assert epochs.store.one("SELECT epoch_id FROM memory_snapshots")["epoch_id"] == original["epoch_id"]


def test_existing_database_gets_additive_snapshot_table_without_changing_notes(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE memories(bot_id TEXT,channel_id TEXT,key TEXT,value TEXT,updated_at REAL,"
            "PRIMARY KEY(bot_id,channel_id,key))"
        )
        db.execute("INSERT INTO memories VALUES('ada','room','topic','keep me',1)")
    store = Store(path)
    try:
        assert store.one("SELECT value FROM memories")["value"] == "keep me"
        assert store.rows("SELECT * FROM memory_snapshots") == []
        epochs = MemoryEpochs(store)
        assert epochs.select(candidate(epochs))["format_version"] == FORMAT_VERSION
    finally:
        store.close()


@pytest.mark.parametrize("channel_id", [CHANNEL, None])
async def test_context_reset_clears_only_target_epochs_and_preserves_canonical_notes(
    kernel, owner, channel_id
):
    configured(kernel)
    saved = seed_scopes(kernel.store)
    before = kernel.store.rows("SELECT * FROM memories") + kernel.store.rows("SELECT * FROM global_memories")
    await kernel.service.control(
        owner,
        {
            "action": "reset_context",
            "kind": "bots",
            "id": "ada",
            "data": {"confirm_bot_id": "ada", **({"channel_id": channel_id} if channel_id else {})},
        },
    )
    epochs = MemoryEpochs(kernel.store)
    assert epochs.load("ada", CHANNEL) is None
    assert epochs.load("ada", "other") == (saved["ada", "other"] if channel_id else None)
    assert epochs.load("socrates", CHANNEL) == saved["socrates", CHANNEL]
    assert before == kernel.store.rows("SELECT * FROM memories") + kernel.store.rows(
        "SELECT * FROM global_memories"
    )


async def test_failed_reset_rolls_back_epoch_clear_with_context_boundary(kernel, owner, monkeypatch):
    configured(kernel)
    saved = seed_scopes(kernel.store)
    original_emit = kernel.store.emit

    def fail_reset_event(kind, *args, **kwargs):
        if kind == "context.reset":
            raise RuntimeError("simulated failure before reset commit")
        return original_emit(kind, *args, **kwargs)

    monkeypatch.setattr(kernel.store, "emit", fail_reset_event)
    with pytest.raises(RuntimeError, match="before reset commit"):
        await kernel.service.control(
            owner,
            {"action": "reset_context", "kind": "bots", "id": "ada", "data": {"confirm_bot_id": "ada"}},
        )
    assert MemoryEpochs(kernel.store).load("ada", CHANNEL) == saved["ada", CHANNEL]
    assert kernel.store.context_boundary("ada", CHANNEL) is None


async def test_bot_deletion_clears_pins_after_work_drains_and_keeps_notes(kernel, owner, monkeypatch):
    saved = seed_scopes(kernel.store)
    epochs = MemoryEpochs(kernel.store)
    before = kernel.store.rows("SELECT * FROM memories") + kernel.store.rows("SELECT * FROM global_memories")

    async def drain(ids, reason):
        assert ids == ["ada"]
        assert epochs.load("ada", CHANNEL) == saved["ada", CHANNEL]
        epochs.save(candidate(epochs))

    monkeypatch.setattr(kernel.engine, "cancel", drain)
    await kernel.service.delete(owner, "bots", "ada")
    assert epochs.load("ada", CHANNEL) is None
    assert epochs.load("ada", "other") is None
    assert epochs.load("socrates", CHANNEL) == saved["socrates", CHANNEL]
    assert before == kernel.store.rows("SELECT * FROM memories") + kernel.store.rows(
        "SELECT * FROM global_memories"
    )


@pytest.fixture
def snapshot_fixture(tmp_path):
    directory = tmp_path / "snapshot-live"
    kernel = Kernel(directory)
    seed_scopes(kernel.store)
    epochs = MemoryEpochs(kernel.store)
    # Pins here reflect the canonical baseline so restored deletions are visible
    # as net changes against a later, independently pinned epoch.
    for bot_id, channel_id in (("ada", CHANNEL), ("ada", "other"), ("socrates", CHANNEL)):
        epochs.save(
            candidate(
                epochs,
                bot_id=bot_id,
                channel_id=channel_id,
                notes=canonical_notes(kernel.store, bot_id, channel_id),
            )
        )
    fingerprint = schema(kernel.store.db)
    kernel.store.close()
    manager = Snapshots(directory, BUILD)
    manifest = manager.capture("Memory snapshot baseline")
    return manager, directory, fingerprint, manifest["id"]


@pytest.mark.parametrize("channel_id", [CHANNEL, None])
def test_context_restore_discards_only_matching_pins(snapshot_fixture, channel_id):
    manager, directory, fingerprint, identifier = snapshot_fixture
    manager.restore(
        identifier,
        fingerprint,
        scope="bot",
        bot_id="ada",
        channel_id=channel_id,
        include_context=True,
    )
    store = Store(directory / "council.sqlite3")
    try:
        epochs = MemoryEpochs(store)
        assert epochs.load("ada", CHANNEL) is None
        assert bool(epochs.load("ada", "other")) == bool(channel_id)
        assert epochs.load("socrates", CHANNEL) is not None
        assert len(store.rows("SELECT * FROM memories")) == 3
    finally:
        store.close()


def test_notes_and_global_restore_keep_pins_and_expose_complete_changes_in_each_channel(snapshot_fixture):
    manager, directory, fingerprint, identifier = snapshot_fixture
    store = Store(directory / "council.sqlite3")
    try:
        store.execute(
            "UPDATE memories SET value='newer private' WHERE bot_id='ada' AND channel_id=?", (CHANNEL,)
        )
        store.execute("INSERT INTO memories VALUES('ada',?,'added','added later',2)", (CHANNEL,))
        store.execute("UPDATE global_memories SET value='newer global',updated_at=2 WHERE bot_id='ada'")
        store.execute("INSERT INTO global_memories VALUES('ada','added','added globally',?,2)", (CHANNEL,))
        epochs = MemoryEpochs(store)
        saved = {}
        for channel_id in (CHANNEL, "other"):
            saved[channel_id] = epochs.save(
                candidate(epochs, channel_id=channel_id, notes=canonical_notes(store, "ada", channel_id))
            )
    finally:
        store.close()
    manager.restore(
        identifier,
        fingerprint,
        scope="bot",
        bot_id="ada",
        channel_id=CHANNEL,
        include_global_memory=True,
    )
    store = Store(directory / "council.sqlite3")
    try:
        epochs = MemoryEpochs(store)
        for channel_id in (CHANNEL, "other"):
            assert epochs.load("ada", channel_id) == saved[channel_id]
            changes = epochs.net_changes(saved[channel_id], canonical_notes(store, "ada", channel_id))
            assert changes["global_memory"] == [
                {"operation": "delete", "key": "added"},
                {
                    "operation": "replace",
                    "note": {
                        "key": "global",
                        "value": "canonical global",
                        "source_channel_id": CHANNEL,
                        "updated_at": 1.0,
                    },
                },
            ]
            if channel_id == CHANNEL:
                assert changes["memory"] == [
                    {"operation": "delete", "key": "added"},
                    {"operation": "replace", "note": {"key": "note", "value": f"ada/{CHANNEL}"}},
                ]
            else:
                assert changes["memory"] == []
    finally:
        store.close()


def test_full_restore_restores_epoch_rows_with_canonical_database_state(snapshot_fixture):
    manager, directory, fingerprint, identifier = snapshot_fixture
    store = Store(directory / "council.sqlite3")
    try:
        epochs = MemoryEpochs(store)
        original = epochs.load("ada", CHANNEL)
        epochs.save(candidate(epochs))
        store.execute("UPDATE memories SET value='experiment' WHERE bot_id='ada'")
    finally:
        store.close()
    manager.restore(identifier, fingerprint)
    store = Store(directory / "council.sqlite3")
    try:
        assert MemoryEpochs(store).load("ada", CHANNEL) == original
        assert (
            store.one("SELECT value FROM memories WHERE bot_id='ada' AND channel_id=?", (CHANNEL,))["value"]
            == f"ada/{CHANNEL}"
        )
    finally:
        store.close()
