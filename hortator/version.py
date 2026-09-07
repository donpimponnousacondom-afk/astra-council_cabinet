"""Capture source identity once; never mistake a later checkout for loaded code."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from . import __version__


def iso_now():
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def source_info(root=None):
    root = Path(root or Path(__file__).resolve().parent.parent).resolve()
    unknown = dict(
        commit=None, short_commit=None, commit_title=None, committed_at=None, branch=None, dirty=None
    )

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=True, timeout=3
        ).stdout.strip()

    try:
        # Do not accidentally describe an unrelated enclosing repository for an installed package.
        if Path(git("rev-parse", "--show-toplevel")).resolve() == root:
            commit, committed_at, title = git("show", "-s", "--format=%H%n%cI%n%s", "HEAD").split("\n", 2)
            return {
                "commit": commit,
                "short_commit": commit[:12],
                "commit_title": title,
                "committed_at": datetime.fromisoformat(committed_at)
                .astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
                "branch": git("branch", "--show-current") or None,
                "dirty": bool(git("status", "--porcelain", "--untracked-files=normal")),
                "provenance": "git",
            }
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    # Container/release builds can carry the same explicit source stamp as their dashboard bundle.
    try:
        raw = os.getenv("HORTATOR_BUILD_INFO") or (root / "hortator" / "_build_info.json").read_text()
        value = json.loads(raw)
        commit = value["commit"]
        if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", commit):
            raise ValueError("Invalid commit")
        title, committed_at = value["commit_title"], value["committed_at"]
        if not isinstance(title, str) or not title or type(value["dirty"]) is not bool:
            raise ValueError("Incomplete source metadata")
        timestamp = datetime.fromisoformat(committed_at)
        if timestamp.tzinfo is None:
            raise ValueError("Commit date needs a timezone")
        branch = value.get("branch")
        return {
            "commit": commit,
            "short_commit": commit[:12],
            "commit_title": title,
            "committed_at": timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "branch": branch if isinstance(branch, str) else None,
            "dirty": value["dirty"],
            "provenance": "build",
        }
    except (OSError, ValueError, KeyError, TypeError):
        return {**unknown, "provenance": "unknown"}


def runtime_version():
    return {"package_version": __version__, **source_info(), "started_at": iso_now()}


def version_text(version):
    lines = ["Hortator · running server"]
    if version["commit"]:
        lines.extend(
            [
                f"Commit: {version['short_commit']}",
                f"Date:   {version['committed_at']}",
                f"Title:  {version['commit_title']}",
                f"Branch: {version['branch'] or 'detached HEAD'}",
                "Source: " + ("uncommitted changes at startup" if version["dirty"] else "clean at startup"),
            ]
        )
    else:
        lines.append("Commit: unavailable (source metadata was not supplied)")
    lines.extend([f"Started: {version['started_at']}", f"Package: {version['package_version']}"])
    return "\n".join(lines)


if __name__ == "__main__":
    # Non-secret deployment stamp, suitable for a Docker build argument.
    print(json.dumps(source_info()))
