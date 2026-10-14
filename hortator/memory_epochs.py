"""Explicitly persisted memory snapshots and complete, coalesced note changes.

The caller owns grants, redaction, template rendering and epoch boundaries. Assembly only reads snapshots;
only preparation/promotion may call select/save. No transaction spans an await.
"""

import json
import time
from copy import deepcopy

from .compaction_progress import digest
from .store import dumps, uid


FORMAT_VERSION = 1
SCOPES = ("memory", "global_memory")


def _ordered(value):
    """Canonical mapping order without changing list order or rendered bytes."""
    if isinstance(value, dict):
        return {key: _ordered(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_ordered(item) for item in value]
    return value


def _validate_notes(notes):
    if not isinstance(notes, dict) or set(notes) - set(SCOPES):
        raise ValueError("Memory snapshot notes must contain only memory/global_memory scopes")
    for scope, records in notes.items():
        if not isinstance(records, dict):
            raise ValueError(f"Memory snapshot {scope} must be a key-to-note mapping")
        for key, note in records.items():
            if (
                not isinstance(key, str)
                or not isinstance(note, dict)
                or note.get("key") != key
                or not isinstance(note.get("value"), str)
            ):
                raise ValueError(f"Memory snapshot {scope} has an invalid complete note record")


def _validate(snapshot):
    if type(snapshot.get("format_version")) is not int or snapshot["format_version"] != FORMAT_VERSION:
        raise ValueError("Unsupported memory snapshot format")
    for field in ("bot_id", "channel_id", "epoch_id", "basis_hash"):
        if not isinstance(snapshot.get(field), str) or not snapshot[field]:
            raise ValueError(f"Memory snapshot requires {field}")
    if type(snapshot.get("created_at")) not in (int, float):
        raise ValueError("Memory snapshot requires a creation timestamp")
    _validate_notes(snapshot.get("notes"))
    layers = snapshot.get("layers")
    if not isinstance(layers, list):
        raise ValueError("Memory snapshot layers must be a list")
    seen = set()
    for layer in layers:
        if (
            not isinstance(layer, dict)
            or layer.get("id") not in snapshot["notes"]
            or layer["id"] in seen
            or layer.get("role") not in ("system", "user")
            or not isinstance(layer.get("content"), str)
        ):
            raise ValueError("Memory snapshot has an invalid or duplicate note layer")
        seen.add(layer["id"])
    # Reject non-JSON values/nonfinite numbers before any persistence mutation.
    dumps(snapshot)


class MemoryEpochs:
    def __init__(self, store):
        self.store = store

    def load(self, bot_id, channel_id):
        """Read a detached row. Malformed JSON remains an explicit storage error."""
        row = self.store.one(
            "SELECT * FROM memory_snapshots WHERE bot_id=? AND channel_id=?", (bot_id, channel_id)
        )
        if row is None:
            return None
        return {
            **{key: value for key, value in row.items() if key not in ("notes_json", "layers_json")},
            "notes": json.loads(row["notes_json"]),
            "layers": json.loads(row["layers_json"]),
        }

    def candidate(self, bot_id, channel_id, basis_hash, notes, layers):
        """Construct a detached candidate; do not read or write durable state."""
        snapshot = {
            "bot_id": bot_id,
            "channel_id": channel_id,
            "epoch_id": uid("mem_"),
            "format_version": FORMAT_VERSION,
            "basis_hash": basis_hash,
            "created_at": time.time(),
            "notes": deepcopy(notes),
            "layers": deepcopy(layers),
        }
        _validate(snapshot)
        return snapshot

    def save(self, snapshot):
        """Persist explicitly; participates in a caller-owned transaction."""
        _validate(snapshot)
        self.store.execute(
            "INSERT INTO memory_snapshots "
            "(bot_id,channel_id,epoch_id,format_version,basis_hash,created_at,notes_json,layers_json) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(bot_id,channel_id) DO UPDATE SET "
            "epoch_id=excluded.epoch_id,format_version=excluded.format_version,"
            "basis_hash=excluded.basis_hash,created_at=excluded.created_at,"
            "notes_json=excluded.notes_json,layers_json=excluded.layers_json",
            (
                snapshot["bot_id"],
                snapshot["channel_id"],
                snapshot["epoch_id"],
                snapshot["format_version"],
                snapshot["basis_hash"],
                snapshot["created_at"],
                dumps(snapshot["notes"]),
                dumps(snapshot["layers"]),
            ),
        )
        return deepcopy(snapshot)

    def select(self, current_candidate):
        """Preparation only: reuse compatible pins despite changed live notes."""
        _validate(current_candidate)
        saved = self.load(current_candidate["bot_id"], current_candidate["channel_id"])
        if saved is not None:
            try:
                _validate(saved)
            except TypeError, ValueError:
                saved = None
        if saved is not None and all(
            saved[field] == current_candidate[field]
            for field in ("bot_id", "channel_id", "format_version", "basis_hash")
        ):
            # Grants/layer eligibility belong in the basis; also reject an
            # inconsistent stored scope set rather than reuse it accidentally.
            if set(saved["notes"]) == set(current_candidate["notes"]):
                return saved
        return self.save(current_candidate)

    @staticmethod
    def net_changes(snapshot, current_notes):
        """Return sorted complete replacements/deletions, never write history."""
        _validate(snapshot)
        _validate_notes(current_notes)
        changes = {}
        for scope in SCOPES:
            previous = snapshot["notes"].get(scope, {})
            current = current_notes.get(scope, {})
            changed = []
            for key in sorted(previous.keys() | current.keys()):
                if key not in current:
                    changed.append({"operation": "delete", "key": key})
                elif key not in previous or current[key] != previous[key]:
                    changed.append({"operation": "replace", "note": deepcopy(current[key])})
            changes[scope] = changed
        return changes

    @staticmethod
    def source_fingerprint(snapshot):
        """Content basis for compaction race checks; no epoch ID or clock."""
        _validate(snapshot)
        return digest(_ordered({key: snapshot[key] for key in ("basis_hash", "notes", "layers")}))

    @staticmethod
    def metadata(snapshot):
        _validate(snapshot)
        return {
            **{key: snapshot[key] for key in ("epoch_id", "format_version", "basis_hash", "created_at")},
            "source_fingerprint": MemoryEpochs.source_fingerprint(snapshot),
            "note_counts": {scope: len(snapshot["notes"].get(scope, {})) for scope in SCOPES},
            "rendered_chars": sum(len(layer["content"]) for layer in snapshot["layers"]),
        }
