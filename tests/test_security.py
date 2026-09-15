import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from hortator.app import create_app
from hortator.discord_gateway import owner_message
from hortator.models import ControlError, OWNER_ID
from hortator.plugins import ToolContext, public_url, PublicResolver
from hortator.security import Actor


@pytest.mark.parametrize(
    "actor",
    [
        Actor("discord", "1482143139828596917"),
        Actor("discord", ".normal.man."),
        Actor("discord", OWNER_ID, True),
        Actor("discord", OWNER_ID, False, "webhook"),
        Actor("model", OWNER_ID),
        Actor("discord", int(OWNER_ID)),
    ],
)
def test_exact_owner_identity(actor):
    with pytest.raises(ControlError):
        actor.require_owner()


async def test_model_cannot_mutate_and_secrets_are_not_discord_editable(kernel):
    actor = Actor("discord", OWNER_ID)
    with pytest.raises(ControlError, match="dashboard"):
        await kernel.service.credential(actor, "providers", "openrouter", "api_key", "secret")
    for payload in ({"headers": {"Authorization": "Bearer never-log-me"}}, {"api_key": "never-log-me"}):
        with pytest.raises(ControlError):
            await kernel.service.save(actor, "providers", "openrouter", payload)
    assert "never-log-me" not in str(kernel.store.events())
    bot = kernel.store.get("bots", "ada")
    bot["enabled_plugins"] = ["council_inspect"]
    result = await kernel.registry.call(
        "council_inspect", {"resource": "status"}, ToolContext(bot, "room", "turn", True), "call"
    )
    assert "error" in result
    hortator = kernel.store.get("bots", "hortator")
    assert not kernel.registry.allowed("council_inspect", ToolContext(hortator, "room", "turn", False))


async def test_vault_encrypts_and_redacts_nested_values(kernel):
    secret = "sk-do-not-leak-this-test-value"
    kernel.vault.put("provider/openrouter/api_key", secret)
    row = kernel.store.one("SELECT value FROM secrets WHERE scope='provider/openrouter/api_key'")
    assert secret.encode() not in row["value"]
    event = kernel.store.emit(
        "test", {"error": "echo " + secret, "nested": [{"authorization": secret}], "prompt": secret}
    )
    assert secret not in str(event)
    assert "[REDACTED]" in str(event)
    assert "[REDACTED]" in kernel.vault.redact("Bearer abc.def.ghi")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "http://[::1]",
        "http://169.254.169.254/latest",
        "http://10.0.0.1",
        "file:///etc/passwd",
        "http://localhost",
        "https://user:password@example.com",
        "http://example.com:8080",
        "http://224.0.0.1",
    ],
)
def test_web_fetch_blocks_nonpublic_urls(url):
    with pytest.raises(ControlError):
        public_url(url)


async def test_dns_rebinding_is_checked_at_socket_resolution(monkeypatch):
    import socket

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop,
        "getaddrinfo",
        AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]),
    )
    with pytest.raises(ControlError):
        await PublicResolver().resolve("apparently-public.example", 443)


def test_web_session_csrf_and_origin(tmp_path):
    app = create_app(tmp_path, start_runtime=False)
    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/export/config").status_code == 401
        password = (tmp_path / "initial-password").read_text().strip()
        assert (
            client.post(
                "/api/auth/login", json={"password": password}, headers={"Origin": "https://attacker.test"}
            ).status_code
            == 403
        )
        login = client.post(
            "/api/auth/login", json={"password": password}, headers={"Origin": "http://testserver"}
        )
        assert login.status_code == 200
        assert "HttpOnly" in login.headers["set-cookie"] and "SameSite=strict" in login.headers["set-cookie"]
        csrf = login.json()["csrf"]
        assert client.get("/api/status").status_code == 200
        command = {"action": "stop", "id": "all"}
        assert client.post("/api/control", json=command).status_code == 403
        assert (
            client.post(
                "/api/control",
                json=command,
                headers={"X-CSRF-Token": csrf, "Origin": "https://attacker.test"},
            ).status_code
            == 403
        )
        assert client.post("/api/control", json=command, headers={"X-CSRF-Token": csrf}).status_code == 200
        assert not client.get("/api/status").json()["settings"]["enabled"]
        assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
        assert client.get("/api/status").status_code == 401


async def test_discord_owner_gate_ignores_spoofed_admin_and_bot(kernel):
    hortator = kernel.store.get("bots", "hortator")
    for author in (
        SimpleNamespace(id=123, bot=False, display_name=".normal.man."),
        SimpleNamespace(id=int(OWNER_ID), bot=True),
    ):
        message = SimpleNamespace(
            author=author, webhook_id=None, channel=SimpleNamespace(id=456), guild=None, content="!stop all"
        )
        assert not owner_message(message)
        assert kernel.connector.scope(hortator, message) is None
        await kernel.connector.receive("hortator", message)
    assert kernel.store.get("settings", "global")["enabled"]


async def test_discord_command_bypasses_model_and_owner_control_stays_available(kernel, monkeypatch):
    reply = AsyncMock()
    monkeypatch.setattr(kernel.connector, "reply", reply)
    message = SimpleNamespace(
        author=SimpleNamespace(id=int(OWNER_ID), bot=False),
        webhook_id=None,
        channel=SimpleNamespace(id=123),
        guild=None,
        content="!stop all",
        attachments=[],
    )
    await kernel.connector.receive("hortator", message)
    assert not kernel.store.get("settings", "global")["enabled"]
    assert not kernel.store.rows("SELECT * FROM requests")
    message.content = "!start all"
    await kernel.connector.receive("hortator", message)
    assert kernel.store.get("settings", "global")["enabled"]
    assert reply.await_count == 2


async def test_application_identity_mismatch_rejected(kernel, owner, monkeypatch):
    await kernel.service.save(owner, "bots", "ada", {"application_id": "111111111111111111"})
    monkeypatch.setattr(
        kernel.connector,
        "validate_token",
        AsyncMock(return_value={"application_id": "222222222222222222", "user_id": "222222222222222222"}),
    )
    with pytest.raises(ControlError, match="different"):
        await kernel.service.credential(owner, "bots", "ada", "token", "wrong-app-token")
    assert not kernel.vault.get("bot/ada/token")


async def test_verified_public_identity_and_invite_survive_secret_redaction(kernel, owner, monkeypatch):
    application_id = "333333333333333333"
    token = "verified-discord-token-test-only"
    monkeypatch.setattr(
        kernel.connector,
        "validate_token",
        AsyncMock(return_value={"application_id": application_id, "user_id": application_id}),
    )
    await kernel.service.credential(owner, "bots", "ada", "token", token)
    public = kernel.service.public("bots", kernel.store.get("bots", "ada"))
    assert public["application_id"] == application_id
    assert f"client_id={application_id}" in public["invite_url"]
    assert public["token_configured"]
    assert token not in str(public)
    assert token not in str(kernel.store.events())
    assert (
        kernel.vault.redact(f"Bot {application_id} with token {token}")
        == f"Bot {application_id} with token [REDACTED]"
    )


async def test_channel_thread_guild_and_webhook_boundaries(kernel):
    from conftest import configured

    bot = configured(kernel)
    author = SimpleNamespace(id=123, bot=False)
    msg = SimpleNamespace(
        author=author,
        webhook_id=None,
        guild=SimpleNamespace(id=111111111111111111),
        channel=SimpleNamespace(id=222222222222222222, parent_id=None),
    )
    assert kernel.connector.scope(bot, msg) == "council"
    msg.channel = SimpleNamespace(id=999, parent_id=222222222222222222)
    assert kernel.connector.scope(bot, msg) == "council"
    msg.guild.id = 999
    assert kernel.connector.scope(bot, msg) is None
    msg.guild.id = 111111111111111111
    msg.webhook_id = 123
    assert kernel.connector.scope(bot, msg) is None
    msg.webhook_id = None
    msg.author.bot = True
    assert kernel.connector.scope(bot, msg) is None


async def test_optimistic_edits_and_duplicate_application_are_rejected(kernel, owner):
    original = kernel.store.get("profiles", "balanced")
    await kernel.service.save(owner, "profiles", "balanced", {"name": "Changed"})
    with pytest.raises(ControlError, match="another session"):
        await kernel.service.save(
            owner, "profiles", "balanced", {"revision": original["revision"], "name": "Old edit"}
        )
    await kernel.service.save(owner, "bots", "ada", {"application_id": "111111111111111111"})
    with pytest.raises(ControlError, match="unique"):
        await kernel.service.save(owner, "bots", "socrates", {"application_id": "111111111111111111"})
    with pytest.raises(ControlError):
        await kernel.service.save(owner, "settings", "global", {"owner_id": "someone-else"})


async def test_hortator_reporting_channel_cannot_share_council_context(kernel, owner):
    await kernel.service.save(owner, "rooms", "council", {"channel_id": "222222222222222222"})
    with pytest.raises(ControlError, match="separate"):
        await kernel.service.save(owner, "settings", "global", {"control_channel_id": "222222222222222222"})


async def test_deleted_identity_cannot_inherit_historical_memory(kernel, owner):
    bot = kernel.store.get("bots", "socrates")
    await kernel.service.delete(owner, "bots", "socrates")
    with pytest.raises(ControlError, match="retired"):
        await kernel.service.save(
            owner, "bots", "socrates", {k: v for k, v in bot.items() if k != "revision"}, create=True
        )
