"""Automatic delivery of immutable site revisions using an operator-owned SSH identity."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import time

from .concurrency import cancel_and_wait, task_group, error_text

from .documents import public_base, safe_path, safe_site
from .models import ControlError, ID_PATTERN
from .store import dumps

REMOTE_DEFAULTS = {"port": 22, "identity": "publishing", "debounce_seconds": 5}


def validate_config(config, *, per_bot=False):
    if per_bot:
        if set(config) - {"local_base_url"}:
            raise ControlError(
                "Bot document overrides may set local_base_url only; publication destination and automation belong to global plugin settings"
            )
        return
    for field in ("auto_publish", "remote_enabled"):
        if field in config and type(config[field]) is not bool:
            raise ControlError(f"document_site.{field} must be a boolean")
    remote = config.get("remote", {})
    if not isinstance(remote, dict):
        raise ControlError("document_site.remote must be an object")
    unknown = set(remote) - {
        "host",
        "port",
        "username",
        "identity",
        "web_root",
        "state_root",
        "debounce_seconds",
    }
    if unknown:
        raise ControlError("Unknown remote publication fields: " + ", ".join(sorted(unknown)))
    patterns = {
        "host": r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}",
        "username": r"[a-z_][a-z0-9_-]{0,63}",
        "identity": r"[a-z][a-z0-9_-]{0,47}",
        "web_root": r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+",
        "state_root": r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+",
    }
    for field, pattern in patterns.items():
        value = remote.get(field, "")
        if not isinstance(value, str) or (value and not re.fullmatch(pattern, value)):
            raise ControlError(f"Invalid remote.{field}")
        if field.endswith("root") and any(p in (".", "..") for p in value.split("/")):
            raise ControlError("Remote directories must be absolute paths without traversal")
    for field, lower, upper in (("port", 1, 65535), ("debounce_seconds", 0, 60)):
        if field in remote and (type(remote[field]) is not int or not lower <= remote[field] <= upper):
            raise ControlError(f"remote.{field} must be an integer from {lower} to {upper}")
    web, state = remote.get("web_root", ""), remote.get("state_root", "")
    if web and state and (web == state or web.startswith(state + "/") or state.startswith(web + "/")):
        raise ControlError("Public document root and private publishing state must be separate directories")
    if config.get("remote_enabled"):
        missing = [field for field in ("host", "username", "web_root", "state_root") if not remote.get(field)]
        if missing:
            raise ControlError("Remote publishing requires: " + ", ".join(missing))
        base = public_base(config.get("public_base_url", ""))
        if not base.startswith("https://"):
            raise ControlError("Remote publishing requires an HTTPS public base URL")


def _git(directory, *args):
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "core.attributesFile=/dev/null",
            "-c",
            "user.name=Hortator publishing",
            "-c",
            "user.email=publishing@hortator.invalid",
            *args,
        ],
        cwd=directory,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise ControlError("Publication snapshot Git operation failed")
    return result.stdout.strip()


def local_snapshot(directory, payload):
    """A separate private content repository. Never Git-track runtime credentials."""
    if not ID_PATTERN.fullmatch(payload["bot_id"]):
        raise ControlError("Invalid publication bot identifier")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", payload["job_id"]):
        raise ControlError("Invalid publication job identifier")
    safe_site(payload["slug"])
    directory = Path(directory).absolute() / "site_history"
    if any(path.is_symlink() for path in (directory, *directory.parents)):
        raise ControlError("Site history must not be a symbolic link")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    git_dir = directory / ".git"
    if git_dir.is_symlink() or (git_dir.exists() and not git_dir.is_dir()):
        raise ControlError("Site history Git directory must be an owned local directory")
    if not git_dir.exists():
        if any(directory.iterdir()):
            raise ControlError("Unmanaged site history files cannot be adopted")
        _git(directory, "-c", "init.templateDir=", "init", "-b", "history")
    if (
        _git(directory, "rev-parse", "--show-toplevel") != str(directory)
        or _git(directory, "rev-parse", "--absolute-git-dir") != str(git_dir)
        or _git(directory, "remote")
    ):
        raise ControlError("Site history must be a dedicated local repository without remotes")
    record = directory / "_snapshots" / (payload["job_id"] + ".json")
    if any(path.is_symlink() for path in (record, *record.parents)):
        raise ControlError("Snapshot metadata contains a symbolic link")
    metadata = {key: value for key, value in payload.items() if key != "files"}
    metadata["files"] = {
        name: {k: v for k, v in entry.items() if k != "content"} for name, entry in payload["files"].items()
    }
    encoded = dumps(metadata)
    if record.exists():
        if record.is_symlink() or record.read_text() != encoded:
            raise ControlError("Snapshot identity collision; refusing to rewrite history")
        commit = _git(directory, "log", "-1", "--format=%H", "--", str(record.relative_to(directory)))
        if commit:
            return commit
    prefix = directory / payload["bot_id"] / payload["slug"]
    for name, entry in payload["files"].items():
        path = prefix / safe_path(name)
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ControlError("Site history contains a symbolic link")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = base64.b64decode(entry["content"], validate=True)
        if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["blob"]:
            raise ControlError("Snapshot file integrity mismatch")
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    record.parent.mkdir(mode=0o700, exist_ok=True)
    record.write_text(encoded)
    record.chmod(0o600)
    _git(directory, "add", "--", str(prefix.relative_to(directory)), str(record.relative_to(directory)))
    _git(
        directory,
        "commit",
        "-m",
        f"{payload['bot_id']}/{payload['slug']} revision {payload['revision']} ({payload['job_id']})",
    )
    return _git(directory, "rev-parse", "HEAD")


class SSHDelivery:
    def __init__(self, directory, vault, remote):
        self.directory, self.vault = Path(directory), vault
        self.remote = {**REMOTE_DEFAULTS, **remote}
        self.receiver = Path(__file__).with_name("publishing_receiver.py").read_bytes()
        self.receiver_hash = hashlib.sha256(self.receiver).hexdigest()
        self.receiver_path = self.remote["state_root"] + "/receiver-" + self.receiver_hash[:20] + ".py"

    async def ssh(self, command, stdin=b"", timeout=120):
        private = self.vault.get(f"ssh/{self.remote['identity']}/private_key")
        if not private:
            raise ControlError("Publishing SSH identity is missing")
        known = self.directory / "ssh" / "known_hosts"
        if any(path.is_symlink() for path in (known, *known.parents)) or not known.is_file():
            raise ControlError("Verified publishing SSH host pins are missing")
        fd = os.memfd_create("hortator-publishing-identity", os.MFD_CLOEXEC)
        process = None
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, private.encode())
            argv = [
                "ssh",
                "-F",
                "/dev/null",
                "-T",
                "-p",
                str(self.remote["port"]),
                "-l",
                self.remote["username"],
                "-i",
                f"/proc/{os.getpid()}/fd/{fd}",
            ]
            options = {
                "BatchMode": "yes",
                "ConnectTimeout": "10",
                "ConnectionAttempts": "1",
                "IdentityAgent": "none",
                "IdentitiesOnly": "yes",
                "PreferredAuthentications": "publickey",
                "PasswordAuthentication": "no",
                "KbdInteractiveAuthentication": "no",
                "StrictHostKeyChecking": "yes",
                "UserKnownHostsFile": str(known),
                "GlobalKnownHostsFile": "/dev/null",
                "UpdateHostKeys": "no",
                "ForwardAgent": "no",
                "ClearAllForwardings": "yes",
                "ServerAliveInterval": "10",
                "ServerAliveCountMax": "2",
            }
            for key, value in options.items():
                argv.extend(["-o", key + "=" + value])
            argv.extend([self.remote["host"], shlex.join(command)])
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )

            async def collect(stream, limit):
                chunks, total = [], 0
                while chunk := await stream.read(65536):
                    total += len(chunk)
                    if total > limit:
                        raise ControlError("SSH receiver output exceeded its response limit")
                    chunks.append(chunk)
                return b"".join(chunks)

            async def send():
                try:
                    process.stdin.write(stdin)
                    await process.stdin.drain()
                    process.stdin.close()
                    await process.stdin.wait_closed()
                except BrokenPipeError, ConnectionResetError:
                    pass  # Preserve the actual SSH/receiver diagnostic below.

            async with asyncio.timeout(timeout):
                async with task_group() as group:
                    group.create_task(send(), name="publishing-stdin")
                    output = group.create_task(collect(process.stdout, 1_000_000), name="publishing-stdout")
                    errors = group.create_task(collect(process.stderr, 100_000), name="publishing-stderr")
                    group.create_task(process.wait(), name="publishing-process")
                out, err = output.result(), errors.result()
            if process.returncode:
                detail = self.vault.redact((err or out).decode(errors="replace"))[:2000]
                raise ControlError("Remote publishing failed: " + detail)
            return out
        finally:
            try:
                if process and process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process:
                    # A killed child can leave a full pipe paused by StreamReader
                    # flow control. Reaping before draining it can wait forever
                    # after a size-limit/timeout cancels the original collectors.
                    process.stdin.close()

                    async def discard(stream):
                        while await stream.read(65536):
                            pass

                    async with task_group() as group:
                        group.create_task(discard(process.stdout), name="publishing-drain-stdout")
                        group.create_task(discard(process.stderr), name="publishing-drain-stderr")
                    await process.wait()
            finally:
                os.close(fd)

    async def install(self):
        # Only trusted application source is installed; model files never enter
        # this command or the private control directory.
        bootstrap = """import hashlib,os,sys
from pathlib import Path
p=Path(sys.argv[1]);expected=sys.argv[2];data=sys.stdin.buffer.read(500001)
if len(data)>500000 or hashlib.sha256(data).hexdigest()!=expected:raise SystemExit('Receiver checksum mismatch')
if any(x.is_symlink() for x in (p,*p.parents)):raise SystemExit('Receiver path is a symlink')
p.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
if p.exists():
 if p.read_bytes()!=data:raise SystemExit('Existing receiver differs')
else:
 fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
 with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
print('receiver ready')"""
        await self.ssh(
            ["python3", "-c", bootstrap, self.receiver_path, self.receiver_hash], self.receiver, 30
        )

    async def action(self, action, payload=None):
        raw = await self.ssh(
            [
                "python3",
                self.receiver_path,
                "--web-root",
                self.remote["web_root"],
                "--state-root",
                self.remote["state_root"],
                action,
            ],
            dumps(payload or {}).encode(),
        )
        try:
            return json.loads(raw)
        except ValueError:
            raise ControlError("Remote receiver did not return a valid receipt") from None

    async def deliver(self, payload):
        await self.install()
        return await self.action("deploy", payload)

    async def install_audit_schedule(self):
        """Operator setup: preserve other crontab entries and manage one audit line."""
        await self.install()
        command = shlex.join(
            [
                "/usr/bin/python3",
                self.receiver_path,
                "--web-root",
                self.remote["web_root"],
                "--state-root",
                self.remote["state_root"],
                "audit",
            ]
        )
        command += " >> " + shlex.quote(self.remote["state_root"] + "/metadata/cron-audit.log") + " 2>&1"
        script = """import subprocess,sys
tag='# hortator-publishing-audit'
old=subprocess.run(['crontab','-l'],capture_output=True,text=True)
if old.returncode not in (0,1):raise SystemExit('Cannot read crontab')
lines=[line for line in old.stdout.splitlines() if not line.rstrip().endswith(tag)]
lines.append('*/15 * * * * '+sys.argv[1]+' '+tag)
new='\\n'.join(lines)+'\\n'
if new!=old.stdout:
 r=subprocess.run(['crontab','-'],input=new,text=True,capture_output=True)
 if r.returncode:raise SystemExit('Cannot install publishing audit schedule')
print('Publishing audit scheduled every 15 minutes; other cron entries preserved')"""
        # Initialize private audit metadata/log parent before cron can run.
        await self.action("audit")
        return (await self.ssh(["python3", "-c", script, command], timeout=30)).decode().strip()


class PublishingWorker:
    def __init__(self, store, vault, directory, documents):
        self.store, self.vault, self.directory, self.documents = store, vault, Path(directory), documents
        self.task = None
        self.last_error = None
        self.delivery_factory = SSHDelivery
        columns = {row["name"] for row in store.rows("PRAGMA table_info(document_sync_queue)")}
        for name, kind in {
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "retry_at": "REAL NOT NULL DEFAULT 0",
            "last_error": "TEXT",
            "delivered_at": "REAL",
            "snapshot_commit": "TEXT",
            "remote_commit": "TEXT",
            "remote_current_revision": "INTEGER",
            "remote_delivery_current": "INTEGER",
            "remote_observed_at": "REAL",
        }.items():
            if name not in columns:
                store.execute(f"ALTER TABLE document_sync_queue ADD COLUMN {name} {kind}")
        documents.set_remote_state(self.status)

    def config(self):
        plugin = self.store.get("plugins", "document_site") or {}
        return plugin, plugin.get("config", {})

    def status(self):
        plugin, config = self.config()
        enabled = bool(plugin.get("enabled") and config.get("remote_enabled"))
        error = None
        if enabled:
            try:
                validate_config(config)
                identity = config.get("remote", {}).get("identity", "publishing")
                if not self.vault.get(f"ssh/{identity}/private_key"):
                    raise ControlError("Publishing SSH identity is missing")
                pins = self.directory / "ssh" / "known_hosts"
                if any(path.is_symlink() for path in (pins, *pins.parents)) or not pins.is_file():
                    raise ControlError("Verified SSH host pins are missing")
            except ControlError as exc:
                error = str(exc)
        return {
            "enabled": enabled,
            "configured": enabled and not error,
            "status": "unconfigured" if error else "enabled" if enabled else "disabled",
            "last_error": error or self.last_error,
            "public_base_url": config.get("public_base_url", ""),
        }

    def allowed(self, job):
        bot = self.store.get("bots", job["bot_id"])
        settings = self.store.get("settings", "global") or {}
        return bool(
            settings.get("enabled", True)
            and bot
            and bot.get("enabled")
            and "document_site" in bot.get("enabled_plugins", [])
            and self.status()["configured"]
        )

    def start(self):
        self.store.execute(
            "UPDATE document_sync_queue SET status='queued',retry_at=0,last_error='Delivery interrupted; verifying the same immutable job on retry' WHERE status='syncing'"
        )
        self.task = self.background.spawn(self.run(), name="publishing-worker")

    async def close(self):
        if self.task:
            await cancel_and_wait(self.task)

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = self.vault.redact(error_text(exc))[:2000]
                self.store.emit("publishing.worker_failed", {"error": self.last_error}, level="error")
            await asyncio.sleep(2)

    async def tick(self):
        if not self.status()["configured"]:
            return
        _, config = self.config()
        remote = {**REMOTE_DEFAULTS, **config["remote"]}
        eligible = [bot["id"] for bot in self.store.list("bots") if self.allowed({"bot_id": bot["id"]})]
        if not eligible:
            return
        placeholders = ",".join("?" for _ in eligible)
        jobs = self.store.rows(
            "SELECT q.* FROM document_sync_queue q WHERE q.status IN ('queued','failed') AND q.retry_at<=? AND q.updated_at<=? "
            f"AND q.bot_id IN ({placeholders}) AND NOT EXISTS (SELECT 1 FROM document_sync_queue older WHERE older.bot_id=q.bot_id AND older.slug=q.slug AND older.revision<q.revision AND older.attempts>0 AND older.status IN ('queued','failed','syncing')) ORDER BY q.created_at LIMIT 1",
            (time.time(), time.time() - remote["debounce_seconds"], *eligible),
        )
        job = next((row for row in jobs if self.allowed(row)), None)
        if not job:
            return
        self.store.execute(
            "UPDATE document_sync_queue SET status='syncing',attempts=attempts+1,updated_at=? WHERE id=?",
            (time.time(), job["id"]),
        )
        self.store.emit(
            "publishing.started",
            {"site": job["slug"], "revision": job["revision"], "job_id": job["id"]},
            bot_id=job["bot_id"],
        )
        try:
            revision = self.store.one(
                "SELECT * FROM document_revisions WHERE bot_id=? AND slug=? AND revision=?",
                (job["bot_id"], job["slug"], job["revision"]),
            )
            if not revision:
                raise ControlError("Queued document revision is missing")
            manifest = json.loads(revision["manifest"])

            def files():
                return {
                    path: {
                        **entry,
                        "content": base64.b64encode(
                            self.documents._read_entry(job["bot_id"], entry)[0]
                        ).decode(),
                    }
                    for path, entry in manifest.items()
                }

            payload = {
                "version": 1,
                "bot_id": job["bot_id"],
                "slug": job["slug"],
                "revision": job["revision"],
                "job_id": job["id"],
                "turn_id": revision["turn_id"],
                "created_at": revision["created_at"],
                "files": await asyncio.to_thread(files),
            }
            commit = await asyncio.to_thread(local_snapshot, self.directory, payload)
            payload["local_snapshot_commit"] = commit
            self.store.execute(
                "UPDATE document_sync_queue SET snapshot_commit=? WHERE id=?", (commit, job["id"])
            )
            if not self.allowed(job) or self.config()[1] != config:
                raise ControlError("Publication grant or destination changed before transfer")
            transport = self.delivery_factory(self.directory, self.vault, remote)
            async with task_group() as group:
                active = group.create_task(transport.deliver(payload), name="site-delivery:" + job["id"])
                try:
                    while not active.done():
                        await asyncio.wait({active}, timeout=0.5)
                        if not self.allowed(job) or self.config()[1] != config:
                            raise ControlError(
                                "Publication interrupted by grant or destination change; retry will verify remote state"
                            )
                finally:
                    if not active.done():
                        active.cancel()
            receipt = active.result()
            expected_hash = hashlib.sha256(
                json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            ).hexdigest()
            if (
                not isinstance(receipt, dict)
                or type(receipt.get("version")) is not int
                or receipt["version"] != 1
                or receipt.get("delivered") is not True
                or type(receipt.get("revision")) is not int
                or any(receipt.get(k) != payload[k] for k in ("bot_id", "slug", "revision", "job_id"))
                or receipt.get("manifest_hash") != expected_hash
                or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", str(receipt.get("remote_commit", "")))
                or type(receipt.get("current_revision")) is not int
                or receipt["current_revision"] < payload["revision"]
                or type(receipt.get("delivery_current")) is not bool
                or receipt["delivery_current"] != (receipt["current_revision"] == payload["revision"])
            ):
                raise ControlError("Remote receipt does not match the queued immutable snapshot")
            # Retain the receiver's actual visible revision even when an older
            # restored database cannot safely reconcile it. An old successful
            # receipt proves historical delivery, not that those bytes are the
            # version currently served by the remote host.
            self.store.execute(
                "UPDATE document_sync_queue SET remote_current_revision=?,remote_delivery_current=?,remote_observed_at=?,remote_commit=? WHERE id=?",
                (
                    receipt["current_revision"],
                    int(receipt["delivery_current"]),
                    time.time(),
                    receipt["remote_commit"],
                    job["id"],
                ),
            )
            local_site = self.store.one(
                "SELECT revision FROM document_sites WHERE bot_id=? AND slug=?", (job["bot_id"], job["slug"])
            )
            if not local_site or receipt["current_revision"] > local_site["revision"]:
                raise ControlError(
                    f"Remote site is ahead of this local data: remote revision {receipt['current_revision']}, "
                    f"local revision {local_site['revision'] if local_site else 'missing'}. "
                    "Restore the matching or newer local backup before publishing; the older receipt does not confirm the current site."
                )
            at = time.time()
            target = public_base(config["public_base_url"]) + f"/{job['bot_id']}/{job['slug']}/"
            self.store.execute(
                "UPDATE document_sync_queue SET status='delivered',delivered_at=?,updated_at=?,last_error=NULL,remote_commit=?,target_url=? WHERE id=?",
                (at, at, receipt["remote_commit"], target, job["id"]),
            )
            self.last_error = None
            self.store.emit(
                "publishing.delivered",
                {
                    "site": job["slug"],
                    "revision": job["revision"],
                    "url": target,
                    "snapshot_commit": commit,
                    "remote_commit": receipt["remote_commit"],
                },
                bot_id=job["bot_id"],
            )
        except asyncio.CancelledError:
            self.store.execute(
                "UPDATE document_sync_queue SET status='queued',retry_at=0,last_error='Delivery interrupted; remote state will be verified on retry' WHERE id=?",
                (job["id"],),
            )
            raise
        except Exception as exc:
            error = self.vault.redact(error_text(exc))[:2000]
            delay = min(300, 5 * 2 ** min(job["attempts"], 6))
            self.store.execute(
                "UPDATE document_sync_queue SET status='failed',last_error=?,retry_at=?,updated_at=? WHERE id=?",
                (error, time.time() + delay, time.time(), job["id"]),
            )
            self.last_error = error
            self.store.emit(
                "publishing.failed",
                {"site": job["slug"], "revision": job["revision"], "error": error, "retry_in_seconds": delay},
                bot_id=job["bot_id"],
                level="error",
            )
