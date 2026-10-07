"""Opt-in native audio input for ordinary council conversation turns."""

import asyncio
import base64
import copy
import json
import math

from .audio_cache import AudioCache, FORMATS, audio_candidate
from .models import ControlError
from .store import dumps

PLUGIN_ID = "audio_input"
DEFAULTS = {"max_clips": 4, "max_request_mib": 20, "max_duration_seconds": 600}
DESCRIPTION = (
    "Receive new Discord voice notes and audio attachments through this bot's current model. "
    "Requires an audio-capable model/provider and ffprobe on the host. No separate key or transcription "
    "service. Audio stays available for the current turn; later turns retain metadata and written observations."
)


def validate_config(config):
    result = {**DEFAULTS, **config}
    for key in DEFAULTS:
        if type(result[key]) is not int or result[key] <= 0:
            raise ControlError(f"audio_input {key} must be a positive integer")
    return result


def audio_parts(messages):
    return [
        part
        for message in messages
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if part.get("type") == "hortator_audio"
    ]


def audio_reserve(messages):
    # Gemini's documented planning rate; approximate for other upstream models.
    return sum(math.ceil(part["audio"]["duration_seconds"] * 32) for part in audio_parts(messages))


def select_audio(rows, after_sequence, config, enabled):
    inputs, omitted = [], []
    total_bytes, duration, historical, disabled = 0, 0, 0, 0
    for row in reversed(rows):
        if row.get("deleted"):
            continue
        for attachment in row["attachments"]:
            if not audio_candidate(attachment):
                continue
            if row["seq"] <= after_sequence:
                historical += 1
                continue
            if not enabled:
                disabled += 1
                continue
            audio = attachment.get("audio") or {}
            identity = {"message_id": row["discord_id"], "attachment_id": attachment.get("id")}
            reasons = []
            if audio.get("status") != "ready":
                reasons.append("unavailable" if audio.get("status") == "unavailable" else "capture_budget")
            else:
                if len(inputs) >= config["max_clips"]:
                    reasons.append("clip_count")
                if total_bytes + audio["size"] > config["max_request_mib"] * 1024 * 1024:
                    reasons.append("audio_bytes")
                if duration + audio["duration_seconds"] > config["max_duration_seconds"]:
                    reasons.append("audio_duration")
            if reasons:
                omitted.append({**identity, "reasons": reasons})
                continue
            inputs.append(
                {
                    "type": "hortator_audio",
                    **identity,
                    "filename": attachment.get("filename"),
                    "audio": dict(audio),
                }
            )
            total_bytes += audio["size"]
            duration += audio["duration_seconds"]
    return {
        "policy": "new_messages_per_turn" if enabled else "audio_input_disabled",
        "after_sequence": after_sequence,
        "inputs": inputs,
        "omitted": omitted,
        "historical_clips": historical,
        "disabled_clips": disabled,
    }


def current_audio(store, channel_id, plan):
    sources, result = {}, []
    for part in plan["inputs"]:
        mid = part["message_id"]
        if mid not in sources:
            row = store.one(
                "SELECT deleted,attachments FROM messages WHERE channel_id=? AND discord_id=?",
                (channel_id, mid),
            )
            sources[mid] = json.loads(row["attachments"]) if row and not row["deleted"] else []
        if any(
            str(a.get("id")) == str(part["attachment_id"])
            and (a.get("audio") or {}).get("sha256") == part["audio"]["sha256"]
            and (a.get("audio") or {}).get("status") == "ready"
            for a in sources[mid]
        ):
            result.append(part)
    return result


def audio_metadata(message_id, attachments, inputs):
    included = {(p["message_id"], p["attachment_id"], p["audio"]["sha256"]) for p in inputs}
    return [
        {
            **a,
            "audio_in_this_request": (message_id, a.get("id"), (a.get("audio") or {}).get("sha256"))
            in included,
        }
        if audio_candidate(a)
        else a
        for a in attachments
    ]


def with_audio(content, inputs):
    if not inputs:
        return content
    parts = list(content) if isinstance(content, list) else [{"type": "text", "text": content}]
    for part in inputs:
        parts.extend(
            [
                {
                    "type": "text",
                    "text": (
                        f"Current-turn audio from message {part['message_id']}, attachment {part['attachment_id']}: "
                        f"{dumps(part['filename'])} ({part['audio']['duration_seconds']:.2f} seconds). "
                        "The following part contains the original audio. After this turn only metadata and your written "
                        "observations remain; do not claim to hear other metadata-only attachments."
                    ),
                },
                part,
            ]
        )
    return parts


class AudioInput:
    def __init__(self, store):
        self.store = store
        self.cache = AudioCache(store)

    def enabled(self, bot):
        plugin = self.store.get("plugins", PLUGIN_ID) or {}
        current = self.store.get("bots", bot["id"]) or {}
        return bool(
            not bot.get("invocation")
            and plugin.get("enabled", False)
            and PLUGIN_ID in bot.get("enabled_plugins", [])
            and PLUGIN_ID in current.get("enabled_plugins", [])
        )

    def config(self, bot):
        plugin = self.store.get("plugins", PLUGIN_ID) or {}
        return validate_config(
            {**plugin.get("config", {}), **bot.get("plugin_config", {}).get(PLUGIN_ID, {})}
        )

    def validate(self, kind, entity, store):
        if kind == "plugins" and entity["id"] == PLUGIN_ID:
            validate_config(entity.get("config", {}))
            for bot in store.list("bots"):
                validate_config(
                    {**entity.get("config", {}), **bot.get("plugin_config", {}).get(PLUGIN_ID, {})}
                )
        elif kind == "bots" and PLUGIN_ID in entity.get("plugin_config", {}):
            plugin = store.get("plugins", PLUGIN_ID) or {}
            validate_config({**plugin.get("config", {}), **entity["plugin_config"][PLUGIN_ID]})

    async def prepare(self, rows, after_sequence, bot):
        if self.enabled(bot):
            config, captures = self.config(bot), 0
            async with asyncio.timeout(60):
                for row in reversed(rows):
                    if row.get("deleted") or row["seq"] <= after_sequence:
                        continue
                    for attachment in list(row["attachments"]):
                        if not audio_candidate(attachment) or captures >= config["max_clips"]:
                            continue
                        # Permission is checked again after waits and at wire expansion.
                        if not self.enabled(bot):
                            break
                        captures += 1
                        result = await self.cache.capture(attachment)
                        # A Discord edit/deletion during download must not resurrect a source,
                        # or overwrite another client's newly captured images/metadata.
                        current = self.store.one(
                            "SELECT deleted,attachments FROM messages WHERE discord_id=?",
                            (row["discord_id"],),
                        )
                        if not current or current["deleted"]:
                            row["deleted"], row["attachments"] = True, []
                            break
                        attachments = json.loads(current["attachments"])
                        changed = False
                        for item in attachments:
                            if all(
                                item.get(k) == attachment.get(k)
                                for k in ("id", "filename", "size", "content_type", "url")
                            ):
                                old = item.get("audio") or {}
                                item["audio"] = result["audio"]
                                changed = True
                                if (
                                    result["audio"].get("error")
                                    and old.get("error") != result["audio"]["error"]
                                ):
                                    self.store.emit(
                                        "attachment.audio_unavailable",
                                        {
                                            "message_id": row["discord_id"],
                                            "attachment_id": item.get("id"),
                                            "error": result["audio"]["error"],
                                        },
                                        bot_id=bot["id"],
                                        level="warning",
                                    )
                        if changed:
                            self.store.execute(
                                "UPDATE messages SET attachments=? WHERE discord_id=? AND deleted=0",
                                (dumps(attachments), row["discord_id"]),
                            )
                        row["attachments"] = attachments
        return select_audio(rows, after_sequence, self.config(bot), self.enabled(bot))

    async def wire_messages(self, messages, bot, channel_id):
        parts = audio_parts(messages)
        if not parts:
            return messages
        if not self.enabled(bot):
            raise ControlError("Audio input grant was revoked; prepare a new turn")
        if len(current_audio(self.store, channel_id, {"inputs": parts})) != len(parts):
            raise ControlError("An audio source was removed or replaced; prepare a new turn")
        config = self.config(self.store.get("bots", bot["id"]))
        if (
            len(parts) > config["max_clips"]
            or sum(p["audio"]["size"] for p in parts) > config["max_request_mib"] * 1024 * 1024
            or sum(p["audio"]["duration_seconds"] for p in parts) > config["max_duration_seconds"]
        ):
            raise ControlError("Audio inputs exceed the current clip, byte or duration budget")

        def expand():
            result = copy.deepcopy(messages)
            for part in audio_parts(result):
                audio = part["audio"]
                if audio.get("format") not in FORMATS:
                    raise ValueError("Unsupported cached audio format")
                data = self.cache.read(audio)
                part.clear()
                part.update(
                    type="input_audio",
                    input_audio={"data": base64.b64encode(data).decode(), "format": audio["format"]},
                )
            return result

        try:
            # Detached, read-only values; no SQLite/vault access in this worker.
            return await asyncio.to_thread(expand)
        except (ValueError, OSError) as exc:
            raise ControlError(f"Audio input unavailable: {exc}") from exc


def register(registry):
    from .plugins import PluginSpec

    audio = AudioInput(registry.store)

    async def unavailable(arguments, context, config, key):
        raise ControlError("Audio input is a conversation capability, not a model tool")

    registry.register(
        PluginSpec(
            PLUGIN_ID,
            "Audio input · experimental",
            DESCRIPTION,
            {"type": "object", "properties": {}, "additionalProperties": False},
            unavailable,
            DEFAULTS,
            model_tool=False,
            keyless=True,
            installed_description=True,
            validate=audio.validate,
        )
    )
