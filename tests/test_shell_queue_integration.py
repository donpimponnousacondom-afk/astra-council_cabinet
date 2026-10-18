"""Admission integration with mocked commands/providers, no sandbox subprocesses."""

import asyncio
import json

import pytest

from hortator.models import OWNER_ID
from support.agentic_runtime import grant
from support.provider import install_client
from support.runtime import completion, settle
from support.runtime_feedback import ready, reply, tool


CHANNEL = "222222222222222222"


def directed(kernel, bot, message_id):
    kernel.store.ingest(
        discord_id=message_id,
        channel_id=CHANNEL,
        room_id="council",
        author_id=OWNER_ID,
        author_name="Human fixture",
        content="Update the task before starting another command.",
        addressing={
            "author_kind": "human",
            "live": True,
            "targets": [{"bot_id": bot["id"], "via": ["mention"]}],
        },
    )


def ready_shell(kernel):
    bot, transport = ready(
        kernel,
        response_protocol="progress",
        enabled_plugins=["shell"],
        cooldown_seconds=0,
        interval_seconds=0,
    )
    plugin = kernel.store.get("plugins", "shell")
    kernel.store.put("plugins", {**plugin, "enabled": True})
    directed(kernel, bot, "555555555555555561")
    return bot, transport


def receipt_outcomes(kernel):
    rows = kernel.store.rows("SELECT body FROM turn_action_receipts")
    return [item["outcome"] for row in rows for item in json.loads(row["body"])["receipts"]]


async def test_registry_runner_reads_global_capacity_without_changing_model_schema(kernel):
    context = grant(kernel, "shell")
    schema = kernel.registry.schemas(context)
    settings = kernel.store.get("settings", "global")
    settings.pop("max_concurrent_shell_jobs", None)
    kernel.store.put("settings", settings)
    assert kernel.registry.agentic.runner._shell_capacity() == 4
    settings["max_concurrent_shell_jobs"] = 2
    kernel.store.put("settings", settings)
    assert kernel.registry.agentic.runner._shell_capacity() == 2
    assert kernel.registry.schemas(context) == schema


@pytest.mark.parametrize(
    ("storage", "revocation"),
    [
        ("disposable", "shell"),
        ("workspace", "shell"),
        ("workspace", "selected_storage"),
        ("filesystem", "shell"),
        ("filesystem", "selected_storage"),
    ],
)
async def test_waiting_shell_rechecks_current_grants_before_dispatch(
    kernel, monkeypatch, storage, revocation
):
    names = ["shell"] + ([storage] if storage != "disposable" else [])
    context = grant(kernel, *names)
    dispatched = False

    async def waiting_run(context, *, before_start, **kwargs):
        nonlocal dispatched
        before_start()
        revoke = "shell" if revocation == "shell" else storage
        current = kernel.store.get("bots", context.bot["id"])
        kernel.store.put("bots", {**current, "enabled_plugins": [name for name in names if name != revoke]})
        before_start()
        dispatched = True
        return {"status": "succeeded"}

    monkeypatch.setattr(kernel.registry.agentic.runner, "run", waiting_run)
    result = await kernel.registry.call(
        "shell",
        {"operation": "run", "command": "true", **({"task": "example"} if storage == "workspace" else {})},
        context,
        "waiting-grant",
    )
    assert result["ok"] is False and result["executed"] is False
    assert "no longer enabled" in result["error"]
    assert not dispatched
    assert not kernel.registry.agentic.runner.inspect()


async def test_waiting_shell_does_not_use_registry_command_deadline(kernel, monkeypatch):
    context = grant(kernel, "shell")
    original_timeout = asyncio.timeout
    deadlines = []

    def short_timeout(seconds):
        # Accelerate any finite registry deadline. An unadmitted command must
        # survive this interval; its own timeout begins in ShellRunner instead.
        deadlines.append(seconds)
        return original_timeout(None if seconds is None else 0.005)

    async def waiting_run(context, *, before_start, **kwargs):
        before_start()
        await asyncio.sleep(0.025)
        before_start()
        return {"status": "succeeded"}

    monkeypatch.setattr("hortator.plugins.asyncio.timeout", short_timeout)
    monkeypatch.setattr(kernel.registry.agentic.runner, "run", waiting_run)
    async with original_timeout(1):
        result = await kernel.registry.call(
            "shell", {"operation": "run", "command": "true"}, context, "waiting-deadline"
        )
    assert result["status"] == "succeeded"
    assert deadlines == [None]


async def test_outer_task_deadline_still_cancels_waiting_shell(kernel, monkeypatch):
    context = grant(kernel, "shell")
    waiting = asyncio.Event()

    async def waiting_run(context, *, before_start, **kwargs):
        before_start()
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(kernel.registry.agentic.runner, "run", waiting_run)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.025):
            await kernel.registry.call(
                "shell", {"operation": "run", "command": "true"}, context, "owning-deadline"
            )
    assert waiting.is_set()
    events = kernel.store.rows("SELECT kind FROM events WHERE turn_id=?", (context.turn_id,))
    assert {row["kind"] for row in events} == {"tool.started", "tool.cancelled"}


async def test_new_human_input_stops_queued_command_without_retry_or_unknown_receipt(kernel, monkeypatch):
    bot, transport = ready_shell(kernel)
    provider_calls = []
    dispatched = False

    async def waiting_run(context, *, before_start, **kwargs):
        nonlocal dispatched
        before_start()
        directed(kernel, bot, "555555555555555562")
        before_start()
        dispatched = True
        return {"status": "succeeded"}

    def handle(request):
        provider_calls.append(json.loads(request.content))
        if len(provider_calls) == 1:
            return reply(tool("shell", {"operation": "run", "command": "true"}))
        return completion("Updated answer without launching the old command.")

    monkeypatch.setattr(kernel.registry.agentic.runner, "run", waiting_run)
    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    assert not dispatched
    assert len(provider_calls) == 1
    turn = kernel.store.one("SELECT id,status FROM turns")
    assert turn["status"] == "cancelled"
    assert receipt_outcomes(kernel) == ["refused"]
    cancelled = kernel.store.one(
        "SELECT data FROM events WHERE turn_id=? AND kind='tool.cancelled'", (turn["id"],)
    )
    assert json.loads(cancelled["data"])["result"]["executed"] is False
    assert not kernel.store.one("SELECT 1 FROM events WHERE kind='tool.failed'")
    assert not kernel.registry.agentic.runner.inspect()
    transport.send.assert_not_awaited()
    await kernel.engine.tick()
    await settle(kernel)
    assert len(provider_calls) == 2
    assert '"outcome":"refused"' in str(provider_calls[-1]["messages"])
    transport.send.assert_awaited_once()


async def test_operator_cancellation_of_queued_shell_preserves_unstarted_receipt(kernel, monkeypatch):
    bot, transport = ready_shell(kernel)
    entered = asyncio.Event()

    async def waiting_run(context, *, before_start, **kwargs):
        before_start()
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            # Runner supplies this receipt when admission has not started a job.
            exc.tool_result = {"ok": False, "executed": False, "status": "cancelled"}
            raise

    monkeypatch.setattr(kernel.registry.agentic.runner, "run", waiting_run)
    await install_client(
        kernel, lambda request: reply(tool("shell", {"operation": "run", "command": "true"}))
    )
    await kernel.engine.tick()
    await asyncio.wait_for(entered.wait(), 2)
    await kernel.engine.cancel([bot["id"]], "Operator stop while queued")
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "cancelled"
    assert receipt_outcomes(kernel) == ["refused"]
    transport.send.assert_not_awaited()


@pytest.mark.parametrize("waiting_for_lock", [False, True])
async def test_cancellation_during_cold_readiness_records_unstarted_command(
    kernel, monkeypatch, waiting_for_lock
):
    bot, transport = ready_shell(kernel)
    runner = kernel.registry.agentic.runner
    entered = asyncio.Event()
    original = runner.readiness

    async def readiness():
        entered.set()
        if waiting_for_lock:
            return await original()
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "readiness", readiness)
    if waiting_for_lock:
        await runner._readiness_lock.acquire()
    try:
        await install_client(
            kernel, lambda request: reply(tool("shell", {"operation": "run", "command": "true"}))
        )
        await kernel.engine.tick()
        await asyncio.wait_for(entered.wait(), 2)
        await kernel.engine.cancel([bot["id"]], "Stop before readiness completed")
        await settle(kernel)
        assert receipt_outcomes(kernel) == ["refused"]
        cancelled = kernel.store.one("SELECT data FROM events WHERE kind='tool.cancelled'")
        assert json.loads(cancelled["data"])["result"]["executed"] is False
        assert not runner.inspect() and not runner._waiting and not runner._admitted
        transport.send.assert_not_awaited()
    finally:
        if waiting_for_lock:
            runner._readiness_lock.release()


async def test_task_budget_expiry_keeps_queued_action_receipt_unstarted(kernel, monkeypatch):
    # A deliberately shortened stored test budget avoids a real thirty-second
    # wait while exercising the engine's actual task-deadline conversion.
    bot, transport = ready(
        kernel,
        response_protocol="progress",
        enabled_plugins=["shell", "workspace"],
        work_task_seconds=0.1,
        cooldown_seconds=0,
        interval_seconds=0,
    )
    for name in ("shell", "workspace"):
        plugin = kernel.store.get("plugins", name)
        kernel.store.put("plugins", {**plugin, "enabled": True})
    directed(kernel, bot, "555555555555555561")
    calls = []

    async def waiting_run(context, *, before_start, **kwargs):
        before_start()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            exc.tool_result = {"ok": False, "executed": False, "status": "cancelled"}
            raise

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            return reply(tool("workspace", {"operation": "start", "task": "waiting-task"}))
        return reply(tool("shell", {"operation": "run", "task": "waiting-task", "command": "true"}))

    monkeypatch.setattr(kernel.registry.agentic.runner, "run", waiting_run)
    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    turn = kernel.store.one("SELECT status,error FROM turns")
    assert turn["status"] == "failed" and "Extended task time budget exhausted" in turn["error"]
    assert receipt_outcomes(kernel) == ["returned", "refused"]
    assert len(calls) == 2
    transport.send.assert_not_awaited()


async def test_admission_wait_resumes_the_same_tool_call_without_provider_retry(kernel, monkeypatch):
    _, transport = ready_shell(kernel)
    provider_calls = []
    dispatched = []

    async def waiting_run(context, *, before_start, command, **kwargs):
        before_start()
        await asyncio.sleep(0)
        before_start()
        dispatched.append(command)
        return {"status": "succeeded", "exit_code": 0}

    def handle(request):
        provider_calls.append(json.loads(request.content))
        if len(provider_calls) == 1:
            return reply(tool("shell", {"operation": "run", "command": "true"}))
        results = [m for m in provider_calls[-1]["messages"] if m["role"] == "tool"]
        assert len(results) == 1 and json.loads(results[0]["content"])["status"] == "succeeded"
        return completion("Command finished.")

    monkeypatch.setattr(kernel.registry.agentic.runner, "run", waiting_run)
    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    assert dispatched == ["true"]
    assert len(provider_calls) == 2
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    transport.send.assert_awaited_once()
