"""Finite synchronous Bash jobs in a fail-closed Linux namespace boundary."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import signal
import ssl
import stat
import subprocess
import sys
import sysconfig
import tempfile
import time

from .concurrency import join_tasks, task_group, error_text

from .models import ControlError


DEFAULTS = {
    "timeout_seconds": 90,
    "output_bytes": 1_048_576,
    "memory_bytes_per_process": 2_147_483_648,
    "process_limit": 16,
    "package_bytes": 2_147_483_648,
    "package_entries": 50_000,
    "retention_days": 7,
    "max_jobs_per_bot": 200,
    "job_storage_bytes_per_bot": 104_857_600,
}
CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 90},
        "output_bytes": {"type": "integer", "minimum": 1024, "maximum": 4_194_304},
        "memory_bytes_per_process": {"type": "integer", "minimum": 67_108_864, "maximum": 4_294_967_296},
        "process_limit": {"type": "integer", "minimum": 4, "maximum": 32},
        "package_bytes": {"type": "integer", "minimum": 67_108_864, "maximum": 4_294_967_296},
        "package_entries": {"type": "integer", "minimum": 1000, "maximum": 100_000},
        "retention_days": {"type": "integer", "minimum": 1, "maximum": 30},
        "max_jobs_per_bot": {"type": "integer", "minimum": 1, "maximum": 1000},
        "job_storage_bytes_per_bot": {"type": "integer", "minimum": 1_048_576, "maximum": 1_073_741_824},
    },
    "additionalProperties": False,
}
TOOLS = (
    "bash sh env ls stat cat head tail wc du cp mv mkdir rm rmdir touch find grep sed awk "
    "sort uniq cut tr xargs tee date sleep printf basename dirname realpath sha256sum base64 "
    "od file gzip gunzip tar zip unzip timeout true false "
    "curl wget git jq perl ps pgrep pkill id whoami groups ss netstat ip "
    "arch b2sum base32 basenc chcon chgrp chmod chown cksum comm csplit dd df dir dircolors "
    "dirname expand expr factor fmt fold hostid install join link ln logname md5sum mkfifo "
    "mknod mktemp nice nl nohup nproc numfmt paste pathchk pinky pr printenv ptx readlink "
    "rev runcon seq sha1sum sha224sum sha384sum sha512sum shred shuf split stdbuf stty sum "
    "sync tac test truncate tsort tty uname unexpand unlink uptime users vdir who yes "
    "cut diff patch xz unxz bzip2 bunzip2 readelf"
).split()
TOOLS = list(dict.fromkeys(TOOLS))
FINAL_STATES = {
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "output_limited",
    "resource_limited",
    "interrupted",
}
SETUP = (
    "Provide Linux with unprivileged user/PID/mount namespaces, bubblewrap supporting "
    "--disable-userns and --size, libseccomp.so.2, Python 3.14/Pillow/pip, uv, micromamba, "
    "the documented OS tool packages, system DNS configuration and a CA certificate bundle. "
    "Install operator-managed OS packages and enable the required namespace policy, then retry readiness. "
    "No unrestricted-host fallback exists."
)


def limits(configuration=None):
    if configuration is not None and not isinstance(configuration, dict):
        raise ControlError("Shell configuration must be an object")
    result = {**DEFAULTS, **(configuration or {})}
    bounds = {
        "timeout_seconds": (0.1, 90),
        "output_bytes": (1024, 4_194_304),
        "memory_bytes_per_process": (67_108_864, 4_294_967_296),
        "process_limit": (4, 32),
        "package_bytes": (67_108_864, 4_294_967_296),
        "package_entries": (1000, 100_000),
        "retention_days": (1, 30),
        "max_jobs_per_bot": (1, 1000),
        "job_storage_bytes_per_bot": (1_048_576, 1_073_741_824),
    }
    errors = [f"Unknown field {name}" for name in result if name not in bounds]
    for name, (low, high) in bounds.items():
        value = result[name]
        numeric = isinstance(value, (float, int)) and not isinstance(value, bool)
        if (
            not numeric
            or not low <= value <= high
            or (name != "timeout_seconds" and not isinstance(value, int))
        ):
            errors.append(
                f"{name} must be {'a number' if name == 'timeout_seconds' else 'an integer'} {low}..{high}"
            )
    if errors:
        raise ControlError("Invalid shell configuration: " + "; ".join(errors))
    return {name: result[name] for name in bounds}


validate_config = limits


def relative(path):
    if not isinstance(path, str) or not path or len(path) > 240 or "\\" in path or "\x00" in path:
        raise ControlError("Use a workspace-relative path, at most 240 characters")
    value = PurePosixPath(path)
    if value.is_absolute() or ".." in value.parts or any(ord(c) < 32 for c in path):
        raise ControlError("Absolute paths, traversal and control characters are forbidden")
    return value.as_posix()


def _identity(context):
    bot = context.bot
    return str(bot["id"] if isinstance(bot, dict) else bot.id), str(context.channel_id), str(context.turn_id)


def _private_json(path, value):
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(8))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(value, output, ensure_ascii=True)
    os.replace(temporary, path)


@dataclass
class _Active:
    metadata: dict
    process: asyncio.subprocess.Process | None = None
    cancelled: bool = False
    init_pidfd: int | None = None
    info_path: Path | None = None
    task: asyncio.Task | None = None


class ShellRunner:
    def __init__(self, jobs_root: Path, workspaces=None):
        self.root = Path(jobs_root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ControlError("Job storage must be a real private directory, not a symlink")
        os.chmod(self.root, 0o700)
        self.workspaces = workspaces
        self.active: dict[str, _Active] = {}
        self._toolchain = None
        self._readiness = None
        self._readiness_lock = asyncio.Lock()
        self._probe = None
        self._closed = False
        # The one-runtime data lock belongs to Kernel. Recovery never signals a
        # persisted PID (it may now belong to an unrelated process).
        for path in self.root.glob("job_*/meta.json"):
            try:
                self._safe_job(path.parent.name)
                metadata = json.loads(path.read_text())
            except OSError, ValueError:
                continue
            if metadata.get("status") == "running":
                metadata.update(status="interrupted", finished_at=time.time(), workspace_committed=False)
                metadata["error"] = (
                    "Server restarted during job; command was not replayed. Inspect retained output."
                )
                _private_json(path, metadata)
            shutil.rmtree(path.parent / ".execution", ignore_errors=True)

    def _save(self, metadata):
        _private_json(self.root / metadata["job_id"] / "meta.json", metadata)

    def _event(self, metadata, kind, **extra):
        if self.workspaces is None or metadata.get("job_id") == "probe":
            return
        fields = (
            "job_id",
            "task",
            "status",
            "elapsed_seconds",
            "exit_code",
            "stdout_bytes",
            "stderr_bytes",
            "workspace_committed",
        )
        self.workspaces.store.emit(
            kind,
            {**{name: metadata[name] for name in fields if name in metadata}, **extra},
            bot_id=metadata["bot_id"],
            turn_id=metadata["turn_id"],
            level="warning" if kind == "job.failed" else "info",
        )

    def _safe_job(self, job_id):
        if not re.fullmatch(r"job_[a-f0-9]{32}", job_id):
            raise ControlError("Invalid job storage ID")
        if self.root.is_symlink():
            raise ControlError("Job storage root became a symlink")
        directory = self.root / job_id
        if directory.is_symlink() or not directory.is_dir():
            raise ControlError("Job storage directory is missing or unsafe")
        for name in ("meta.json", "stdout.log", "stderr.log"):
            path = directory / name
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ControlError("Job metadata/output must be private regular files without links")

    def _load(self, job_id):
        if not re.fullmatch(r"job_[a-f0-9]{32}", job_id):
            raise ControlError("Invalid job ID")
        self._safe_job(job_id)
        if job_id in self.active:
            return dict(self.active[job_id].metadata)
        try:
            return json.loads((self.root / job_id / "meta.json").read_text())
        except FileNotFoundError as error:
            raise ControlError("Job is missing or expired; run a new command if needed") from error

    def _owned(self, context, job_id):
        value = self._load(job_id)
        bot, channel, turn = _identity(context)
        if value["bot_id"] != bot or value["channel_id"] != channel or value["turn_id"] != turn:
            raise ControlError(
                "Job belongs to another bot, channel, or turn; reopen persistent files via workspace"
            )
        return value

    def status(self, context, job_id):
        value = self._owned(context, job_id)
        return {**value, "untrusted_output": True}

    def read(self, context, job_id, stream="stdout", offset=0, limit=8000):
        metadata = self._owned(context, job_id)
        if stream not in ("stdout", "stderr"):
            raise ControlError("stream must be stdout or stderr")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8000:
            raise ControlError("offset must be a nonnegative byte offset; limit must be 1..8000 bytes")
        if metadata.get("output_expired"):
            raise ControlError("Job output expired under retention; run a new command if needed")
        path = self.root / job_id / f"{stream}.log"
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ControlError("Job output must be a regular file without links")
            total = info.st_size
            if offset > total:
                raise ControlError(f"offset exceeds {stream} length of {total} bytes")
            source.seek(offset)
            data = source.read(limit)
        end = offset + len(data)
        return {
            "job_id": job_id,
            "stream": stream,
            "untrusted_output": True,
            "offset_unit": "bytes",
            "start": offset,
            "end": end,
            "total_bytes": total,
            "next_offset": end if end < total else None,
            "text": data.decode("utf-8", errors="replace"),
            # Byte-accurate representation keeps split Unicode recoverable.
            "data_base64": base64.b64encode(data).decode(),
            "status": metadata["status"],
        }

    def inspect(self, bot_id=None, channel_id=None, limit=50):
        result = []
        for path in self.root.glob("job_*/meta.json"):
            self._safe_job(path.parent.name)
            metadata = json.loads(path.read_text())
            if bot_id is not None and metadata["bot_id"] != bot_id:
                continue
            if channel_id is not None and metadata["channel_id"] != channel_id:
                continue
            result.append(metadata)
        return sorted(result, key=lambda row: row["started_at"], reverse=True)[: min(100, max(1, limit))]

    def _reserve(self, bot_id, config):
        jobs = []
        for path in self.root.glob("job_*/meta.json"):
            self._safe_job(path.parent.name)
            job = json.loads(path.read_text())
            if job.get("job_id") != path.parent.name:
                raise ControlError("Job metadata identity does not match its storage directory")
            jobs.append(job)
        jobs = [job for job in jobs if job["bot_id"] == bot_id]
        cutoff = time.time() - config["retention_days"] * 86_400
        stored = 0
        for job in jobs:
            path = self.root / job["job_id"]
            self._safe_job(job["job_id"])
            turn_running = self.workspaces.store.one("SELECT status FROM turns WHERE id=?", (job["turn_id"],))
            protected = job["job_id"] in self.active or (turn_running and turn_running["status"] == "running")
            if job["started_at"] < cutoff and not protected:
                for stream in ("stdout", "stderr"):
                    (path / f"{stream}.log").unlink(missing_ok=True)
                job["output_expired"] = True
                self._save(job)
            else:
                stored += sum(
                    (path / f"{stream}.log").stat().st_size
                    for stream in ("stdout", "stderr")
                    if (path / f"{stream}.log").exists()
                )
        if len(jobs) >= config["max_jobs_per_bot"]:
            raise ControlError(
                "Saved job count quota reached; operator must archive/remove old inactive jobs"
            )
        if stored + config["output_bytes"] > config["job_storage_bytes_per_bot"]:
            raise ControlError("Job output storage quota reached; lower output_bytes or archive expired jobs")

    @staticmethod
    def _discover_toolchain():
        if sys.platform != "linux" or os.getuid() == 0:
            raise ControlError("Shell runner requires a non-root Linux service account. " + SETUP)
        try:
            descriptor = os.pidfd_open(os.getpid())
            try:
                signal.pidfd_send_signal(descriptor, 0)
            finally:
                os.close(descriptor)
        except (OSError, AttributeError) as error:
            raise ControlError("Linux pidfd process-tree cancellation is unavailable. " + SETUP) from error
        bwrap = shutil.which("bwrap", path="/usr/bin:/bin")
        if not bwrap:
            raise ControlError("bubblewrap is unavailable. " + SETUP)
        if sys.version_info[:2] != (3, 14):
            raise ControlError("The application and sandbox require Python 3.14. " + SETUP)
        mounts = {}
        dependencies = set()
        available = []

        def libraries(source):
            if source in dependencies:
                return
            dependencies.add(source)
            probe = subprocess.run(
                ["/usr/bin/ldd", str(source)],
                capture_output=True,
                text=True,
                timeout=5,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            )
            for line in probe.stdout.splitlines():
                if "not found" in line:
                    raise ControlError(
                        f"An installed {Path(source).name} shared library is unavailable: {line.strip()}. "
                        + SETUP
                    )
                match = re.search(r"(?:=>\s+)?(/\S+)", line)
                if match:
                    library = Path(match.group(1)).resolve()
                    mounts[f"/lib/{Path(match.group(1)).name}"] = library
                    if (
                        match.group(1).startswith(("/lib/", "/lib64/", "/usr/lib/"))
                        or "ld-linux" in library.name
                    ):
                        mounts[match.group(1)] = library

        for name in TOOLS:
            selected = shutil.which(name, path="/usr/bin:/bin")
            if selected:
                source = Path(selected).resolve()
                mounts[f"/bin/{name}"] = source
                available.append(name)
                libraries(source)
        missing = sorted(set(TOOLS) - set(available))
        if missing:
            raise ControlError("Missing sandbox tools: " + ", ".join(missing) + ". " + SETUP)
        for name in ("uv", "micromamba"):
            selected = shutil.which(name)
            if not selected:
                raise ControlError(f"{name} is unavailable. " + SETUP)
            source = Path(selected).resolve()
            mounts[f"/bin/{name}"] = source
            libraries(source)
            available.append(name)
        git_core = Path(subprocess.check_output(["/usr/bin/git", "--exec-path"], text=True).strip())
        mounts["/usr/lib/git-core"] = git_core
        for executable in git_core.iterdir():
            if executable.is_file():
                libraries(executable.resolve())
        for directory in ("/usr/share/git-core/templates", "/usr/libexec/coreutils", "/usr/lib/coreutils"):
            if Path(directory).is_dir():
                mounts[directory] = Path(directory)
                for executable in Path(directory).glob("*.so"):
                    libraries(executable)
        perl_dirs = subprocess.check_output(
            ["/usr/bin/perl", "-e", 'print join("\\n", @INC)'],
            text=True,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        ).splitlines()
        for directory in perl_dirs:
            path = Path(directory)
            if path.is_absolute() and path.is_dir():
                mounts[directory] = path
                for extension in path.rglob("*.so"):
                    libraries(extension.resolve())
        assets = Path(__file__).with_name("shell_assets")
        for name in ("passwd", "group"):
            mounts[f"/etc/{name}"] = assets / name
        mounts["/bin/pip"] = assets / "pip"
        mounts["/bin/pkg"] = assets / "pkg"
        available += ["pip", "pip3", "pip3.14", "pkg"]
        python = Path(sys.executable).resolve()
        mounts["/runtime/python/bin/python3.14"] = python
        libraries(python)
        stdlib = Path(sysconfig.get_path("stdlib"))
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        for entry in stdlib.iterdir():
            if entry.name not in {"site-packages", "__pycache__", "idlelib", "test"}:
                mounts[f"/runtime/python/lib/{version}/{entry.name}"] = entry
        for extension in (stdlib / "lib-dynload").glob("*.so"):
            if not extension.name.startswith("_tkinter"):
                libraries(extension)
        mounts[f"/runtime/python/lib/{version}/EXTERNALLY-MANAGED"] = assets / "EXTERNALLY-MANAGED"
        pip = importlib.util.find_spec("pip")
        if pip is None or pip.origin is None:
            raise ControlError("pip is unavailable. " + SETUP)
        mounts[f"/runtime/python/lib/{version}/site-packages/pip"] = Path(pip.origin).parent
        pillow = importlib.util.find_spec("PIL")
        if pillow is None or pillow.origin is None:
            raise ControlError("Pillow is unavailable in the application environment. " + SETUP)
        pillow_path = Path(pillow.origin).parent
        mounts[f"/runtime/python/lib/{version}/site-packages/PIL"] = pillow_path
        bundled = pillow_path.parent / "pillow.libs"
        if bundled.is_dir():
            mounts[f"/runtime/python/lib/{version}/site-packages/pillow.libs"] = bundled
        for extension in pillow_path.glob("*.so"):
            libraries(extension)
        # Never bind an entire host /usr, /etc, home, venv, or repository.
        seccomp = next(
            (
                Path(base) / "libseccomp.so.2"
                for base in (
                    "/lib/x86_64-linux-gnu",
                    "/usr/lib/x86_64-linux-gnu",
                    "/lib/aarch64-linux-gnu",
                    "/usr/lib/aarch64-linux-gnu",
                    "/usr/lib64",
                    "/usr/lib",
                )
                if (Path(base) / "libseccomp.so.2").exists()
            ),
            None,
        )
        if seccomp is None:
            raise ControlError("libseccomp.so.2 is unavailable. " + SETUP)
        mounts["/lib/libseccomp.so.2"] = seccomp.resolve()
        libraries(seccomp.resolve())
        magic = Path("/usr/share/file/magic.mgc")
        if magic.exists():
            mounts["/usr/share/misc/magic.mgc"] = magic.resolve()
        # Resolve host symlinks before binding only these public configuration
        # files; do not expose /run, all of /etc, or private certificate keys.
        resolver = Path("/etc/resolv.conf")
        if not resolver.is_file():
            raise ControlError("System DNS configuration /etc/resolv.conf is unavailable. " + SETUP)
        mounts["/etc/resolv.conf"] = resolver.resolve()
        for filename in ("/etc/hosts", "/etc/nsswitch.conf"):
            path = Path(filename)
            if path.is_file():
                mounts[filename] = path.resolve()
        certificates = next(
            (
                Path(filename)
                for filename in (
                    ssl.get_default_verify_paths().openssl_cafile,
                    "/etc/ssl/certs/ca-certificates.crt",
                    "/etc/pki/tls/certs/ca-bundle.crt",
                    "/etc/ssl/ca-bundle.pem",
                )
                if filename and Path(filename).is_file()
            ),
            None,
        )
        if certificates is None:
            raise ControlError("System CA certificate bundle is unavailable. " + SETUP)
        mounts["/etc/ssl/certs/ca-certificates.crt"] = certificates.resolve()
        return {
            "bwrap": bwrap,
            "mounts": mounts,
            "tools": available + ["python", "python3", "python3.14", "Pillow"],
        }

    async def readiness(self, refresh=False):
        async with self._readiness_lock:
            if self._closed:
                return {"ready": False, "error": "Shell runner is shutting down", "setup": SETUP}
            if self._readiness is not None and not refresh:
                return dict(self._readiness)
            try:
                self._toolchain = await asyncio.to_thread(self._discover_toolchain)
                if self._closed:
                    raise ControlError("Shell runner is shutting down")
                with tempfile.TemporaryDirectory(prefix=".probe-", dir=self.root) as directory:
                    root = Path(directory)
                    source = root / "source"
                    source.mkdir(mode=0o700)
                    config = {
                        **limits(),
                        "command": (
                            "set -e; python3.14 -c 'import ensurepip, pip, venv; "
                            'from PIL import Image; print("ready")\'; '
                            "uv --version; micromamba --version; git --version; "
                            "perl -MJSON::PP -e 'print encode_json({ready=>1})'; "
                            "curl --version > /dev/null; wget --version > /dev/null"
                        ),
                        "cwd": ".",
                        "quota_bytes": 1_048_576,
                        "max_file_bytes": 524_288,
                        "max_files": 16,
                        "timeout_seconds": 5,
                    }
                    metadata = {"job_id": "probe", "status": "running"}
                    probe = _Active(metadata, task=asyncio.current_task())
                    self._probe = probe
                    try:
                        await self._execute(root, source, config, probe)
                    finally:
                        self._probe = None
                    if metadata.get("status") != "succeeded":
                        raise ControlError(metadata.get("error", "Isolation probe failed"))
                self._readiness = {
                    "ready": True,
                    "boundary": "bubblewrap + seccomp",
                    "network": "host",
                    "message": "Host networking, DNS and HTTPS enabled. Python 3.14, uv/venv and disposable native packages ready.",
                    "python": "3.14",
                    "package_storage": "Per-job /packages tmpfs; discarded after every job",
                    "tools": self._toolchain["tools"],
                    "limits": limits(),
                    "memory_scope": "per process; process count bounds multiplied address space",
                }
            except Exception as error:
                self._readiness = {
                    "ready": False,
                    "boundary": "unavailable",
                    "error": str(error)[:1200],
                    "setup": SETUP,
                    "network": "unavailable",
                }
            return dict(self._readiness)

    def _command(self, source, config_path, config):
        command = [
            self._toolchain["bwrap"],
            "--unshare-all",
            "--share-net",
            "--unshare-user",
            "--disable-userns",
            "--assert-userns-disabled",
            "--die-with-parent",
            "--as-pid-1",
            "--new-session",
            "--uid",
            "1000",
            "--gid",
            "1000",
            "--cap-drop",
            "ALL",
            "--clearenv",
            "--setenv",
            "PATH",
            "/packages/venv/bin:/packages/native/bin:/runtime/python/bin:/bin",
            "--setenv",
            "HOME",
            "/workspace",
            "--setenv",
            "TMPDIR",
            "/tmp",
            "--setenv",
            "LC_ALL",
            "C",
            "--setenv",
            "LD_LIBRARY_PATH",
            "/packages/native/lib:/lib",
            "--setenv",
            "PYTHONDONTWRITEBYTECODE",
            "1",
            "--setenv",
            "SSL_CERT_FILE",
            "/etc/ssl/certs/ca-certificates.crt",
            "--proc",
            "/proc",
            "--remount-ro",
            "/proc",
            "--size",
            str(config["quota_bytes"]),
            "--tmpfs",
            "/workspace",
            "--size",
            "16777216",
            "--tmpfs",
            "/tmp",
            "--size",
            str(config["package_bytes"]),
            "--tmpfs",
            "/packages",
            "--ro-bind",
            str(source),
            "/source",
            "--ro-bind",
            str(Path(__file__).with_name("shell_worker.py")),
            "/runner/worker.py",
            "--ro-bind",
            str(config_path),
            "/runner/config.json",
        ]
        environment = {
            "VIRTUAL_ENV": "/packages/venv",
            "UV_PYTHON": "/packages/venv/bin/python3.14",
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_CACHE_DIR": "/packages/cache/uv",
            "UV_TOOL_DIR": "/packages/uv-tools",
            "UV_TOOL_BIN_DIR": "/packages/native/bin",
            "UV_SYSTEM_CERTS": "true",
            "UV_CONCURRENT_DOWNLOADS": "2",
            "UV_CONCURRENT_BUILDS": "1",
            "UV_CONCURRENT_INSTALLS": "1",
            "RAYON_NUM_THREADS": "1",
            "PIP_CACHE_DIR": "/packages/cache/pip",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "MAMBA_ROOT_PREFIX": "/packages/mamba",
            "MAMBA_DOWNLOAD_THREADS": "2",
            "MAMBA_EXTRACT_THREADS": "1",
            "MAMBA_REPODATA_USE_ZST": "true",
            "GIT_EXEC_PATH": "/usr/lib/git-core",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSL_CAINFO": "/etc/ssl/certs/ca-certificates.crt",
            "CURL_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
            "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
            "MAMBA_SSL_VERIFY": "/etc/ssl/certs/ca-certificates.crt",
        }
        for name, value in environment.items():
            command += ["--setenv", name, value]
        for device in ("null", "zero", "random", "urandom"):
            command += ["--dev-bind", f"/dev/{device}", f"/dev/{device}"]
        for destination, path in sorted(self._toolchain["mounts"].items()):
            command += ["--ro-bind", str(path), destination]
        for name in ("python", "python3", "python3.14"):
            command += ["--symlink", "/runtime/python/bin/python3.14", f"/bin/{name}"]
        for name in ("pip3", "pip3.14"):
            command += ["--symlink", "/bin/pip", f"/bin/{name}"]
        command += [
            "--symlink",
            "/bin",
            "/usr/bin",
            "--symlink",
            "/etc/ssl/certs/ca-certificates.crt",
            "/etc/ssl/cert.pem",
            "--remount-ro",
            "/",
            "--chdir",
            "/workspace",
            "/runtime/python/bin/python3.14",
            "-I",
            "/runner/worker.py",
        ]
        return command

    async def _execute(self, directory, source, config, active):
        execution = directory / ".execution"
        execution.mkdir(mode=0o700)
        staged = execution / "result"
        staged.mkdir(mode=0o700)
        config_path = execution / "config.json"
        active.info_path = execution / "namespace.json"
        _private_json(config_path, config)
        metadata = active.metadata
        logs = {}
        for stream in ("stdout", "stderr"):
            logs[stream] = os.fdopen(
                os.open(
                    directory / f"{stream}.log", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
                ),
                "wb",
            )
        total_output = 0
        output_file = None
        output_remaining = 0
        total_files, total_bytes = 0, 0
        completed = False
        diagnostic = bytearray()
        process = None
        try:
            info_fd = os.open(active.info_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            command = self._command(source, config_path, config)
            command[1:1] = ["--info-fd", str(info_fd)]
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
                    start_new_session=True,
                    limit=100_000,
                    pass_fds=(info_fd,),
                )
            finally:
                os.close(info_fd)
            active.process = process
            if active.cancelled:
                await self._terminate(active)

            async def stderr():
                while data := await process.stderr.read(4096):
                    diagnostic.extend(data[: max(0, 4096 - len(diagnostic))])

            async with asyncio.timeout(config["timeout_seconds"] + 10):
                async with task_group() as group:
                    errors = group.create_task(stderr(), name="sandbox-stderr")
                    while line := await process.stdout.readline():
                        record = json.loads(line)
                        kind = record["type"]
                        if kind in logs:
                            data = base64.b64decode(record["data"], validate=True)
                            total_output += len(data)
                            if total_output > config["output_bytes"]:
                                raise ControlError("Sandbox output exceeded transport quota")
                            logs[kind].write(data)
                            logs[kind].flush()
                        elif kind == "started":
                            metadata["pid_namespace"] = record["pid_namespace"]
                            self._capture_init(active)
                            self._event(metadata, "job.progress", phase="sandbox_ready")
                        elif kind == "result":
                            if record["status"] not in FINAL_STATES:
                                raise ControlError("Invalid worker status")
                            metadata.update(
                                {
                                    name: record[name]
                                    for name in ("status", "exit_code", "elapsed_seconds", "cwd")
                                }
                            )
                        elif kind in ("file", "directory"):
                            if output_file is not None or metadata.get("status") not in (
                                "succeeded",
                                "failed",
                            ):
                                raise ControlError("Invalid workspace export sequence")
                            path = relative(record["path"])
                            target = staged / path
                            total_files += 1
                            if total_files > config["max_files"]:
                                raise ControlError("Workspace export exceeded file quota")
                            if kind == "directory":
                                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                            else:
                                size = record["size"]
                                if type(size) is not int or not 0 <= size <= config["max_file_bytes"]:
                                    raise ControlError("Workspace export exceeded per-file quota")
                                total_bytes += size
                                if total_bytes > config["quota_bytes"]:
                                    raise ControlError("Workspace export exceeded byte quota")
                                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                                output_file = os.fdopen(
                                    os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
                                )
                                output_remaining = size
                        elif kind == "data":
                            data = base64.b64decode(record["data"], validate=True)
                            if output_file is None or len(data) > output_remaining:
                                raise ControlError("Invalid workspace file chunk")
                            output_file.write(data)
                            output_remaining -= len(data)
                        elif kind == "endfile":
                            if output_file is None or output_remaining:
                                raise ControlError("Incomplete workspace file")
                            output_file.close()
                            output_file = None
                        elif kind == "complete":
                            if output_file is not None:
                                raise ControlError("Incomplete workspace export")
                            completed = True
                        elif kind == "error":
                            raise ControlError(record["error"])
                        else:
                            raise ControlError("Unknown sandbox transport frame")
                    await process.wait()
                    await errors
                    if not completed or process.returncode:
                        raise ControlError(
                            diagnostic.decode(errors="replace") or "Sandbox ended without complete output"
                        )
        except asyncio.CancelledError:
            metadata.update(status="cancelled", error="Job cancelled; sandbox workspace changes discarded")
            raise
        except TimeoutError:
            metadata.update(
                status="timed_out", error="Job execution/export deadline exceeded; changes discarded"
            )
        except Exception as error:
            metadata.update(status="failed", error=error_text(error)[:1200])
        finally:
            if process is not None and process.returncode is None:
                await self._terminate(active)
            if output_file is not None:
                output_file.close()
            for output in logs.values():
                output.close()
            if active.cancelled:
                metadata.update(
                    status="cancelled", error="Job cancelled; sandbox workspace changes discarded"
                )
            metadata["stdout_bytes"] = (directory / "stdout.log").stat().st_size
            metadata["stderr_bytes"] = (directory / "stderr.log").stat().st_size
            if active.init_pidfd is not None:
                os.close(active.init_pidfd)
                active.init_pidfd = None
        return staged if completed and not metadata.get("error") else None

    @staticmethod
    def _capture_init(active):
        if active.init_pidfd is not None or active.info_path is None or active.process is None:
            return
        descriptor = None
        try:
            pid = json.loads(active.info_path.read_text())["child-pid"]
            # The trusted bwrap info channel is written before mount setup.
            # Pin the process before checking its parent: PID reuse between a
            # prior parent check and pidfd_open could otherwise select a peer.
            descriptor = os.pidfd_open(pid)
            parent = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
            if parent == active.process.pid:
                active.init_pidfd = descriptor
                descriptor = None
        except OSError, ValueError, KeyError:
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)

    async def _terminate(self, active):
        process = active.process
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        for _ in range(100):
            self._capture_init(active)
            if active.init_pidfd is not None or process.returncode is not None:
                break
            await asyncio.sleep(0.01)
        if active.init_pidfd is not None:
            try:
                signal.pidfd_send_signal(active.init_pidfd, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.returncode is None:
            process.kill()
        await process.wait()

    async def run(
        self, context, *, command, task="default", cwd=None, timeout_seconds=None, configuration=None
    ):
        if self._closed:
            raise ControlError("Shell runner is shutting down")
        config = limits(configuration)
        if not isinstance(command, str) or not command or len(command) > 32_000 or "\x00" in command:
            raise ControlError("command must contain 1..32000 characters without NUL")
        if timeout_seconds is not None:
            if (
                type(timeout_seconds) not in (int, float)
                or not 0.1 <= timeout_seconds <= config["timeout_seconds"]
            ):
                raise ControlError(f"timeout_seconds must be 0.1..{config['timeout_seconds']}")
            config["timeout_seconds"] = timeout_seconds
        if cwd is not None:
            cwd = relative(cwd)
        readiness = await self.readiness()
        if self._closed:
            raise ControlError("Shell runner is shutting down")
        if not readiness["ready"]:
            raise ControlError(readiness["error"] + " " + SETUP)
        if self.workspaces is None:
            raise ControlError("Workspace store is unavailable")
        bot, channel, turn = _identity(context)
        # Concurrent provider work is separate; a finite global shell slot also
        # bounds total address-space/tmpfs multiplication across bots.
        if self.active:
            raise ControlError("The shell runner is busy; inspect the active job and retry after completion")
        self._reserve(bot, config)
        prepared = self.workspaces.prepare_job(context, task, None)
        job_id = "job_" + secrets.token_hex(16)
        directory = self.root / job_id
        metadata = {
            "job_id": job_id,
            "bot_id": bot,
            "channel_id": channel,
            "turn_id": turn,
            "task": task,
            "workspace_id": prepared["workspace_id"],
            "status": "running",
            "started_at": time.time(),
            "cwd": cwd or prepared["cwd"],
            "workspace_committed": False,
            "limits": config,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
        }
        created = False
        try:
            directory.mkdir(mode=0o700)
            created = True
            self._save(metadata)
            self._event(metadata, "job.started")
        except BaseException:
            self.workspaces.release_job(prepared["workspace_id"])
            if created:
                shutil.rmtree(directory, ignore_errors=True)
            raise
        active = _Active(metadata)
        active.task = asyncio.current_task()
        self.active[job_id] = active
        started = time.monotonic()
        try:
            run_config = {
                **config,
                **{name: prepared[name] for name in ("quota_bytes", "max_file_bytes", "max_files")},
                "cwd": metadata["cwd"],
                "command": command,
            }
            staged = await self._execute(directory, Path(prepared["path"]), run_config, active)
            if staged is not None and metadata["status"] in ("succeeded", "failed"):
                self.workspaces.finish_job(prepared["workspace_id"], staged, metadata["cwd"])
                metadata["workspace_committed"] = True
        except asyncio.CancelledError:
            metadata.update(status="cancelled", error="Job cancelled; sandbox workspace changes discarded")
            raise
        except Exception as error:
            metadata.update(status="failed", error=str(error)[:1200])
        finally:
            self.workspaces.release_job(prepared["workspace_id"])
            metadata.update(finished_at=time.time(), elapsed_seconds=round(time.monotonic() - started, 3))
            try:
                self._save(metadata)
            finally:
                shutil.rmtree(directory / ".execution", ignore_errors=True)
                self.active.pop(job_id, None)
            event = (
                "job.completed"
                if metadata["status"] == "succeeded"
                else ("job.cancelled" if metadata["status"] == "cancelled" else "job.failed")
            )
            self._event(metadata, event)
        return {
            **metadata,
            "stdout": self.read(context, job_id, "stdout", limit=3000),
            "stderr": self.read(context, job_id, "stderr", limit=3000),
            "untrusted_output": True,
        }

    async def cancel(self, context, job_id):
        self._owned(context, job_id)
        active = self.active.get(job_id)
        if active is not None:
            active.cancelled = True
            if active.process is not None and active.process.returncode is None:
                await self._terminate(active)
        return {
            "job_id": job_id,
            "cancellation_requested": active is not None,
            "status": "cancelling" if active is not None else self._load(job_id)["status"],
        }

    async def close(self):
        self._closed = True
        running = list(self.active.values()) + ([self._probe] if self._probe is not None else [])
        for active in running:
            active.cancelled = True
            if active.process is not None and active.process.returncode is None:
                await self._terminate(active)
            if active.task is not None and active.task is not asyncio.current_task():
                await join_tasks(active.task)
