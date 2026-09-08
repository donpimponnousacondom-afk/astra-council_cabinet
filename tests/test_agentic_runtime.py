import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from conftest import configured
from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.store import dumps
from hortator.working_set import bound_exchanges, prompt_exchanges
from test_provider import install_client
from test_runtime import settle
from test_runtime_feedback import ready, reply, responses, tool, enable_documents


def grant(kernel, *names, **changes):
    bot = configured(kernel, enabled_plugins=list(names), **changes)
    for name in names:
        plugin = kernel.store.get("plugins", name)
        plugin.pop("revision")
        kernel.store.put("plugins", {**plugin, "enabled": True})
    return ToolContext(bot, "222222222222222222", "turn-agentic")


@pytest.mark.parametrize(
    "order", [("web_fetch", "workspace", "document_site"), ("document_site", "web_fetch", "workspace")]
)
async def test_switching_task_plugins_never_stacks_or_renews_budgets(kernel, order):
    ready(
        kernel,
        enabled_plugins=list(order),
        max_tool_rounds=1,
        work_task_rounds=2,
        document_task_rounds=2,
        work_task_calls_per_round=2,
    )
    enable_documents(kernel)
    plugin = kernel.store.get("plugins", "workspace")
    kernel.store.put("plugins", {**plugin, "enabled": True})
    requests, budgets = [], []

    async def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        index = len(requests) - 1
        if index:
            result = responses(body)[-1]
            budgets.append(result["task_budget"])
            evidence = kernel.store.one(
                "SELECT content FROM tool_result_evidence WHERE id=?", (result["result_id"],)
            )
            assert json.loads(evidence["content"]) == {
                key: value for key, value in result.items() if key != "result_id"
            }
        if index == 3:
            assert {x["function"]["name"] for x in body["tools"]} == {"council_speak", "council_silence"}
            return reply(tool("council_silence", {"label": "Finished within shared budget"}))
        name = order[index]
        args = {"operation": "start"}
        if name == "workspace":
            args["task"] = "notes"
        if name == "document_site":
            args["operation"] = "create"
            args["site"] = "notes"
        return reply(tool(name, args))

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "silent"
    assert len(requests) == 4
    assert [b["rounds_remaining"] for b in budgets] == [2, 1, 0]
    assert budgets[0]["seconds_remaining"] >= budgets[-1]["seconds_remaining"]
    assert len(kernel.store.rows("SELECT * FROM events WHERE kind='tool.task_started'")) == 1


async def test_complete_near_megabyte_reading_keeps_small_active_prompt_and_original_evidence(kernel):
    # Real extraction/storage/context/provider adapter; only HTTP provider and
    # public page transport are synthetic. No helper/paid model or refetch occurs.
    ready(
        kernel,
        enabled_plugins=["web_fetch"],
        max_tool_rounds=1,
        work_task_rounds=100,
        tool_working_set_tokens=4800,
    )
    profile = kernel.store.get("profiles", "balanced")
    kernel.store.put(
        "profiles", {**profile, "context_window": 14000, "response_tokens": 1500, "summary_tokens": 1000}
    )
    lines = [f"{index:06d} café 🙂 observation about sample {index % 37}\n" for index in range(20000)]
    expected = "".join(lines) + "LATE-MARKER-Ω"
    payload = ("<html><body>" + expected + "</body></html>").encode()
    assert 800_000 < len(payload) < 1_000_000
    fetcher = AsyncMock(return_value=(payload, "text/html; charset=utf-8", "https://example.com/long"))
    kernel.registry.fetched_documents.fetcher = fetcher
    bodies, collected, document_id = [], [], None

    async def handle(request):
        nonlocal document_id
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return reply(tool("web_fetch", {"operation": "start"}))
        if len(bodies) == 2:
            return reply(tool("web_fetch", {"url": "https://example.com/long", "length": 10000}))
        result = responses(body)[-1]
        assert "text" in result, "The latest bounded chunk must remain in the active model prompt"
        document_id = result["document_id"]
        collected.append(result["text"])
        if result["next"]:
            return reply(tool("web_fetch", result["next"]))
        return reply(tool("council_silence", {"label": "Full document read"}))

    await install_client(kernel, handle)
    await kernel.engine.tick()
    # More than 50 provider/tool cycles, all local and bounded.
    await asyncio.wait_for(asyncio.gather(*kernel.engine.tasks.values()), timeout=30)
    turn = kernel.store.one("SELECT * FROM turns")
    assert turn["status"] == "silent", turn["error"]
    assert "".join(collected) == expected
    fetcher.assert_awaited_once()
    assert len(bodies) > 50
    assert max(kernel.engine.contexts.estimate_request(b["messages"], b["tools"]) for b in bodies) < 13000
    assert any("omitted from" in dumps(b["messages"]) for b in bodies)
    assert expected[:100] in dumps(bodies[2]) or "omitted" in dumps(bodies[2])
    stored = kernel.store.rows("SELECT content FROM tool_result_evidence WHERE tool='web_fetch'")
    assert sum(len(json.loads(row["content"]).get("text", "")) for row in stored) == len(expected)
    assert (
        kernel.registry.fetched_documents.read_document(
            "ada", "222222222222222222", document_id, offset=len(expected), length=1
        )["text"]
        == ""
    )
    assert len(kernel.store.rows("SELECT * FROM requests")) == len(bodies)


async def test_result_rereading_is_owned_and_rechecks_grants_even_with_stale_context(kernel):
    context = grant(kernel, "workspace", "web_fetch")
    result = await kernel.registry.call(
        "workspace", {"operation": "start", "task": "notes"}, context, "same-id"
    )
    repeated = await kernel.registry.call("workspace", {"operation": "list"}, context, "same-id")
    assert result["result_id"] != repeated["result_id"]
    args = {"operation": "read_result", "result_id": result["result_id"], "length": 100}
    read = await kernel.registry.call("web_fetch", args, context, "read")
    assert read["source_tool"] == "workspace" and read["range"] == {"start": 0, "end": 100}
    for foreign in (
        ToolContext(context.bot, "other-channel", context.turn_id),
        ToolContext(context.bot, context.channel_id, "other-turn"),
    ):
        denied = await kernel.registry.call("web_fetch", args, foreign, "denied")
        assert not denied["ok"] and "another bot, channel or turn" in denied["error"]
    current = kernel.store.get("bots", "ada")
    kernel.store.put("bots", {**current, "enabled_plugins": ["web_fetch"]})
    denied = await kernel.registry.call("web_fetch", args, context, "revoked")
    assert not denied["ok"] and "grant" in denied["error"]
    assert not kernel.registry.allowed("workspace", context)


async def test_reread_pages_retain_transitive_source_permissions(kernel):
    context = grant(kernel, "workspace", "web_fetch", "shell")
    original = kernel.registry.evidence.record(context, "shell", "job", {"text": "private job output"})
    page = await kernel.registry.call(
        "workspace", {"operation": "read_result", "result_id": original["result_id"]}, context, "page"
    )
    assert page["source_tool"] == "shell"
    current = kernel.store.get("bots", "ada")
    kernel.store.put("bots", {**current, "enabled_plugins": ["workspace", "web_fetch"]})
    denied = await kernel.registry.call(
        "web_fetch", {"operation": "read_result", "result_id": page["result_id"]}, context, "revoked-page"
    )
    assert denied["ok"] is False and "grant" in denied["error"]


async def test_working_set_accounts_for_large_existing_summary_before_calling_provider(kernel):
    ready(kernel, enabled_plugins=["web_fetch"])
    profile = kernel.store.get("profiles", "balanced")
    kernel.store.put(
        "profiles",
        {
            **profile,
            "context_window": 14000,
            "response_tokens": 1500,
            "summary_tokens": 1000,
            "compact_threshold": 0.7,
        },
    )
    kernel.store.execute("UPDATE contexts SET summary=?", ("Remember this conversation fact. " * 1200,))
    fetcher = AsyncMock(return_value=(b"sample word " * 1800, "text/plain", "https://example.com/large"))
    kernel.registry.fetched_documents.fetcher = fetcher
    requests = []

    async def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply(tool("web_fetch", {"url": "https://example.com/large"}))
        assert responses(body)[-1]["omitted_from_active_prompt"] is True
        return reply(tool("council_silence", {"label": "Reference retained"}))

    await install_client(kernel, handle)
    await kernel.engine.tick()
    await settle(kernel)
    turn = kernel.store.one("SELECT * FROM turns")
    assert turn["status"] == "silent", turn["error"]
    assert len(requests) == 2


def test_retained_tool_pairs_preserve_native_assistant_continuation(kernel):
    assistant = {
        "role": "assistant",
        "tool_calls": [tool("web_fetch", {"url": "https://example.com"}, "old")],
        "reasoning_details": [{"signature": "provider-signature", "data": "opaque-continuation"}],
    }
    extras = [
        assistant,
        {
            "role": "tool",
            "tool_call_id": "old",
            "content": dumps({"text": "x y z " * 6000, "result_id": "old-result"}),
        },
        {"role": "assistant", "tool_calls": [tool("web_fetch", {"operation": "start"}, "new")]},
        {"role": "tool", "tool_call_id": "new", "content": "{}"},
    ]
    messages, _ = bound_exchanges(extras, kernel.engine.contexts.estimate, 1000)
    retained = [m for m in messages if m.get("tool_calls", [{}])[0].get("id") == "old"]
    assert not retained or retained[0] == assistant


async def test_shell_argument_errors_are_complete_without_executing(kernel):
    context = grant(kernel, "workspace", "shell")
    result = await kernel.registry.call(
        "shell",
        {"operation": "run", "command": "", "cwd": "../outside", "timeout_seconds": 100},
        context,
        "invalid",
    )
    assert result["executed"] is False
    assert {e["path"] for e in result["errors"]} >= {"$", "$.command", "$.cwd", "$.timeout_seconds"}
    assert not kernel.registry.agentic.runner.inspect()


def test_prompt_minimization_preserves_original_objects_and_complete_tool_pairs(kernel):
    extras = []
    for index in range(30):
        extras += [
            {"role": "assistant", "tool_calls": [tool("web_fetch", {"document_id": "x"}, str(index))]},
            {
                "role": "tool",
                "tool_call_id": str(index),
                "content": dumps(
                    {"text": "large Unicode 🙂 output\n" * 1500, "result_id": f"result_{index}"}
                ),
            },
        ]
    original = copy.deepcopy(extras)
    bounded, meta = bound_exchanges(extras, kernel.engine.contexts.estimate, 1000)
    assert extras == original
    assert meta["omitted_groups"] > 0
    assert kernel.engine.contexts.estimate(prompt_exchanges(bounded)) <= 1000
    model_input = prompt_exchanges(bounded)
    assert all("_working_set_index" not in message for message in model_input)
    ids = {call["id"] for message in model_input for call in message.get("tool_calls", [])}
    assert {m["tool_call_id"] for m in model_input if m["role"] == "tool"} == ids


async def test_working_limits_and_keyless_configuration_are_validated(kernel, owner):
    for name in ("workspace", "shell", "web_fetch"):
        assert kernel.service.public("plugins", kernel.store.get("plugins", name))["keyless"] is True
    before = kernel.store.get("plugins", "web_fetch")
    with pytest.raises(ControlError, match="configuration"):
        await kernel.service.save(
            owner, "plugins", "web_fetch", {"config": {"chunk_chars": True, "max_download_bytes": 2_000_000}}
        )
    assert kernel.store.get("plugins", "web_fetch") == before
    with pytest.raises(ControlError, match="configuration"):
        await kernel.service.save(owner, "bots", "ada", {"plugin_config": {"shell": {"process_limit": "12"}}})
