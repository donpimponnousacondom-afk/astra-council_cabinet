"""Opt-in Mistral voice experiments; the ordinary TTS plugin stays independent."""

import base64
import copy
import json
import subprocess
from urllib.parse import urlsplit, urlunsplit

from . import tts
from .audio_cache import AudioCache, audio_candidate, joined_worker, probe
from .models import ControlError

PLUGIN_ID = "frenchy_tts"
DESCRIPTION = (
    'Frenchy TTS — experimental voice playground. List presets with {"operation":"voices"}; '
    'use type:"custom" for saved clones. Names, emotion tags and languages come from that catalog. '
    'Speak with {"text":"Bonjour!","voice":"<returned-id>"}. French voices can give English a French accent. '
    'Native voice notes follow your separate written answer; use delivery:"file" for ordinary attachments. '
    'To clone a short reference: {"operation":"clone_voice","name":"My voice","message_id":"...",'
    '"attachment_id":"..."}. Reuse the returned voice ID. For one-shot imitation, supply the same '
    "reference fields with text instead; no voice is saved. reference_artifact_id can select your own "
    "current-turn audio instead of a Discord attachment. References must be audio up to 30 seconds. "
    "Saved cloning can require a paid provider plan. Do not guess voice names or claim a clone was created without a returned ID. A timed-out clone "
    "may already exist: list custom voices before trying again. Prepare the artifact with discord_attach "
    "and answer normally. Slash/panel interaction replies retain ordinary audio attachments."
)

PARAMETERS = copy.deepcopy(tts.PARAMETERS)
PARAMETERS["properties"].update(
    {
        "operation": {"type": "string", "enum": ["generate", "voices", "clone_voice"], "default": "generate"},
        "delivery": {"type": "string", "enum": ["voice_note", "file"]},
        "name": {"type": "string", "minLength": 1, "maxLength": 100},
        "reference_artifact_id": {"type": "string", "minLength": 1, "maxLength": 100},
        "message_id": {"type": "string", "pattern": "^[0-9]+$"},
        "attachment_id": {"type": "string", "pattern": "^[0-9]+$"},
    }
)
REFERENCE = {
    "oneOf": [
        {"required": ["reference_artifact_id"], "properties": {"message_id": False, "attachment_id": False}},
        {"required": ["message_id", "attachment_id"], "properties": {"reference_artifact_id": False}},
    ]
}
PARAMETERS["allOf"] = [
    {
        "if": {"properties": {"operation": {"const": "voices"}}, "required": ["operation"]},
        "then": {
            "properties": {
                k: False
                for k in (
                    "text",
                    "voice",
                    "delivery",
                    "name",
                    "reference_artifact_id",
                    "message_id",
                    "attachment_id",
                )
            }
        },
        "else": {"properties": {k: False for k in ("type", "page_size", "page_token")}},
    },
    {
        "if": {"properties": {"operation": {"const": "clone_voice"}}, "required": ["operation"]},
        "then": {
            **REFERENCE,
            "required": ["name"],
            "properties": {"text": False, "voice": False, "delivery": False},
        },
        "else": {"properties": {"name": False}},
    },
    {
        "if": {
            "not": {
                "properties": {"operation": {"enum": ["voices", "clone_voice"]}},
                "required": ["operation"],
            }
        },
        "then": {"required": ["text"]},
    },
    {
        "if": {
            "anyOf": [{"required": [k]} for k in ("reference_artifact_id", "message_id", "attachment_id")]
        },
        "then": {**REFERENCE, "properties": {"voice": False}},
    },
]
PARAMETERS["examples"] = [
    {"text": "Bonjour!"},
    {"operation": "voices"},
    {"operation": "clone_voice", "name": "My voice", "message_id": "123456789", "attachment_id": "987654321"},
    {"operation": "generate", "text": "Bonjour!", "voice": "<returned-id>"},
]


def read_reference(path):
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ControlError("Reference audio exceeds 20 MiB")
    info = probe(path)
    if info["duration_seconds"] > 30:
        raise ControlError("Reference audio exceeds 30 seconds; supply a shorter clip")
    return base64.b64encode(path.read_bytes()).decode(), path.name


class Frenchy:
    def __init__(self, registry):
        self.registry, self.store = registry, registry.store
        self.cache = AudioCache(self.store)

    def attachment(self, args, context):
        row = self.store.one(
            "SELECT attachments FROM messages WHERE discord_id=? AND channel_id=? AND deleted=0",
            (args["message_id"], context.channel_id),
        )
        attachment = (
            next(
                (a for a in json.loads(row["attachments"]) if str(a.get("id")) == args["attachment_id"]), None
            )
            if row
            else None
        )
        if not attachment or not audio_candidate(attachment):
            raise ControlError(
                "Reference audio was not observed in this channel or was deleted; use actual message/attachment IDs"
            )
        return row, attachment

    async def reference(self, args, context):
        if "reference_artifact_id" in args:
            path = self.registry.resolve_artifact(args["reference_artifact_id"], context)
            data = await joined_worker(read_reference, path)
            self.registry.resolve_artifact(args["reference_artifact_id"], context)
        else:
            row, attachment = self.attachment(args, context)
            captured = await self.cache.capture(attachment)
            info = captured.get("audio", {})
            if info.get("status") != "ready":
                raise ControlError("Reference audio unavailable: " + info.get("error", "capture failed"))
            data = await joined_worker(read_reference, self.cache.path(info["sha256"]))
            latest, _ = self.attachment(args, context)
            if latest["attachments"] != row["attachments"]:
                raise ControlError("Reference attachment changed during preparation; use its current IDs")
            data = data[0], attachment["filename"]
        if not self.registry.allowed(PLUGIN_ID, context):
            raise ControlError("Frenchy TTS grant was revoked during reference preparation")
        return data

    async def call(self, args, context, config, key):
        if tts.api_format(config) != "mistral":
            raise ControlError(
                "Frenchy TTS requires a Mistral-compatible endpoint; configure api_format: mistral for a proxy"
            )
        operation = args.get("operation", "generate")
        if operation == "voices":
            return await tts.voices(args, config, key, tool_name=PLUGIN_ID)
        delivery = args.get("delivery", config.get("delivery", "voice_note"))
        if operation == "generate" and delivery not in ("voice_note", "file"):
            raise ControlError("Frenchy TTS delivery must be voice_note or file")
        reference = (
            await self.reference(args, context)
            if any(k in args for k in ("reference_artifact_id", "message_id"))
            else None
        )
        if operation == "clone_voice":
            url = urlsplit(config["endpoint"])
            suffix = "/v1/audio/speech"
            endpoint = config.get("clone_endpoint")
            if not endpoint:
                if not url.path.endswith(suffix):
                    raise ControlError("Set Frenchy TTS clone_endpoint for this speech endpoint")
                endpoint = urlunsplit(
                    url._replace(path=url.path[: -len(suffix)] + "/v1/audio/voices", query="", fragment="")
                )
            raw, _ = await self.registry.media_request(
                {**config, "endpoint": endpoint},
                key,
                {"name": args["name"], "sample_audio": reference[0], "sample_filename": reference[1]},
            )
            try:
                voice = json.loads(raw)
                if not isinstance(voice, dict) or not isinstance(voice.get("id"), str) or not voice["id"]:
                    raise ValueError()
            except (ValueError, UnicodeError) as exc:
                raise ControlError(
                    "Clone response has no usable voice ID. Creation may have succeeded; list custom voices before retrying."
                ) from exc
            return {
                "voice": voice,
                "saved_upstream": True,
                "guidance": "Use this voice.id in Frenchy TTS voice; it is saved in the configured Mistral account.",
            }
        body, fmt = tts.speech_body(args, config)
        if reference:
            body.pop("voice_id", None)
            body.pop("voice", None)
            body["ref_audio"] = reference[0]
        else:
            body.pop("ref_audio", None)
        raw, mime = await self.registry.media_request(config, key, body)
        data, mime = tts.decode_audio(raw, mime, fmt)
        if delivery == "voice_note" and len(data) <= 8_000_000:
            try:
                return await self.registry.voice_notes.prepare(data, args["text"], context)
            except (ControlError, ValueError, OSError, subprocess.SubprocessError) as exc:
                return {
                    **self.registry.artifact(data, mime, "." + fmt, context),
                    "delivery": "file",
                    "warning": f"Voice-note preparation failed; original audio retained as a file: {exc}",
                }
        return {**self.registry.artifact(data, mime, "." + fmt, context), "delivery": "file"}


def register(registry):
    from .plugins import PluginSpec

    registry.frenchy = Frenchy(registry)
    registry.register(
        PluginSpec(
            PLUGIN_ID,
            "Frenchy TTS · experimental",
            DESCRIPTION,
            PARAMETERS,
            registry.frenchy.call,
            {
                "endpoint": "https://api.mistral.ai/v1/audio/speech",
                "api_format": "mistral",
                "delivery": "voice_note",
                "request_json": {
                    "model": "voxtral-mini-tts-2603",
                    "voice_id": "fr_marie_excited",
                    "response_format": "mp3",
                },
            },
            installed_description=True,
        )
    )
