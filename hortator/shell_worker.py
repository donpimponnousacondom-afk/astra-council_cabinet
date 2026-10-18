"""Trusted, stdlib-only worker. Run ONLY inside shell_runner's bubblewrap boundary.

Its stdout is a bounded framed transport, not the model command's stdout. No
application package, credentials, or writable host directory exists here.
"""

from __future__ import annotations

import base64
from contextlib import ExitStack
import ctypes
import errno
import json
import os
from pathlib import Path
import resource
import select
import selectors
import signal
import stat
import subprocess
import sys
import time
import venv


def emit(kind, **fields):
    sys.stdout.write(json.dumps({"type": kind, **fields}, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def restrict(config):
    # Nondumpable supervisor: the child cannot open /proc/<parent>/fd/1 to
    # impersonate the framed transport or ptrace the trusted file exporter.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) or libc.prctl(38, 1, 0, 0, 0):
        raise RuntimeError("Cannot protect supervisor/set no_new_privs")
    limits = {
        resource.RLIMIT_AS: config["memory_bytes_per_process"],
        resource.RLIMIT_NPROC: config["process_limit"],
        resource.RLIMIT_FSIZE: max(config["max_file_bytes"], config["package_bytes"]),
        resource.RLIMIT_NOFILE: 128,
        resource.RLIMIT_CORE: 0,
        resource.RLIMIT_MEMLOCK: 0,
        resource.RLIMIT_CPU: max(1, int(config["timeout_seconds"]) + 1),
    }
    for kind, value in limits.items():
        resource.setrlimit(kind, (value, value))
    # Namespaces are the filesystem/process boundary. Seccomp also
    # removes kernel interfaces that could allocate unaccounted shared memory,
    # bypass the transport or modify namespace topology. Network sockets use
    # the host network namespace; filesystem and process restrictions remain.
    seccomp = ctypes.CDLL("/lib/libseccomp.so.2", use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    policy = seccomp.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not policy:
        raise RuntimeError("Cannot initialize seccomp")
    deny = (
        "unshare setns mount umount2 pivot_root chroot ptrace "
        "process_vm_readv process_vm_writev memfd_create shmget shmat shmctl "
        "bpf perf_event_open io_uring_setup io_uring_enter io_uring_register userfaultfd "
        "keyctl add_key request_key reboot kexec_load kexec_file_load open_by_handle_at "
        "name_to_handle_at syslog swapon swapoff mknod mknodat fsopen fsconfig fsmount "
        "fspick open_tree move_mount mount_setattr mq_open"
    ).split()
    try:
        for name in deny:
            number = seccomp.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and seccomp.seccomp_rule_add(policy, 0x00050000 | errno.EPERM, number, 0):
                raise RuntimeError("Cannot install required seccomp rule")
        if seccomp.seccomp_load(policy):
            raise RuntimeError("Cannot load seccomp")
    finally:
        seccomp.seccomp_release(policy)


def files(root, config):
    """Validate the entire tree before exporting any changes. Never follow links."""
    result, total = [], 0
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as items:
            for item in items:
                relative = Path(item.path).relative_to(root).as_posix()
                if len(relative) > 240 or any(ord(c) < 32 for c in relative):
                    raise ValueError("Workspace output has an unsupported path")
                info = item.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(item.path))
                    result.append(("directory", relative, 0, False))
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    if info.st_size > config["max_file_bytes"]:
                        raise ValueError("Workspace output exceeds the per-file byte limit")
                    total += info.st_size
                    result.append(("file", relative, info.st_size, bool(info.st_mode & stat.S_IXUSR)))
                else:
                    kind = (
                        "symlink"
                        if stat.S_ISLNK(info.st_mode)
                        else "hardlinked regular file"
                        if stat.S_ISREG(info.st_mode)
                        else "special file"
                    )
                    raise ValueError(
                        f"Workspace output contains a {kind} at {relative!r}; "
                        "saved files must be regular files or directories. No workspace changes "
                        "from this call will be saved. Keep linked/runtime/cache files in /packages or /tmp."
                    )
                if len(result) > config["max_files"] or total > config["quota_bytes"]:
                    raise ValueError("Workspace output exceeds its file-count or byte quota")
    return sorted(result, key=lambda row: (row[0] != "directory", row[1]))


def kill_descendants():
    # This worker is namespace PID 1. Keep it alive until the framed export is
    # complete, then its exit gives the kernel a final whole-tree backstop.
    current = os.getpid()
    for _ in range(50):
        found = False
        for name in os.listdir("/proc"):
            if not name.isdigit() or int(name) in (1, current):
                continue
            try:
                status = Path("/proc", name, "stat").read_text().rsplit(")", 1)[1].split()[0]
                if status != "Z":
                    found = True
                    os.kill(int(name), signal.SIGKILL)
            except FileNotFoundError, ProcessLookupError:
                pass
        if not found:
            return
        time.sleep(0.01)
    raise RuntimeError("Could not confirm every sandbox descendant stopped; output discarded")


def entry_limit_exceeded(root, limit):
    pending, count = [root], 0
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    count += 1
                    if count > limit:
                        return {
                            "resource": "entries",
                            "path": str(root),
                            "limit": limit,
                            "observed_at_least": count,
                        }
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
        except FileNotFoundError:
            # A model process may remove its own directory while we scan.
            continue
    return None


def scratch_directory(path):
    """Restore required scratch directories without following replaced links."""
    path = Path(path)
    try:
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISDIR(info.st_mode):
            path.unlink()
        path.mkdir(exist_ok=True, mode=0o700)
        path.chmod(0o700)
    except OSError as error:
        raise RuntimeError(
            f"Cannot prepare task scratch directory {str(path)!r}: {error.strerror}. "
            "Bash did not start, so a command cannot repair this setup failure. "
            "Finish the current human task to release its temporary scratch before retrying. "
            "Saved workspace files are unchanged."
        ) from error


def prepare_scratch():
    scratch_directory("/packages/tmp")
    scratch_directory("/packages/tmp/runtime")
    os.environ["TMPDIR"] = "/packages/tmp"
    os.environ["XDG_RUNTIME_DIR"] = "/packages/tmp/runtime"
    if not Path("/packages/venv/bin/python3.14").exists() or not Path("/packages/venv/pyvenv.cfg").is_file():
        try:
            # Repair scaffolding in place: installed packages must survive.
            venv.EnvBuilder(system_site_packages=True, symlinks=True).create("/packages/venv")
        except (OSError, ValueError) as error:
            cause = error.strerror if isinstance(error, OSError) else str(error)
            raise RuntimeError(
                f"Cannot repair task Python environment '/packages/venv': {cause}. "
                "Bash did not start; retained packages may be incomplete. "
                "Finish the current human task to release its temporary scratch before retrying. "
                "Saved workspace files are unchanged."
            ) from error


def run_command(config, cwd, root):
    emit("started", pid_namespace=os.readlink("/proc/self/ns/pid"))
    started = time.monotonic()
    cwd_bytes = bytearray()
    cwd_size = 0
    # A pipe cannot collide with retained scratch entries or require tmpfs space.
    # Only its write end reaches the command, never the worker's framed transport.
    with ExitStack() as resources:
        read_fd, write_fd = os.pipe()
        cwd_read = resources.enter_context(os.fdopen(read_fd, "rb", buffering=0))
        cwd_write = resources.enter_context(os.fdopen(write_fd, "wb", buffering=0))
        os.set_blocking(cwd_read.fileno(), False)
        command = f"trap 'builtin pwd -P >&{write_fd}' EXIT\n" + config["command"]
        child = subprocess.Popen(
            ["/bin/bash", "--noprofile", "--norc", "-c", command],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(write_fd,),
            start_new_session=True,
            # Only command processes load task-installed native libraries. The
            # trusted supervisor must always start/import/restrict against readonly
            # baseline libraries, even when scratch survives earlier commands.
            env={**os.environ, "LD_LIBRARY_PATH": "/packages/native/lib:/lib"},
        )
        cwd_write.close()
        resources.callback(child.stdout.close)
        resources.callback(child.stderr.close)
        selector = resources.enter_context(selectors.DefaultSelector())
        selector.register(cwd_read, selectors.EVENT_READ, "cwd")
        selector.register(0, selectors.EVENT_READ, "parent")
        for stream in ("stdout", "stderr"):
            pipe = getattr(child, stream)
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, stream)
        count = 0
        state = None
        resource_limit = None
        cleaned = False
        last_scan = started
        while any(key.data != "parent" for key in selector.get_map().values()) or child.poll() is None:
            if time.monotonic() - started >= config["timeout_seconds"] and state is None:
                state = "timed_out"
            if time.monotonic() - last_scan >= 0.05 and state is None:
                last_scan = time.monotonic()
                resource_limit = entry_limit_exceeded(root, config["max_files"]) or entry_limit_exceeded(
                    "/packages", config["package_entries"]
                )
                if resource_limit is not None:
                    state = "resource_limited"
            if (state is not None or child.poll() is not None) and not cleaned:
                kill_descendants()
                cleaned = True
            for key, _ in selector.select(0.03):
                if key.data == "parent":
                    if not os.read(0, 1):
                        state = "cancelled"
                        selector.unregister(0)
                    continue
                data = os.read(key.fileobj.fileno(), 16_384)
                if not data:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                if key.data == "cwd":
                    cwd_size += len(data)
                    cwd_bytes.extend(data[: max(0, 1024 - len(cwd_bytes))])
                    continue
                allowed = min(len(data), config["output_bytes"] - count)
                if allowed:
                    emit(key.data, data=base64.b64encode(data[:allowed]).decode())
                    count += allowed
                if allowed != len(data):
                    state = "output_limited"
        code = child.wait()
        kill_descendants()
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
            except ChildProcessError:
                break
    state = state or ("succeeded" if code == 0 else "failed")
    new_cwd = config["cwd"]
    try:
        if cwd_size > 1024:
            raise ValueError("Command working directory exceeds its bounded receipt")
        candidate = cwd_bytes.decode().strip()
        candidate_path = Path(candidate)
        if candidate_path.is_absolute():
            resolved = candidate_path.resolve(strict=True)
            if resolved.is_relative_to(root) and resolved.is_dir():
                new_cwd = resolved.relative_to(root).as_posix()
    except OSError, ValueError:
        pass
    emit(
        "result",
        status=state,
        exit_code=code,
        elapsed_seconds=round(time.monotonic() - started, 3),
        cwd=new_cwd,
        **({"resource_limit": resource_limit} if state == "resource_limited" else {}),
    )
    return state


def main():
    if select.select([0], [], [], 0)[0] and not os.read(0, 1):
        raise RuntimeError("Runner parent disconnected before sandbox startup")
    config = json.loads(Path("/runner/config.json").read_text())
    os.umask(0o077)
    root = Path("/workspace")
    for kind, relative, _, executable in files(Path("/source"), config):
        destination = root / relative
        if kind == "directory":
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with open(Path("/source") / relative, "rb") as source, open(destination, "xb") as target:
                while data := source.read(32_768):
                    target.write(data)
                os.fchmod(target.fileno(), 0o700 if executable else 0o600)
    cwd = root / config["cwd"]
    if not cwd.is_dir() or cwd.resolve().is_relative_to(root) is False:
        raise ValueError("Working directory is missing or outside the workspace")
    prepare_scratch()
    restrict(config)
    if select.select([0], [], [], 0)[0] and not os.read(0, 1):
        raise RuntimeError("Runner parent disconnected before Bash startup")
    state = run_command(config, cwd, root)
    if state in ("succeeded", "failed"):
        for kind, relative, size, executable in files(root, config):
            # Validate the same filesystem bounds, but never send disposable
            # file contents back to the host for staging or persistence.
            if not config.get("persist_files", True):
                continue
            emit(kind, path=relative, size=size, **({"executable": executable} if kind == "file" else {}))
            if kind == "file":
                with open(root / relative, "rb") as source:
                    while data := source.read(32_768):
                        emit("data", data=base64.b64encode(data).decode())
                emit("endfile")
    emit("complete")


if __name__ == "__main__":
    try:
        main()
    except BaseException as error:
        try:
            kill_descendants()
        except BaseException:
            pass
        emit("error", error=f"{type(error).__name__}: {error}"[:1000])
        sys.exit(1)
