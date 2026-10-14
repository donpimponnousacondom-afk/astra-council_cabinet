"""Render durable early memory snapshots and complete late net changes."""

from copy import deepcopy

from .compaction_progress import digest
from .memory_epochs import FORMAT_VERSION, SCOPES, MemoryEpochs
from .models import ControlError
from .store import dumps
from .timekeeping import council_timezone, present_times


class MemoryPrompt:
    def __init__(self, builder):
        self.builder = builder
        self.store = builder.store
        self.epochs = MemoryEpochs(self.store)

    @staticmethod
    def keyed_records(records):
        keyed = {note["key"]: note for note in records}
        if len(keyed) != len(records):
            raise ControlError(
                "Global-memory note keys are ambiguous after existing vault redaction; "
                "resolve the key collision in the dashboard and reset the affected conversation's context "
                "before retrying. "
                "Canonical notes and saved snapshots were retained; no provider request was sent"
            )
        return keyed

    def capture(self, bot, channel_id, layers=None):
        """Detach current scoped data/rendering; never create or rotate a pin."""
        layers = self.builder.layers(bot, channel_id) if layers is None else layers
        current = self.store.get("bots", bot["id"]) or {}
        plugin = self.store.get("plugins", "memory") or {}
        grants = {
            "memory": bool(
                plugin.get("enabled")
                and "memory" in bot.get("enabled_plugins", [])
                and "memory" in current.get("enabled_plugins", [])
            ),
            "global_memory": bool(self.builder.global_memory and self.builder.global_memory.enabled(bot)),
        }
        templates, notes = {}, {}
        values = self.builder.prompt_values(bot)
        for scope in SCOPES:
            raw = self.builder.prompt(bot, scope, raw=True) if grants[scope] else None
            # An omitted notes placeholder is an intentional injection opt-out.
            if not raw or "notes" not in raw["variables"]:
                continue
            delta = self.builder.prompt(bot, scope + "_delta", {"memory_delta": "[]"})
            if not delta or "memory_delta" not in delta["variables"]:
                raise ControlError(
                    f"Pinned {scope} requires an enabled, nonempty {scope}_delta template "
                    "containing {memory_delta}; restore that layer or disable this scope's note injection. "
                    "No provider request was sent"
                )
            # Freeze embedded custom usage counters with the note snapshot.
            # Only stable rendering inputs belong in its compatibility basis.
            templates[scope] = {
                "template": raw,
                "values": {key: values[key] for key in raw["variables"] if key in values},
            }
            records = (
                self.store.rows(
                    "SELECT key,value FROM memories WHERE bot_id=? AND channel_id=? ORDER BY key",
                    (bot["id"], channel_id),
                )
                if scope == "memory"
                else self.builder.global_memory.vault.redact(self.builder.global_memory.notes(bot["id"]))
            )
            notes[scope] = self.keyed_records(records)
        basis = digest(
            {
                "version": FORMAT_VERSION,
                "bot_id": bot["id"],
                "channel_id": channel_id,
                "boundary": self.store.context_boundary(bot["id"], channel_id),
                "timezone": council_timezone(self.store),
                "templates": templates,
            }
        )
        return self.epochs.candidate(
            bot["id"], channel_id, basis, notes, [layer for layer in layers if layer["id"] in notes]
        )

    def fitting_fingerprint(self, bot, channel_id, snapshot):
        """Validate all memory input measured by a final compaction fit."""
        budgets = [
            layer
            for layer in self.builder.layers(bot, channel_id)
            if layer["id"] in {scope + "_budget" for scope in SCOPES}
        ]
        deltas = [
            self.builder.prompt(bot, scope + "_delta", {"memory_delta": "[]"}) for scope in snapshot["notes"]
        ]
        return digest([self.epochs.source_fingerprint(snapshot), budgets, deltas])

    def prepare(self, bot, channel_id):
        """Explicit preparation boundary; writes are synchronous on Store's thread."""
        candidate = self.capture(bot, channel_id)
        snapshot = self.epochs.select(candidate)
        if snapshot["epoch_id"] == candidate["epoch_id"]:
            self.store.emit(
                "memory.snapshot_prepared",
                {"channel_id": channel_id, **self.epochs.metadata(snapshot)},
                bot_id=bot["id"],
                level="debug",
            )
        return snapshot

    def redact_snapshot(self, snapshot):
        """Project saved global records through today's existing vault boundary."""
        if "global_memory" not in snapshot["notes"]:
            return snapshot
        projected = deepcopy(snapshot)
        vault = self.builder.global_memory.vault
        records = list(snapshot["notes"]["global_memory"].values())
        redacted = vault.redact(records)
        projected["notes"]["global_memory"] = self.keyed_records(redacted)
        timezone = council_timezone(self.store)
        for layer in projected["layers"]:
            if layer["id"] != "global_memory":
                continue
            # Redact structured values before JSON serialization: a newly known
            # secret containing quotes/backslashes cannot match its rendered form.
            # Substitute only saved note records, keeping custom wrappers and
            # their historical counters frozen. Every {notes} occurrence is covered.
            for old, new in zip(records, redacted, strict=True):
                if old != new:
                    layer["content"] = layer["content"].replace(
                        dumps(present_times(old, timezone)), dumps(present_times(new, timezone))
                    )
            layer.update(vault.redact(layer))
        return projected

    def project(self, bot, channel_id, layers, snapshot=None):
        """Read-only assembly. Refresh deltas; never rewrite the pinned prefix."""
        candidate = self.capture(bot, channel_id, layers)
        snapshot = snapshot or self.epochs.load(bot["id"], channel_id) or candidate
        if (snapshot["bot_id"], snapshot["channel_id"]) != (bot["id"], channel_id):
            raise ControlError("Memory snapshot does not belong to this bot and conversation")
        compatible = all(snapshot[field] == candidate[field] for field in ("format_version", "basis_hash"))
        # Owner template/grant changes must take effect immediately, including
        # during a captured turn. Its next preparation will persist the new pin.
        if not compatible:
            snapshot = candidate
        projected = self.redact_snapshot(snapshot)
        changes = self.epochs.net_changes(projected, candidate["notes"])
        pinned = {layer["id"]: deepcopy(layer) for layer in projected["layers"]}
        live = {layer["id"]: layer for layer in layers}
        memory_ids = {*SCOPES, *(scope + "_budget" for scope in SCOPES)}
        boundary = next(
            (
                i
                for i, layer in enumerate(layers)
                if layer["id"] in memory_ids | {"slash_invocation", "panel_invocation"}
            ),
            len(layers),
        )
        ordered = layers[:boundary]
        delta_layers = []
        for scope in SCOPES:
            if budget := live.get(scope + "_budget"):
                ordered.append(budget)
            note = pinned.get(scope) if scope in candidate["notes"] else live.get(scope)
            if note:
                ordered.append(note)
            if scope in candidate["notes"]:
                data = changes[scope]
                if scope == "global_memory":
                    data = present_times(data, council_timezone(self.store))
                delta_layers.append(self.builder.prompt(bot, scope + "_delta", {"memory_delta": dumps(data)}))
        ordered.extend(layer for layer in layers[boundary:] if layer["id"] not in memory_ids)
        meta = {
            **self.epochs.metadata(snapshot),
            "compatible_with_prepared_snapshot": compatible,
            "delta_keys": {
                scope: [change.get("key", change.get("note", {}).get("key")) for change in entries]
                for scope, entries in changes.items()
                if scope in candidate["notes"]
            },
            "delta_chars": sum(len(layer["content"]) for layer in delta_layers),
        }
        return ordered, delta_layers, meta
