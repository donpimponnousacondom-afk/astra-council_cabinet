from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hortator import discord_gateway
from hortator.discord_gateway import CouncilClient


@pytest.fixture
def reporting(kernel, monkeypatch):
    manager = kernel.connector
    clock = [1000.0]
    monkeypatch.setattr(discord_gateway, "time", SimpleNamespace(time=lambda: clock[0]))
    settings = kernel.store.get("settings", "global")
    settings.update(control_guild_id="111111111111111111", control_channel_id="222222222222222222")
    kernel.store.put("settings", settings)
    channel = SimpleNamespace(guild=SimpleNamespace(id=int(settings["control_guild_id"])))
    manager.clients["hortator"] = SimpleNamespace(is_ready=lambda: True, get_channel=lambda _: channel)
    manager.reply = AsyncMock()
    manager.notification_cursor = kernel.store.emit("test.reporting_start")["seq"]
    return manager, clock


@pytest.mark.parametrize("separate_polls", [False, True])
async def test_brief_resume_is_recorded_without_chat_noise(kernel, reporting, separate_polls):
    manager, clock = reporting
    manager.gateway("ada", "reconnecting", source="disconnect_callback", close_code=1000)
    if separate_polls:
        await manager.notifications()
    clock[0] += 2
    manager.gateway("ada", "online", source="resumed")
    await manager.notifications()
    manager.reply.assert_not_awaited()
    assert not manager.pending_reconnects
    reconnect, recovered = sorted(
        (e for e in kernel.store.events(bot_id="ada") if e["kind"].startswith("discord.")),
        key=lambda e: e["seq"],
    )
    assert reconnect["data"]["close_code"] == 1000
    assert reconnect["data"]["reason"] == "Discord did not supply a disconnect reason"
    assert "error" not in reconnect["data"]
    assert recovered["data"]["source"] == "resumed"
    assert recovered["data"]["reconnect_duration_ms"] == 2000


async def test_sustained_outage_reports_cause_then_recovery_without_startup_throttle(kernel, reporting):
    manager, clock = reporting
    manager.gateway("ada", "online", source="ready")
    await manager.notifications()
    assert manager.reply.await_count == 1
    clock[0] += 1
    started = clock[0]
    manager.gateway("ada", "reconnecting", source="disconnect_callback")
    await manager.notifications()
    clock[0] += 20
    manager.gateway("ada", "reconnecting", "Heartbeat is unhealthy", source="heartbeat_watchdog")
    await manager.notifications()
    assert manager.reply.await_count == 1
    clock[0] = started + 31
    await manager.notifications()
    assert manager.reply.await_count == 2
    outage = manager.reply.await_args.args[1]
    assert "interrupted for 31.0s" in outage and "Heartbeat is unhealthy" in outage
    assert manager.pending_reconnects["ada"]["data"]["reconnect_started_at"] == started
    await manager.notifications()
    assert manager.reply.await_count == 2
    clock[0] += 1
    manager.gateway("ada", "online", source="resumed")
    await manager.notifications()
    assert manager.reply.await_count == 3
    assert "restored after 32.0s" in manager.reply.await_args.args[1]
    assert not manager.pending_reconnects


async def test_outage_grace_does_not_delay_failures_or_leak_secrets(kernel, reporting):
    manager, _ = reporting
    secret = "fake-notification-secret-only"
    kernel.vault.put("provider/openrouter/api_key", secret)
    manager.gateway("ada", "reconnecting", f"Disconnected with {secret}")
    kernel.store.emit("provider.failure", {"provider_id": "openrouter", "error": f"Failed: {secret}"})
    await manager.notifications()
    assert manager.reply.await_count == 1
    assert "[provider.failure]" in manager.reply.await_args.args[1]
    assert secret not in str(manager.reply.await_args)
    manager.gateway("ada", "failed", f"Invalid session: {secret}")
    await manager.notifications()
    assert manager.reply.await_count == 2
    assert "[discord.failed]" in manager.reply.await_args.args[1]
    assert secret not in str(manager.reply.await_args_list)
    assert secret not in str(kernel.store.events())
    assert not manager.pending_reconnects


async def test_disabled_notifications_and_offline_clear_pending_outages(kernel, reporting):
    manager, clock = reporting
    manager.gateway("ada", "reconnecting")
    await manager.notifications()
    settings = kernel.store.get("settings", "global")
    settings["incident_notifications"] = False
    kernel.store.put("settings", settings)
    clock[0] += 31
    await manager.notifications()
    assert not manager.pending_reconnects
    settings["incident_notifications"] = True
    kernel.store.put("settings", settings)
    await manager.notifications()
    manager.reply.assert_not_awaited()
    manager.gateway("ada", "offline")
    manager.gateway("ada", "reconnecting")
    await manager.notifications()
    manager.gateway("ada", "offline")
    clock[0] += 31
    await manager.notifications()
    assert not manager.pending_reconnects
    manager.reply.assert_not_awaited()


async def test_intentional_client_close_does_not_report_network_failure(kernel):
    manager = kernel.connector
    client = CouncilClient(manager, "ada")
    await client.close()
    cursor = kernel.store.emit("test.client_closed")["seq"]
    await client.on_disconnect()
    assert kernel.store.events(after=cursor) == []
