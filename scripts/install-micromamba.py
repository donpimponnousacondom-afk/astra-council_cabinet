#!/usr/bin/env python3.14
"""Operator-only, pinned bootstrap. Never downloads during model execution/readiness."""

import argparse
import hashlib
import os
from pathlib import Path
import platform
import tempfile
import urllib.request

VERSION = "2.9.0-0"
RELEASES = {
    "x86_64": ("linux-64", "366cd9cd8be14df1ab8ed50352a82111082a36686b2d389fdb79a92c3fafb3e3"),
    "aarch64": ("linux-aarch64", "9f93b974adcb4d166996af969b6cd371287d1a3e52733704727884d9b74cb7a7"),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() not in RELEASES:
        parser.error("This bootstrap supports Linux x86_64 and aarch64")
    architecture, digest = RELEASES[platform.machine()]
    args.bin_dir.mkdir(parents=True, exist_ok=True)
    destination = args.bin_dir / "micromamba"
    if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == digest:
        print(f"micromamba {VERSION} already verified at {destination}")
        return
    url = f"https://github.com/mamba-org/micromamba-releases/releases/download/{VERSION}/micromamba-{architecture}"
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != digest:
        raise SystemExit("micromamba download failed its pinned SHA-256/size check")
    descriptor, name = tempfile.mkstemp(prefix=".micromamba-", dir=args.bin_dir)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            os.fchmod(output.fileno(), 0o755)
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)
    print(f"Installed verified micromamba {VERSION} at {destination}")


if __name__ == "__main__":
    main()
