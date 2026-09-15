import shutil
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient

from hortator.app import create_app
from hortator.models import ControlError, no_secrets
from hortator.security import Vault
from hortator.ssh_identity import ssh_identity
from hortator.store import Store


@pytest.fixture
def identity_vault(tmp_path, monkeypatch):
    monkeypatch.delenv("HORTATOR_MASTER_KEY", raising=False)
    directory = tmp_path / "source"
    store = Store(directory / "council.sqlite3")
    vault = Vault(store, directory)
    yield vault, directory
    store.close()


def cli(directory, *args):
    return subprocess.run(
        [sys.executable, "-m", "hortator.cli", "--data-dir", str(directory), *args],
        text=True,
        capture_output=True,
        timeout=15,
    )


def test_encrypted_identity_is_stable_and_has_a_working_key_pair(identity_vault):
    vault, directory = identity_vault
    first = ssh_identity(vault, "publishing", create=True)
    private = vault.get("ssh/publishing/private_key")
    ciphertext = vault.store.one("SELECT value FROM secrets WHERE scope='ssh/publishing/private_key'")[
        "value"
    ]
    assert first.created
    assert first.public_key.startswith("ssh-ed25519 ")
    assert first.public_key.endswith(" hortator-publishing")
    assert private.encode() not in ciphertext
    assert b"OPENSSH PRIVATE KEY" not in (directory / "council.sqlite3").read_bytes()
    assert vault.fernet.decrypt(ciphertext).decode() == private
    key = serialization.load_ssh_private_key(private.encode(), password=None)
    public = serialization.load_ssh_public_key(first.public_key.encode())
    public.verify(key.sign(b"Hortator SSH identity check"), b"Hortator SSH identity check")
    again = ssh_identity(vault, "publishing", create=True)
    assert not again.created
    assert again.public_key == first.public_key
    assert again.fingerprint == first.fingerprint
    assert vault.get("ssh/publishing/private_key") == private
    events = vault.store.events()
    assert len(events) == 1
    assert private not in str(events)
    assert vault.redact({"private_key": "unregistered-private-value"}) == {"private_key": "[REDACTED]"}
    with pytest.raises(ValueError):
        no_secrets({"private_key": private})


@pytest.mark.parametrize("name", ["../publishing", "Publishing", "", "a" * 49, "x\n", "a/b", "x x"])
def test_invalid_names_cannot_write_credentials(identity_vault, name):
    vault, _ = identity_vault
    with pytest.raises(ControlError, match="SSH identity name"):
        ssh_identity(vault, name, create=True)
    assert not vault.store.rows("SELECT * FROM secrets")


def test_lookup_and_corrupt_identity_never_create_or_replace_a_key(identity_vault):
    vault, _ = identity_vault
    with pytest.raises(ControlError, match="does not exist"):
        ssh_identity(vault, "publishing")
    vault.put("ssh/publishing/private_key", "invalid-test-key")
    with pytest.raises(ControlError, match="not replaced"):
        ssh_identity(vault, "publishing", create=True)
    assert vault.get("ssh/publishing/private_key") == "invalid-test-key"


def test_cli_and_backup_restore_only_expose_public_identity(identity_vault, tmp_path):
    vault, directory = identity_vault
    vault.put("provider/test/api_key", "synthetic-preserved-provider-secret")
    result = cli(directory, "ssh-key", "create")
    assert result.returncode == 0, result.stderr
    public = result.stdout.strip()
    assert public.startswith("ssh-ed25519 ")
    assert "Created identity: SHA256:" in result.stderr
    assert "PRIVATE KEY" not in result.stdout + result.stderr
    assert not vault.store.list("bots")  # The operator command must not seed/reconfigure a council.
    assert "synthetic-preserved-provider-secret" not in result.stdout + result.stderr
    repeated = cli(directory, "ssh-key", "create")
    assert repeated.returncode == 0
    assert repeated.stdout.strip() == public
    assert "Existing identity:" in repeated.stderr
    ssh_dir = directory / "ssh"
    ssh_dir.mkdir(mode=0o700)
    (ssh_dir / "publishing.pub").write_text(public + "\n")
    destination = tmp_path / "snapshot"
    backed_up = cli(directory, "backup", str(destination))
    assert backed_up.returncode == 0, backed_up.stderr
    restored = cli(destination, "ssh-key", "public")
    assert restored.returncode == 0, restored.stderr
    assert restored.stdout.strip() == public
    assert restored.stderr == repeated.stderr
    assert (destination / "ssh" / "publishing.pub").read_text() == public + "\n"
    assert (destination / "ssh" / "publishing.pub").stat().st_mode & 0o777 == 0o600
    assert (destination / "ssh").stat().st_mode & 0o777 == 0o700
    if shutil.which("ssh-keygen"):
        checked = subprocess.run(
            ["ssh-keygen", "-lf", str(ssh_dir / "publishing.pub")], capture_output=True, text=True, timeout=5
        )
        assert checked.returncode == 0
        fingerprint = repeated.stderr.strip().split()[-1]
        assert fingerprint in checked.stdout


def test_bootstrap_refuses_missing_database_or_key(tmp_path, monkeypatch):
    monkeypatch.delenv("HORTATOR_MASTER_KEY", raising=False)
    missing = tmp_path / "missing"
    assert cli(missing, "ssh-key", "create").returncode != 0
    assert not missing.exists()
    directory = tmp_path / "initialized"
    store = Store(directory / "council.sqlite3")
    try:
        result = cli(directory, "ssh-key", "create")
        assert result.returncode != 0
        assert "matching master key" in result.stderr
        assert not (directory / "master.key").exists()
    finally:
        store.close()


def test_bootstrap_refuses_wrong_key_without_writing(identity_vault, monkeypatch):
    from cryptography.fernet import Fernet

    vault, directory = identity_vault
    vault.put("provider/test/api_key", "synthetic-existing-secret")
    before = vault.store.rows("SELECT * FROM secrets")
    monkeypatch.setenv("HORTATOR_MASTER_KEY", Fernet.generate_key().decode())
    result = cli(directory, "ssh-key", "create")
    assert result.returncode != 0
    assert "Cannot unlock Hortator credentials" in result.stderr
    assert vault.store.rows("SELECT * FROM secrets") == before


def test_bootstrap_cannot_modify_credentials_under_a_running_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("HORTATOR_MASTER_KEY", raising=False)
    with TestClient(create_app(tmp_path, start_runtime=False)):
        result = cli(tmp_path, "ssh-key", "create")
        assert result.returncode != 0
        assert "Stop the runtime" in result.stderr
