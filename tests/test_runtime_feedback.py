import asyncio
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from conftest import configured, ingest
from test_provider import install_client
from test_runtime import settle


def tool(name, arguments, call_id="call"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
        },
    }


def reply(*calls):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"tool_calls": list(calls)},
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        },
    )


def responses(body):
    return [json.loads(message["content"]) for message in body["messages"] if message["role"] == "tool"]


def ready(kernel, **changes):
    bot = configured(kernel, **changes)
    ingest(kernel)
    transport = AsyncMock()
    transport.send.return_value = "888888888888888888"
    kernel.engine.transport = transport
    return bot, transport


def enable_documents(kernel):
    plugin = kernel.store.get("plugins", "document_site")
    plugin.pop("revision")
    kernel.store.put("plugins", {**plugin, "enabled": True})


@pytest.mark.parametrize("name", ["council_speak", "council_silence"])
async def test_empty_terminal_call_returns_usage_then_allows_repaired_final_decision(kernel, name):
    _, transport = ready(kernel)
    requests = []

    async def handle(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return reply(tool(name, {}))
        report = responses(requests[-1])[-1]
        assert report["usage_only"] is True
        assert report["executed"] is False
        assert report["usage"]["tool"] == name
        return reply(tool("council_speak", {"content": "Ready now"}))

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(requests) == 2
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    transport.send.assert_awaited_once()
    assert transport.send.await_args.args[2] == "Ready now"


@pytest.mark.parametrize(
    "arguments,paths",
    [
        (
            {"content": 17, "reply_to": "unknown", "artifact_ids": ["missing", 4]},
            {"$.content", "$.reply_to", "$.artifact_ids[0]", "$.artifact_ids[1]"},
        ),
        (
            {
                "content": "<think>private</think>",
                "reply_to": "unknown",
                "artifact_ids": ["missing", "also-missing"],
            },
            {"$.content", "$.reply_to", "$.artifact_ids[0]", "$.artifact_ids[1]"},
        ),
    ],
)
async def test_terminal_schema_and_semantic_failures_are_aggregated_without_dispatch(
    kernel, arguments, paths
):
    _, transport = ready(kernel)
    requests = []

    async def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply(tool("council_speak", arguments))
        report = responses(body)[-1]
        assert {error["path"] for error in report["errors"]} == paths
        assert report["error_count"] == 4
        assert report["executed"] is False
        assert report["usage"]["parameters"]["required"] == ["content"]
        return reply(tool("council_silence", {"label": "Repaired"}))

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(requests) == 2
    assert kernel.store.one("SELECT status FROM turns")["status"] == "silent"
    transport.send.assert_not_awaited()
    assert not kernel.store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("terminal_first", [False, True])
async def test_mixed_terminal_and_mutating_tool_batch_executes_neither(kernel, terminal_first):
    _, transport = ready(kernel, enabled_plugins=["memory"])
    requests = []

    async def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            calls = [
                tool("memory", {"operation": "write", "key": "bad", "value": "Must not be saved"}, "write"),
                tool("council_speak", {"content": "Must not be sent"}, "speak"),
            ]
            return reply(*(list(reversed(calls)) if terminal_first else calls))
        reports = responses(body)
        assert len(reports) == 2
        assert all(report["executed"] is False and "only tool call" in report["error"] for report in reports)
        assert all("usage" in report for report in reports)
        return reply(tool("council_silence", {"label": "Understood"}))

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "silent"
    assert not kernel.store.rows("SELECT * FROM memories")
    transport.send.assert_not_awaited()


@pytest.mark.parametrize("name,raw", [("council_speak", '{"content":'), ("memory", '{"operation":')])
async def test_malformed_terminal_or_plugin_call_delivers_full_usage_to_next_request(kernel, name, raw):
    ready(kernel, enabled_plugins=["memory"])
    count = 0

    async def handle(request):
        nonlocal count
        count += 1
        if count == 1:
            return reply(tool(name, raw))
        report = responses(json.loads(request.content))[-1]
        assert report["errors"][0]["rule"] == "json"
        assert report["usage"]["tool"] == name
        assert report["usage"]["parameters"]["required"]
        return reply(tool("council_silence", {"label": "Done"}))

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    assert count == 2
    assert kernel.store.one("SELECT status FROM turns")["status"] == "silent"


async def test_repeated_terminal_help_stops_at_existing_round_budget(kernel):
    _, transport = ready(kernel, max_tool_rounds=2)
    requests = []

    async def handle(request):
        requests.append(json.loads(request.content))
        return reply(tool("council_speak", {}))

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(requests) == 3
    turn = kernel.store.one("SELECT status,error FROM turns")
    assert turn["status"] == "failed" and "budget exhausted" in turn["error"]
    transport.send.assert_not_awaited()


@pytest.mark.parametrize("denied", ["council_inspect", "does_not_exist"])
async def test_mixed_batch_does_not_disclose_usage_for_ungranted_or_unknown_tools(kernel, denied):
    _, transport = ready(kernel)
    requests = []

    async def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply(tool("council_speak", {"content": 12}, "terminal"), tool(denied, {}, "denied"))
        reports = responses(body)
        assert reports[0]["usage"]["tool"] == "council_speak"
        assert reports[0]["error_count"] == 2  # Batch error and invalid content type together.
        assert "usage" not in reports[1]
        assert "not enabled" in reports[1]["error"]
        assert reports[1]["executed"] is False
        return reply(tool("council_silence", {"label": "Understood"}))

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "silent"
    transport.send.assert_not_awaited()


async def test_autonomous_document_start_unlocks_extended_rounds_and_call_batch_for_council_bot(kernel):
    ready(
        kernel,
        enabled_plugins=["document_site"],
        max_tool_rounds=1,
        max_calls_per_round=1,
        document_task_rounds=2,
        document_task_calls_per_round=2,
    )
    enable_documents(kernel)
    # A council peer, not The Boss, supplied the message. Plugin availability is the authorization.
    kernel.store.execute("UPDATE messages SET author_id='777777777777777777'")
    requests = []

    async def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        index = len(requests)
        if index == 1:
            return reply(tool("document_site", {"operation": "create", "site": "summary"}))
        if index == 2:
            budget = responses(body)[-1]["task_budget"]
            assert budget["active"] is True and budget["rounds_remaining"] == 2
            assert budget["calls_per_round"] == 2
            return reply(
                tool(
                    "document_site",
                    {
                        "operation": "write",
                        "site": "summary",
                        "path": "index.html",
                        "content": "<h1>Summary</h1>",
                    },
                    "html",
                ),
                tool(
                    "document_site",
                    {
                        "operation": "write",
                        "site": "summary",
                        "path": "style.css",
                        "content": "body { color: green; }",
                    },
                    "css",
                ),
            )
        if index == 3:
            return reply(tool("document_site", {"operation": "publish", "site": "summary"}))
        assert {item["function"]["name"] for item in body["tools"]} == {"council_speak", "council_silence"}
        report = responses(body)[-1]
        assert report["local_ready"] is True
        assert report["remote_status"] == "disabled"
        return reply(tool("council_silence", {"label": "Local document ready"}))

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(requests) == 4
    assert kernel.store.one("SELECT status FROM turns")["status"] == "silent"
    assert kernel.registry.documents.read_file("ada", "summary", "index.html")[0] == b"<h1>Summary</h1>"
    assert (
        kernel.store.one("SELECT status FROM document_sync_queue ORDER BY revision DESC")["status"]
        == "queued"
    )


async def test_restarting_document_tasks_does_not_renew_extra_rounds_or_time(kernel):
    ready(kernel, enabled_plugins=["document_site"], max_tool_rounds=1, document_task_rounds=2)
    enable_documents(kernel)
    requests, budgets = [], []

    async def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) > 1:
            budgets.append(responses(body)[-1]["task_budget"])
        return reply(tool("document_site", {"operation": "create", "site": f"summary-{len(requests)}"}))

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(requests) == 4
    assert [budget["rounds_remaining"] for budget in budgets] == [2, 1, 0]
    assert all(budget["renewable_this_turn"] is False for budget in budgets)
    assert (
        budgets[0]["seconds_remaining"] >= budgets[1]["seconds_remaining"] >= budgets[2]["seconds_remaining"]
    )
    assert len(kernel.store.rows("SELECT * FROM document_sites")) == 3
    assert len(kernel.store.rows("SELECT * FROM events WHERE kind='tool.task_started'")) == 1
    assert kernel.store.one("SELECT status FROM turns")["status"] == "failed"


async def test_zero_extra_rounds_preserves_normal_budget(kernel):
    ready(kernel, enabled_plugins=["document_site"], max_tool_rounds=1, document_task_rounds=0)
    enable_documents(kernel)
    requests = []

    async def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply(tool("document_site", {"operation": "create", "site": "summary"}))
        assert responses(body)[-1]["task_budget"]["active"] is False
        assert {item["function"]["name"] for item in body["tools"]} == {"council_speak", "council_silence"}
        return reply(tool("council_silence", {"label": "Normal budget"}))

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(requests) == 2
    assert kernel.store.one("SELECT status FROM turns")["status"] == "silent"


async def test_document_deadline_cancels_generation_and_preserves_local_draft(kernel):
    # Short internal fixture deadline exercises cancellation without a 30-second production minimum wait.
    _, transport = ready(kernel, enabled_plugins=["document_site"], document_task_seconds=0.1)
    enable_documents(kernel)
    count = 0

    async def handle(request):
        nonlocal count
        count += 1
        if count == 1:
            return reply(tool("document_site", {"operation": "create", "site": "summary"}))
        await asyncio.sleep(0.5)
        return reply(tool("council_speak", {"content": "Too late"}))

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    turn = kernel.store.one("SELECT status,error FROM turns")
    assert turn["status"] == "failed" and "time budget exhausted" in turn["error"]
    assert kernel.store.one("SELECT status FROM requests ORDER BY started_at DESC")["status"] == "cancelled"
    assert kernel.store.one("SELECT slug FROM document_sites")["slug"] == "summary"
    transport.send.assert_not_awaited()


@pytest.mark.parametrize("cooldown", [False, True])
async def test_deadline_blocks_expired_cooldown_dispatch_but_does_not_cancel_accepted_send(kernel, cooldown):
    bot, transport = ready(kernel, enabled_plugins=["document_site"], document_task_seconds=0.1)
    enable_documents(kernel)
    count = 0
    if cooldown:
        kernel.store.execute("UPDATE bot_runtime SET last_sent=? WHERE bot_id=?", (time.time(), bot["id"]))
    else:

        async def send(*args, **kwargs):
            await asyncio.sleep(0.2)
            return "888888888888888888"

        transport.send.side_effect = send

    async def handle(request):
        nonlocal count
        count += 1
        if count == 1:
            return reply(tool("document_site", {"operation": "create", "site": "summary"}))
        return reply(tool("council_speak", {"content": "Local draft created"}))

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    turn = kernel.store.one("SELECT status,error FROM turns")
    if cooldown:
        assert turn["status"] == "failed" and "before Discord dispatch" in turn["error"]
        transport.send.assert_not_awaited()
        assert kernel.store.one("SELECT status FROM outbox")["status"] == "failed"
    else:
        assert turn["status"] == "sent"
        transport.send.assert_awaited_once()
        assert kernel.store.one("SELECT status FROM outbox")["status"] == "sent"
