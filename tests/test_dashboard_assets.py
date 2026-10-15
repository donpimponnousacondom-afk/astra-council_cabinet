from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hortator.app import create_app


@pytest.mark.parametrize("native_ico", [False, True])
def test_browser_favicon_probe_and_api_auth_are_independent(tmp_path, monkeypatch, native_ico):
    assets = tmp_path / "assets"
    assets.mkdir()
    svg = (Path(__file__).resolve().parents[1] / "web/public/favicon.svg").read_bytes()
    (assets / "favicon.svg").write_bytes(svg)
    if native_ico:
        (assets / "favicon.ico").write_bytes(b"native icon fixture")
    monkeypatch.setenv("HORTATOR_WEB_DIR", str(assets))

    with TestClient(create_app(tmp_path / "runtime", start_runtime=False)) as client:
        probe = client.get("/favicon.ico", follow_redirects=False)
        assert probe.status_code == (200 if native_ico else 308)
        if not native_ico:
            assert probe.headers["location"] == "/favicon.svg"
        icon = client.get("/favicon.ico")
        assert icon.status_code == 200
        assert icon.content == (b"native icon fixture" if native_ico else svg)
        if not native_ico:
            assert icon.headers["content-type"] == "image/svg+xml"
        assert client.head("/favicon.ico").status_code == 200
        assert client.get("/api/auth/session").status_code == 401
        assert client.get("/missing-asset.ico").status_code == 404
