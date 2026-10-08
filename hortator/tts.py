"""Speech generation and authenticated, provider-specific voice discovery."""

import base64
import binascii
import json
from urllib.parse import urlsplit, urlunsplit

import httpx

from .models import ControlError


DESCRIPTION = (
    'Generate speech: {"text":"Hello"}, optionally with "voice" from discovery. '
    'List available voices: {"operation":"voices"}; follow returned next until null. '
    "Discovery depends on the configured provider; it does not need an API key from you. "
    "Use returned voice IDs, not guessed names. A voice override applies only to this call. "
    "Generation returns an artifact ID for discord_attach, then answer normally."
)
PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "operation": {"type": "string", "enum": ["generate", "voices"], "default": "generate"},
        "text": {"type": "string", "minLength": 1, "maxLength": 4000},
        "voice": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": "Exact voice ID returned by discovery, or a provider-documented voice identifier.",
        },
        "type": {"type": "string", "enum": ["preset", "custom", "all"], "default": "preset"},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
        "page_token": {"type": "string", "minLength": 1, "maxLength": 4096},
    },
    "allOf": [
        {
            "if": {"properties": {"operation": {"const": "voices"}}, "required": ["operation"]},
            "then": {"properties": {"text": False, "voice": False}},
            "else": {
                "required": ["text"],
                "properties": {"type": False, "page_size": False, "page_token": False},
            },
        },
    ],
    "examples": [{"text": "Hello"}, {"operation": "voices"}, {"operation": "generate", "text": "Hello"}],
}
MIMES = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
}


def api_format(config):
    value = config.get("api_format", "auto")
    if value == "auto":
        return "mistral" if urlsplit(config["endpoint"]).hostname == "api.mistral.ai" else "openai"
    if value not in ("openai", "mistral"):
        raise ControlError("TTS api_format must be auto, openai or mistral")
    return value


async def read_response(response, limit):
    """Bound response reads, including useful upstream error bodies; caller redacts."""
    failed = not response.is_success
    cap = 2000 if failed else limit
    data = bytearray()
    async for chunk in response.aiter_bytes():
        if failed:
            data.extend(chunk[: cap - len(data)])
            if len(data) >= cap:
                break
        else:
            data.extend(chunk)
            if len(data) > cap:
                raise ControlError(f"Media response exceeds {limit:,} bytes")
    if failed:
        detail = data.decode("utf-8", errors="replace")
        raise ControlError(f"Media HTTP {response.status_code} {response.reason_phrase}: {detail}")
    return bytes(data)


async def voices(args, config, key):
    adapter = api_format(config)
    if adapter != "mistral":
        return {
            "supported": False,
            "reason": "This TTS provider has no implemented voice-list adapter. /v1/audio/voices is not universal.",
            "configured_voice": config.get("request_json", {}).get("voice"),
            "next": None,
        }
    endpoint = config.get("voices_endpoint")
    if not endpoint:
        url = urlsplit(config["endpoint"])
        suffix = "/v1/audio/speech"
        if not url.path.endswith(suffix):
            raise ControlError("Set the TTS voices_endpoint for this Mistral-compatible speech endpoint")
        endpoint = urlunsplit(
            url._replace(path=url.path[: -len(suffix)] + "/v2/audio/voices", query="", fragment="")
        )
    params = {"type": args.get("type", "preset"), "page_size": args.get("page_size", 100)}
    if "page_token" in args:
        params["page_token"] = args["page_token"]
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        async with client.stream("GET", endpoint, params=params, headers=headers, timeout=30) as response:
            raw = await read_response(response, 1_000_000)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ControlError("Voice list endpoint returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ControlError("Voice list endpoint did not return the Mistral v2 data array")
    token = payload.get("next_page_token")
    if token is not None and (not isinstance(token, str) or len(token) > 4096):
        raise ControlError("Voice list endpoint returned an invalid continuation token")
    items = payload["data"]
    if any(not isinstance(v, dict) or not isinstance(v.get("id"), str) or not v["id"] for v in items):
        raise ControlError("Voice list endpoint returned a voice without an ID")
    # Keep provider metadata intact, including emotion tags/languages and nullable fields.
    return {
        "supported": True,
        "voices": items,
        "configured_voice": config.get("request_json", {}).get("voice_id"),
        "next": {"operation": "voices", **params, "page_token": token} if token else None,
        "next_tool": "tts",
        "guidance": "Use a returned voice id as tts.voice to select it for one generation. Follow next for more voices.",
    }


def speech_body(args, config):
    adapter = api_format(config)
    body = {**config.get("request_json", {}), "input": args["text"]}
    if "voice" in args:
        body["voice_id" if adapter == "mistral" else "voice"] = args["voice"]
    fmt = body.get("response_format", "mp3")
    if fmt not in MIMES:
        raise ControlError("Unsupported audio response format")
    if body.get("stream"):
        raise ControlError("TTS attachments require a complete response; set request_json.stream to false")
    return body, fmt


def decode_audio(data, mime, fmt):
    if "json" in mime.lower():
        try:
            payload = json.loads(data)
            encoded = payload.get("audio_data") if isinstance(payload, dict) else None
            if not isinstance(encoded, str) or not encoded:
                raise ValueError("Missing audio_data")
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, UnicodeError, binascii.Error) as exc:
            raise ControlError("TTS JSON response did not contain valid base64 audio_data") from exc
        mime = MIMES[fmt]
    if not data or "html" in mime.lower() or "event-stream" in mime.lower():
        raise ControlError("TTS endpoint did not return complete audio")
    return data, mime
