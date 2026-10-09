"""Independent shell grants, real disposable commands and historical access checks."""

import asyncio
import json
import os

import pytest
from aiohttp import web
from jsonschema import Draft202012Validator

from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.shell_runner import ShellRunner
from hortator.working_set import ToolEvidence
from hortator.tool_feedback import full_schema
from support.agentic_runtime import grant


async def require_runner(kernel):
    runner = kernel.registry.agentic.runner
    ready = await runner.readiness()
    if not ready["ready"]:
        if os.environ.get("HORTATOR_REQUIRE_SANDBOX") == "1":
            pytest.fail(ready["error"])
        pytest.skip(ready["error"])
    return runner


async def test_shell_discovery_needs_no_workspace_and_task_is_optional_but_validated(kernel):
    context = grant(kernel, "shell")
    help_result = await kernel.registry.call("shell", {}, context, "help")
    assert help_result["usage_only"] and not help_result["executed"]
    example = help_result["usage"]["example"]
    assert example["operation"] == "run" and "task" not in example
    validator = Draft202012Validator(full_schema(kernel.registry.spec_for("shell", context).parameters))
    assert validator.is_valid({"operation": "run", "command": "curl --version"})
    for task in (None, "", "../other"):
        assert not validator.is_valid({"operation": "run", "command": "true", "task": task})
    assert not kernel.registry.agentic.runner.inspect()
    workspace_only = grant(kernel, "workspace")
    assert not kernel.registry.allowed("shell", workspace_only)


async def test_shell_only_posts_with_curl_and_inline_python_without_persistent_files(kernel):
    context = grant(kernel, "shell")
    runner = await require_runner(kernel)
    assert kernel.registry.allowed("shell", context)
    assert not kernel.registry.allowed("workspace", context)
    assert not kernel.store.get("plugins", "workspace")["enabled"]
    received = []

    async def post(request):
        received.append(await request.text())
        return web.Response(text="accepted")

    app = web.Application()
    app.router.add_post("/", post)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        command = (
            "mkdir scratch; cd scratch; printf transient > file.txt\n"
            f"curl --fail --silent --show-error -X POST --data curl http://127.0.0.1:{port}/\n"
            "python3 - <<'PY'\n"
            "import http.client\n"
            f"c = http.client.HTTPConnection('127.0.0.1', {port})\n"
            "c.request('POST', '/', 'python')\n"
            "print(c.getresponse().read().decode())\n"
            "PY"
        )
        result = await kernel.registry.call(
            "shell", {"operation": "run", "command": command}, context, "disposable"
        )
    finally:
        await server.cleanup()
    assert result["status"] == "succeeded", result
    assert result["storage"] == "disposable" and result["workspace_id"] is None
    assert not result["workspace_committed"]
    assert received == ["curl", "python"]
    assert "acceptedaccepted" in result["stdout"]["text"]
    assert not kernel.store.rows("SELECT * FROM workspaces")
    assert not kernel.store.rows("SELECT * FROM workspace_turn_refs")
    assert not list(runner.root.glob("job_*/.execution"))
    assert {p.name for p in (runner.root / result["job_id"]).iterdir()} == {
        "meta.json",
        "stdout.log",
        "stderr.log",
    }
    next_job = await runner.run(context, command="pwd; test ! -e scratch && echo clean")
    assert next_job["stdout"]["text"] == "/workspace\nclean\n"
    for operation in ("status", "read", "cancel"):
        value = await kernel.registry.call(
            "shell", {"operation": operation, "job_id": result["job_id"]}, context, operation
        )
        assert value["storage"] == "disposable"
    page = await kernel.registry.call(
        "shell", {"operation": "read_result", "result_id": result["result_id"]}, context, "page"
    )
    assert "acceptedaccepted" in page["text"]
    for foreign in (
        ToolContext({**context.bot, "id": "other"}, context.channel_id, context.turn_id),
        ToolContext(context.bot, "other-channel", context.turn_id),
        ToolContext(context.bot, context.channel_id, "other-turn"),
    ):
        denied = await kernel.registry.call(
            "shell", {"operation": "read", "job_id": result["job_id"]}, foreign, "foreign"
        )
        assert denied["ok"] is False


async def test_explicit_task_never_falls_back_and_persistent_evidence_keeps_grants(kernel):
    context = grant(kernel, "shell", "workspace", "web_fetch")
    runner = await require_runner(kernel)
    missing = await kernel.registry.call(
        "shell", {"operation": "run", "task": "notes", "command": "echo wrong"}, context, "missing"
    )
    assert missing["ok"] is False and not runner.inspect()
    await kernel.registry.call("workspace", {"operation": "start", "task": "notes"}, context, "start")
    persistent = await kernel.registry.call(
        "shell",
        {"operation": "run", "task": "notes", "command": "echo kept > note; cat note"},
        context,
        "kept",
    )
    assert persistent["workspace_committed"] and persistent["storage"] == "workspace"
    follow = await runner.run(context, task="notes", command="cat note")
    assert follow["stdout"]["text"] == "kept\n"
    disposable = await kernel.registry.call(
        "shell", {"operation": "run", "command": "test ! -e note && echo empty"}, context, "empty"
    )
    assert disposable["stdout"]["text"] == "empty\n"
    receipts = [persistent["result_id"]]
    for operation in ("read", "status", "cancel"):
        result = await kernel.registry.call(
            "shell", {"operation": operation, "job_id": persistent["job_id"]}, context, operation
        )
        receipts.append(result["result_id"])
    page = await kernel.registry.call(
        "web_fetch", {"operation": "read_result", "result_id": persistent["result_id"]}, context, "page"
    )
    receipts.append(page["result_id"])
    current = kernel.store.get("bots", context.bot["id"])
    kernel.store.put("bots", {**current, "enabled_plugins": ["shell", "web_fetch"]})
    for operation in ("run", "read", "status", "cancel"):
        args = (
            {"task": "notes", "command": "echo wrong"}
            if operation == "run"
            else {"job_id": persistent["job_id"]}
        )
        denied = await kernel.registry.call("shell", {"operation": operation, **args}, context, "denied")
        assert denied["ok"] is False and "workspace" in denied["error"]
    for receipt in receipts:
        denied = await kernel.registry.call(
            "web_fetch", {"operation": "read_result", "result_id": receipt}, context, "denied-page"
        )
        assert denied["ok"] is False and "grant" in denied["error"]
    allowed = await kernel.registry.call(
        "shell", {"operation": "read_result", "result_id": disposable["result_id"]}, context, "allowed"
    )
    assert "empty" in allowed["text"]
    # Globally disabled workspace also leaves disposable shell available.
    plugin = kernel.store.get("plugins", "workspace")
    kernel.store.put("plugins", {**plugin, "enabled": False})
    assert kernel.registry.allowed("shell", context)
    kernel.store.put(
        "bots", {**kernel.store.get("bots", context.bot["id"]), "enabled_plugins": ["web_fetch"]}
    )
    denied = await kernel.registry.call(
        "web_fetch", {"operation": "read_result", "result_id": disposable["result_id"]}, context, "no-shell"
    )
    assert denied["ok"] is False and "grant" in denied["error"]


async def test_legacy_shell_and_transitive_pages_preserve_workspace_requirement(kernel):
    context = grant(kernel, "shell", "workspace", "web_fetch")
    evidence = kernel.registry.evidence
    shell = evidence.record(context, "shell", "old", {"text": "old private files"})
    page = evidence.record(
        context, "web_fetch", "old-page", {"text": "old private files"}, source_result_id=shell["result_id"]
    )
    # Simulate an existing database from before independent shell grants.
    kernel.store.execute("ALTER TABLE tool_result_evidence DROP COLUMN grant_version")
    evidence = ToolEvidence(kernel.store, kernel.vault)
    current_page = evidence.record(
        context, "web_fetch", "new-page", {"text": "old private files"}, source_result_id=page["result_id"]
    )
    bot = kernel.store.get("bots", context.bot["id"])
    kernel.store.put("bots", {**bot, "enabled_plugins": ["shell", "web_fetch"]})
    for result in (shell, page, current_page):
        with pytest.raises(ControlError, match="grant"):
            evidence.read({"result_id": result["result_id"]}, context, kernel.registry.allowed)


@pytest.mark.parametrize("ending", ["timeout", "cancel", "failure"])
async def test_disposable_cleanup_on_unsuccessful_run(kernel, ending):
    context = grant(kernel, "shell")
    runner = await require_runner(kernel)
    if ending == "cancel":
        async with asyncio.TaskGroup() as group:
            task = group.create_task(runner.run(context, command="echo temp > file; echo ready; sleep 30"))
            async with asyncio.timeout(10):
                while not runner.active or not next(iter(runner.active.values())).process:
                    await asyncio.sleep(0.01)
            task.cancel()
        result = runner.inspect()[0]
        assert result["status"] == "cancelled"
    else:
        result = await runner.run(
            context,
            command="echo temp > file; " + ("sleep 30" if ending == "timeout" else "exit 7"),
            timeout_seconds=0.2 if ending == "timeout" else None,
        )
        assert result["status"] == ("timed_out" if ending == "timeout" else "failed")
    assert not runner.active and not runner.workspaces._active
    assert not list(runner.root.glob("job_*/.execution"))
    assert not kernel.store.rows("SELECT * FROM workspaces")
    # A restart also removes a disposable job's interrupted execution directory.
    directory = runner.root / result["job_id"]
    execution = directory / ".execution"
    execution.mkdir()
    (execution / "orphan").write_text("interrupted")
    metadata = json.loads((directory / "meta.json").read_text())
    metadata["status"] = "running"
    (directory / "meta.json").write_text(json.dumps(metadata))
    restarted = ShellRunner(runner.root, runner.workspaces)
    assert not execution.exists()
    assert restarted.status(context, result["job_id"])["status"] == "interrupted"
    await restarted.close()
