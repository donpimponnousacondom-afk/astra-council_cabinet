"""Private task tmpfs, retained by namespace descriptors rather than a daemon.

The short trusted setup process mounts only in its own user/mount namespaces.
Each command enters those namespaces before building the usual fresh bubblewrap
boundary. No model process runs in the outer namespace or receives its handles.
The host sees only an empty private mountpoint directory. Closing the handles
releases the tmpfs; package trees never pass through the workspace exporter.
"""

from __future__ import annotations

import asyncio
import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import tempfile


@dataclass
class ScratchNamespace:
    path: Path
    user_fd: int
    mount_fd: int
    data_fd: int
    byte_limit: int
    entry_limit: int

    @classmethod
    async def create(cls, root, byte_limit, entry_limit):
        path = Path(tempfile.mkdtemp(prefix="scratch-", dir=root))
        process = None
        descriptors = []
        retained = False
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                str(Path(__file__)),
                "create",
                str(path),
                str(byte_limit),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
                start_new_session=True,
            )
            async with asyncio.timeout(10):
                ready = await process.stdout.readline()
                if ready != b"ready\n":
                    diagnostic = (await process.stderr.read(4000)).decode(errors="replace")
                    raise RuntimeError("Cannot create private task scratch: " + diagnostic)
                # The trusted child stays alive on its parent pipe until these
                # descriptors pin both namespaces and their mounted filesystem.
                for suffix in ("ns/user", "ns/mnt", "root" + str(path)):
                    descriptors.append(os.open(f"/proc/{process.pid}/{suffix}", os.O_RDONLY | os.O_CLOEXEC))
                process.stdin.close()
                await process.wait()
                if process.returncode != 0:
                    raise RuntimeError("Task scratch setup did not complete")
            result = cls(path, *descriptors, byte_limit, entry_limit)
            retained = True
            return result
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            if not retained:
                for descriptor in descriptors:
                    os.close(descriptor)
                path.rmdir()

    def wrap(self, command):
        return [
            sys.executable,
            "-I",
            str(Path(__file__)),
            "exec",
            str(self.user_fd),
            str(self.mount_fd),
            *command,
        ]

    def facts(self):
        usage = os.fstatvfs(self.data_fd)
        return {
            "shared_paths": ["/packages", "/tmp"],
            "capacity_bytes": self.byte_limit,
            "used_bytes": (usage.f_blocks - usage.f_bfree) * usage.f_frsize,
            "available_bytes": usage.f_bavail * usage.f_frsize,
            "entry_limit": self.entry_limit,
        }

    def close(self):
        for name in ("data_fd", "mount_fd", "user_fd"):
            descriptor = getattr(self, name)
            if descriptor >= 0:
                os.close(descriptor)
                setattr(self, name, -1)
        try:
            self.path.rmdir()
        except FileNotFoundError:
            pass


def main():
    mode, *args = sys.argv[1:]
    if mode == "exec":
        user_fd, mount_fd = map(int, args[:2])
        os.setns(user_fd, os.CLONE_NEWUSER)
        os.setns(mount_fd, os.CLONE_NEWNS)
        # These are trusted setup capabilities, never command capabilities.
        os.close(user_fd)
        os.close(mount_fd)
        os.execv(args[2], args[2:])
    if mode != "create":
        raise ValueError("Unknown scratch setup operation")
    path, size = args
    os.umask(0o077)
    uid, gid = os.getuid(), os.getgid()
    os.unshare(os.CLONE_NEWUSER)
    Path("/proc/self/setgroups").write_text("deny")
    Path("/proc/self/uid_map").write_text(f"0 {uid} 1")
    Path("/proc/self/gid_map").write_text(f"0 {gid} 1")
    os.unshare(os.CLONE_NEWNS)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]
    libc.mount.restype = ctypes.c_int
    for source, target, kind, flags, data in (
        (None, b"/", None, 16384 | 262144, None),  # MS_REC | MS_PRIVATE
        (b"tmpfs", os.fsencode(path), b"tmpfs", 2 | 4, f"size={int(size)},mode=0700".encode()),
    ):
        if libc.mount(source, target, kind, flags, data):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
    print("ready", flush=True)
    # EOF also tears down setup if the parent goes away before pinning it.
    sys.stdin.buffer.read(1)


if __name__ == "__main__":
    main()
