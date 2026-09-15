import asyncio
import hashlib
import json

import pytest

from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.publishing import local_snapshot, validate_config


class Delivery:
    payloads = []
    failure = False
    gate = None

    def __init__(self, *args):
        pass

    async def deliver(self, payload):
        self.payloads.append(payload)
        if self.gate is not None:
            await self.gate.wait()
        if self.failure:
            raise ControlError("test network failure")
        manifest = {
            name: {k: v for k, v in entry.items() if k != "content"}
            for name, entry in payload["files"].items()
        }
        return {
            "version": 1,
            "delivered": True,
            "delivery_current": True,
            "current_revision": payload["revision"],
            **{key: payload[key] for key in ("bot_id", "slug", "revision", "job_id")},
            "remote_commit": "a" * 40,
            "manifest_hash": hashlib.sha256(
                json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            ).hexdigest(),
        }


@pytest.fixture
def publication(kernel):
    k = kernel
    bot = k.store.get("bots", "ada")
    bot.update(enabled=True, enabled_plugins=["document_site"])
    bot.pop("revision")
    k.store.put("bots", bot)
    plugin = k.store.get("plugins", "document_site")
    plugin.update(
        enabled=True,
        config={
            "auto_publish": True,
            "remote_enabled": True,
            "public_base_url": "https://council.example.test",
            "remote": {
                "host": "ssh.example.test",
                "username": "publisher",
                "web_root": "/srv/web",
                "state_root": "/srv/publishing",
                "debounce_seconds": 0,
            },
        },
    )
    plugin.pop("revision")
    k.store.put("plugins", plugin)
    k.vault.put("ssh/publishing/private_key", "synthetic-test-key-never-used-for-ssh")
    pins = k.directory / "ssh"
    pins.mkdir(exist_ok=True)
    (pins / "known_hosts").write_text("test pins used only with fake delivery")
    Delivery.payloads, Delivery.failure, Delivery.gate = [], False, None
    k.publishing.delivery_factory = Delivery
    context = ToolContext(bot, "channel", "turn-publishing")
    return k, context


async def document(publication, operation, **args):
    k, context = publication
    return await k.registry.documents.call({"operation": operation, "site": "summary", **args}, context, {})


async def test_automatic_save_snapshots_before_verified_delivery_and_exposes_current_url(publication):
    k, _ = publication
    await document(publication, "create")
    await document(publication, "write", path="index.html", content="first")
    assert (await document(publication, "status"))["public_url"] is None
    await k.publishing.tick()
    site = await document(publication, "status")
    assert site["public_url"] == "https://council.example.test/ada/summary/"
    assert site["delivery_current"] and site["synced_revision"] == 1
    assert site["sync"]["status"] == "delivered"
    payload = Delivery.payloads[0]
    assert len(payload["local_snapshot_commit"]) == 40
    history = k.directory / "site_history"
    assert (history / ".git").is_dir()
    assert (history / "ada/summary/index.html").read_text() == "first"
    assert not (history / "master.key").exists()
    assert not (history / "council.sqlite3").exists()
    await document(publication, "publish")
    await k.publishing.tick()
    assert len(Delivery.payloads) == 1
    await document(publication, "write", path="index.html", content="second")
    pending = await document(publication, "status")
    assert pending["public_url"] == site["public_url"]
    assert pending["synced_revision"] == 1 and not pending["delivery_current"]
    await k.publishing.tick()
    assert (await document(publication, "status"))["synced_revision"] == 2


async def test_network_failure_keeps_snapshot_and_retries_same_job_id(publication):
    k, _ = publication
    await document(publication, "create")
    await document(publication, "write", path="note.txt", content="retained")
    Delivery.failure = True
    await k.publishing.tick()
    failed = (await document(publication, "status"))["sync"]
    assert failed["status"] == "failed" and failed["attempts"] == 1
    assert failed["snapshot_commit"] and failed["retry_at"] > 0
    assert (await document(publication, "status"))["public_url"] is None
    Delivery.failure = False
    k.store.execute("UPDATE document_sync_queue SET retry_at=0")
    await k.publishing.tick()
    assert Delivery.payloads[0]["job_id"] == Delivery.payloads[1]["job_id"]
    assert Delivery.payloads[0]["local_snapshot_commit"] == Delivery.payloads[1]["local_snapshot_commit"]
    assert (await document(publication, "status"))["public_url"].endswith("/note.txt")


async def test_disabled_or_deleted_bot_never_uploads(publication):
    k, context = publication
    await document(publication, "create")
    await document(publication, "write", path="note.txt", content="x")
    bot = k.store.get("bots", "ada")
    bot.update(enabled=False)
    bot.pop("revision")
    k.store.put("bots", bot)
    await k.publishing.tick()
    assert not Delivery.payloads
    k.store.execute("DELETE FROM entities WHERE kind='bots' AND id='ada'")
    await k.publishing.tick()
    assert not Delivery.payloads


async def test_grant_revocation_cancels_active_delivery_and_keeps_uncertainty(publication):
    k, _ = publication
    await document(publication, "create")
    await document(publication, "write", path="note.txt", content="x")
    Delivery.gate = asyncio.Event()
    task = asyncio.create_task(k.publishing.tick())
    for _ in range(100):
        if Delivery.payloads:
            break
        await asyncio.sleep(0.01)
    assert Delivery.payloads
    bot = k.store.get("bots", "ada")
    bot.update(enabled_plugins=[])
    bot.pop("revision")
    k.store.put("bots", bot)
    await task
    state = (await document(publication, "status"))["sync"]
    assert state["status"] == "failed"
    assert "grant or destination change" in state["last_error"]
    assert state["delivered_at"] is None


async def test_mismatched_receipt_never_claims_delivery(publication):
    k, _ = publication

    class WrongReceipt(Delivery):
        async def deliver(self, payload):
            receipt = await super().deliver(payload)
            receipt["revision"] += 1
            return receipt

    k.publishing.delivery_factory = WrongReceipt
    await document(publication, "create")
    await document(publication, "write", path="note.txt", content="x")
    await k.publishing.tick()
    assert (await document(publication, "status"))["public_url"] is None
    assert "does not match" in (await document(publication, "status"))["sync"]["last_error"]


@pytest.mark.parametrize(
    "config",
    [
        {"remote_enabled": "yes"},
        {"auto_publish": 1},
        {"remote": {"port": "22"}},
        {"remote": {"web_root": "/srv/web", "state_root": "/srv/web/history"}},
        {"remote": {"web_root": "/srv/../web"}},
        {"remote": {"host": "host;id"}},
        {"remote": {"username": "user@otherhost"}},
        {"remote_enabled": True},
    ],
)
def test_configuration_rejects_unsafe_or_incomplete_destinations(config):
    with pytest.raises(ControlError):
        validate_config(config)


def test_snapshot_job_path_cannot_escape(tmp_path):
    with pytest.raises(ControlError):
        local_snapshot(tmp_path, {"bot_id": "ada", "slug": "site", "job_id": "../outside"})
    assert not (tmp_path / "site_history").exists()


def test_standalone_receiver_retains_remote_host_syntax_compatibility():
    import ast
    from pathlib import Path

    from hortator import publishing_receiver

    source = Path(publishing_receiver.__file__).read_text()
    ast.parse(source, feature_version=(3, 10))
