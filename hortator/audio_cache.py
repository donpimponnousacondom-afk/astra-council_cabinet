"""Original Discord audio bytes, bounded probing, and durable private references."""

import asyncio
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit

import httpx

from .concurrency import task_group
from .vision import trusted_discord_url

MAX_AUDIO_BYTES = 20 * 1024 * 1024
FORMATS = {"wav", "mp3", "ogg", "flac", "aac", "aiff", "m4a", "webm"}


def audio_candidate(attachment):
    mime = str(attachment.get("content_type") or "").lower()
    if mime.startswith("video/"):
        return False
    return mime.startswith("audio/") or str(attachment.get("filename", "")).lower().endswith(
        tuple("." + suffix for suffix in (*FORMATS, "opus", "aif"))
    )


def reuse_audio(attachment, previous):
    if not previous or any(
        attachment.get(key) != previous.get(key) for key in ("id", "filename", "size", "content_type")
    ):
        return attachment
    try:
        old, new = urlsplit(previous.get("url", "")), urlsplit(attachment.get("url", ""))
        if (old.hostname, old.path) != (new.hostname, new.path):
            return attachment
    except ValueError:
        return attachment
    audio = previous.get("audio") or {}
    if audio.get("retry_on_refreshed_url") and previous.get("url") != attachment.get("url"):
        return attachment
    return {**attachment, "audio": copy.deepcopy(audio)} if audio else attachment


async def joined_worker(function, *args):
    """Join bounded filesystem/probe work even if its requesting turn is cancelled."""

    async def run():
        try:
            return await asyncio.to_thread(function, *args), None
        except Exception as error:
            return None, error

    async with task_group() as group:
        task = group.create_task(run(), name="audio-file-work")
        cancelled = None
        while True:
            try:
                value, error = await asyncio.shield(task)
                break
            except asyncio.CancelledError as exc:
                cancelled = exc
                if task.cancelled():
                    raise
        if cancelled is not None:
            raise cancelled
        if error is not None:
            raise error
        return value


def probe(path):
    # Probe only; do not transcode, decode an entire recording, or follow playlists.
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                "wav,mp3,ogg,flac,aac,aiff,mov,matroska,webm",
                "-show_entries",
                "stream=codec_type,codec_name,duration:stream_disposition=attached_pic:format=format_name,duration",
                "-of",
                "json",
                str(path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ValueError("Audio input needs ffprobe installed on the harness host") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Audio probe exceeded 10 seconds; reattach a smaller clip") from exc
    try:
        value = json.loads(result.stdout)
        stream = next(s for s in value["streams"] if s.get("codec_type") == "audio")
        container = value["format"]
        formats = set(container["format_name"].split(","))
        format_ = "m4a" if "mov" in formats else "webm" if "matroska" in formats else next(iter(formats))
        duration = float(container.get("duration") or stream["duration"])
        if result.returncode or format_ not in FORMATS or not math.isfinite(duration) or duration <= 0:
            raise ValueError
        details = {"format": format_, "duration_seconds": duration, "codec": stream["codec_name"]}
    except (ValueError, KeyError, IndexError, TypeError, StopIteration) as exc:
        raise ValueError(
            "Audio container or duration could not be read; reattach a supported audio file"
        ) from exc
    if any(
        s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
        for s in value["streams"]
    ):
        raise ValueError("Audio input does not accept video tracks; attach an audio-only file")
    return details


class AudioCache:
    def __init__(self, store, client=None):
        self.root = store.path.parent / "audio"
        self.client = client
        self.slots = asyncio.Semaphore(2)

    def path(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid audio reference")
        return self.root / digest

    def read(self, audio):
        path = self.path(audio.get("sha256"))
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_AUDIO_BYTES:
            raise ValueError("Cached audio is missing or exceeds 20 MiB")
        data = path.read_bytes()
        if len(data) != audio.get("size") or hashlib.sha256(data).hexdigest() != audio["sha256"]:
            raise ValueError("Cached audio integrity check failed")
        return data

    def prepare(self, data):
        if not data or len(data) > MAX_AUDIO_BYTES:
            raise ValueError("Audio is empty or exceeds 20 MiB")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A seekable file gives ffprobe durations for MP3/M4A as well as voice OGG.
        with tempfile.TemporaryDirectory(prefix=".capture-", dir=self.root) as scratch:
            source = Path(scratch) / "input"
            with source.open("xb") as handle:
                os.chmod(source, 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            details = probe(source)
            digest = hashlib.sha256(data).hexdigest()
            audio = {"status": "ready", "sha256": digest, "size": len(data), **details}
            target = self.path(digest)
            # Atomic replacement also repairs a corrupt cache entry, using verified bytes.
            os.replace(source, target)
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return audio

    async def capture(self, attachment):
        result = dict(attachment)
        async with self.slots:
            try:
                previous = result.get("audio") or {}
                if previous.get("status") == "unavailable":
                    return result
                if previous.get("status") == "ready":
                    try:
                        await joined_worker(self.read, previous)
                        return result
                    except ValueError, OSError:
                        pass
                url = result.get("url")
                if not trusted_discord_url(url):
                    raise ValueError("Audio URL is not a trusted Discord attachment CDN URL")
                if (result.get("size") or 0) > MAX_AUDIO_BYTES:
                    raise ValueError("Audio exceeds the 20 MiB file limit")

                async def fetch(client):
                    async with asyncio.timeout(15):
                        async with client.stream("GET", url, follow_redirects=False, timeout=15) as response:
                            if response.status_code != 200:
                                raise ValueError(
                                    f"Discord audio download returned HTTP {response.status_code}; reattach if expired"
                                )
                            if int(response.headers.get("content-length", 0)) > MAX_AUDIO_BYTES:
                                raise ValueError("Audio exceeds the 20 MiB file limit")
                            data = bytearray()
                            async for chunk in response.aiter_bytes():
                                if len(data) + len(chunk) > MAX_AUDIO_BYTES:
                                    raise ValueError("Audio exceeds the 20 MiB file limit")
                                data.extend(chunk)
                            return bytes(data)

                if self.client is not None:
                    data = await fetch(self.client)
                else:
                    async with httpx.AsyncClient(trust_env=False) as client:
                        data = await fetch(client)
                result["audio"] = await joined_worker(self.prepare, data)
            except (ValueError, OSError, httpx.HTTPError, TimeoutError) as exc:
                # Library exceptions may include signed URLs; retain safe, useful reasons.
                error = (
                    str(exc)
                    if isinstance(exc, ValueError)
                    else f"Audio capture failed ({type(exc).__name__}); reattach to retry"
                )
                result["audio"] = {
                    "status": "unavailable",
                    "error": error,
                    "retry_on_refreshed_url": isinstance(exc, (httpx.HTTPError, TimeoutError, OSError))
                    or error.startswith("Discord audio download returned HTTP"),
                }
        return result
