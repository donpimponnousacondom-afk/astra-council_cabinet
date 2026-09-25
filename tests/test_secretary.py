import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from conftest import configured, ingest
from hortator.app import Kernel, create_app
from hortator.models import ControlError
from hortator.plugins import ToolContext
from support.provider import install_client
from support.runtime import completion, settle

CHANNEL = "222222222222222222"


def setup(k, **changes):
    bot = configured(k, interval_seconds=0, enabled_plugins=["secretary"], **changes)
    plugin = k.store.get("plugins", "secretary")
    k.store.put("plugins", {**plugin, "enabled": True})
    ingest(k)
    k.engine.transport = AsyncMock()
    k.engine.transport.send.return_value = "777777777777777777"
    return ToolContext(bot, CHANNEL, "origin")


def schedule(k, context, **args):
    return k.registry.secretary.mutate(
        {
            "operation": "schedule",
            "key": "usage",
            "message": "Remind root to check usage",
            "after_seconds": 3600,
            **args,
        },
        context.bot,
        context.channel_id,
        context.turn_id,
    )


def make_due(k, row, *, seconds_ago=1):
    k.store.execute(
        "UPDATE secretary_reminders SET due_at=? WHERE id=?", (time.time() - seconds_ago, row["id"])
    )


async def test_waiting_is_idle_then_one_fresh_turn_without_cadence_or_cooldown(kernel):
    context = setup(kernel, cooldown_seconds=600)
    row = schedule(kernel, context)
    assert not kernel.jobs.tasks and not kernel.engine.tasks
    await kernel.engine.tick()
    assert not kernel.engine.tasks
    kernel.engine.transport.typing.assert_not_awaited()
    make_due(kernel, row)
    kernel.store.execute("UPDATE contexts SET last_seen=999")
    kernel.store.execute(
        "UPDATE bot_runtime SET next_at=?,last_sent=? WHERE bot_id='ada'", (time.time() + 3600, time.time())
    )
    await install_client(kernel, lambda _: completion("Time to check usage!"))
    await kernel.engine.tick()
    await settle(kernel)
    turn = kernel.store.one("SELECT * FROM turns")
    assert turn["trigger"] == "scheduled_alarm" and turn["status"] == "sent"
    assert len(kernel.store.rows("SELECT * FROM outbox")) == 1
    request = json.loads(kernel.store.one("SELECT body FROM requests")["body"])
    assert json.dumps(request["messages"]).count("Remind root to check usage") == 1
    assert not kernel.store.one("SELECT 1 FROM events WHERE kind='delivery.cooldown'")
    assert not kernel.store.one("SELECT 1 FROM human_attention_claims")
    row = kernel.registry.secretary.receipt(kernel.registry.secretary.get(row["id"]))
    assert row["state"] == "fired" and row["last_turn_status"] == "sent"
    await kernel.engine.tick()
    assert len(kernel.store.rows("SELECT * FROM turns")) == 1


@pytest.mark.parametrize(
    "guard",
    ["bot", "council", "provider", "plugin", "grant", "scope", "gateway", "retry", "circuit", "budget"],
)
async def test_due_alarm_waits_behind_existing_gates_without_consuming(kernel, guard):
    context = setup(kernel)
    row = schedule(kernel, context)
    make_due(kernel, row)
    if guard in ("bot", "council", "provider", "plugin"):
        kind, identity = {
            "bot": ("bots", "ada"),
            "council": ("settings", "global"),
            "provider": ("providers", "openrouter"),
            "plugin": ("plugins", "secretary"),
        }[guard]
        entity = kernel.store.get(kind, identity)
        kernel.store.put(kind, {**entity, "enabled": False})
    elif guard in ("grant", "scope"):
        kernel.store.put("bots", {**context.bot, "enabled_plugins" if guard == "grant" else "room_ids": []})
    elif guard == "gateway":
        kernel.store.execute("UPDATE bot_runtime SET gateway_status='offline'")
    elif guard == "retry":
        kernel.store.execute("UPDATE bot_runtime SET retry_until=?", (time.time() + 600,))
    elif guard == "circuit":
        kernel.store.health("openrouter")
        kernel.store.execute("UPDATE provider_health SET circuit_until=?", (time.time() + 600,))
    else:
        kernel.engine.budget_error = lambda _: "Turn budget reached"
    await kernel.engine.tick()
    assert not kernel.engine.tasks
    assert kernel.registry.secretary.get(row["id"])["state"] == "scheduled"
    assert kernel.registry.secretary.get(row["id"])["last_turn_id"] is None


async def test_human_attention_has_priority_and_never_consumes_waiting_alarm(kernel):
    context = setup(kernel)
    row = schedule(kernel, context)
    make_due(kernel, row)
    kernel.store.execute(
        "UPDATE messages SET addressing=?",
        (
            json.dumps(
                {"author_kind": "human", "live": True, "targets": [{"bot_id": "ada", "via": ["mention"]}]}
            ),
        ),
    )
    await install_client(kernel, lambda _: completion("Hello!"))
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.registry.secretary.get(row["id"])["state"] == "scheduled"
    assert "scheduled_alarm" not in kernel.store.one("SELECT body FROM requests")["body"]
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.registry.secretary.get(row["id"])["state"] == "fired"


async def test_repeats_coalesce_snooze_rearms_and_cancel_stops(kernel):
    context = setup(kernel)
    secretary = kernel.registry.secretary
    row = schedule(kernel, context, repeat_seconds=3600)
    make_due(kernel, row, seconds_ago=86400)
    await install_client(kernel, lambda _: completion())
    now = time.time()
    await kernel.engine.tick()
    await settle(kernel)
    saved = secretary.get(row["id"])
    assert saved["state"] == "scheduled" and saved["due_at"] >= now + 3600
    await kernel.engine.tick()
    assert len(kernel.store.rows("SELECT * FROM turns")) == 1
    result = secretary.mutate(
        {"operation": "snooze", "reminder_id": row["id"], "after_seconds": 7200},
        context.bot,
        CHANNEL,
        "snooze",
    )
    assert result["repeat_seconds"] == 3600
    assert secretary.get(row["id"])["due_at"] >= now + 7200
    secretary.mutate(
        {"operation": "update", "reminder_id": row["id"], "message": "Edited reminder", "repeat_seconds": 0},
        context.bot,
        CHANNEL,
        "edit",
    )
    make_due(kernel, row)
    await kernel.engine.tick()
    await settle(kernel)
    assert secretary.get(row["id"])["state"] == "fired"
    result = secretary.mutate(
        {"operation": "snooze", "reminder_id": row["id"], "after_seconds": 60}, context.bot, CHANNEL, "snooze"
    )
    assert result["state"] == "scheduled"
    # Raising the minimum must never prevent cancellation of an older repeat.
    kernel.store.put(
        "plugins", {**kernel.store.get("plugins", "secretary"), "config": {"min_repeat_seconds": 86400}}
    )
    secretary.mutate({"operation": "cancel", "reminder_id": row["id"]}, context.bot, CHANNEL, "cancel")
    make_due(kernel, row)
    await kernel.engine.tick()
    assert secretary.get(row["id"])["state"] == "cancelled"
    assert len(kernel.store.rows("SELECT * FROM turns")) == 2


async def test_scope_idempotency_quota_and_usage(kernel):
    context = setup(kernel, plugin_config={"secretary": {"max_active_reminders": 1}})
    assert (await kernel.registry.call("secretary", {}, context, "usage"))["usage_only"]
    assert not kernel.registry.secretary.inventory()
    row = schedule(kernel, context)
    assert schedule(kernel, context)["id"] == row["id"]
    with pytest.raises(ControlError, match="already exists"):
        schedule(kernel, context, message="Different")
    with pytest.raises(ControlError, match="allowance"):
        schedule(kernel, context, key="second")
    for bot, channel in [(context.bot, "other"), ({**context.bot, "id": "loki"}, CHANNEL)]:
        with pytest.raises(ControlError, match="not found"):
            kernel.registry.secretary.mutate(
                {"operation": "cancel", "reminder_id": row["id"]}, bot, channel, "evil"
            )
    config = kernel.store.get("plugins", "secretary")
    kernel.store.put("plugins", {**config, "enabled": False})
    result = await kernel.registry.call("secretary", {"operation": "list"}, context, "revoked")
    assert "not enabled" in result["error"]


async def test_list_is_bounded_paged_and_usage_examples_are_actionable(kernel):
    from hortator.tool_feedback import errors_for

    context = setup(kernel)
    usage = (await kernel.registry.call("secretary", {}, context, "usage"))["usage"]
    assert not errors_for(usage["parameters"], usage["example"])
    for number in range(25):
        schedule(kernel, context, key=f"alarm-{number}", message="Long reminder " * 280)
    result = await kernel.registry.call("secretary", {"operation": "list"}, context, "list")
    assert len(result["reminders"]) == 20 and result["next_offset"] == 20 and result["total"] == 25
    assert len(json.dumps(result)) < 15000 and result["reminders"][0]["message_truncated"]
    second = await kernel.registry.call("secretary", {"operation": "list", "offset": 20}, context, "list2")
    assert len(second["reminders"]) == 5 and second["next_offset"] is None
    row = await kernel.registry.call(
        "secretary",
        {"operation": "status", "reminder_id": second["reminders"][0]["reminder_id"]},
        context,
        "status",
    )
    assert row["message"] == "Long reminder " * 280


@pytest.mark.parametrize(
    "extra",
    [
        {"at": "tomorrow"},
        {"at": "2030-01-01T12:00:00"},
        {"at": "2020-01-01T12:00:00+02:00"},
        {"after_seconds": 0},
        {"after_seconds": 1, "at": "2030-01-01T12:00:00+02:00"},
        {"after_seconds": 1, "repeat_seconds": 1},
        {"after_seconds": 1, "message": " "},
    ],
)
async def test_invalid_dates_intervals_and_messages_leave_no_rows(kernel, extra):
    context = setup(kernel)
    args = {"operation": "schedule", "key": "test", "message": "test", **extra}
    with pytest.raises(ControlError):
        kernel.registry.secretary.mutate(args, context.bot, CHANNEL, "test")
    assert not kernel.registry.secretary.inventory()


async def test_absolute_date_preserves_instant_and_reports_council_timezone(kernel):
    context = setup(kernel)
    target = datetime.now(timezone.utc) + timedelta(hours=2)
    row = kernel.registry.secretary.mutate(
        {"operation": "schedule", "key": "date", "message": "check", "at": target.isoformat()},
        context.bot,
        CHANNEL,
        "test",
    )
    assert datetime.fromisoformat(row["due_at"]).timestamp() == pytest.approx(target.timestamp())
    assert row["timezone"] == "Europe/Madrid"


async def test_reset_cancels_only_selected_bot_channel_and_cannot_rearm_old_alarm(kernel, owner):
    context = setup(kernel)
    row = schedule(kernel, context)
    # A different bot's independent alarm must survive.
    kernel.store.put("bots", {**context.bot, "id": "curie"})
    other = configured(kernel, bot_id="curie", enabled_plugins=["secretary"])
    ingest(kernel, bot_id="curie")
    second = schedule(kernel, ToolContext(other, CHANNEL, "second"))
    await kernel.service.control(
        owner,
        {
            "action": "reset_context",
            "kind": "bots",
            "id": "ada",
            "data": {"confirm_bot_id": "ada", "channel_id": CHANNEL},
        },
    )
    assert kernel.registry.secretary.get(row["id"])["state"] == "cancelled"
    assert kernel.registry.secretary.get(second["id"])["state"] == "scheduled"
    with pytest.raises(ControlError, match="clean slate"):
        kernel.registry.secretary.mutate(
            {"operation": "snooze", "reminder_id": row["id"], "after_seconds": 60},
            context.bot,
            CHANNEL,
            "new",
        )


async def test_claim_and_turn_are_atomic_and_stale_candidates_rejected(kernel, monkeypatch):
    context = setup(kernel)
    row = schedule(kernel, context)
    make_due(kernel, row)
    wake = kernel.registry.pending_wake(context.bot)
    original = kernel.engine.configuration
    calls = 0

    def broken(bot):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("test admission failure")
        return original(bot)

    monkeypatch.setattr(kernel.engine, "configuration", broken)
    with pytest.raises(RuntimeError, match="admission"):
        kernel.engine.launch(context.bot, CHANNEL, scheduled_wake=wake)
    assert kernel.registry.secretary.get(row["id"])["state"] == "scheduled"
    assert not kernel.store.one("SELECT 1 FROM turns")
    monkeypatch.setattr(kernel.engine, "configuration", original)
    kernel.registry.secretary.mutate(
        {"operation": "snooze", "reminder_id": row["id"], "after_seconds": 60}, context.bot, CHANNEL, "snooze"
    )
    with pytest.raises(ControlError, match="changed"):
        kernel.engine.launch(context.bot, CHANNEL, scheduled_wake=wake)


@pytest.mark.parametrize("outcome", ["silent", "failed", "cancelled"])
async def test_consumed_occurrence_is_not_replayed_on_failed_silent_or_preentry_cancelled_turn(
    kernel, outcome
):
    context = setup(kernel)
    row = schedule(kernel, context)
    make_due(kernel, row)
    if outcome == "failed":
        import httpx

        await install_client(kernel, lambda _: httpx.Response(400, json={"error": "bad request"}))
    else:
        await install_client(kernel, lambda _: completion(silence=True))
    await kernel.engine.tick()
    if outcome == "cancelled":
        kernel.engine.tasks["ada"].cancel()
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == outcome
    assert kernel.registry.secretary.get(row["id"])["state"] == "fired"
    await kernel.engine.tick()
    assert len(kernel.store.rows("SELECT * FROM turns")) == 1


async def test_scheduled_alarms_survive_kernel_restart_and_terminal_rows_expire(tmp_path):
    first = Kernel(tmp_path)
    async with first.lifetime():
        context = setup(first)
        row = schedule(first, context)
        fired = schedule(first, context, key="old")
        first.store.execute(
            "UPDATE secretary_reminders SET state='fired',updated_at=? WHERE id=?",
            (time.time() - 31 * 86400, fired["id"]),
        )
    second = Kernel(tmp_path)
    async with second.lifetime():
        assert second.registry.secretary.get(row["id"])["state"] == "scheduled"
        second.registry.secretary.clean()
        with pytest.raises(ControlError):
            second.registry.secretary.get(fired["id"])
        assert second.registry.secretary.get(row["id"])["state"] == "scheduled"


def test_owner_api_auth_csrf_and_stale_revision(tmp_path):
    app = create_app(tmp_path, start_runtime=False)
    with TestClient(app) as client:
        assert client.get("/api/secretary").status_code == 401
        login = client.post(
            "/api/auth/login", json={"password": (tmp_path / "initial-password").read_text().strip()}
        )

        def seed():
            context = setup(app.state.kernel)
            return schedule(app.state.kernel, context)

        row = client.portal.call(seed)
        path = f"/api/secretary/{row['id']}"
        body = {"operation": "snooze", "after_seconds": 7200, "revision": row["revision"]}
        assert client.post(path, json=body).status_code == 403
        headers = {"X-CSRF-Token": login.json()["csrf"]}
        response = client.post(path, json=body, headers=headers)
        assert response.status_code == 200
        assert response.json()["revision"] == row["revision"] + 1
        assert client.post(path, json=body, headers=headers).status_code == 409
        assert client.get("/api/secretary?bot_id=curie").json() == []
        result = client.get("/api/secretary?bot_id=ada").json()
        assert len(result) == 1 and result[0]["id"] == row["id"]
        due = result[0]["due_at"]

        def disable_plugin():
            store = app.state.kernel.store
            store.put("plugins", {**store.get("plugins", "secretary"), "enabled": False})

        client.portal.call(disable_plugin)
        update = {
            "operation": "update",
            "message": "The owner's corrected reminder",
            "repeat_seconds": 3600,
            "revision": result[0]["revision"],
        }
        response = client.post(path, json=update, headers=headers)
        assert response.status_code == 200
        updated = response.json()
        assert updated["message"] == update["message"]
        assert updated["repeat_seconds"] == 3600
        assert updated["state"] == "scheduled" and updated["due_at"] == due
        assert client.post(path, json=update, headers=headers).status_code == 409
        response = client.post(
            path,
            json={"operation": "cancel", "revision": updated["revision"]},
            headers=headers,
        )
        assert response.status_code == 200
        cancelled = response.json()
        assert cancelled["state"] == "cancelled"
        response = client.post(
            path, json={**update, "revision": cancelled["revision"], "repeat_seconds": 0}, headers=headers
        )
        assert response.status_code == 200
        assert response.json()["state"] == "cancelled"
        assert response.json()["repeat_seconds"] == 0
        assert response.json()["due_at"] == due
