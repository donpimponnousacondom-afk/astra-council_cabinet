import json
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from conftest import configured
from hortator import version
from hortator.app import create_app
from hortator.plugins import ToolContext


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-09-07T22:00:00+02:00",
            "GIT_COMMITTER_DATE": "2026-09-07T22:00:00+02:00",
        },
    ).stdout.strip()


@pytest.fixture
def source(tmp_path, monkeypatch):
    # This repository is an isolated fixture, never the shared working checkout.
    monkeypatch.delenv("HORTATOR_BUILD_INFO", raising=False)
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-b", "feat/build-test")
    git(root, "config", "user.name", "Version test")
    git(root, "config", "user.email", "version-test@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.hooksPath", str(tmp_path / "no-test-hooks"))
    (root / "module.py").write_text("VERSION = 1\n")
    (root / ".gitignore").write_text("data/\n")
    git(root, "add", "module.py", ".gitignore")
    git(root, "commit", "-m", "First build · native Unicode")
    return root


def test_git_source_identity_dates_dirty_and_detached_state(source):
    first = version.source_info(source)
    assert first["commit"] == git(source, "rev-parse", "HEAD")
    assert first["committed_at"] == "2026-09-07T20:00:00Z"
    assert first["commit_title"] == "First build · native Unicode"
    assert first["branch"] == "feat/build-test" and first["dirty"] is False
    (source / "data").mkdir()
    (source / "data" / "ignored-state").write_text("fixture")
    assert version.source_info(source)["dirty"] is False
    (source / "module.py").write_text("VERSION = 2\n")
    assert version.source_info(source)["dirty"] is True
    git(source, "add", "module.py")
    git(source, "commit", "-m", "Second build")
    assert version.source_info(source)["commit"] != first["commit"]
    git(source, "switch", "--detach", "HEAD")
    assert version.source_info(source)["branch"] is None
    assert version.source_info(source / "data")["commit"] is None


def test_running_api_version_is_authenticated_frozen_and_not_mutable(source, tmp_path, monkeypatch):
    read_source = version.source_info
    monkeypatch.setattr(version, "source_info", lambda: read_source(source))
    directory = tmp_path / "runtime"
    with TestClient(create_app(directory, start_runtime=False)) as client:
        assert client.get("/api/version").status_code == 401
        assert (
            client.post(
                "/api/auth/login", json={"password": (directory / "initial-password").read_text().strip()}
            ).status_code
            == 200
        )
        first = client.get("/api/version").json()
        assert first["dirty"] is False and first["started_at"].endswith("Z")
        (source / "module.py").write_text("VERSION = 3\n")
        git(source, "add", "module.py")
        git(source, "commit", "-m", "Checkout moved after process startup")
        assert first["commit"] != git(source, "rev-parse", "HEAD")
        assert client.get("/api/version").json() == first
        assert client.get("/api/status").json()["version"] == first
        inspected = client.app.state.kernel.service.inspect("version")
        inspected["commit_title"] = "Caller mutation"
        assert client.get("/api/version").json() == first


@pytest.mark.parametrize("manifest", [False, True])
def test_build_stamp_without_git_is_explicit_and_filters_extra_fields(tmp_path, monkeypatch, manifest):
    monkeypatch.setenv("PATH", "")
    stamp = {
        "commit": "a" * 40,
        "commit_title": "Release fixture",
        "committed_at": "2026-09-07T22:00:00+02:00",
        "branch": "feat/release-test",
        "dirty": False,
        "private_extra": "must not be published",
    }
    if manifest:
        monkeypatch.delenv("HORTATOR_BUILD_INFO", raising=False)
        (tmp_path / "hortator").mkdir()
        (tmp_path / "hortator" / "_build_info.json").write_text(json.dumps(stamp))
    else:
        monkeypatch.setenv("HORTATOR_BUILD_INFO", json.dumps(stamp))
    result = version.source_info(tmp_path)
    assert result["commit"] == stamp["commit"] and result["provenance"] == "build"
    assert result["committed_at"] == "2026-09-07T20:00:00Z"
    assert "private_extra" not in result


@pytest.mark.parametrize("raw", ["", "not json", '{"commit":"bogus"}', '{"commit":null}', "[]"])
def test_missing_or_invalid_metadata_reports_unknown_without_breaking_startup(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HORTATOR_BUILD_INFO", raw)
    result = version.source_info(tmp_path)
    assert result["provenance"] == "unknown"
    assert result["commit"] is None and result["committed_at"] is None and result["dirty"] is None


async def test_version_inspection_requires_trusted_hortator_context(kernel):
    bot = configured(kernel, "hortator", enabled_plugins=["council_inspect"])
    result = await kernel.registry.call(
        "council_inspect",
        {"resource": "version"},
        ToolContext(bot, "channel", "turn", owner_verified=True),
        "version-call",
    )
    assert {key: value for key, value in result.items() if key != "result_id"} == kernel.service.version()
    assert kernel.store.one("SELECT id FROM tool_result_evidence WHERE id=?", (result["result_id"],))
    assert not kernel.store.rows("SELECT * FROM requests")
    denied = await kernel.registry.call(
        "council_inspect",
        {"resource": "version"},
        ToolContext(bot, "channel", "turn", owner_verified=False),
        "untrusted-call",
    )
    assert {key: value for key, value in denied.items() if key != "result_id"} == {
        "ok": False,
        "error": "ControlError: Tool is not enabled for this bot and trusted request",
    }
    assert kernel.store.one("SELECT id FROM tool_result_evidence WHERE id=?", (denied["result_id"],))
