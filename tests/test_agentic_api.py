import json
import subprocess
import sys

from fastapi.testclient import TestClient

from hortator.app import Kernel, create_app
from hortator.plugins import ToolContext
from scripts.agentic_fixture import seed_agentic


def test_private_inspection_authentication_paging_and_scope(tmp_path):
    app = create_app(tmp_path, start_runtime=False)
    with TestClient(app) as client:
        client.portal.call(seed_agentic, app.state.kernel)
        assert client.get("/api/agentic-tools").status_code == 401
        assert (
            client.get(
                "/api/agentic-tools/inspect",
                params={
                    "resource": "files",
                    "bot_id": "ada",
                    "channel_id": "222222222222222222",
                    "task": "browser-files",
                },
            ).status_code
            == 401
        )
        client.post("/api/auth/login", json={"password": (tmp_path / "initial-password").read_text().strip()})
        catalog = client.get("/api/agentic-tools?bot_id=ada&limit=5")
        assert catalog.status_code == 200
        body = catalog.json()
        assert len(body["workspaces"]) == len(body["documents"]) == 1
        assert "ready" in body["runner"] and body["export_limit_bytes"] == 8_000_000
        assert body["defaults"]["web_fetch"]["max_download_bytes"] == 1_000_000
        params = {
            "resource": "file",
            "bot_id": "ada",
            "channel_id": "222222222222222222",
            "task": "browser-files",
            "path": "notes.txt",
            "length": 100,
        }
        first = client.get("/api/agentic-tools/inspect", params=params).json()
        assert first["text"].startswith("PRIVATE WORKSPACE NOTES")
        assert first["next"]["offset"] == first["end_offset"]
        binary = client.get("/api/agentic-tools/inspect", params={**params, "path": "pixel.png"}).json()
        assert binary["mime"] == "image/png" and binary["width"] == binary["height"] == 1
        assert binary["export_limit_bytes"] == 8_000_000
        assert (
            client.get("/api/agentic-tools/inspect", params={**params, "channel_id": "other"}).status_code
            == 400
        )
        assert (
            client.get(
                "/api/agentic-tools/inspect", params={**params, "path": "../../master.key"}
            ).status_code
            == 400
        )
        assert (
            client.get("/api/agentic-tools/inspect", params={**params, "length": 100000}).status_code == 422
        )
        document = body["documents"][0]
        params = {
            "resource": "document",
            "bot_id": "ada",
            "channel_id": document["channel_id"],
            "id": document["document_id"],
            "length": 100,
        }
        first = client.get("/api/agentic-tools/inspect", params=params).json()
        assert first["text"].startswith("PUBLIC FIXTURE SNAPSHOT") and first["range"] == {
            "start": 0,
            "end": 100,
        }
        assert "Access-Control-Allow-Origin" not in catalog.headers
        csrf = client.get("/api/auth/session").json()["csrf"]
        saved = client.post(
            "/api/control",
            headers={"X-CSRF-Token": csrf},
            json={
                "action": "save",
                "kind": "plugins",
                "id": "web_fetch",
                "data": {"config": {"chunk_chars": 256}},
            },
        )
        assert saved.status_code == 200
        clamped = client.get("/api/agentic-tools/inspect", params={**params, "length": 8000}).json()
        assert clamped["range"]["end"] == 256


async def test_backup_restores_workspace_snapshot_evidence_and_interrupted_jobs(kernel, tmp_path):
    await seed_agentic(kernel)
    context = ToolContext(kernel.store.get("bots", "ada"), "222222222222222222", "fixture-tool-data")
    result = kernel.registry.evidence.record(context, "workspace", "test", {"text": "durable evidence"})
    job_id = "job_" + "a" * 32
    job_dir = kernel.directory / "jobs" / job_id
    job_dir.mkdir(mode=0o700)
    (job_dir / "meta.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "bot_id": "ada",
                "channel_id": context.channel_id,
                "turn_id": context.turn_id,
                "status": "running",
                "started_at": 1,
            }
        )
    )
    (job_dir / "stdout.log").write_text("saved job output")
    (job_dir / "stderr.log").write_text("")
    destination = tmp_path / "restored"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "hortator.cli",
            "--data-dir",
            str(kernel.directory),
            "backup",
            str(destination),
        ],
        capture_output=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    restored = Kernel(destination)
    try:
        file = restored.registry.workspaces.read_file("ada", context.channel_id, "browser-files", "notes.txt")
        assert file["content"].startswith("PRIVATE WORKSPACE NOTES")
        document = restored.registry.fetched_documents.list_documents()[0]
        assert restored.registry.fetched_documents.read_document(
            "ada", context.channel_id, document["document_id"]
        )["text"].startswith("PUBLIC FIXTURE SNAPSHOT")
        assert (
            restored.store.one("SELECT content FROM tool_result_evidence WHERE id=?", (result["result_id"],))[
                "content"
            ]
            == '{"text":"durable evidence"}'
        )
        job = next(job for job in restored.registry.agentic.runner.inspect() if job["job_id"] == job_id)
        assert job["status"] == "interrupted" and job["workspace_committed"] is False
        assert (destination / "jobs" / job_id / "stdout.log").read_text() == "saved job output"
        for folder in ("workspaces", "fetched_documents", "jobs"):
            assert (destination / folder).stat().st_mode & 0o777 == 0o700
            for file in (destination / folder).rglob("*"):
                assert file.stat().st_mode & 0o777 == (0o700 if file.is_dir() else 0o600)
    finally:
        await restored.close()
