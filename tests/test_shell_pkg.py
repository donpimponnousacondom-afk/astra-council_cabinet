"""Exercise the actual pkg wrapper without contacting a package repository."""

import os
from pathlib import Path
import subprocess

import pytest


PKG = Path(__file__).parents[1] / "hortator" / "shell_assets" / "pkg"


@pytest.mark.parametrize(
    "arguments,packages",
    [
        (["nodejs"], ["nodejs"]),
        (["-y", "nodejs"], ["nodejs"]),
        (["nodejs", "-y"], ["nodejs"]),
        (["--yes", "nodejs", "zstd", "-y"], ["nodejs", "zstd"]),
        (["nodejs>=22,<27"], ["nodejs>=22,<27"]),
        # Other arguments must remain package operands after the fixed separator.
        (["--prefix", "/tmp/elsewhere"], ["--prefix", "/tmp/elsewhere"]),
    ],
)
def test_pkg_confirmation_flags_preserve_fixed_installer_arguments(tmp_path, arguments, packages):
    executable = tmp_path / "micromamba"
    executable.write_text('#!/bin/bash\nprintf "%s\\0" "$@"\n')
    executable.chmod(0o755)
    result = subprocess.run(
        ["bash", str(PKG), "install", *arguments],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True,
        check=True,
    )
    argv = result.stdout.decode().split("\0")[:-1]
    separator = argv.index("--")
    assert argv[separator + 1 :] == packages
    assert argv[:separator] == [
        "--no-rc",
        "create",
        "--yes",
        "--prefix",
        "/packages/native",
        "--root-prefix",
        "/packages/mamba",
        "--override-channels",
        "--channel",
        "conda-forge",
        "--ssl-verify",
        "/etc/ssl/certs/ca-certificates.crt",
    ]


@pytest.mark.parametrize("arguments", [[], ["install"], ["install", "-y", "--yes"]])
def test_pkg_requires_a_package(arguments):
    result = subprocess.run(["bash", str(PKG), *arguments], capture_output=True, text=True)
    assert result.returncode == 2
    assert "Usage: pkg install PACKAGE" in result.stderr
