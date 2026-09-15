"""Synthetic private tool data for isolated browser/API verification only."""

import json
import time

from hortator.plugins import ToolContext


async def seed_agentic(kernel):
    if not str(kernel.directory).startswith(("/tmp/", "/private/tmp/")):
        raise ValueError("Agentic verification fixtures require a temporary data directory")
    context = ToolContext(kernel.store.get("bots", "ada"), "222222222222222222", "fixture-tool-data")
    await kernel.registry.workspaces.call({"operation": "start", "task": "browser-files"}, context)
    await kernel.registry.workspaces.call(
        {
            "operation": "write",
            "task": "browser-files",
            "path": "notes.txt",
            "content": "PRIVATE WORKSPACE NOTES\n" + "Unicode café 🙂\n" * 1800,
        },
        context,
    )
    await kernel.registry.workspaces.call(
        {
            "operation": "write",
            "task": "browser-files",
            "path": "pixel.png",
            "encoding": "base64",
            "content": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jX1kAAAAASUVORK5CYII=",
        },
        context,
    )
    original = kernel.registry.fetched_documents.fetcher

    async def fetcher(url, limit):
        return (
            ("PUBLIC FIXTURE SNAPSHOT\n" + "Unicode café 🙂 line\n" * 1800).encode(),
            "text/plain; charset=utf-8",
            url,
        )

    kernel.registry.fetched_documents.fetcher = fetcher
    try:
        await kernel.registry.fetched_documents.call(
            {"url": "https://example.com/browser-reading"}, context, {}, ""
        )
    finally:
        kernel.registry.fetched_documents.fetcher = original
    # This completed record is a UI fixture, not evidence of shell execution.
    job_id = "job_" + "b" * 32
    directory = kernel.directory / "jobs" / job_id
    directory.mkdir(mode=0o700, exist_ok=True)
    stdout = ("SYNTHETIC JOB OUTPUT\n" + "Unicode café 🙂 output\n" * 1000).encode()
    stderr = b"Synthetic diagnostic line\n"
    metadata = {
        "job_id": job_id,
        "bot_id": context.bot["id"],
        "channel_id": context.channel_id,
        "turn_id": context.turn_id,
        "task": "browser-files",
        "status": "succeeded",
        "started_at": time.time(),
        "finished_at": time.time(),
        "exit_code": 0,
        "workspace_committed": True,
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
    }
    for name, data in (
        ("meta.json", json.dumps(metadata).encode()),
        ("stdout.log", stdout),
        ("stderr.log", stderr),
    ):
        path = directory / name
        path.write_bytes(data)
        path.chmod(0o600)
