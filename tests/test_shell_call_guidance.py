"""Shared convention placement, cache-prefix stability and recovery isolation."""

import copy
import json
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from conftest import configured, ingest
from hortator.prompt_templates import DEFAULT_PROMPTS
from hortator.provider import ProviderError
from hortator.slash_commands import SlashContexts
from scripts.replay_tool_request import SHELL_INSTRUCTION
from support.provider import install_client
from support.runtime import completion
from support.runtime_feedback import ready, reply, tool
from test_response_recovery import CHANNEL, DRAFT, THOUGHT, NOTICE, run, stopped


SHELL = [{"type": "function", "function": {"name": "shell", "parameters": {"type": "object"}}}]


@pytest.mark.parametrize("bot_id", ["ada", "socrates"])
@pytest.mark.parametrize("invocation", [None, "slash", "panel"])
async def test_default_matches_benchmark_without_changing_prefix_or_tools(
    kernel, monkeypatch, bot_id, invocation
):
    monkeypatch.setattr("hortator.context.time.time", lambda: 1800000000)
    monkeypatch.setattr(
        "hortator.context.datetime",
        SimpleNamespace(
            now=lambda tz: datetime.fromtimestamp(1800000000, tz), fromtimestamp=datetime.fromtimestamp
        ),
    )
    bot = configured(kernel, bot_id=bot_id, dynamic_prompt="existing tail")
    ingest(kernel, bot_id=bot_id)
    builder = (
        SlashContexts(kernel.store, kernel.pool, {"kind": invocation}, None)
        if invocation
        else kernel.engine.contexts
    )
    profile = kernel.store.get("profiles", "balanced")
    tools = copy.deepcopy(SHELL)
    args = (profile, CHANNEL, kernel.store.transcript(CHANNEL), "")
    extras = [{"role": "system", "content": "existing repair feedback"}]
    baseline, before = await builder.assemble(
        {**bot, "disabled_prompt_layers": ["shell_call_guidance"]}, *args, tools=tools, extras=extras
    )
    messages, meta = await builder.assemble(bot, *args, tools=tools, extras=extras)
    assert messages == baseline + [{"role": "system", "content": SHELL_INSTRUCTION}]
    assert tools == SHELL
    assert meta["final_prompt_count"] == 1
    assert meta["prompt_layers"][-1]["id"] == "shell_call_guidance"
    assert meta["estimated_tokens"] == await builder.estimate_async(messages, tools)
    assert meta["estimated_tokens"] > before["estimated_tokens"]


@pytest.mark.parametrize("tools", [[], [{"type": "function", "function": {"name": "end_cycle"}}]])
async def test_granted_shell_does_not_inject_guidance_when_not_advertised(kernel, tools):
    bot = configured(kernel, enabled_plugins=["shell"])
    ingest(kernel)
    messages, meta = await kernel.engine.contexts.assemble(
        bot,
        kernel.store.get("profiles", "balanced"),
        CHANNEL,
        kernel.store.transcript(CHANNEL),
        "",
        tools=tools,
    )
    assert meta["final_prompt_count"] == 0
    assert SHELL_INSTRUCTION not in str(messages)
    assert not any(p["id"] == "shell_call_guidance" for p in meta["prompt_layers"])


async def test_existing_prompt_controls_apply_without_hidden_fallback(kernel):
    bot = configured(kernel)
    template = kernel.store.get("prompts", "runtime-shell-call-guidance")
    assert template["content"] == SHELL_INSTRUCTION
    kernel.store.put("prompts", {**template, "role": "user", "content": "Operator convention"})
    args = (bot, kernel.store.get("profiles", "balanced"), CHANNEL, [], "")
    messages, _ = await kernel.engine.contexts.assemble(*args, tools=SHELL)
    assert messages[-1] == {"role": "user", "content": "Operator convention"}
    template = kernel.store.get("prompts", template["id"])
    kernel.store.put("prompts", {**template, "content": ""})
    messages, meta = await kernel.engine.contexts.assemble(*args, tools=SHELL)
    assert meta["final_prompt_count"] == 0 and SHELL_INSTRUCTION not in str(messages)
    assert next(p for p in DEFAULT_PROMPTS if p["id"] == template["id"])["content"] == SHELL_INSTRUCTION


@pytest.mark.parametrize("receipts", [False, True])
async def test_wire_tail_with_recovery_retries_and_receipts_stays_private(kernel, monkeypatch, receipts):
    ready(kernel, enabled_plugins=["memory", "shell", "engram"], cooldown_seconds=0)
    for name in ("shell", "engram"):
        plugin = kernel.store.get("plugins", name)
        kernel.store.put("plugins", {**plugin, "enabled": True})
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return stopped()
        if len(bodies) == 2:
            return httpx.Response(503, text="temporary fixture failure")
        if len(bodies) == 3:
            return reply(tool("memory", {"operation": "read"}))
        return completion("Considered answer")

    await install_client(kernel, handler)
    await run(kernel)
    if receipts:
        offer = SimpleNamespace(message={"role": "user", "content": "ACTION_RECEIPT_FIXTURE"}, receipt_ids=())
        monkeypatch.setattr(kernel.engine.action_receipts, "offer", lambda *args, **kwargs: offer)
    provider = kernel.store.get("providers", "openrouter")
    kernel.store.put("providers", {**provider, "retry_count": 1, "retry_delay_seconds": 0})
    await run(kernel)
    assert len(bodies) == 4
    assert bodies[1] == bodies[2]
    for body in bodies:
        assert body["messages"][-1] == {"role": "system", "content": SHELL_INSTRUCTION}
    note = bodies[1]["messages"][-2]
    assert note["role"] == "user" and note["content"].startswith(NOTICE)
    assert THOUGHT in note["content"] and DRAFT in note["content"]
    if receipts:
        assert bodies[1]["messages"][-3] == offer.message
    assert THOUGHT not in str(bodies[3])
    # The note remains private even though it is no longer the last message.
    for table in ("requests", "messages", "contexts", "events"):
        assert THOUGHT not in str(kernel.store.rows(f"SELECT * FROM {table}"))
    requests = kernel.store.rows("SELECT body,context FROM requests ORDER BY started_at")
    public = json.loads(requests[1]["body"])
    assert "original in private request diagnostics" in public["messages"][-2]["content"]
    assert public["messages"][-1]["content"] == SHELL_INSTRUCTION
    estimated = json.loads(requests[1]["context"])["estimated_tokens"]
    assert estimated == await kernel.engine.contexts.estimate_async(bodies[1]["messages"], bodies[1]["tools"])
    captures = kernel.store.rows("SELECT body FROM request_diagnostics")
    assert THOUGHT in str(captures)


async def test_missing_recovery_object_fails_before_request_or_transport(kernel):
    bot = configured(kernel)
    await install_client(kernel, lambda _: pytest.fail("invalid recovery must not reach transport"))
    with pytest.raises(ProviderError, match="Recovery note missing"):
        await kernel.pool.complete(
            bot=bot,
            profile=kernel.store.get("profiles", "balanced"),
            messages=[{"role": "user", "content": "ordinary input"}],
            tools=[],
            turn_id=None,
            context={},
            recovery={"message": {"role": "user", "content": THOUGHT}},
        )
    assert not kernel.store.rows("SELECT * FROM requests")
