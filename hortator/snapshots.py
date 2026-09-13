"""Immutable offline application snapshots; no source Git or live SQLite copying."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .models import ControlError, SCHEMAS
from .memory_budget import DEFAULT_MEMORY_CHAR_LIMIT, NOTE_CHAR_LIMIT, hard_limit

FORMAT = 1
STORES = ("artifacts", "images", "sites", "ssh", "workspaces", "jobs", "fetched_documents", "site_history")
PAUSE_FILE = "snapshot-paused.json"
ID = re.compile(r"[0-9]{8}T[0-9]{12}Z-[a-f0-9]{12}")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def schema(db):
    rows = db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    return hashlib.sha256(canonical([list(row) for row in rows])).hexdigest()


def config_schema():
    return hashlib.sha256(
        canonical({name: model.model_json_schema() for name, model in SCHEMAS.items()})
    ).hexdigest()


def connection(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_tree(path):
    for entry in files(path):
        with entry.open("rb") as stream:
            os.fsync(stream.fileno())
    for folder, _, _ in os.walk(path, topdown=False):
        sync_directory(folder)


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        temporary.chmod(0o600)
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    sync_directory(path.parent)


def remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def regular(path):
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise ControlError(f"Snapshot refuses a non-regular file: {path.name}")


def files(root):
    result = []
    for folder, dirs, names in os.walk(root, followlinks=False):
        for name in dirs:
            if (Path(folder) / name).is_symlink():
                raise ControlError("Snapshot refuses symlink directories; inspect managed storage")
        for name in names:
            path = Path(folder) / name
            regular(path)
            result.append(path)
    return sorted(result)


def private_copy(source, target):
    if source.is_symlink():
        raise ControlError("Snapshot refuses symlinks; inspect managed storage")
    if source.is_dir():
        target.mkdir(mode=0o700)
        for child in source.iterdir():
            private_copy(child, target / child.name)
    else:
        regular(source)
        shutil.copyfile(source, target)
        target.chmod(0o600 | (source.stat().st_mode & 0o100))


class Snapshots:
    def __init__(self, directory, build, *, root=None):
        self.directory = Path(directory).resolve()
        self.root = Path(
            root
            or os.getenv("HORTATOR_SNAPSHOT_DIR")
            or self.directory.with_name(self.directory.name + "-snapshots")
        ).resolve()
        if self.root == self.directory or self.root.is_relative_to(self.directory):
            raise ControlError("Snapshots must be stored outside the live data directory")
        source_root = Path(__file__).resolve().parent.parent
        if self.root == source_root or self.root.is_relative_to(source_root):
            raise ControlError("Snapshots must be stored outside the source checkout")
        self.build = dict(build)
        self.journal = self.root / (
            ".restore-" + hashlib.sha256(str(self.directory).encode()).hexdigest()[:20] + ".json"
        )

    def path(self, identifier):
        if not isinstance(identifier, str) or not ID.fullmatch(identifier):
            raise ControlError("Invalid snapshot identifier", 404)
        path = self.root / identifier
        if not path.is_dir() or path.is_symlink():
            raise ControlError("Snapshot not found", 404)
        return path

    def read(self, identifier):
        path = self.path(identifier) / "manifest.json"
        regular(path)
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ControlError("Snapshot manifest exceeds its inspection limit")
        try:
            value = json.loads(path.read_text())
            if value["id"] != identifier or value["format"] != FORMAT:
                raise ValueError()
            return value
        except (ValueError, KeyError, TypeError) as exc:
            raise ControlError("Snapshot manifest is invalid or uses an unsupported format") from exc

    def compatibility(self, manifest, current_schema=None):
        reasons = []
        recorded = manifest.get("build", {})
        if not self.build.get("commit") or self.build.get("dirty") is not False:
            reasons.append("Running code has unknown or uncommitted source identity")
        if not recorded.get("commit") or recorded.get("dirty") is not False:
            reasons.append("Snapshot has unknown or uncommitted source identity")
        if recorded.get("commit") != self.build.get("commit"):
            reasons.append("Snapshot requires its exact source commit")
        if manifest.get("config_schema") != config_schema():
            reasons.append("Configuration schema differs")
        if current_schema and manifest.get("database_schema") != current_schema:
            reasons.append("Database schema differs")
        return {"compatible": not reasons, "reasons": reasons}

    def catalog(self, current_schema):
        entries = []
        if self.root.is_dir():
            for path in sorted(self.root.iterdir(), reverse=True):
                if not ID.fullmatch(path.name):
                    continue
                try:
                    item = self.read(path.name)
                    entries.append(self.public(item, current_schema))
                except (ControlError, OSError) as error:
                    entries.append(
                        {"id": path.name, "compatible": False, "reasons": [str(error)], "invalid": True}
                    )
        return {"snapshots": entries, "directory": str(self.root)}

    def public(self, manifest, current_schema=None):
        result = {
            key: manifest.get(key)
            for key in (
                "id",
                "name",
                "note",
                "created_at",
                "build",
                "database_schema",
                "file_count",
                "bytes",
                "reason",
                "bots",
                "recovery_for",
            )
        } | self.compatibility(manifest, current_schema)
        return result | {
            "format_version": FORMAT,
            "source_commit": manifest["build"].get("commit"),
            "compatibility_reason": "; ".join(result["reasons"]),
        }

    def capture(self, name, note="", *, reason="requested", recovery_for=None):
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or len(note) > 2000:
            raise ControlError("Snapshot needs a name (1–100 characters) and optional note (up to 2,000)")
        now = datetime.now(UTC)
        identifier = now.strftime("%Y%m%dT%H%M%S%fZ-") + uuid.uuid4().hex[:12]
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        temporary = self.root / (".creating-" + identifier)
        temporary.mkdir(mode=0o700)
        payload = temporary / "data"
        payload.mkdir(mode=0o700)
        try:
            with (
                connection(self.directory / "council.sqlite3") as source,
                sqlite3.connect(payload / "council.sqlite3") as target,
            ):
                source.backup(target)
                database_schema = schema(target)
                bots = []
                for (bot_id,) in source.execute("SELECT id FROM entities WHERE kind='bots' ORDER BY id"):
                    channels = [
                        row[0]
                        for row in source.execute(
                            "SELECT channel_id FROM contexts WHERE bot_id=? UNION SELECT channel_id FROM memories WHERE bot_id=? ORDER BY channel_id",
                            (bot_id, bot_id),
                        )
                    ]
                    bots.append({"id": bot_id, "channels": channels})
            key = os.getenv("HORTATOR_MASTER_KEY") or (self.directory / "master.key").read_text().strip()
            (payload / "master.key").write_text(key)
            for name_ in (*STORES, "initial-password"):
                source_path = self.directory / name_
                if source_path.exists() or source_path.is_symlink():
                    private_copy(source_path, payload / name_)
            self.validate_database(payload)
            inventory = {}
            for entry in files(payload):
                entry.chmod(0o400 | (entry.stat().st_mode & 0o100))
                inventory[entry.relative_to(payload).as_posix()] = {
                    "sha256": digest_file(entry),
                    "bytes": entry.stat().st_size,
                }
            manifest = {
                "format": FORMAT,
                "id": identifier,
                "name": name.strip(),
                "note": note,
                "created_at": now.isoformat(),
                "reason": reason,
                "recovery_for": recovery_for,
                "build": self.build,
                "database_schema": database_schema,
                "config_schema": config_schema(),
                "files": inventory,
                "file_count": len(inventory),
                "bytes": sum(item["bytes"] for item in inventory.values()),
                "bots": bots,
                "excluded": [
                    "runtime.lock",
                    "logs",
                    PAUSE_FILE,
                    "host launcher/environment/terminal settings",
                ],
            }
            (temporary / "manifest.json").write_bytes(canonical(manifest))
            (temporary / "manifest.json").chmod(0o400)
            sync_tree(temporary)
            temporary.rename(self.root / identifier)
            sync_directory(self.root)
            return manifest
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def validate_database(self, payload):
        try:
            key = (payload / "master.key").read_text().strip()
            cipher = Fernet(key.encode())
            with connection(payload / "council.sqlite3") as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ControlError("Snapshot SQLite integrity check failed")
                if db.execute("PRAGMA foreign_key_check").fetchone():
                    raise ControlError("Snapshot has broken SQLite references")
                for (value,) in db.execute("SELECT value FROM secrets"):
                    cipher.decrypt(value)
                return schema(db)
        except (InvalidToken, ValueError, sqlite3.Error, OSError) as error:
            raise ControlError(
                "Snapshot database or its matching encryption key failed validation"
            ) from error

    def verify(self, identifier, current_schema, *, require_compatible=True):
        manifest = self.read(identifier)
        compatibility = self.compatibility(manifest, current_schema)
        if require_compatible and not compatibility["compatible"]:
            raise ControlError("Snapshot is incompatible: " + "; ".join(compatibility["reasons"]), 409)
        payload = self.path(identifier) / "data"
        if payload.is_symlink():
            raise ControlError("Snapshot data cannot be a symlink")
        found = {entry.relative_to(payload).as_posix(): entry for entry in files(payload)}
        recorded = manifest.get("files", {})
        if set(found) != set(recorded):
            raise ControlError("Snapshot file inventory no longer matches its manifest", 409)
        for name, path in found.items():
            if (
                path.stat().st_size != recorded[name]["bytes"]
                or digest_file(path) != recorded[name]["sha256"]
            ):
                raise ControlError(f"Snapshot integrity check failed for {name}", 409)
        if self.validate_database(payload) != manifest["database_schema"]:
            raise ControlError("Snapshot database schema differs from its manifest", 409)
        return manifest

    def restore(
        self,
        identifier,
        current_schema,
        *,
        scope="full",
        bot_id=None,
        channel_id=None,
        include_context=False,
        include_global_memory=False,
    ):
        manifest = self.verify(identifier, current_schema)
        payload = self.path(identifier) / "data"
        if scope not in ("full", "bot"):
            raise ControlError("Restore scope must be full or bot")
        if scope == "bot":
            if not bot_id or bot_id not in {item["id"] for item in manifest["bots"]}:
                raise ControlError("The selected bot does not exist in this snapshot")
            with connection(self.directory / "council.sqlite3") as current:
                if not current.execute(
                    "SELECT 1 FROM entities WHERE kind='bots' AND id=?", (bot_id,)
                ).fetchone():
                    raise ControlError(
                        "The selected bot no longer exists; selective restore cannot recreate or transfer ownership"
                    )
            if channel_id and channel_id not in next(
                item["channels"] for item in manifest["bots"] if item["id"] == bot_id
            ):
                raise ControlError("The selected channel is absent from this bot's snapshot")
        if scope == "bot":
            self.validate_bot_budget(payload, bot_id, channel_id, include_global_memory)
            if include_context:
                self.validate_context_anchors(payload, bot_id, channel_id)
        environment_key = os.getenv("HORTATOR_MASTER_KEY")
        if (
            scope == "full"
            and environment_key
            and environment_key.strip() != (payload / "master.key").read_text().strip()
        ):
            raise ControlError(
                "Snapshot encryption key differs from HORTATOR_MASTER_KEY; use its matching environment key before restoring",
                409,
            )
        recovery = self.capture(
            "Before restore: " + manifest["name"][:80], reason="recovery", recovery_for=identifier
        )
        self.pause(
            {
                "note": "Restore in progress; inspect recovery snapshot before resuming if interrupted",
                "snapshot_id": identifier,
                "recovery_snapshot_id": recovery["id"],
            }
        )
        if scope == "bot":
            self.restore_bot(payload, bot_id, channel_id, include_context, include_global_memory)
        else:
            self.restore_full(payload, recovery["id"])
        receipt = {
            "snapshot_id": identifier,
            "recovery_snapshot_id": recovery["id"],
            "scope": scope,
            "bot_id": bot_id,
            "channel_id": channel_id,
            "include_context": include_context,
            "include_global_memory": include_global_memory,
            "restored_at": datetime.now(UTC).isoformat(),
            "paused": True,
            "note": "Runtime is paused until explicitly resumed. External Discord messages, remote publications and provider charges are not rolled back.",
        }
        if scope == "bot" and include_context:
            receipt["context_warning"] = (
                "Newer shared channel transcripts remain and can re-enter context. Use a full restore/new channel for an exact experimental replay."
            )
        self.pause(receipt)
        return receipt

    def recover_pending(self):
        """Rollback an interrupted file swap before any Kernel touches SQLite."""
        if not self.journal.exists():
            return False
        regular(self.journal)
        transaction = json.loads(self.journal.read_text())
        if transaction.get("directory") != str(self.directory):
            raise ControlError(
                "Restore journal belongs to another data directory; operator inspection required"
            )
        staging = self.directory.parent / transaction["staging"]
        displaced = self.directory.parent / transaction["displaced"]
        if (
            not re.fullmatch(r"\.hortator-restore-[a-f0-9]{32}", staging.name)
            or not re.fullmatch(r"\.hortator-displaced-[a-f0-9]{32}", displaced.name)
            or staging.is_symlink()
            or displaced.is_symlink()
        ):
            raise ControlError("Restore journal contains unsafe staging paths")
        allowed = {
            "council.sqlite3",
            "council.sqlite3-wal",
            "council.sqlite3-shm",
            "master.key",
            "initial-password",
            *STORES,
        }
        if set(transaction["original"]) != allowed:
            raise ControlError("Restore journal has an invalid managed-store inventory")
        for name, existed in transaction["original"].items():
            saved, current = displaced / name, self.directory / name
            if saved.exists() or saved.is_symlink():
                remove(current)
                saved.rename(current)
            elif not existed:
                remove(current)
        sync_directory(self.directory)
        if displaced.is_dir():
            sync_directory(displaced)
        self.pause(
            {
                "note": "Interrupted restore was rolled back to its previous local state. Inspect it before resuming.",
                "recovery_snapshot_id": transaction["recovery_snapshot_id"],
                "restore_rolled_back": True,
            }
        )
        self.journal.unlink()
        sync_directory(self.root)
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(displaced, ignore_errors=True)
        return True

    def restore_full(self, payload, recovery_id):
        staging = self.directory.parent / (".hortator-restore-" + uuid.uuid4().hex)
        displaced = self.directory.parent / (".hortator-displaced-" + uuid.uuid4().hex)
        staging.mkdir(mode=0o700)
        displaced.mkdir(mode=0o700)
        names = (
            "council.sqlite3",
            "council.sqlite3-wal",
            "council.sqlite3-shm",
            "master.key",
            "initial-password",
            *STORES,
        )
        try:
            for name in names:
                if (payload / name).exists():
                    private_copy(payload / name, staging / name)
            self.validate_database(staging)
            sync_tree(staging)
            transaction = {
                "directory": str(self.directory),
                "staging": staging.name,
                "displaced": displaced.name,
                "recovery_snapshot_id": recovery_id,
                "original": {name: (self.directory / name).exists() for name in names},
            }
            atomic_json(self.journal, transaction)
            # Keep runtime.lock and console logs at their original paths. A whole
            # live-root rename would break single-process locking/log ownership.
            for name in names:
                target = self.directory / name
                if target.exists() or target.is_symlink():
                    target.rename(displaced / name)
            sync_directory(displaced)
            sync_directory(self.directory)
            for source in staging.iterdir():
                source.rename(self.directory / source.name)
            sync_directory(staging)
            sync_directory(self.directory)
            self.journal.unlink()
            sync_directory(self.root)
        except BaseException:
            if self.journal.exists():
                self.recover_pending()
            else:
                shutil.rmtree(staging, ignore_errors=True)
                shutil.rmtree(displaced, ignore_errors=True)
            raise
        else:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(displaced, ignore_errors=True)

    def validate_context_anchors(self, payload, bot_id, channel_id):
        with (
            connection(self.directory / "council.sqlite3") as current,
            connection(payload / "council.sqlite3") as source,
        ):
            clause, args = "bot_id=?", [bot_id]
            if channel_id:
                clause += " AND channel_id=?"
                args.append(channel_id)
            for channel, checkpoint, last_seen in source.execute(
                f"SELECT channel_id,checkpoint,last_seen FROM contexts WHERE {clause}", args
            ):
                for label, sequence in (("checkpoint", checkpoint), ("last_seen", last_seen)):
                    if sequence == 0:
                        continue
                    expected = source.execute(
                        "SELECT discord_id,channel_id FROM messages WHERE seq=?", (sequence,)
                    ).fetchone()
                    actual = current.execute(
                        "SELECT discord_id,channel_id FROM messages WHERE seq=?", (sequence,)
                    ).fetchone()
                    if expected is None or actual != expected or expected[1] != channel:
                        raise ControlError(
                            f"Cannot restore context {bot_id}/{channel}: its {label} message anchor is missing or differs in the current transcript. Restore notes only, or use a full snapshot for this context.",
                            409,
                        )

    def validate_bot_budget(self, payload, bot_id, channel_id, include_global_memory):
        with (
            connection(self.directory / "council.sqlite3") as current,
            connection(payload / "council.sqlite3") as source,
        ):
            bot = json.loads(
                current.execute("SELECT body FROM entities WHERE kind='bots' AND id=?", (bot_id,)).fetchone()[
                    0
                ]
            )
            limit = bot.get("memory_char_limit", DEFAULT_MEMORY_CHAR_LIMIT)
            totals = {}
            for channel, value in source.execute(
                "SELECT channel_id,value FROM memories WHERE bot_id=?", (bot_id,)
            ):
                if channel_id and channel != channel_id:
                    continue
                if len(value) > NOTE_CHAR_LIMIT:
                    raise ControlError(
                        "A snapshot note exceeds the current per-note limit; selective restore was not applied",
                        409,
                    )
                totals[channel] = totals.get(channel, 0) + len(value)
            if any(value > hard_limit(limit) for value in totals.values()):
                raise ControlError(
                    "Snapshot notes exceed this bot's current memory budget plus its 5% allowance; increase its budget before selective restore",
                    409,
                )
            if (
                include_global_memory
                and source.execute("SELECT 1 FROM sqlite_master WHERE name='global_memories'").fetchone()
            ):
                values = [
                    row[0]
                    for row in source.execute("SELECT value FROM global_memories WHERE bot_id=?", (bot_id,))
                ]
                limit = bot.get("global_memory_char_limit", DEFAULT_MEMORY_CHAR_LIMIT)
                if any(len(value) > NOTE_CHAR_LIMIT for value in values) or sum(
                    map(len, values)
                ) > hard_limit(limit):
                    raise ControlError(
                        "Snapshot global notes exceed this bot's current global-memory budget; increase it before selective restore",
                        409,
                    )

    def restore_bot(self, payload, bot_id, channel_id, include_context, include_global_memory):
        with (
            connection(payload / "council.sqlite3") as source,
            sqlite3.connect(self.directory / "council.sqlite3") as target,
        ):
            target.execute("PRAGMA foreign_keys=ON")
            source.row_factory = sqlite3.Row
            tables = ["memories"] + (["contexts"] if include_context else [])
            available = {
                row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if include_global_memory and "global_memories" in available:
                tables.append("global_memories")
            for table in tables:
                clause, args = "bot_id=?", [bot_id]
                if channel_id and table != "global_memories":
                    clause += " AND channel_id=?"
                    args.append(channel_id)
                rows = source.execute(f"SELECT * FROM {table} WHERE {clause}", args).fetchall()
                target.execute(f"DELETE FROM {table} WHERE {clause}", args)
                if rows:
                    columns = list(rows[0].keys())
                    target.executemany(
                        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                        [tuple(row) for row in rows],
                    )

    def pause(self, receipt):
        atomic_json(self.directory / PAUSE_FILE, receipt)
