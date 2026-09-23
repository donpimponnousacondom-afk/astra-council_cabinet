from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import configured, ingest
from hortator.app import create_app
from hortator.models import OWNER_ID
from support.addressing import message, pair, receive
from support.provider import install_client
from support.runtime import completion, settle


def test_api_accepts_zero_timer_preserves_cooldown_and_rejects_negative(tmp_path):
    with TestClient(create_app(tmp_path, start_runtime=False)) as client:
        password = (tmp_path / "initial-password").read_text().strip()
        session = client.post("/api/auth/login", json={"password": password}).json()
        client.headers["X-CSRF-Token"] = session["csrf"]
        original = client.get("/api/config/bots/ada").json()
        command = {"action": "save", "kind": "bots", "id": "ada", "data": {"interval_seconds": 0}}
        saved = client.post("/api/control", json=command)
        assert saved.status_code == 200, saved.text
        actual = client.get("/api/config/bots/ada").json()
        assert actual["interval_seconds"] == 0
        assert actual["cooldown_seconds"] == original["cooldown_seconds"]
        assert actual["evaluate_when_idle"] == original["evaluate_when_idle"]
        for changes in ({"interval_seconds": -1}, {"interval_seconds": None}, {"cooldown_seconds": 0}):
            response = client.post("/api/control", json={**command, "data": changes})
            assert response.status_code == 400, response.text
        assert client.get("/api/config/bots/ada").json()["revision"] == actual["revision"]


@pytest.mark.parametrize("idle", [False, True])
async def test_zero_timer_observes_without_spending_on_ordinary_bot_or_historical_input(kernel, idle):
    ada, socrates = pair(kernel, interval_seconds=0)
    for bot in (ada, socrates):
        kernel.store.put("bots", {**bot, "evaluate_when_idle": idle, "allow_silence": False})
    kernel.store.execute("UPDATE bot_runtime SET next_at=0")
    await receive(kernel, message(555555555555555550))  # Ordinary human message.
    await receive(
        kernel, message(555555555555555551, author=ada["application_id"], bot=True, mentions=[socrates])
    )
    await receive(
        kernel, message(555555555555555552, author="777777777777777777", bot=True, mentions=[socrates])
    )
    await receive(kernel, message(555555555555555553, mentions=[socrates]), historical=True)
    await receive(kernel, message(555555555555555554, content="Quoted <@444444444444444444>"))
    before = kernel.store.rows("SELECT * FROM contexts")
    assert len(kernel.store.rows("SELECT * FROM messages")) >= 4
    for _ in range(3):
        await kernel.engine.tick()
        assert not kernel.engine.tasks
    assert not kernel.store.rows("SELECT * FROM requests")
    assert not kernel.store.rows("SELECT * FROM turns")
    assert kernel.store.rows("SELECT * FROM contexts") == before


async def test_positive_interval_restores_pending_new_input_without_resetting_context(kernel, owner):
    configured(kernel, interval_seconds=0, evaluate_when_idle=False)
    ingest(kernel)
    await kernel.engine.tick()
    assert not kernel.engine.tasks
    before = kernel.store.rows("SELECT * FROM messages")
    await kernel.service.save(owner, "bots", "ada", {"interval_seconds": 1})
    await install_client(kernel, lambda _: completion(silence=True))
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "silent"
    assert kernel.store.rows("SELECT * FROM messages") == before


async def test_zero_timer_does_not_automatically_retry_failed_human_request(kernel):
    _, socrates = pair(kernel, interval_seconds=0)
    await receive(kernel, message(555555555555555555, mentions=[socrates]))
    await install_client(kernel, lambda _: httpx.Response(400, json={"error": "Rejected test request"}))
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "failed"
    assert kernel.store.one("SELECT count(*) AS n FROM human_attention_claims")["n"] == 1
    kernel.store.execute("UPDATE bot_runtime SET next_at=0,retry_until=0")
    await kernel.engine.tick()
    assert not kernel.engine.tasks
    assert len(kernel.store.rows("SELECT * FROM turns")) == 1


async def test_explicit_compaction_still_selects_context_when_timer_is_off(kernel, owner):
    configured(kernel, interval_seconds=0, evaluate_when_idle=False)
    ingest(kernel)
    await install_client(kernel, lambda _: completion("The owner greeted the council."))
    await kernel.service.control(owner, {"action": "compact", "id": "ada"})
    await settle(kernel)
    turn = kernel.store.one("SELECT * FROM turns")
    assert turn["trigger"] == "manual_compaction"
    assert turn["status"] == "completed", turn["error"]


async def test_interval_command_zero_preserves_cooldown_and_positive_restores_both(kernel, monkeypatch):
    monkeypatch.setattr(kernel.connector, "reply", AsyncMock())
    msg = SimpleNamespace(
        author=SimpleNamespace(id=int(OWNER_ID), bot=False),
        webhook_id=None,
        channel=SimpleNamespace(id=123),
        guild=None,
        content="!interval ada 0",
        attachments=[],
    )
    original = kernel.store.get("bots", "ada")
    await kernel.connector.receive("hortator", msg)
    assert kernel.store.get("bots", "ada")["interval_seconds"] == 0
    assert kernel.store.get("bots", "ada")["cooldown_seconds"] == original["cooldown_seconds"]
    msg.content = "!interval ada 90"
    await kernel.connector.receive("hortator", msg)
    saved = kernel.store.get("bots", "ada")
    assert saved["interval_seconds"] == saved["cooldown_seconds"] == 90
    assert not kernel.store.rows("SELECT * FROM requests")
