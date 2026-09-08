"""Publishing integration with real local Git and a real receiver in temporary roots.

The HTTP check verifies the deployed filesystem's URL/asset tree using a local
test HTTP server. Apache rewrite/header behavior is separately checked live;
none of these tests contacts the production publishing host or a provider.
"""

import asyncio
import base64
import copy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Thread
from types import SimpleNamespace
from urllib.request import urlopen

import pytest

from hortator.app import Kernel
from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.publishing import SSHDelivery, local_snapshot
from hortator.publishing_receiver import Receiver


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


class LocalTransport:
    """Real receiver protocol; only the SSH network hop is substituted."""

    def __init__(self, receiver):
        self.receiver = receiver
        self.payloads = []
        self.before_transfer = None
        self.after_transfer = None
        self.entered = asyncio.Event()
        self.applied = asyncio.Event()
        self.lose_receipts = 0

    def factory(self, *args):
        return self

    async def deliver(self, payload):
        self.payloads.append(copy.deepcopy(payload))
        self.entered.set()
        if self.before_transfer is not None:
            await self.before_transfer.wait()
        receipt = await asyncio.to_thread(self.receiver.deploy, payload)
        self.applied.set()
        if self.after_transfer is not None:
            await self.after_transfer.wait()
        if self.lose_receipts:
            self.lose_receipts -= 1
            raise ControlError("Synthetic connection loss after the receiver applied the release")
        return receipt


@pytest.fixture
async def publication_integration(tmp_path, monkeypatch):
    monkeypatch.delenv("HORTATOR_MASTER_KEY", raising=False)
    if not shutil.which("git"):
        pytest.skip("Real Git is required for publishing integration")
    web = tmp_path / "remote-web"
    web.mkdir()
    (web / "index.html").write_text("Existing domain page")
    receiver = Receiver(web, tmp_path / "remote-state")
    transport = LocalTransport(receiver)
    kernel = Kernel(tmp_path / "local-data")
    bot = kernel.store.get("bots", "ada")
    bot.update(enabled=True, enabled_plugins=["document_site"])
    bot.pop("revision")
    kernel.store.put("bots", bot)
    plugin = kernel.store.get("plugins", "document_site")
    plugin.update(
        enabled=True,
        config={
            "auto_publish": True,
            "remote_enabled": True,
            "public_base_url": "https://council.example.test",
            "remote": {
                "host": "ssh.example.test",
                "username": "publisher",
                "web_root": str(web),
                "state_root": str(receiver.state),
                "debounce_seconds": 0,
            },
        },
    )
    plugin.pop("revision")
    kernel.store.put("plugins", plugin)
    kernel.vault.put("ssh/publishing/private_key", "synthetic-private-key-outside-snapshots")
    pins = kernel.directory / "ssh"
    pins.mkdir()
    (pins / "known_hosts").write_text("synthetic-pins-for-receiver-substitution-only")
    kernel.publishing.delivery_factory = transport.factory
    rig = SimpleNamespace(kernel=kernel, receiver=receiver, transport=transport, calls=0)
    try:
        yield rig
    finally:
        await rig.kernel.close()


async def document(rig, operation, *, site="summary", **arguments):
    rig.calls += 1
    bot = rig.kernel.store.get("bots", "ada")
    result = await rig.kernel.registry.call(
        "document_site",
        {"operation": operation, "site": site, **arguments},
        ToolContext(bot, "test-channel", "turn-integration"),
        f"tool-integration-{rig.calls}",
    )
    assert "error" not in result, result
    return result


def job(rig, revision=1):
    return rig.kernel.store.one(
        "SELECT * FROM document_sync_queue WHERE bot_id='ada' AND slug='summary' AND revision=?", (revision,)
    )


async def restart(rig, directory=None):
    target = directory or rig.kernel.directory
    await rig.kernel.close()
    rig.kernel = Kernel(target)
    rig.kernel.publishing.delivery_factory = rig.transport.factory
    # Exercise startup's recovery of an interrupted syncing row without
    # starting Discord or provider/scheduler tasks in this test environment.
    rig.kernel.publishing.start()
    await rig.kernel.publishing.close()


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


async def test_saved_split_assets_publish_through_both_git_snapshots_and_http_tree(publication_integration):
    rig = publication_integration
    await document(rig, "create", title="Split-asset report")
    assets = {
        "index.html": '<link rel="stylesheet" href="css/main.css"><script src="js/app.js"></script><img src="chart.svg">',
        "css/main.css": "body { color: #345678; }",
        "js/app.js": "document.body.dataset.loaded = 'yes';",
        "chart.svg": '<svg xmlns="http://www.w3.org/2000/svg"><circle r="12"/></svg>',
    }
    for path, content in assets.items():
        await document(rig, "write", path=path, content=content)
    assert not (await document(rig, "status"))["public_url"]
    await rig.kernel.publishing.tick()
    status = await document(rig, "status")
    assert status["delivery_current"] and status["synced_revision"] == 4
    assert status["public_url"] == "https://council.example.test/ada/summary/"
    assert [job(rig, revision)["status"] for revision in range(1, 5)] == [
        "superseded",
        "superseded",
        "superseded",
        "delivered",
    ]
    receipt = job(rig, 4)
    local = rig.kernel.directory / "site_history"
    assert receipt["snapshot_commit"] == git(local, "rev-parse", "HEAD")
    assert receipt["remote_commit"] == git(rig.receiver.history, "rev-parse", "HEAD")
    assert git(local, "rev-list", "--count", "HEAD") == "1"
    assert git(rig.receiver.history, "rev-list", "--count", "HEAD") == "1"
    payload = rig.transport.payloads[0]
    remote_metadata = next((rig.receiver.history / "deployments").glob("*.json"))
    recorded = json.loads(remote_metadata.read_text())
    assert recorded["local_snapshot_commit"] == receipt["snapshot_commit"]
    assert recorded["job_id"] == receipt["id"]
    assert recorded["turn_id"] == "turn-integration"
    assert set(payload["files"]) == set(assets)
    assert rig.receiver.audit()["drift_count"] == 0
    handler = partial(QuietHandler, directory=str(rig.receiver.web))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}/ada/summary/"
        for path, content in assets.items():
            with urlopen(base + path, timeout=3) as response:
                assert response.status == 200
                assert response.read().decode() == content
        with urlopen(base, timeout=3) as response:
            assert response.read().decode() == assets["index.html"]
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=3)
    assert (rig.receiver.web / "index.html").read_text() == "Existing domain page"
    tracked = git(local, "ls-files")
    assert all(name not in tracked for name in ("master.key", "council.sqlite3", "known_hosts"))
    assert "synthetic-private-key" not in git(local, "show", "HEAD")
    assert "synthetic-private-key" not in git(rig.receiver.history, "show", receipt["remote_commit"])


async def test_inflight_payload_is_immutable_while_newer_document_save_queues_next_revision(
    publication_integration,
):
    rig = publication_integration
    await document(rig, "create")
    await document(rig, "write", path="index.html", content="Original visible content")
    rig.transport.before_transfer = asyncio.Event()
    task = asyncio.create_task(rig.kernel.publishing.tick())
    try:
        await asyncio.wait_for(rig.transport.entered.wait(), 5)
        first_payload = copy.deepcopy(rig.transport.payloads[0])
        await document(rig, "write", path="index.html", content="A newer pending edit")
        assert job(rig, 1)["status"] == "syncing" and job(rig, 2)["status"] == "queued"
        assert rig.transport.payloads[0] == first_payload
        rig.transport.before_transfer.set()
        await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    status = await document(rig, "status")
    assert status["synced_revision"] == 1 and not status["delivery_current"]
    remote = rig.receiver.web / "ada/summary/index.html"
    assert remote.read_text() == "Original visible content"
    await rig.kernel.publishing.tick()
    assert (await document(rig, "status"))["delivery_current"]
    assert remote.read_text() == "A newer pending edit"
    assert rig.transport.payloads[0] == first_payload
    assert base64.b64decode(first_payload["files"]["index.html"]["content"]) == b"Original visible content"
    assert git(rig.receiver.history, "rev-list", "--count", "HEAD") == "2"


async def test_lost_receipt_restart_retries_the_same_local_and_remote_snapshots(publication_integration):
    rig = publication_integration
    await document(rig, "create")
    await document(rig, "write", path="index.html", content="Applied before connection loss")
    rig.transport.lose_receipts = 1
    await rig.kernel.publishing.tick()
    failed = job(rig)
    assert failed["status"] == "failed" and failed["attempts"] == 1
    assert (rig.receiver.web / "ada/summary/index.html").read_text() == "Applied before connection loss"
    assert not (await document(rig, "status"))["public_url"]
    # A hard process exit can leave syncing persisted. Startup must turn that
    # row back into a retry of its original job, not generate a new identity.
    rig.kernel.store.execute("UPDATE document_sync_queue SET status='syncing' WHERE id=?", (failed["id"],))
    await restart(rig)
    assert job(rig)["status"] == "queued"
    await rig.kernel.publishing.tick()
    delivered = job(rig)
    assert delivered["status"] == "delivered" and delivered["attempts"] == 2
    assert delivered["id"] == failed["id"]
    assert delivered["snapshot_commit"] == failed["snapshot_commit"]
    assert rig.transport.payloads[0] == rig.transport.payloads[1]
    assert git(rig.kernel.directory / "site_history", "rev-list", "--count", "HEAD") == "1"
    assert git(rig.receiver.history, "rev-list", "--count", "HEAD") == "1"
    assert (await document(rig, "status"))["delivery_current"]


async def test_failed_prepared_remote_job_survives_new_save_and_finishes_before_new_revision(
    publication_integration,
    monkeypatch,
):
    rig = publication_integration
    await document(rig, "create")
    await document(rig, "write", path="index.html", content="First revision")
    original = os.replace

    def fail_activation(source, destination):
        if Path(destination) == rig.receiver.web / "ada/summary":
            raise OSError("Synthetic remote crash before atomic activation")
        return original(source, destination)

    monkeypatch.setattr(os, "replace", fail_activation)
    await rig.kernel.publishing.tick()
    assert job(rig)["status"] == "failed"
    pending = json.loads((rig.receiver.metadata / "sites/ada/summary.json").read_text())["pending"]
    assert pending == job(rig)["id"]
    monkeypatch.setattr(os, "replace", original)
    await document(rig, "write", path="index.html", content="Newer revision after the crash")
    assert job(rig)["status"] == "failed", "Attempted jobs must retain their receiver recovery retry"
    rig.kernel.store.execute("UPDATE document_sync_queue SET retry_at=0")
    await rig.kernel.publishing.tick()
    assert job(rig)["status"] == "delivered"
    await rig.kernel.publishing.tick()
    assert job(rig, 2)["status"] == "delivered"
    assert (rig.receiver.web / "ada/summary/index.html").read_text() == "Newer revision after the crash"
    assert git(rig.receiver.history, "rev-list", "--count", "HEAD") == "2"


async def test_grant_revocation_after_remote_apply_keeps_uncertainty_and_can_reconcile(
    publication_integration,
):
    rig = publication_integration
    await document(rig, "create")
    await document(rig, "write", path="index.html", content="Remote applies while confirmation is in transit")
    rig.transport.after_transfer = asyncio.Event()
    task = asyncio.create_task(rig.kernel.publishing.tick())
    try:
        await asyncio.wait_for(rig.transport.applied.wait(), 5)
        bot = rig.kernel.store.get("bots", "ada")
        bot.update(enabled_plugins=[])
        bot.pop("revision")
        rig.kernel.store.put("bots", bot)
        await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert job(rig)["status"] == "failed"
    assert job(rig)["delivered_at"] is None
    assert (rig.receiver.web / "ada/summary/index.html").exists()
    assert "grant or destination change" in job(rig)["last_error"]
    bot = rig.kernel.store.get("bots", "ada")
    bot.update(enabled_plugins=["document_site"])
    bot.pop("revision")
    rig.kernel.store.put("bots", bot)
    rig.transport.after_transfer = None
    rig.kernel.store.execute("UPDATE document_sync_queue SET retry_at=0")
    await rig.kernel.publishing.tick()
    assert (await document(rig, "status"))["delivery_current"]
    assert git(rig.receiver.history, "rev-list", "--count", "HEAD") == "1"


async def test_backup_portably_restores_snapshot_history_identity_and_pending_receipt(
    publication_integration, tmp_path
):
    rig = publication_integration
    await document(rig, "create")
    await document(rig, "write", path="index.html", content="Portable publication")
    rig.transport.lose_receipts = 1
    await rig.kernel.publishing.tick()
    before = job(rig)
    source = rig.kernel.directory
    backup = tmp_path / "restored-on-another-computer"
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "hortator.cli", "--data-dir", str(source), "backup", str(backup)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "synthetic-private-key" not in result.stdout + result.stderr
    assert (backup / "site_history/.git").is_dir()
    assert git(backup / "site_history", "rev-parse", "HEAD") == before["snapshot_commit"]
    assert (backup / "site_history").stat().st_mode & 0o777 == 0o700
    assert (backup / "site_history/ada/summary/index.html").stat().st_mode & 0o777 == 0o600
    await restart(rig, backup)
    assert rig.kernel.vault.get("ssh/publishing/private_key") == "synthetic-private-key-outside-snapshots"
    assert (rig.kernel.directory / "ssh/known_hosts").read_bytes() == (
        source / "ssh/known_hosts"
    ).read_bytes()
    rig.kernel.store.execute("UPDATE document_sync_queue SET retry_at=0")
    await rig.kernel.publishing.tick()
    after = job(rig)
    assert after["status"] == "delivered"
    assert after["id"] == before["id"] and after["snapshot_commit"] == before["snapshot_commit"]
    assert git(backup / "site_history", "rev-list", "--count", "HEAD") == "1"
    assert git(rig.receiver.history, "rev-list", "--count", "HEAD") == "1"
    assert (await document(rig, "status"))["delivery_current"]


async def test_restored_older_pending_job_cannot_claim_currentness_over_a_newer_remote_release(
    publication_integration,
    tmp_path,
):
    rig = publication_integration
    await document(rig, "create")
    await document(rig, "write", path="index.html", content="Older lost receipt")
    rig.transport.lose_receipts = 1
    await rig.kernel.publishing.tick()
    backup = tmp_path / "old-pending-backup"
    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-m",
            "hortator.cli",
            "--data-dir",
            str(rig.kernel.directory),
            "backup",
            str(backup),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    await document(rig, "write", path="index.html", content="Newer remote release")
    rig.kernel.store.execute("UPDATE document_sync_queue SET retry_at=0")
    await rig.kernel.publishing.tick()  # Reconcile the older attempted job first.
    await rig.kernel.publishing.tick()
    assert job(rig, 2)["status"] == "delivered"
    await restart(rig, backup)
    rig.kernel.store.execute("UPDATE document_sync_queue SET retry_at=0")
    await rig.kernel.publishing.tick()
    status = await document(rig, "status")
    assert not status["delivery_current"], "The receiver explicitly reported that a newer revision is visible"
    assert status["sync"]["status"] == "failed"
    assert "Remote site is ahead" in status["sync"]["last_error"]
    assert job(rig)["remote_current_revision"] == 2
    assert job(rig)["remote_delivery_current"] == 0
    assert (rig.receiver.web / "ada/summary/index.html").read_text() == "Newer remote release"
    assert git(rig.receiver.history, "rev-list", "--count", "HEAD") == "2"


async def test_ssh_identity_is_ephemeral_and_requires_pinned_passwordless_transport(
    publication_integration,
    tmp_path,
    monkeypatch,
):
    rig = publication_integration
    binaries = tmp_path / "test-bin"
    binaries.mkdir()
    executable = binaries / "ssh"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """import json,sys
from pathlib import Path
args=sys.argv[1:]; identity=args[args.index('-i')+1]
key=Path(identity).read_text()
body=sys.stdin.buffer.read(10000)
print(json.dumps({'args':args,'identity_readable':bool(key),'body':body.decode()}))
"""
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
    remote = rig.kernel.store.get("plugins", "document_site")["config"]["remote"]
    transport = SSHDelivery(rig.kernel.directory, rig.kernel.vault, remote)
    raw = await transport.ssh(["python3", "receiver.py", "status"], b'{"version":1}', timeout=5)
    observed = json.loads(raw)
    assert observed["identity_readable"] and observed["body"] == '{"version":1}'
    assert b"synthetic-private-key" not in raw
    args = observed["args"]
    options = {
        args[index + 1].split("=", 1)[0]: args[index + 1].split("=", 1)[1]
        for index, argument in enumerate(args)
        if argument == "-o"
    }
    assert options["StrictHostKeyChecking"] == "yes"
    assert options["UserKnownHostsFile"] == str(rig.kernel.directory / "ssh/known_hosts")
    assert options["GlobalKnownHostsFile"] == "/dev/null"
    assert options["BatchMode"] == options["IdentitiesOnly"] == "yes"
    assert options["IdentityAgent"] == "none" and options["ForwardAgent"] == "no"
    assert options["PasswordAuthentication"] == options["KbdInteractiveAuthentication"] == "no"
    assert args[args.index("-F") + 1] == "/dev/null"
    assert not Path(args[args.index("-i") + 1]).exists(), (
        "Private key descriptor must close after the child exits"
    )
    (rig.kernel.directory / "ssh/known_hosts").unlink()
    with pytest.raises(ControlError, match="host pins"):
        await transport.ssh(["python3", "receiver.py", "status"])


def fake_ssh(tmp_path, monkeypatch, program):
    binaries = tmp_path / "bounded-test-bin"
    binaries.mkdir()
    executable = binaries / "ssh"
    executable.write_text(f"#!{sys.executable}\n" + program)
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])


@pytest.mark.parametrize("stream", [1, 2])
async def test_ssh_bounds_each_stream_and_terminates_a_flooding_child(
    publication_integration,
    tmp_path,
    monkeypatch,
    stream,
):
    rig = publication_integration
    pid_file = tmp_path / "flooding-child.pid"
    fake_ssh(
        tmp_path,
        monkeypatch,
        f"""import os
from pathlib import Path
Path({str(pid_file)!r}).write_text(str(os.getpid()))
while True: os.write({stream}, b'x'*65536)
""",
    )
    remote = rig.kernel.store.get("plugins", "document_site")["config"]["remote"]
    transport = SSHDelivery(rig.kernel.directory, rig.kernel.vault, remote)
    with pytest.raises(ControlError, match="output exceeded"):
        await transport.ssh(["receiver.py", "status"], timeout=5)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


async def test_ssh_timeout_reaps_the_child_process(publication_integration, tmp_path, monkeypatch):
    rig = publication_integration
    pid_file = tmp_path / "timed-out-child.pid"
    fake_ssh(
        tmp_path,
        monkeypatch,
        f"""import os,time
from pathlib import Path
Path({str(pid_file)!r}).write_text(str(os.getpid()))
time.sleep(30)
""",
    )
    remote = rig.kernel.store.get("plugins", "document_site")["config"]["remote"]
    transport = SSHDelivery(rig.kernel.directory, rig.kernel.vault, remote)
    with pytest.raises(TimeoutError):
        await transport.ssh(["receiver.py", "status"], timeout=0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


async def test_symlinked_pin_directory_fails_before_ssh_launch(publication_integration, tmp_path):
    rig = publication_integration
    pins = rig.kernel.directory / "ssh"
    outside = tmp_path / "unrelated-trust-directory"
    pins.rename(outside)
    pins.symlink_to(outside, target_is_directory=True)
    assert not rig.kernel.publishing.status()["configured"]
    remote = rig.kernel.store.get("plugins", "document_site")["config"]["remote"]
    transport = SSHDelivery(rig.kernel.directory, rig.kernel.vault, remote)
    with pytest.raises(ControlError, match="host pins"):
        await transport.ssh(["receiver.py", "status"])


@pytest.mark.parametrize("link", ["metadata", "git"])
async def test_local_snapshot_rejects_indirected_git_or_metadata_paths(
    publication_integration,
    tmp_path,
    link,
):
    rig = publication_integration
    await document(rig, "create")
    await document(rig, "write", path="index.html", content="Owned snapshot content")
    await rig.kernel.publishing.tick()
    history = rig.kernel.directory / "site_history"
    component = history / ("_snapshots" if link == "metadata" else ".git")
    outside = tmp_path / "unrelated-original-folder"
    component.rename(outside)
    component.symlink_to(outside, target_is_directory=True)
    before = {
        str(path.relative_to(outside)): path.read_bytes() for path in outside.rglob("*") if path.is_file()
    }
    await document(rig, "write", path="index.html", content="Do not write through the link")
    await rig.kernel.publishing.tick()
    assert job(rig, 2)["status"] == "failed"
    assert (
        "symbolic link" in job(rig, 2)["last_error"] or "owned local directory" in job(rig, 2)["last_error"]
    )
    assert {
        str(path.relative_to(outside)): path.read_bytes() for path in outside.rglob("*") if path.is_file()
    } == before
    assert (rig.receiver.web / "ada/summary/index.html").read_text() == "Owned snapshot content"


async def test_snapshot_git_ignores_an_unrelated_git_environment(
    publication_integration, tmp_path, monkeypatch
):
    rig = publication_integration
    await document(rig, "create")
    await document(rig, "write", path="index.html", content="First snapshot")
    await rig.kernel.publishing.tick()
    # The payload is built by the real worker; drop the receiver-only commit
    # field before invoking the same local snapshot contract in another root.
    payload = copy.deepcopy(rig.transport.payloads[0])
    payload.pop("local_snapshot_commit")
    unrelated = tmp_path / "unrelated-git-dir"
    unrelated.mkdir()
    sentinel = unrelated / "keep.txt"
    sentinel.write_text("untouched")
    monkeypatch.setenv("GIT_DIR", str(unrelated))
    monkeypatch.setenv("GIT_WORK_TREE", str(unrelated))
    monkeypatch.setenv("GIT_INDEX_FILE", str(unrelated / "index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(unrelated))
    destination = tmp_path / "independent-data"
    commit = await asyncio.to_thread(local_snapshot, destination, payload)
    assert len(commit) == 40
    assert list(unrelated.iterdir()) == [sentinel]
    assert sentinel.read_text() == "untouched"
    assert (destination / "site_history/ada/summary/index.html").read_text() == "First snapshot"
