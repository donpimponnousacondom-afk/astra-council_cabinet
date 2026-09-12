import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import configured
from hortator.app import create_app
from hortator.memory_budget import budget_for
from hortator.models import Bot, ControlError
from hortator.plugins import ToolContext
from test_provider import install_client
from test_runtime import completion, settle
from test_runtime_feedback import ready, reply, responses, tool


async def write(kernel, context, key, value):
    return await kernel.registry.call(
        "memory", {"operation": "write", "key": key, "value": value}, context, f"write-{key}"
    )


async def test_older_bots_default_to_48000_and_changes_are_per_bot(kernel, owner):
    old = kernel.store.get("bots", "ada")
    old.pop("revision")
    old.pop("memory_char_limit", None)
    kernel.store.put("bots", old)
    other = kernel.store.get("bots", "hortator")
    assert Bot.model_validate(old).memory_char_limit == 48_000
    assert kernel.service.public("bots", kernel.store.get("bots", "ada"))["memory_char_limit"] == 48_000
    assert "memory_char_limit" not in kernel.store.get("bots", "ada")
    saved = await kernel.service.save(owner, "bots", "ada", {"memory_char_limit": 1200})
    assert saved["memory_char_limit"] == 1200
    assert saved["enabled_plugins"] == old["enabled_plugins"]
    assert kernel.store.get("bots", "hortator") == other


@pytest.mark.parametrize("limit", [0, -1, 48_001, 1.5, True, "1000", None])
async def test_invalid_budgets_are_rejected_without_mutation(kernel, owner, limit):
    before = kernel.store.get("bots", "ada")
    raw = {key: value for key, value in before.items() if key != "revision"}
    with pytest.raises(ValidationError):
        Bot.model_validate({**raw, "memory_char_limit": limit})
    with pytest.raises(ControlError, match="memory_char_limit"):
        await kernel.service.save(owner, "bots", "ada", {"memory_char_limit": limit})
    assert kernel.store.get("bots", "ada") == before


def test_authenticated_api_exposes_bounds_and_rejects_zero_even_when_plugin_disabled(tmp_path):
    app = create_app(tmp_path, start_runtime=False)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": (tmp_path / "initial-password").read_text().strip()})
        csrf = client.get("/api/auth/session").json()["csrf"]
        schema = client.get("/api/config-schemas").json()["bots"]["properties"]["memory_char_limit"]
        assert (schema["minimum"], schema["maximum"], schema["default"]) == (1, 48_000, 48_000)
        for limit in [0, -1, 48_001]:
            response = client.post(
                "/api/control",
                headers={"X-CSRF-Token": csrf},
                json={
                    "action": "save",
                    "kind": "bots",
                    "id": "ada",
                    "data": {"memory_char_limit": limit, "enabled_plugins": []},
                },
            )
            assert response.status_code == 400
            assert "memory_char_limit" in response.json()["error"]
        assert client.get("/api/config/bots/ada").json()["memory_char_limit"] == 48_000


async def test_48000_budget_accepts_5_percent_once_then_requires_shrinking(kernel):
    bot = configured(kernel, enabled_plugins=["memory"])
    context = ToolContext(bot, "channel", "memory-boundary")
    for index in range(6):
        assert (await write(kernel, context, str(index), "é" * 8000))["saved"]
    result = await write(kernel, context, "overflow", "x" * 2400)
    assert result["saved"] and result["budget"]["used_chars"] == 50_400
    assert result["budget"]["over_budget_chars"] == 2400
    assert "WARNING" in result["warning"]
    before = kernel.store.rows("SELECT * FROM memories")
    assert (await write(kernel, context, "extra", "x"))["ok"] is False
    assert (await write(kernel, context, "0", "z" * 8000))["ok"] is False
    assert kernel.store.rows("SELECT * FROM memories") == before
    reduced = await write(kernel, context, "overflow", "x" * 128)
    assert reduced["saved"] and reduced["budget"]["used_chars"] == 48_128
    assert reduced["budget"]["must_consolidate"]
    deleted = await kernel.registry.call(
        "memory", {"operation": "delete", "key": "overflow"}, context, "delete"
    )
    assert deleted["budget"]["used_chars"] == 48_000
    assert not deleted["budget"]["must_consolidate"] and "warning" not in deleted
    replacement = await write(kernel, context, "0", "z" * 8000)
    assert replacement["saved"] and replacement["budget"]["used_chars"] == 48_000
    assert (await write(kernel, context, "large", "x" * 8001))["ok"] is False
    warnings = [event for event in kernel.store.events() if event["kind"] == "memory.budget_warning"]
    assert len(warnings) == 2
    assert all(event["data"]["channel_id"] == "channel" for event in warnings)


async def test_no_cross_bot_or_channel_sharing_and_no_nul_or_unicode_undercount(kernel):
    bot = configured(kernel, enabled_plugins=["memory"], memory_char_limit=20)
    other = configured(kernel, bot_id="socrates", enabled_plugins=["memory"], memory_char_limit=40)
    a = ToolContext(bot, "channel", "scopes")
    b = ToolContext(other, "channel", "scopes")
    c = ToolContext(bot, "other-channel", "scopes")
    assert (await write(kernel, a, "note", "é\0" * 10))["budget"]["used_chars"] == 20
    rejected = await write(kernel, a, "too-large", "xx")
    assert rejected["ok"] is False and "22" in rejected["error"]
    assert (await write(kernel, b, "note", "x" * 40))["saved"]
    assert (await write(kernel, c, "note", "x" * 20))["saved"]
    assert budget_for(kernel.store, bot, "channel")["used_chars"] == 20


async def test_tiny_budget_has_no_rounded_up_grace(kernel):
    bot = configured(kernel, enabled_plugins=["memory"], memory_char_limit=1)
    context = ToolContext(bot, "channel", "tiny")
    saved = await write(kernel, context, "note", "a")
    assert saved["budget"]["hard_limit_chars"] == 1
    assert (await write(kernel, context, "other", "b"))["ok"] is False


async def test_active_tool_context_obeys_new_lower_budget(kernel, owner):
    bot = configured(kernel, enabled_plugins=["memory"], memory_char_limit=100)
    context = ToolContext(bot, "channel", "old-context")
    await write(kernel, context, "note", "x" * 50)
    await kernel.service.save(owner, "bots", bot["id"], {"memory_char_limit": 80})
    assert (await write(kernel, context, "note", "x" * 85))["ok"] is False
    accepted = await write(kernel, context, "note", "x" * 84)
    assert accepted["budget"]["limit_chars"] == 80
    assert accepted["budget"]["must_consolidate"]


async def test_owner_writes_share_allowance_and_lowering_preserves_notes(kernel, owner):
    bot = configured(kernel, enabled_plugins=["memory"], memory_char_limit=100)
    kernel.store.context(bot["id"], "channel")
    saved = await kernel.service.control(
        owner,
        {
            "action": "memory",
            "kind": "bots",
            "id": bot["id"],
            "data": {
                "channel_id": "channel",
                "key": "note",
                "value": "x" * 105,
            },
        },
    )
    assert saved["budget"]["must_consolidate"]
    # An unrelated configuration save remains possible during consolidation.
    await kernel.service.save(owner, "bots", bot["id"], {"persona": "A revised persona"})
    with pytest.raises(ControlError, match="Memory write not saved"):
        await kernel.service.control(
            owner,
            {
                "action": "memory",
                "kind": "bots",
                "id": bot["id"],
                "data": {
                    "channel_id": "channel",
                    "key": "extra",
                    "value": "x",
                },
            },
        )
    with pytest.raises(ControlError, match="already uses 105"):
        await kernel.service.save(owner, "bots", bot["id"], {"memory_char_limit": 90})
    assert kernel.store.one("SELECT value FROM memories")["value"] == "x" * 105
    await kernel.service.control(
        owner,
        {
            "action": "memory",
            "kind": "bots",
            "id": bot["id"],
            "data": {
                "channel_id": "channel",
                "key": "note",
                "value": "x" * 90,
            },
        },
    )
    saved = await kernel.service.save(owner, "bots", bot["id"], {"memory_char_limit": 90})
    assert saved["memory_char_limit"] == 90


@pytest.mark.parametrize("global_disable", [False, True])
async def test_plugin_switch_controls_tools_and_note_injection_without_deleting(
    kernel, owner, global_disable
):
    bot = configured(kernel, enabled_plugins=["memory"], memory_char_limit=100)
    context = ToolContext(bot, "channel", "plugin-switch")
    kernel.store.context(bot["id"], "channel")
    await write(kernel, context, "topic", "Stored private note")
    assert "memory" in {layer["id"] for layer in kernel.engine.contexts.layers(bot, "channel")}
    kind, id_, off, on = (
        ("plugins", "memory", {"enabled": False}, {"enabled": True})
        if global_disable
        else ("bots", bot["id"], {"enabled_plugins": []}, {"enabled_plugins": ["memory"]})
    )
    await kernel.service.save(owner, kind, id_, off)
    assert not kernel.registry.allowed("memory", context)
    assert all(spec["function"]["name"] != "memory" for spec in kernel.registry.schemas(context))
    assert not {"memory", "memory_budget"} & {
        layer["id"] for layer in kernel.engine.contexts.layers(bot, "channel")
    }
    assert (await write(kernel, context, "extra", "Not saved"))["ok"] is False
    assert kernel.service.context(bot["id"], "channel")["memories"][0]["value"] == "Stored private note"
    await kernel.service.save(owner, kind, id_, on)
    assert "memory" in {layer["id"] for layer in kernel.engine.contexts.layers(bot, "channel")}


async def test_tools_usage_and_next_prompt_report_current_budget(kernel):
    bot = configured(kernel, enabled_plugins=["memory"], memory_char_limit=100)
    context = ToolContext(bot, "channel", "model-guidance")
    await write(kernel, context, "note", "x" * 104)
    schema = next(
        s["function"] for s in kernel.registry.schemas(context) if s["function"]["name"] == "memory"
    )
    assert "104/100" in schema["description"] and "WARNING" in schema["description"]
    for arguments in ["{}", '{"operation":', '{"key":"missing-operation"}']:
        report = await kernel.registry.call_raw("memory", arguments, context, "usage")
        assert "104/100" in report["usage"]["description"]
        assert "WARNING" in report["usage"]["description"]
    layers = kernel.engine.contexts.layers(bot, "channel")
    assert "4 characters over budget" in next(
        layer["content"] for layer in layers if layer["id"] == "memory_budget"
    )
    read = await kernel.registry.call("memory", {"operation": "read"}, context, "read")
    assert read["budget"]["remaining_chars"] == 0 and "warning" in read


async def test_model_gets_accepted_overshoot_and_warning_on_next_tool_round(kernel):
    _, transport = ready(kernel, enabled_plugins=["memory"], memory_char_limit=100, max_tool_rounds=2)
    bodies = []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            assert "0/100 characters used" in body["messages"][0]["content"]
            return reply(tool("memory", {"operation": "write", "key": "note", "value": "x" * 104}))
        if len(bodies) == 2:
            assert "WARNING: 4 characters over budget" in body["messages"][0]["content"]
            assert responses(body)[-1]["saved"] and "warning" in responses(body)[-1]
            return reply(tool("memory", {"operation": "write", "key": "note", "value": "x" * 90}))
        assert "90/100 characters used" in body["messages"][0]["content"]
        assert "WARNING" not in body["messages"][0]["content"]
        return completion("Consolidated my note.")

    await install_client(kernel, respond)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(bodies) == 3
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    transport.send.assert_awaited_once()
