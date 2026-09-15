import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hortator.app import create_app
from hortator.models import Bot, ControlError, OWNER_ID
from hortator.plugins import ToolContext
from hortator.security import Actor
from test_global_memory import enabled, write


async def test_production_registration_defaults_keyless_contract_and_context_layers(kernel, owner):
    assert "global_memory" in kernel.registry.specs
    assert hasattr(kernel.registry, "global_memory")
    plugin = kernel.service.public("plugins", kernel.store.get("plugins", "global_memory"))
    assert plugin["keyless"] and not plugin["enabled"]
    bot = enabled(kernel, limit=100)
    context = ToolContext(bot, "channel-a", "automatic-layer")
    await write(kernel, context, "topic", "x" * 104)
    schema = next(
        item["function"]
        for item in kernel.registry.schemas(context)
        if item["function"]["name"] == "global_memory"
    )
    assert "104/100" in schema["description"] and "WARNING" in schema["description"]
    for raw in ["{}", '{"operation":', '{"operation":"write","key":12}']:
        result = await kernel.registry.call_raw("global_memory", raw, context, "usage-dynamic")
        assert "104/100" in result["usage"]["description"]
    layers = kernel.engine.contexts.layers(bot, "channel-b")
    assert {"global_memory_budget", "global_memory"} <= {layer["id"] for layer in layers}
    assert "channel-a" in next(layer["content"] for layer in layers if layer["id"] == "global_memory")
    await kernel.service.save(owner, "plugins", "global_memory", {"enabled": False})
    assert not {"global_memory_budget", "global_memory"} & {
        layer["id"] for layer in kernel.engine.contexts.layers(bot, "channel-b")
    }
    assert kernel.service.global_memory_view(bot["id"])["notes"][0]["value"] == "x" * 104


async def test_existing_bots_get_public_default_and_per_bot_quota_is_independent(kernel, owner):
    original = kernel.store.get("bots", "ada")
    original.pop("revision")
    original.pop("global_memory_char_limit", None)
    kernel.store.put("bots", original)
    other = kernel.store.get("bots", "socrates")
    assert Bot.model_validate(original).global_memory_char_limit == 48_000
    assert (
        kernel.service.public("bots", kernel.store.get("bots", "ada"))["global_memory_char_limit"] == 48_000
    )
    assert "global_memory_char_limit" not in kernel.store.get("bots", "ada")
    saved = await kernel.service.save(owner, "bots", "ada", {"global_memory_char_limit": 500})
    assert saved["global_memory_char_limit"] == 500
    assert saved["memory_char_limit"] == original["memory_char_limit"]
    assert kernel.store.get("bots", "socrates") == other


@pytest.mark.parametrize("limit", [0, -1, 48_001, 1.5, True, "1000", None])
async def test_shared_bot_schema_and_config_api_reject_invalid_global_quotas(kernel, owner, limit):
    old = kernel.store.get("bots", "ada")
    value = {key: val for key, val in old.items() if key != "revision"}
    with pytest.raises(ValidationError):
        Bot.model_validate({**value, "global_memory_char_limit": limit})
    with pytest.raises(ControlError, match="global_memory_char_limit"):
        await kernel.service.save(owner, "bots", "ada", {"global_memory_char_limit": limit})
    assert kernel.store.get("bots", "ada") == old


async def test_owner_service_cancels_only_target_before_write_and_audits(kernel, owner, monkeypatch):
    bot = enabled(kernel, limit=100)
    memory = kernel.registry.global_memory
    await write(kernel, ToolContext(bot, "source", "initial"), "topic", "before")

    async def check_cancel(ids, reason):
        assert ids == [bot["id"]]
        assert "owner" in reason
        assert memory.notes(bot["id"])[0]["value"] == "before"

    monkeypatch.setattr(kernel.engine, "cancel", AsyncMock(side_effect=check_cancel))
    result = await kernel.service.global_memory_change(
        owner, bot["id"], {"operation": "write", "key": "topic", "value": "after"}
    )
    assert result["saved"]
    kernel.engine.cancel.assert_awaited_once()
    assert memory.notes(bot["id"])[0]["value"] == "after"
    assert any(
        event["kind"] == "control.executed" and event["data"].get("action") == "global_memory"
        for event in kernel.store.events()
    )
    kernel.engine.cancel.reset_mock()
    with pytest.raises(ControlError, match="value"):
        await kernel.service.global_memory_change(owner, bot["id"], {"operation": "write", "key": "topic"})
    await kernel.service.global_memory_change(owner, bot["id"], {})
    await kernel.service.global_memory_change(owner, bot["id"], {"operation": "read"})
    kernel.engine.cancel.assert_not_awaited()


@pytest.mark.parametrize(
    "actor",
    [
        Actor("model", OWNER_ID),
        Actor("discord", OWNER_ID, is_bot=True),
        Actor("dashboard", "999999999999999999"),
    ],
)
async def test_global_owner_endpoint_helper_rejects_forged_authority(kernel, actor):
    with pytest.raises(ControlError, match="Only The Boss"):
        await kernel.service.global_memory_change(
            actor, "ada", {"operation": "write", "key": "topic", "value": "forged"}
        )
    assert not kernel.registry.global_memory.notes("ada")


def test_authenticated_global_memory_api_has_no_context_prerequisite_and_enforces_csrf(tmp_path):
    app = create_app(tmp_path, start_runtime=False)
    with TestClient(app) as client:
        endpoint = "/api/global-memory/ada"
        assert client.get(endpoint).status_code == 401
        password = (tmp_path / "initial-password").read_text().strip()
        assert client.post("/api/auth/login", json={"password": password}).status_code == 200
        view = client.get(endpoint)
        assert view.status_code == 200
        assert view.json()["notes"] == [] and not view.json()["enabled"]
        assert client.get("/api/global-memory/missing").status_code == 404
        request = {"operation": "write", "key": "owner-note", "value": "Independent of any channel."}
        assert client.post(endpoint, json=request).status_code == 403
        headers = {"X-CSRF-Token": client.get("/api/auth/session").json()["csrf"]}
        assert client.post(endpoint, json=request, headers=headers).json()["saved"]
        assert client.get(endpoint).json()["notes"][0]["value"] == request["value"]
        wrong = client.post(endpoint, json={"operation": "write", "key": 5, "unknown": True}, headers=headers)
        assert wrong.status_code == 400
        parsed = json.loads(wrong.json()["error"])
        assert parsed["error_count"] >= 3 and "usage" in parsed
        schema = client.get("/api/config-schemas").json()["bots"]["properties"]["global_memory_char_limit"]
        assert (schema["minimum"], schema["maximum"], schema["default"]) == (1, 48_000, 48_000)
