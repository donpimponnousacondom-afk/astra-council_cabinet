"""Prepared Discord voice notes and their independently tracked delivery."""

import asyncio
import base64
from contextlib import closing
import json
import math
from pathlib import Path
import subprocess
import tempfile
import time

import discord
from discord.http import MultipartParameters

from .audio_cache import joined_worker, probe
from .concurrency import error_text
from .models import ControlError
from .store import dumps, uid


def convert(data, directory):
    """Finite, detached conversion; no Store/vault objects enter the worker."""
    with tempfile.TemporaryDirectory(prefix=".voice-", dir=directory) as scratch:
        source, target = Path(scratch) / "input", Path(scratch) / "voice.ogg"
        source.write_bytes(data)
        duration = probe(source)["duration_seconds"]
        if duration > 1200:
            raise ControlError("Discord voice notes must be at most 20 minutes")
        common = [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-threads",
            "1",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            "wav,mp3,ogg,flac,aac,aiff,mov,matroska,webm",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
        ]
        subprocess.run(
            [
                *common,
                "-ar",
                "48000",
                "-c:a",
                "libopus",
                "-b:a",
                "32k",
                "-threads",
                "1",
                "-f",
                "ogg",
                str(target),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
            timeout=30,
        )
        # One unsigned sample per millisecond keeps the waveform source bounded.
        samples = subprocess.run(
            [*common, "-ar", "1000", "-f", "u8", "-t", "1200", "pipe:1"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=30,
        ).stdout
        count = max(1, min(256, math.ceil(duration * 10)))
        waveform = bytes(
            min(
                255,
                max(
                    (
                        abs(v - 128) * 2
                        for v in samples[i * len(samples) // count : (i + 1) * len(samples) // count]
                    ),
                    default=0,
                ),
            )
            for i in range(count)
        )
        if target.stat().st_size > 8_000_000:
            raise ControlError("Generated voice note exceeds the 8 MB delivery limit")
        audio = target.read_bytes()
        return audio, {
            "duration_secs": probe(target)["duration_seconds"],
            "waveform": base64.b64encode(waveform).decode(),
        }


async def send_native(client, channel_id, path, metadata, nonce, reference):
    """Use discord.py's authenticated, rate-limited transport with native metadata."""
    payload = {
        "flags": 1 << 13,
        "allowed_mentions": discord.AllowedMentions.none().to_dict(),
        "attachments": [{"id": 0, "filename": "voice-message.ogg", **metadata}],
    }
    if nonce:
        payload.update(nonce=nonce, enforce_nonce=True)
    if reference:
        payload["message_reference"] = reference.to_dict()
    with closing(discord.File(path, filename="voice-message.ogg")) as file:
        params = MultipartParameters(
            payload=None,
            multipart=[
                {"name": "payload_json", "value": dumps(payload)},
                {
                    "name": "files[0]",
                    "value": file.fp,
                    "filename": file.filename,
                    "content_type": "audio/ogg",
                },
            ],
            files=[file],
        )
        response = await client.http.send_message(int(channel_id), params=params)
    return str(response["id"])


class VoiceNotes:
    def __init__(self, registry):
        self.registry = registry
        self.store = registry.store
        self.slots = asyncio.Semaphore(2)
        self.store.execute(
            "CREATE TABLE IF NOT EXISTS artifact_voice_notes ("
            "artifact_id TEXT PRIMARY KEY REFERENCES artifacts(id) ON DELETE CASCADE, "
            "metadata TEXT NOT NULL, transcript TEXT NOT NULL)"
        )

    async def prepare(self, data, text, context):
        async with self.slots:
            audio, metadata = await joined_worker(convert, data, self.registry.directory)
        artifact = self.registry.artifact(audio, "audio/ogg", ".ogg", context)
        self.store.execute(
            "INSERT INTO artifact_voice_notes VALUES(?,?,?)",
            (artifact["artifact_id"], dumps(metadata), self.registry.vault.redact(text)),
        )
        return {**artifact, "delivery": "voice_note", "duration_seconds": metadata["duration_secs"]}

    def split(self, artifact_ids, context):
        regular, notes = [], []
        for artifact_id in artifact_ids:
            path = self.registry.resolve_artifact(artifact_id, context)
            row = self.store.one("SELECT * FROM artifact_voice_notes WHERE artifact_id=?", (artifact_id,))
            if row:
                notes.append(
                    {
                        "artifact_id": artifact_id,
                        "path": path,
                        "metadata": json.loads(row["metadata"]),
                        "transcript": row["transcript"],
                    }
                )
            else:
                regular.append(path)
        return regular, notes

    async def deliver(
        self, engine, notes, bot, context, destination, parent_id, parent_discord_id, room, guard
    ):
        """Caller holds the destination lock; text has already been confirmed."""
        from .runtime import DeliveryError

        parent = self.store.one("SELECT routing FROM outbox WHERE id=?", (parent_id,))
        routing = json.loads(parent["routing"])
        pending = []
        for note in notes:
            outbox_id = uid("out_")
            content = "[Voice note transcript]\n" + note["transcript"]
            self.store.execute(
                "INSERT INTO outbox(id,turn_id,bot_id,channel_id,content,reply_to,artifacts,status,created_at,routing) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    outbox_id,
                    context.turn_id,
                    bot["id"],
                    destination,
                    content,
                    parent_discord_id,
                    dumps([note["artifact_id"]]),
                    "pending",
                    time.time(),
                    dumps({**routing, "kind": "voice_note", "parent_outbox_id": parent_id}),
                ),
            )
            pending.append((outbox_id, content, note))
            self.store.emit(
                "delivery.queued",
                {"outbox_id": outbox_id, "parent_outbox_id": parent_id, "kind": "voice_note"},
                bot_id=bot["id"],
                turn_id=context.turn_id,
            )
        try:
            for outbox_id, content, note in pending:
                try:
                    gap = room["send_gap_seconds"] if room else 1.5
                    await asyncio.sleep(max(0, engine.room_last[destination] + gap - time.time()))
                    guard()
                    self.store.execute("UPDATE outbox SET status='sending' WHERE id=?", (outbox_id,))
                    self.store.emit(
                        "delivery.sending",
                        {"outbox_id": outbox_id, "kind": "voice_note"},
                        bot_id=bot["id"],
                        turn_id=context.turn_id,
                    )
                    discord_id = await engine.transport.send(
                        bot,
                        destination,
                        "",
                        parent_discord_id,
                        [note["path"]],
                        nonce=outbox_id,
                        footer="",
                        strict_reply=True,
                        destination_guard=guard,
                        voice_note=note["metadata"],
                    )
                    sent = time.time()
                    self.store.execute(
                        "UPDATE outbox SET status='sent',sent_at=?,discord_id=? WHERE id=?",
                        (sent, discord_id, outbox_id),
                    )
                    engine.room_last[destination] = sent
                    self.store.execute("UPDATE bot_runtime SET last_sent=? WHERE bot_id=?", (sent, bot["id"]))
                    self.store.ingest(
                        discord_id=discord_id,
                        channel_id=destination,
                        author_id=engine.vault.get(f"bot/{bot['id']}/user_id") or bot["application_id"],
                        author_name=bot["name"],
                        content=content,
                        room_id=room["id"] if room else f"owner:{bot['id']}",
                        bot_id=bot["id"],
                        reply_to=parent_discord_id,
                        addressing={
                            "author_kind": "bot",
                            "live": False,
                            "routing": {"mode": routing["mode"], "source_bot_id": bot["id"]},
                        }
                        if routing
                        else None,
                        guild_id=routing["guild_id"] if routing else None,
                        parent_id=room["channel_id"]
                        if routing and destination != room["channel_id"]
                        else None,
                    )
                    self.store.execute(
                        "UPDATE messages SET content=? WHERE discord_id=?", (content, discord_id)
                    )
                    self.store.emit(
                        "delivery.sent",
                        {
                            "outbox_id": outbox_id,
                            "parent_outbox_id": parent_id,
                            "discord_id": discord_id,
                            "kind": "voice_note",
                        },
                        bot_id=bot["id"],
                        turn_id=context.turn_id,
                    )
                except (Exception, asyncio.CancelledError) as exc:
                    row = self.store.one("SELECT status FROM outbox WHERE id=?", (outbox_id,))
                    if row["status"] == "sent":
                        raise
                    uncertain = row["status"] == "sending" and (
                        not isinstance(exc, DeliveryError) or exc.uncertain
                    )
                    status = (
                        "unknown"
                        if uncertain
                        else "suppressed"
                        if isinstance(exc, asyncio.CancelledError)
                        else "failed"
                    )
                    if uncertain:
                        engine.room_last[destination] = time.time()
                        self.store.execute(
                            "UPDATE bot_runtime SET last_sent=? WHERE bot_id=?", (time.time(), bot["id"])
                        )
                    self.store.execute(
                        "UPDATE outbox SET status=?,error=? WHERE id=?",
                        (status, engine.vault.redact(error_text(exc)), outbox_id),
                    )
                    self.store.emit(
                        "delivery." + status,
                        {
                            "outbox_id": outbox_id,
                            "parent_outbox_id": parent_id,
                            "kind": "voice_note",
                            "error": error_text(exc),
                            "reason": "Text reply already delivered; voice note not confirmed. No automatic resend.",
                        },
                        bot_id=bot["id"],
                        turn_id=context.turn_id,
                        level="warning" if status == "suppressed" else "error",
                    )
                    if isinstance(exc, asyncio.CancelledError):
                        raise
        finally:
            for outbox_id, _, _ in pending:
                row = self.store.one("SELECT status FROM outbox WHERE id=?", (outbox_id,))
                if row["status"] != "pending":
                    continue
                self.store.execute(
                    "UPDATE outbox SET status='suppressed',error='Voice-note batch interrupted before dispatch' WHERE id=? AND status='pending'",
                    (outbox_id,),
                )
                self.store.emit(
                    "delivery.suppressed",
                    {
                        "outbox_id": outbox_id,
                        "kind": "voice_note",
                        "reason": "Voice-note batch interrupted before dispatch",
                    },
                    bot_id=bot["id"],
                    turn_id=context.turn_id,
                    level="warning",
                )
