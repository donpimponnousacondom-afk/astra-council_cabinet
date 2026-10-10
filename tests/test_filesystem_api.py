from fastapi.testclient import TestClient

from hortator.app import create_app
from support.agentic_runtime import grant


def test_operator_filesystem_inspection_without_channel_or_task(tmp_path):
    app = create_app(tmp_path, start_runtime=False)
    with TestClient(app) as client:
        kernel = app.state.kernel

        async def prepare():
            context = grant(kernel, "filesystem")
            for name in ("notes.txt", "sub/item.txt"):
                await kernel.registry.call(
                    "filesystem",
                    {"operation": "write", "path": name, "content": "operator preview"},
                    context,
                    name,
                )
            plugin = kernel.store.get("plugins", "filesystem")
            kernel.store.put("plugins", {**plugin, "enabled": False})

        client.portal.call(prepare)
        assert (
            client.get(
                "/api/agentic-tools/inspect", params={"resource": "filesystem_files", "bot_id": "ada"}
            ).status_code
            == 401
        )
        client.post("/api/auth/login", json={"password": (tmp_path / "initial-password").read_text().strip()})
        catalog = client.get("/api/agentic-tools?bot_id=ada").json()
        assert len(catalog["filesystems"]) == 1 and catalog["filesystems"][0]["usage"]["used_bytes"] == 32
        plugin = client.get("/api/config/plugins/filesystem").json()
        assert plugin["keyless"] and not plugin["enabled"]
        params = {"resource": "filesystem_files", "bot_id": "ada"}
        listing = client.get("/api/agentic-tools/inspect", params=params)
        assert listing.status_code == 200
        assert [entry["path"] for entry in listing.json()["files"]] == ["notes.txt", "sub"]
        nested = client.get("/api/agentic-tools/inspect", params={**params, "path": "sub"}).json()
        assert nested["files"][0]["path"] == "sub/item.txt"
        read = client.get(
            "/api/agentic-tools/inspect",
            params={**params, "resource": "filesystem_file", "path": "notes.txt", "length": 8},
        ).json()
        assert read["text"] == "operator" and read["next"]["offset"] == 8
        assert client.get("/api/agentic-tools/inspect", params={**params, "path": "../"}).status_code == 400
        assert (
            client.get("/api/agentic-tools/inspect", params={**params, "bot_id": "socrates"}).status_code
            == 400
        )
