import asyncio
import hashlib
import io
import json
import time

import pytest
from PIL import Image

from hortator.plugins import ToolContext
from hortator.store import dumps
from hortator.vision import validate_image
from test_provider import install_client
from test_runtime import completion
from test_runtime_feedback import ready, reply, responses, tool


def enable(kernel, names):
    for name in names:
        plugin = kernel.store.get("plugins", name)
        kernel.store.put("plugins", {**plugin, "enabled": True})


async def require_runner(kernel):
    state = await kernel.registry.agentic.runner.readiness()
    if not state["ready"]:
        pytest.skip("Real OS isolation unavailable: " + state.get("error", "unknown"))


async def test_real_large_image_import_isolated_compression_and_existing_discord_delivery(kernel):
    await require_runner(kernel)
    bot, transport = ready(kernel, enabled_plugins=["workspace", "shell"], max_tool_rounds=1)
    enable(kernel, ["workspace", "shell"])
    output = io.BytesIO()
    Image.new("RGB", (3072, 1024), (18, 80, 150)).save(output, format="PNG", compress_level=0)
    original = output.getvalue()
    assert 8 * 1024 * 1024 < len(original) < 20 * 1024 * 1024
    digest = hashlib.sha256(original).hexdigest()
    images = kernel.registry.workspaces.images
    images.root.mkdir(mode=0o700, exist_ok=True)
    images.path(digest).write_bytes(original)
    images.path(digest).chmod(0o600)
    attachment = {
        "id": "987654321",
        "filename": "original.png",
        "content_type": "image/png",
        "size": len(original),
        "url": "https://cdn.discordapp.com/attachments/222/987654321/original.png",
        "vision": {"status": "ready", "sha256": digest, **validate_image(original)},
    }
    kernel.store.execute("UPDATE messages SET attachments=?", (dumps([attachment]),))
    count, artifact_id = 0, None
    command = """python3 - <<'PY'
from PIL import Image
from pathlib import Path
import json
p = Path('original.png')
with Image.open(p) as image:
    image.save('compressed.jpg', quality=60, optimize=True)
    print(json.dumps({'bytes': p.stat().st_size, 'width': image.width, 'height': image.height,
                      'compressed_bytes': Path('compressed.jpg').stat().st_size}))
PY"""

    async def handle(request):
        nonlocal count, artifact_id
        count += 1
        body = json.loads(request.content)
        if count == 1:
            return reply(tool("workspace", {"operation": "start", "task": "compress-image"}))
        if count == 2:
            return reply(
                tool(
                    "workspace",
                    {
                        "operation": "import_attachment",
                        "task": "compress-image",
                        "path": "original.png",
                        "message_id": "555555555555555555",
                        "attachment_id": "987654321",
                    },
                )
            )
        if count == 3:
            report = responses(body)[-1]
            assert report["bytes"] == len(original) and report["width"] == 3072 and report["height"] == 1024
            return reply(tool("shell", {"operation": "run", "task": "compress-image", "command": command}))
        if count == 4:
            report = responses(body)[-1]
            assert report["status"] == "succeeded", report
            measurements = json.loads(report["stdout"]["text"])
            assert measurements["bytes"] == len(original) and measurements["width"] == 3072
            assert measurements["compressed_bytes"] < 8_000_000
            return reply(
                tool("workspace", {"operation": "export", "task": "compress-image", "path": "compressed.jpg"})
            )
        if count == 5:
            artifact_id = responses(body)[-1]["artifact_id"]
            return reply(
                tool(
                    "discord_attach",
                    {"artifact_ids": [artifact_id]},
                )
            )
        if count == 6:
            assert responses(body)[-1]["prepared"] is True
            assert responses(body)[-1]["posted"] is False
            return completion("Compressed a copy; original preserved.")
        raise AssertionError("Unexpected extra model call")

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await asyncio.wait_for(asyncio.gather(*kernel.engine.tasks.values()), 30)
    turn = kernel.store.one("SELECT * FROM turns")
    assert turn["status"] == "sent", turn["error"]
    transport.send.assert_awaited_once()
    paths = transport.send.await_args.args[4]
    assert len(paths) == 1 and paths[0].stat().st_size < 8_000_000
    with Image.open(paths[0]) as compressed:
        assert compressed.size == (3072, 1024)
    assert (
        kernel.registry.workspaces.stat_file(
            bot["id"], "222222222222222222", "compress-image", "original.png"
        )["sha256"]
        == digest
    )
    assert (
        kernel.registry.resolve_artifact(artifact_id, ToolContext(bot, "222222222222222222", turn["id"]))
        == paths[0]
    )
    assert kernel.store.one("SELECT status FROM outbox")["status"] == "sent"


async def test_revoking_shell_grant_cancels_real_job_before_any_delivery(kernel, owner):
    await require_runner(kernel)
    bot, transport = ready(kernel, enabled_plugins=["workspace", "shell"])
    enable(kernel, ["workspace", "shell"])
    await kernel.registry.workspaces.call(
        {"operation": "start", "task": "cancel-test"}, ToolContext(bot, "222222222222222222", "setup")
    )

    async def handle(request):
        return reply(
            tool(
                "shell",
                {
                    "operation": "run",
                    "task": "cancel-test",
                    "command": "sleep 50 & wait; printf late > late.txt",
                },
            )
        )

    await install_client(kernel, handle)
    await kernel.engine.tick()
    runner = kernel.registry.agentic.runner
    async with asyncio.timeout(15):
        while not runner.active or not next(iter(runner.active.values())).process:
            await asyncio.sleep(0.01)
    started = time.monotonic()
    await kernel.service.save(owner, "bots", "ada", {"enabled_plugins": ["workspace"]})
    assert time.monotonic() - started < 5
    assert not runner.active
    assert runner.inspect()[0]["status"] == "cancelled"
    assert not runner.inspect()[0]["workspace_committed"]
    assert kernel.store.one("SELECT status FROM turns")["status"] == "cancelled"
    transport.send.assert_not_awaited()
    assert not kernel.registry.allowed("shell", ToolContext(bot, "222222222222222222", "stale"))
