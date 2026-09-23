import asyncio
import errno
import json
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import aiohttp
import discord
import pytest

from hortator import slash_commands as slash
from hortator.concurrency import cancel_and_wait
from test_provider import install_client
from test_runtime import completion, settle
from test_slash_commands import enable, interaction


def http_error(status, code=0):
    cls = discord.NotFound if status == 404 else discord.HTTPException
    return cls(NS(status=status, reason="Discord fixture response"), {"code": code, "message": "fixture"})


def receipt(item, *, loading=True, private=True):
    return NS(
        id=888888888888888888,
        flags=discord.MessageFlags._from_value((128 if loading else 0) | (64 if private else 0)),
        application_id=item.application_id,
        interaction_metadata=NS(id=item.id),
    )


@pytest.fixture(autouse=True)
def quick_retries(monkeypatch):
    configured = (slash.ACK_ATTEMPTS, slash.ACK_RETRY_DELAY, slash.ACK_SECONDS)
    monkeypatch.setattr(slash, "ACK_RETRY_DELAY", 0)
    monkeypatch.setattr(slash, "ACK_RECEIPT_DELAY", 0)
    return configured


def test_acknowledgement_retry_contract(quick_retries):
    assert quick_retries == (30, 0.090, 2.9)


def events(kernel, kind):
    return [
        {**row, "data": json.loads(row["data"])}
        for row in kernel.store.rows("SELECT * FROM events WHERE kind=? ORDER BY seq", (kind,))
    ]


@pytest.mark.parametrize("failures", [2, 4])
async def test_connection_failures_retry_ack_not_model_or_tools(kernel, failures):
    bot = enable(kernel)
    await install_client(kernel, lambda _: completion("done"))
    item = interaction(bot, private=False)
    item.response.defer.side_effect = (
        [OSError(errno.ECONNREFUSED, "refused")]
        + [aiohttp.ServerDisconnectedError() for _ in range(failures - 1)]
        + [None]
    )
    await kernel.connector.slash.receive("ada", item)
    await settle(kernel)
    assert item.response.defer.await_count == failures + 1
    assert all(
        c.kwargs == {"thinking": True, "ephemeral": False} for c in item.response.defer.await_args_list
    )
    assert kernel.store.one("SELECT count(*) AS n FROM requests")["n"] == 1
    assert kernel.store.one("SELECT status FROM slash_invocations")["status"] == "sent"
    retries = events(kernel, "discord.slash_ack_retry")
    assert [r["data"]["attempt"] for r in retries] == list(range(1, failures + 1))
    assert all(r["level"] == "warning" and r["bot_id"] == "ada" for r in retries)
    assert retries[0]["data"]["errno"] == errno.ECONNREFUSED
    await kernel.connector.slash.receive("ada", item)
    assert item.response.defer.await_count == failures + 1


async def test_lost_ack_response_recovered_by_get_after_local_deadline(kernel, monkeypatch):
    monkeypatch.setattr(slash, "ACK_SECONDS", 0.04)
    bot = enable(kernel)
    await install_client(kernel, lambda _: completion())
    item = interaction(bot)

    async def stalled(**kwargs):
        await asyncio.sleep(100)

    item.response.defer.side_effect = stalled
    item.original_response = AsyncMock(side_effect=[aiohttp.ServerDisconnectedError(), receipt(item)])
    await kernel.connector.slash.receive("ada", item)
    await settle(kernel)
    item.response.defer.assert_awaited_once()
    assert item.original_response.await_count == 2
    assert kernel.store.one("SELECT status,response_id FROM slash_invocations") == {
        "status": "sent",
        "response_id": str(receipt(item).id),
    }
    assert events(kernel, "discord.slash_ack_checking")[0]["data"]["error_origin"] == "local_deadline"
    assert events(kernel, "discord.slash_ack_recovered")
    assert len(kernel.store.rows("SELECT * FROM requests")) == 1


async def test_ack_can_complete_after_old_two_and_half_second_cutoff(kernel):
    item = interaction(enable(kernel))
    await install_client(kernel, lambda _: completion())

    async def slow_success(**kwargs):
        await asyncio.sleep(2.55)

    item.response.defer.side_effect = slow_success
    await kernel.connector.slash.receive("ada", item)
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM slash_invocations")["status"] == "sent"
    assert not events(kernel, "discord.slash_ack_checking")


async def test_receipt_read_timeouts_are_bounded_and_explicit(kernel, monkeypatch):
    monkeypatch.setattr(slash, "ACK_RECEIPT_SECONDS", 0.01)
    item = interaction(enable(kernel))
    item.response.defer.side_effect = http_error(400, 40060)

    async def stalled():
        await asyncio.sleep(100)

    item.original_response = AsyncMock(side_effect=stalled)
    await kernel.connector.slash.receive("ada", item)
    assert item.original_response.await_count == 3
    data = events(kernel, "discord.slash_ack_receipt_failed")[0]["data"]
    assert data["error_origin"] == "local_deadline"
    assert data["timeout_seconds"] == 0.01
    assert "receipt lookup deadline exceeded" in data["error"]
    assert not kernel.store.rows("SELECT * FROM requests")


async def test_already_acknowledged_40060_is_verified_before_start(kernel):
    bot = enable(kernel)
    await install_client(kernel, lambda _: completion())
    item = interaction(bot)
    item.response.defer.side_effect = [OSError("response lost"), http_error(400, 40060)]
    item.original_response = AsyncMock(return_value=receipt(item))
    await kernel.connector.slash.receive("ada", item)
    await settle(kernel)
    assert item.response.defer.await_count == 2
    item.original_response.assert_awaited_once()
    assert events(kernel, "discord.slash_ack_checking")[0]["data"]["discord_code"] == 40060
    assert kernel.store.one("SELECT status FROM slash_invocations")["status"] == "sent"


@pytest.mark.parametrize(
    "error", [http_error(401), http_error(403), http_error(404, 10062), ValueError("invalid local request")]
)
async def test_nontransient_refusal_never_retries_or_runs_provider(kernel, error):
    item = interaction(enable(kernel))
    item.response.defer.side_effect = error
    item.original_response = AsyncMock()
    await kernel.connector.slash.receive("ada", item)
    item.response.defer.assert_awaited_once()
    item.original_response.assert_not_awaited()
    assert not kernel.store.rows("SELECT * FROM requests")
    assert kernel.store.one("SELECT status,turn_id FROM slash_invocations") == {
        "status": "failed",
        "turn_id": None,
    }
    data = events(kernel, "discord.slash_ack_failed")[0]["data"]
    assert data["channel_id"] == str(item.channel_id)
    assert "no model or tool work was started" in data["reason"]


async def test_thirty_attempts_and_three_missing_receipts_stop_without_work(kernel):
    item = interaction(enable(kernel))
    item.response.defer.side_effect = OSError("unreachable")
    item.original_response = AsyncMock(side_effect=http_error(404, 10015))
    await kernel.connector.slash.receive("ada", item)
    assert item.response.defer.await_count == 30
    assert item.original_response.await_count == 3
    assert len(events(kernel, "discord.slash_ack_failed")) == 1
    assert not kernel.store.rows("SELECT * FROM requests")
    assert not kernel.store.rows("SELECT * FROM outbox")
    assert kernel.store.health("openrouter")["consecutive_failures"] == 0


async def test_stale_interaction_does_not_get_new_deadline(kernel):
    item = interaction(enable(kernel))
    item.created_at -= timedelta(seconds=4)
    await kernel.connector.slash.receive("ada", item)
    item.response.defer.assert_not_awaited()
    data = events(kernel, "discord.slash_ack_failed")[0]["data"]
    assert data["attempt"] == 0 and data["error_origin"] == "platform_deadline"
    assert data["interaction_age_ms"] >= 4000


@pytest.mark.parametrize("mismatch", ["visibility", "finished", "application", "interaction"])
async def test_receipt_must_match_loading_state_privacy_and_identity(kernel, mismatch):
    item = interaction(enable(kernel))
    item.response.defer.side_effect = http_error(400, 40060)
    message = receipt(item, loading=mismatch != "finished", private=mismatch != "visibility")
    if mismatch == "application":
        message.application_id = 111
    if mismatch == "interaction":
        message.interaction_metadata.id = 111
    item.original_response = AsyncMock(return_value=message)
    await kernel.connector.slash.receive("ada", item)
    assert events(kernel, "discord.slash_ack_receipt_mismatch")
    assert not kernel.store.rows("SELECT * FROM turns")
    item.edit_original_response.assert_not_awaited()


@pytest.mark.parametrize("phase", ["defer", "read", "wait"])
async def test_cancel_during_ack_recovery_propagates_and_stops_retries(kernel, monkeypatch, phase):
    monkeypatch.setattr(slash, "ACK_RETRY_DELAY", 30 if phase == "wait" else 0)
    monkeypatch.setattr(slash, "ACK_SECONDS", 60 if phase == "wait" else 2.9)
    item = interaction(enable(kernel))
    began = asyncio.Event()

    async def block(*args, **kwargs):
        began.set()
        await asyncio.sleep(100)

    async def fail(*args, **kwargs):
        began.set()
        raise OSError("response lost")

    item.response.defer.side_effect = block if phase == "defer" else fail
    item.original_response = AsyncMock(side_effect=block)
    if phase == "read":
        item.response.defer.side_effect = http_error(400, 40060)
    task = kernel.background.spawn(kernel.connector.slash.receive("ada", item), name="test-ack-cancel")
    await began.wait()
    await cancel_and_wait(task)
    assert task.cancelled()
    assert kernel.store.one("SELECT status FROM slash_invocations")["status"] == "cancelled"
    assert events(kernel, "discord.slash_ack_cancelled")
    assert not kernel.store.rows("SELECT * FROM requests")


async def test_ack_errors_keep_http_errno_and_causes_but_never_tokens(kernel):
    item = interaction(enable(kernel))
    kernel.vault.put("test/provider", "SYNTHETIC_PROVIDER_SECRET")
    nested = OSError(errno.ECONNRESET, item.token)
    error = aiohttp.ServerDisconnectedError("SYNTHETIC_PROVIDER_SECRET " + item.token)
    error.__cause__ = nested
    item.response.defer.side_effect = error
    item.original_response = AsyncMock(side_effect=http_error(403, 50001))
    await kernel.connector.slash.receive("ada", item)
    for table in ("events", "slash_invocations", "requests", "turns", "outbox"):
        encoded = json.dumps(kernel.store.rows(f"SELECT * FROM {table}"))
        assert item.token not in encoded and "SYNTHETIC_PROVIDER_SECRET" not in encoded
    data = events(kernel, "discord.slash_ack_retry")[0]["data"]
    assert data["errno"] == errno.ECONNRESET
    assert len(data["causes"]) == 2
    data = events(kernel, "discord.slash_ack_receipt_failed")[0]["data"]
    assert data["http_status"] == 403 and data["discord_code"] == 50001
