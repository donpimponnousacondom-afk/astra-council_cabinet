import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from hortator.app import Kernel, create_app
from hortator.plugins import ToolContext


def test_second_runtime_cannot_claim_same_database(tmp_path):
    with TestClient(create_app(tmp_path, start_runtime=False)):
        with pytest.raises(RuntimeError, match="Another Hortator process"):
            with TestClient(create_app(tmp_path, start_runtime=False)):
                pass


async def test_live_backup_restores_credentials_memory_and_trajectory(kernel, tmp_path):
    source = kernel.directory
    kernel.vault.put("provider/openrouter/api_key", "backup-test-credential")
    bot = kernel.store.get("bots", "ada")
    await kernel.registry.memory(
        {"operation": "write", "key": "topic", "value": "Preserve this across restore"},
        ToolContext(bot, "channel", "turn"),
        {},
        "",
    )
    kernel.store.emit("test.backup_marker", {"bot": "ada"})
    artifacts = source / "artifacts"
    artifacts.mkdir(exist_ok=True)
    (artifacts / "test-attachment.txt").write_text("Owned test artifact")
    destination = tmp_path / "backup"
    result = subprocess.run(
        [sys.executable, "-m", "hortator.cli", "--data-dir", str(source), "backup", str(destination)],
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "backup-test-credential" not in result.stdout + result.stderr
    assert destination.stat().st_mode & 0o777 == 0o700
    assert (destination / "master.key").stat().st_mode & 0o777 == 0o600
    assert (destination / "council.sqlite3").stat().st_mode & 0o777 == 0o600
    restored = Kernel(destination)
    try:
        assert restored.vault.get("provider/openrouter/api_key") == "backup-test-credential"
        assert (
            restored.store.one("SELECT value FROM memories WHERE bot_id='ada'")["value"]
            == "Preserve this across restore"
        )
        assert any(e["kind"] == "test.backup_marker" for e in restored.store.events())
        assert (destination / "artifacts" / "test-attachment.txt").read_text() == "Owned test artifact"
        assert restored.vault.get("auth/password") == kernel.vault.get("auth/password")
    finally:
        await restored.close()
    # A backup must never overwrite a previous archive.
    again = subprocess.run(
        [sys.executable, "-m", "hortator.cli", "--data-dir", str(source), "backup", str(destination)],
        text=True,
        capture_output=True,
        timeout=15,
        env=os.environ.copy(),
    )
    assert again.returncode != 0
