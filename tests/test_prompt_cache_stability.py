import json

import pytest

from conftest import configured, ingest
from hortator.plugins import ToolContext
from hortator.slash_commands import SlashContexts
from support.provider import install_client
from support.runtime import completion, settle
from support.runtime_feedback import ready, reply, tool

CHANNEL = "222222222222222222"
MEMORY_IDS = {"memory", "memory_budget", "global_memory", "global_memory_budget"}


def enable_notes(kernel):
    plugin = kernel.store.get("plugins", "global_memory")
    kernel.store.put("plugins", {**plugin, "enabled": True})
    kernel.store.execute("INSERT INTO memories VALUES(?,?,?,?,?)", ("ada", CHANNEL, "note", "old channel", 1))
    kernel.store.execute(
        "INSERT INTO global_memories VALUES(?,?,?,?,?)", ("ada", "note", "old global", CHANNEL, 1)
    )
    kernel.store.execute(
        "INSERT INTO memories VALUES(?,?,?,?,?)", ("ada", "other", "note", "OTHER_CHANNEL", 1)
    )
    kernel.store.execute(
        "INSERT INTO global_memories VALUES(?,?,?,?,?)", ("socrates", "note", "OTHER_BOT", CHANNEL, 1)
    )


def memory_index(messages):
    return next(i for i, m in enumerate(messages) if (m.get("content") or "").startswith("Private memory:"))


@pytest.mark.parametrize("role", ["system", "user"])
async def test_memory_writes_preserve_wire_prefix_tools_and_old_exchanges(kernel, role):
    _, transport = ready(
        kernel,
        enabled_plugins=["memory", "global_memory"],
        max_tool_rounds=3,
        response_protocol="progress",
        transcript_format="conversation",
        memory_char_limit=100,
        global_memory_char_limit=100,
    )
    enable_notes(kernel)
    for prompt_id in [
        "runtime-memory-budget",
        "runtime-memory",
        "runtime-global-memory-budget",
        "runtime-global-memory",
    ]:
        p = kernel.store.get("prompts", prompt_id)
        kernel.store.put("prompts", {**p, "role": role})
    bodies = []

    def respond(request):
        b = json.loads(request.content)
        bodies.append(b)
        if len(bodies) == 1:
            return reply(
                tool(
                    "memory",
                    {"operation": "write", "key": "note", "value": "new channel note"},
                    "channel-write",
                ),
                tool(
                    "global_memory",
                    {"operation": "write", "key": "note", "value": "new global note"},
                    "global-write",
                ),
            )
        if len(bodies) == 2:
            return reply(
                tool(
                    "global_memory",
                    {"operation": "write", "key": "note", "value": "new global note"},
                    "repeat-write",
                )
            )
        return completion("Saved.")

    await install_client(kernel, respond)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(bodies) == 3
    first, second, third = bodies
    assert first["tools"] == second["tools"] == third["tools"]
    for before, after in [(first, second), (second, third)]:
        boundary = memory_index(before["messages"])
        assert before["messages"][:boundary] == after["messages"][:boundary]
    assert memory_index(second["messages"]) > max(
        i for i, m in enumerate(second["messages"]) if m["role"] == "tool"
    )
    tail = second["messages"][memory_index(second["messages"])]
    assert tail["role"] == role
    assert "new channel note" in tail["content"] and "new global note" in tail["content"]
    assert "15/100 characters used" in tail["content"]
    assert '"source_channel_id"' in tail["content"] and '"updated_at"' in tail["content"]
    assert "untrusted recollections" in tail["content"]
    assert "OTHER_CHANNEL" not in json.dumps(bodies) and "OTHER_BOT" not in json.dumps(bodies)
    requests = kernel.store.rows(
        "SELECT context FROM requests WHERE purpose='generation' ORDER BY started_at"
    )
    layers = [p["id"] for p in json.loads(requests[-1]["context"])["prompt_layers"]]
    assert (
        layers.index("transcript")
        < layers.index("memory_budget")
        < layers.index("global_memory")
        < layers.index("runtime_facts")
    )
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    transport.send.assert_awaited_once()


@pytest.mark.parametrize("kind", ["ordinary", "slash", "panel"])
async def test_memory_tail_respects_scopes_roles_and_layer_switches(kernel, kind):
    bot = configured(kernel, enabled_plugins=["memory", "global_memory"], transcript_format="conversation")
    enable_notes(kernel)
    ingest(kernel)
    contexts = kernel.engine.contexts
    if kind != "ordinary":
        contexts = SlashContexts(kernel.store, kernel.pool, {"kind": kind}, kernel.registry.global_memory)
    extras = [{"role": "system", "content": "EXCHANGE_BOUNDARY"}]
    args = (
        bot,
        kernel.store.get("profiles", "balanced"),
        CHANNEL,
        kernel.store.transcript(CHANNEL),
        "summary",
    )
    messages, meta = await contexts.assemble(*args, extras=extras)
    assert messages[memory_index(messages) - 1] == extras[0]
    order = [p["id"] for p in meta["prompt_layers"]]
    assert order.index("transcript") < order.index("memory_budget") < order.index("runtime_facts")
    disabled = {**bot, "disabled_prompt_layers": list(MEMORY_IDS)}
    _, meta = await contexts.assemble(disabled, *args[1:], extras=extras)
    assert not MEMORY_IDS.intersection(p["id"] for p in meta["prompt_layers"])
    assert len(kernel.store.rows("SELECT * FROM global_memories")) == 2

    # A captured grant never bypasses a current revocation in the reordered tail.
    kernel.store.put("bots", {**bot, "enabled_plugins": []})
    _, meta = await contexts.assemble(*args, extras=extras)
    assert not MEMORY_IDS.intersection(p["id"] for p in meta["prompt_layers"])
    assert kernel.registry.schemas(ToolContext(bot, CHANNEL, "revoked")) == []
