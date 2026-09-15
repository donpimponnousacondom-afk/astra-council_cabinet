import pytest
from fastapi.testclient import TestClient

from hortator.app import create_app
from scripts.site_preview_fixture import seed_site_preview, HTML


@pytest.fixture
def site_client(tmp_path):
    app = create_app(tmp_path, start_runtime=False)
    with TestClient(app) as client:
        client.portal.call(seed_site_preview, app.state.kernel)
        yield client, tmp_path


def login(client, path):
    return client.post("/api/auth/login", json={"password": (path / "initial-password").read_text().strip()})


def test_only_published_snapshot_is_public_and_drafts_require_auth(site_client):
    client, path = site_client
    page = client.get("/sites/ada/sandbox-fixture/")
    assert page.status_code == 200 and page.text == HTML
    assert "PRIVATE DRAFT" not in page.text
    draft_url = "/api/documents/ada/sandbox-fixture/files/index.html"
    assert client.get(draft_url).status_code == 401
    assert client.get("/api/documents").status_code == 401
    unpublished = client.get("/sites/ada/unpublished-fixture/")
    assert unpublished.status_code in (400, 404)
    assert "NEVER PUBLISHED" not in unpublished.text
    assert login(client, path).status_code == 200
    draft = client.get(draft_url)
    assert draft.status_code == 200 and "PRIVATE DRAFT" in draft.text
    assert draft.headers["content-disposition"].startswith("attachment;")
    assert "Access-Control-Allow-Origin" not in draft.headers
    assert client.get("/sites/ada/sandbox-fixture/").text == HTML


def test_generated_html_and_assets_receive_sandbox_and_exact_site_csp(site_client):
    client, _ = site_client
    prefix = "http://testserver/sites/ada/sandbox-fixture/"
    for filename, mime in [
        ("index.html", "text/html"),
        ("style.css", "text/css"),
        ("main.js", "javascript"),
        ("data.json", "application/json"),
        ("pixel.svg", "image/svg+xml"),
    ]:
        response = client.get("/sites/ada/sandbox-fixture/" + filename)
        assert response.status_code == 200
        assert mime in response.headers["content-type"]
        csp = response.headers["content-security-policy"]
        directives = {part.strip().split(" ", 1)[0]: part.strip() for part in csp.split(";") if part.strip()}
        assert directives["sandbox"] == "sandbox allow-scripts"
        assert "default-src 'none'" in csp
        for kind in ("script-src", "style-src", "img-src", "font-src", "media-src", "connect-src"):
            assert prefix in directives[kind]
            assert "'self'" not in directives[kind]
            assert "*" not in directives[kind]
        assert "object-src 'none'" in csp and "frame-src 'none'" in csp
        assert "base-uri 'none'" in csp and "form-action 'none'" in csp
        assert response.headers["access-control-allow-origin"] == "*"
        assert "access-control-allow-credentials" not in response.headers
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "DENY"


def test_site_cors_never_extends_to_authenticated_api(site_client):
    client, path = site_client
    login(client, path)
    response = client.get("/api/auth/session", headers={"Origin": "null"})
    assert "access-control-allow-origin" not in response.headers
    assert "sandbox" not in response.headers["content-security-policy"]
    asset = client.get("/sites/ada/sandbox-fixture/data.json", headers={"Origin": "null"})
    assert asset.headers["access-control-allow-origin"] == "*"


@pytest.mark.parametrize(
    "suffix", ["%2e%2e/%2e%2e/council.sqlite3", "%2Fetc%2Fpasswd", "..%5Cmaster.key", "missing.html"]
)
def test_site_path_cannot_escape_manifest_or_return_spa(site_client, suffix):
    client, _ = site_client
    response = client.get("/sites/ada/sandbox-fixture/" + suffix)
    assert response.status_code in (400, 404)
    assert "SQLite format 3" not in response.text
    assert "Published sandbox fixture" not in response.text
    assert "access-control-allow-origin" not in response.headers
