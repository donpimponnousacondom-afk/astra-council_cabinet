"""Failures explain ownership, effect and inspection without exposing private payloads."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from conftest import configured, ingest
from hortator.discord_gateway import CouncilClient
from hortator.provider import ProviderError
from hortator.shell_runner import ShellRunner
from test_console import output
from test_provider import call_args, install_client
from test_provider_streaming import packet, stored, wire
from test_typing import connect_channel, prepare_bot


@pytest.mark.parametrize("mode", ["headers", "json", "sse"])
async def test_local_deadline_records_phase_preserves_health_and_partial_private_evidence(kernel, mode):
    bot = configured(kernel)
    provider = kernel.store.get("providers", "openrouter")
    provider["timeout_seconds"] = 0.03
    kernel.store.put("providers", provider)
    kernel.store.execute("UPDATE provider_health SET consecutive_failures=2 WHERE provider_id='openrouter'")
    before = kernel.store.health("openrouter")

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            if mode == "sse":
                yield wire(
                    packet({"reasoning_content": "private deliberation", "content": "unfinished"}), done=False
                )
            else:
                yield b'{"choices": ['
            await asyncio.Future()

    async def respond(request):
        if mode == "headers":
            await asyncio.Future()
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream" if mode == "sse" else "application/json"},
            stream=Stream(),
        )

    await install_client(kernel, respond)
    with pytest.raises(ProviderError, match="Local total request deadline exceeded") as caught:
        await kernel.pool.complete(**call_args(kernel, bot), purpose="compaction")
    row, diagnostics = stored(kernel)
    assert caught.value.request_id == row["id"]
    assert kernel.store.health("openrouter") == before
    assert diagnostics["provider_error"]["origin"] == "local_deadline"
    event = next(e for e in kernel.store.events() if e["kind"] == "request.failed")
    assert event["data"]["purpose"] == "compaction"
    assert event["data"]["phase"] == ("await_response_headers" if mode == "headers" else f"read_{mode}")
    assert event["data"]["duration_ms"] >= 25
    assert not any(e["kind"] in {"provider.failure", "provider.circuit_open"} for e in kernel.store.events())
    if mode == "sse":
        assert diagnostics["reasoning_content"] == "private deliberation"
        assert json.loads(row["response"])["content"] == "unfinished"
        assert "private deliberation" not in str(kernel.store.events())


@pytest.mark.parametrize(
    "kind,phase,fault",
    [
        (httpx.ConnectTimeout, "connect", True),
        (httpx.ReadTimeout, "read", True),
        (httpx.WriteTimeout, "write", True),
        (httpx.PoolTimeout, "pool", False),
    ],
)
async def test_http_timeout_phase_and_local_pool_are_distinct(kernel, kind, phase, fault):
    bot = configured(kernel)

    def respond(request):
        raise kind("", request=request)

    await install_client(kernel, respond)
    with pytest.raises(ProviderError, match=f"{phase} timeout") as caught:
        await kernel.pool.complete(**call_args(kernel, bot))
    assert caught.value.provider_fault is fault
    row, diagnostics = stored(kernel)
    assert diagnostics["provider_error"]["details"]["timeout_kind"] == phase
    assert diagnostics["provider_error"]["origin"] == ("transport" if fault else "local_client")
    assert kernel.store.health("openrouter")["consecutive_failures"] == int(fault)
    failures = [e for e in kernel.store.events() if e["kind"] == "provider.failure"]
    assert len(failures) == int(fault)
    if failures:
        assert failures[0]["request_id"] == row["id"] and failures[0]["bot_id"] == bot["id"]


async def test_provider_cancellation_still_propagates_without_becoming_deadline_failure(kernel):
    bot = configured(kernel)
    started = asyncio.Event()

    async def respond(request):
        started.set()
        await asyncio.Future()

    await install_client(kernel, respond)
    async with asyncio.TaskGroup() as group:
        task = group.create_task(kernel.pool.complete(**call_args(kernel, bot)))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    row, diagnostics = stored(kernel)
    assert row["status"] == "cancelled" and diagnostics["provider_error"]["origin"] == "cancelled"
    assert kernel.store.health("openrouter")["consecutive_failures"] == 0


@pytest.mark.parametrize("cancelled", [False, True])
async def test_compaction_failure_preserves_context_and_identifies_interruption(
    kernel, monkeypatch, cancelled
):
    bot = configured(kernel)
    ingest(kernel)
    channel = "222222222222222222"
    old = kernel.store.context(bot["id"], channel)
    error = asyncio.CancelledError() if cancelled else ProviderError("Local deadline", provider_fault=False)
    error.request_id = "req-observability"
    monkeypatch.setattr(kernel.pool, "complete", AsyncMock(side_effect=error))
    with pytest.raises(type(error)):
        await kernel.engine.contexts.prepare(
            bot, kernel.store.get("profiles", "balanced"), channel, "turn-observability", [], force=True
        )
    event = next(
        e
        for e in kernel.store.events()
        if e["kind"] == f"compaction.{'cancelled' if cancelled else 'failed'}"
    )
    assert event["level"] == ("warning" if cancelled else "error")
    assert event["request_id"] == error.request_id
    assert event["data"]["previous_context_retained"] is True
    assert event["data"]["channel_id"] == channel and event["data"]["error"]
    current = kernel.store.context(bot["id"], channel)
    assert (current["summary"], current["checkpoint"]) == (old["summary"], old["checkpoint"])


async def test_callback_exception_and_message_identity_are_recorded_and_redacted(kernel):
    secret = "callback-private-credential"
    kernel.vault.put("plugin/fixture/api_key", secret)
    client = SimpleNamespace(manager=kernel.connector, bot_id="ada")
    try:
        raise RuntimeError(f"callback fixture {secret}")
    except RuntimeError:
        await CouncilClient.on_error(client, "on_raw_message_edit")
    callback = next(e for e in kernel.store.events() if e["kind"] == "discord.handler_error")
    assert callback["data"]["handler"] == "on_raw_message_edit"
    assert "RuntimeError" in callback["data"]["error"] and callback["data"]["traceback"]
    kernel.connector.receive = AsyncMock(side_effect=TimeoutError())
    await CouncilClient.on_message(client, SimpleNamespace(id=456, channel=SimpleNamespace(id=123)))
    event = next(e for e in kernel.store.events() if e["kind"] == "discord.receive_failed")
    assert event["data"]["channel_id"] == "123" and event["data"]["message_id"] == "456"
    assert "Operation timed out" in event["data"]["error"]
    with output(details=True) as console:
        console.bind(kernel)
        console.key("e")
        assert secret not in console.stream.getvalue()
    assert secret not in str(kernel.store.events())


async def test_typing_deadline_has_explanation_and_continues_retrying(kernel, monkeypatch):
    bot, channel = prepare_bot(kernel)
    monkeypatch.setattr("hortator.discord_gateway.TYPING_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("hortator.discord_gateway.TYPING_INTERVAL_SECONDS", 0.001)
    retried = asyncio.Event()
    calls = 0

    async def pulse():
        nonlocal calls
        calls += 1
        if calls == 2:
            retried.set()
        await asyncio.Future()

    connect_channel(kernel, bot, channel, pulse)
    async with asyncio.TaskGroup() as group:
        task = group.create_task(kernel.connector.typing(bot, channel, turn_id="typing-warning"))
        await asyncio.wait_for(retried.wait(), 2)
        task.cancel()
    events = [e for e in kernel.store.events() if e["kind"] == "discord.typing_failed"]
    assert len(events) == 1
    assert "local 0.01-second deadline" in events[0]["data"]["error"]
    assert "turn continues" in events[0]["data"]["reason"]


async def test_folded_incidents_show_hidden_causes_and_separate_channel_failures(kernel):
    with output() as console:
        console.bind(kernel)
        console.stream.seek(0)
        console.stream.truncate()
        for channel in ("room-one", "room-two"):
            kernel.store.emit(
                "discord.history_failed",
                {
                    "error": "Access denied",
                    "channel_id": channel,
                    "stage": "fetch_history",
                },
                bot_id="ada",
                level="warning",
            )
        kernel.store.emit(
            "workspace.cleanup_pending",
            {
                "notice": "Commit succeeded; cleanup will retry",
                "task": "example",
            },
            level="warning",
        )
        runner = object.__new__(ShellRunner)
        runner.workspaces = SimpleNamespace(store=kernel.store)
        runner._event(
            {
                "job_id": "job-fixture",
                "bot_id": "ada",
                "turn_id": "turn-fixture",
                "status": "failed",
                "error": "PermissionError: Cannot commit workspace",
                "exit_code": 0,
                "workspace_committed": False,
            },
            "job.failed",
        )
        text = console.stream.getvalue()
        assert text.count("Access denied") == 2
        assert "room-one" in text and "room-two" in text and "fetch_history" in text
        assert "Commit succeeded; cleanup will retry" in text
        assert "Cannot commit workspace" in text and "runner_or_validation_failure" in text
