import asyncio
import json
import errno
from unittest.mock import AsyncMock

import httpx
import pytest

from conftest import configured, ingest
from test_provider import install_client, call_args
from test_runtime import completion, settle
from test_slash_commands import enable, interaction
from hortator.models import Provider
from hortator.provider import ProviderError


def policy(kernel, retries=3, delay=0):
    provider = kernel.store.get("providers", "openrouter")
    provider.update(retry_count=retries, retry_delay_seconds=delay)
    kernel.store.put("providers", provider)


def test_new_provider_defaults_and_strict_bounds():
    p = Provider(id="test", name="Test")
    assert (p.retry_count, p.retry_delay_seconds) == (3, 10)
    for value in (-1, 11, 1.5, True):
        with pytest.raises(ValueError):
            Provider(id="test", name="Test", retry_count=value)


@pytest.mark.parametrize("failure", ["connect", "503", "429", "stream"])
async def test_transient_retry_keeps_payload_and_ledger(kernel, failure):
    bot = configured(kernel)
    policy(kernel)
    requests = []

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"reasoning_content":"private partial"}}]}\n\n'
            raise httpx.ReadError("fixture disconnected")

    def handler(request):
        requests.append(request.content)
        if len(requests) == 1:
            if failure == "connect":
                try:
                    raise OSError(errno.ECONNREFUSED, "fixture refused")
                except OSError as error:
                    raise httpx.ConnectError("All connection attempts failed") from error
            if failure == "stream":
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=BrokenStream()
                )
            return httpx.Response(int(failure), json={"error": "temporary fixture"})
        return completion("Recovered")

    await install_client(kernel, handler)
    result = await kernel.pool.complete(**call_args(kernel, bot))
    assert result.content == "Recovered"
    assert len(requests) == 2 and requests[0] == requests[1]
    rows = kernel.store.rows("SELECT id,status,context FROM requests ORDER BY started_at")
    assert [r["status"] for r in rows] == ["failed", "completed"]
    contexts = [json.loads(r["context"]) for r in rows]
    assert contexts[0]["retry_group_id"] == contexts[1]["retry_group_id"]
    assert contexts[1]["previous_request_id"] == rows[0]["id"]
    assert [c["attempt"] for c in contexts] == [1, 2]
    events = kernel.store.events()
    assert any(e["kind"] == "request.retry_scheduled" and e["level"] == "warning" for e in events)
    assert not any(e["kind"] == "provider.failure" for e in events)
    assert kernel.store.health("openrouter")["consecutive_failures"] == 0
    if failure in {"connect", "stream"}:
        raw = kernel.store.one("SELECT body FROM request_diagnostics WHERE request_id=?", (rows[0]["id"],))[
            "body"
        ]
        assert (
            ("ECONNREFUSED" in raw or "fixture refused" in raw)
            if failure == "connect"
            else "private partial" in raw
        )


@pytest.mark.parametrize("status,expected_attempts", [(503, 4), (401, 1), (400, 1), (413, 1)])
async def test_exhaustion_and_permanent_errors(kernel, status, expected_attempts):
    bot = configured(kernel)
    policy(kernel)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": "fixture"})

    await install_client(kernel, handler)
    with pytest.raises(ProviderError):
        await kernel.pool.complete(**call_args(kernel, bot))
    assert len(requests) == expected_attempts
    assert (
        len(kernel.store.rows("SELECT * FROM events WHERE kind='request.retry_scheduled'"))
        == expected_attempts - 1
    )
    failures = kernel.store.rows("SELECT * FROM events WHERE kind='provider.failure'")
    assert len(failures) == (1 if status in (503, 401) else 0)
    assert (
        kernel.store.rows("SELECT level FROM events WHERE kind='request.failed' ORDER BY seq")[-1]["level"]
        == "error"
    )


async def test_cancel_retry_wait_releases_slot_without_another_request(kernel):
    bot = configured(kernel)
    policy(kernel, delay=10)
    await install_client(kernel, lambda _: httpx.Response(503, json={"error": "fixture"}))
    async with asyncio.TaskGroup() as group:
        task = group.create_task(kernel.pool.complete(**call_args(kernel, bot)))
        while not kernel.store.rows("SELECT seq FROM events WHERE kind='request.retry_scheduled'"):
            await asyncio.sleep(0.001)
        task.cancel()
    assert len(kernel.store.rows("SELECT * FROM requests")) == 1
    assert kernel.pool.active["openrouter"] == 0
    assert kernel.store.rows("SELECT seq FROM events WHERE kind='request.retry_cancelled'")


@pytest.mark.parametrize("slash", [False, True])
async def test_tool_work_is_not_repeated_by_request_retry(kernel, slash):
    bot = enable(kernel) if slash else configured(kernel, enabled_plugins=["memory"])
    policy(kernel)
    kernel.engine.transport = AsyncMock()
    kernel.engine.transport.send.return_value = "999999999999999999"
    ingest(kernel)
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "note1",
                                        "type": "function",
                                        "function": {
                                            "name": "memory",
                                            "arguments": json.dumps(
                                                {
                                                    "operation": "write",
                                                    "key": "kept",
                                                    "value": "completed work",
                                                }
                                            ),
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10},
                },
            )
        if len(seen) == 2:
            raise httpx.ConnectError("fixture")
        return completion("Task finished")

    await install_client(kernel, handler)
    if slash:
        item = interaction(bot)
        await kernel.connector.slash.receive(bot["id"], item)
    else:
        await kernel.engine.tick()
    await settle(kernel)
    assert len(seen) == 3 and seen[1] == seen[2]
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    assert (
        len(
            kernel.store.rows(
                "SELECT seq FROM events WHERE kind='tool.started' AND json_extract(data,'$.name')='memory'"
            )
        )
        == 1
    )
    assert len(kernel.store.rows("SELECT * FROM outbox")) == 1


def test_transport_evidence_preserves_grouped_socket_causes():
    from hortator.provider import exception_causes

    group = ExceptionGroup(
        "connect attempts",
        [OSError(errno.ECONNREFUSED, "IPv4 refused"), OSError(errno.ENETUNREACH, "IPv6 unreachable")],
    )
    error = httpx.ConnectError("All connection attempts failed")
    error.__cause__ = group
    values = exception_causes(error)
    assert {item.get("errno") for item in values} >= {errno.ECONNREFUSED, errno.ENETUNREACH}


async def test_compaction_retries_same_context_without_reintroducing_output_caps(kernel):
    bot = configured(kernel)
    policy(kernel)
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return (
            httpx.Response(503, json={"error": "busy"}) if len(seen) == 1 else completion("Complete summary")
        )

    await install_client(kernel, handler)
    args = call_args(kernel, bot)
    args["profile"]["request_json"] = {"max_tokens": 20, "reasoning_effort": "high"}
    result = await kernel.pool.complete(**args, purpose="compaction")
    assert result.content == "Complete summary"
    assert seen[0] == seen[1] and "max_tokens" not in seen[1]
    assert seen[1]["reasoning_effort"] == "high"
    assert all(row["purpose"] == "compaction" for row in kernel.store.rows("SELECT purpose FROM requests"))


async def test_slash_deadline_cancels_retry_wait(kernel, monkeypatch):
    bot = enable(kernel)
    policy(kernel, delay=10)
    monkeypatch.setattr("hortator.slash_commands.MAX_SECONDS", 0.1)
    await install_client(kernel, lambda _: httpx.Response(503, json={"error": "busy"}))
    item = interaction(bot)
    await kernel.connector.slash.receive(bot["id"], item)
    await settle(kernel)
    assert len(kernel.store.rows("SELECT id FROM requests")) == 1
    assert kernel.store.rows("SELECT seq FROM events WHERE kind='request.retry_cancelled'")
    assert kernel.store.one("SELECT status FROM turns")["status"] == "failed"
    assert kernel.pool.active["openrouter"] == 0
