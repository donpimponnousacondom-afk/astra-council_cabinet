import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import configured
from hortator.app import create_app
from hortator.models import ControlError
from hortator.provider import ProviderError
from test_console import output, read_all_evidence
from test_provider import call_args, install_client
from test_runtime import completion


@pytest.mark.parametrize("header_name", ["User-Agent", "user-agent", "USER-AGENT"])
async def test_user_agent_is_shared_by_discovery_generation_and_compaction_without_duplicates(
    kernel, owner, header_name
):
    bot = configured(kernel)
    provider = await kernel.service.save(
        owner,
        "providers",
        "openrouter",
        {"headers": {header_name: "Council-Client/1.0", "X-Title": "Preserved title"}},
    )
    observed = []

    def handler(request):
        assert request.headers.get_list("user-agent") == ["Council-Client/1.0"]
        assert request.headers["x-title"] == "Preserved title"
        observed.append(request.method)
        return (
            httpx.Response(200, json={"data": [{"id": "fixture-model"}]})
            if request.method == "GET"
            else completion("Answer")
        )

    await install_client(kernel, handler)
    await kernel.pool.probe(provider)
    await kernel.pool.complete(**call_args(kernel, bot))
    await kernel.pool.complete(**call_args(kernel, bot), purpose="compaction")
    assert observed == ["GET", "POST", "POST"]
    assert kernel.store.get("providers", "openrouter")["headers"] == {
        "User-Agent": "Council-Client/1.0",
        "X-Title": "Preserved title",
    }


async def test_blank_user_agent_restores_client_default_and_other_provider_is_unaffected(kernel, owner):
    configured(kernel)
    first = await kernel.service.save(
        owner, "providers", "openrouter", {"headers": {"User-Agent": "Custom/1.0"}}
    )
    other = {**first, "id": "other", "headers": {}}
    seen = []

    def handler(request):
        seen.append(request.headers.get_list("user-agent"))
        return httpx.Response(200, json={"data": []})

    await install_client(kernel, handler)
    default = kernel.pool.client.headers["user-agent"]
    await kernel.pool.probe(first)
    await kernel.pool.probe(other)
    cleared = await kernel.service.save(
        owner, "providers", "openrouter", {"headers": {"User-Agent": "   ", "X-Title": "Retained"}}
    )
    await kernel.pool.probe(cleared)
    assert seen == [["Custom/1.0"], [default], [default]]
    assert cleared["headers"] == {"X-Title": "Retained"}


@pytest.mark.parametrize(
    "headers",
    [
        {"User-Agent": "client\r\nAuthorization: injected"},
        {"User-Agent": "client\tvalue"},
        {"User-Agent": "client/é"},
        {"User-Agent": "x" * 1025},
        {"User-Agent": "one", "user-agent": "two"},
    ],
)
async def test_invalid_user_agent_is_rejected_when_saving_before_http_execution(kernel, owner, headers):
    before = kernel.store.get("providers", "openrouter")
    with pytest.raises(ControlError, match="User-Agent"):
        await kernel.service.save(owner, "providers", "openrouter", {"headers": headers})
    assert kernel.store.get("providers", "openrouter") == before


async def test_discovery_failure_records_both_statuses_and_private_safe_evidence_without_changing_health(
    kernel,
):
    configured(kernel)
    provider = kernel.store.get("providers", "openrouter")
    secret = "discovery-private-test-credential"
    kernel.vault.put("provider/openrouter/api_key", secret)
    kernel.pool.failure(provider, ProviderError("Existing completion failure", http_status=503))
    health = kernel.store.health(provider["id"])
    sent = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer " + secret
        sent.append(request)
        return httpx.Response(
            401,
            json={
                "error": {"message": "Unauthorized client " + secret, "api_key": secret},
                "message": "UNAUTHENTICATED",
            },
            headers={
                "server": "edge-fixture",
                "x-request-id": "support-reference",
                "set-cookie": "private-cookie",
                "authorization": "response-secret",
            },
        )

    await install_client(kernel, handler)
    with output() as console:
        console.bind(kernel)
        with pytest.raises(ProviderError) as raised:
            await kernel.pool.probe(provider)
        error = raised.value
        assert error.status == 502 and error.http_status == 401 and error.provider_fault is False
        assert "Provider returned HTTP 401" in str(error) and "Hortator API HTTP 502" in str(error)
        assert "Unauthorized client" in str(error) and secret not in str(error)
        console.key("P")
        console.key("P")
        read_all_evidence(console)
        text = console.stream.getvalue()
        assert "Provider returned HTTP 401" in text and "Hortator API HTTP 502" in text
        assert "UNAUTHENTICATED" in text and "support-reference" in text
        assert all(value not in text for value in (secret, "private-cookie", "response-secret"))
    event = kernel.store.events(limit=1)[0]
    assert event["kind"] == "provider.discovery_failed"
    assert event["data"]["api_status"] == 502 and event["data"]["upstream_status"] == 401
    assert set(event["data"]["response_headers"]) == {"content-type", "server", "x-request-id"}
    assert kernel.store.health(provider["id"]) == health
    assert len(sent) == 1 and not kernel.store.rows("SELECT * FROM requests")


@pytest.mark.parametrize(
    "problem", ["timeout", "missing_key", "html_challenge", "invalid_catalog", "error_envelope"]
)
async def test_discovery_reports_transport_setup_and_invalid_catalog_failures_honestly(kernel, problem):
    configured(kernel)
    provider = kernel.store.get("providers", "openrouter")
    if problem == "missing_key":
        provider["requires_key"] = True

    def handler(request):
        if problem == "timeout":
            raise httpx.ReadTimeout("Fixture timeout", request=request)
        if problem == "html_challenge":
            return httpx.Response(
                403, text="<html>Challenge " + "x" * 5000, headers={"cf-mitigated": "challenge"}
            )
        if problem == "error_envelope":
            return httpx.Response(200, json={"error": {"message": "Invalid client"}})
        return httpx.Response(200, json={"data": {"wrong": "shape"}})

    await install_client(kernel, handler)
    with pytest.raises(ProviderError) as raised:
        await kernel.pool.probe(provider)
    details = raised.value.details
    assert details["upstream_status"] == (
        None if problem in ("timeout", "missing_key") else 403 if problem == "html_challenge" else 200
    )
    assert details["api_status"] == 502
    if problem == "html_challenge":
        assert details["response_truncated"] and len(details["response_excerpt"]) == 2000
        assert details["response_headers"]["cf-mitigated"] == "challenge"
    assert kernel.store.events(limit=1)[0]["kind"] == "provider.discovery_failed"


def test_control_api_distinguishes_provider_401_from_dashboard_login_401(tmp_path):
    app = create_app(tmp_path, start_runtime=False)
    with TestClient(app) as client:
        command = {"action": "probe", "kind": "providers", "id": "openrouter"}
        assert client.post("/api/control", json=command).status_code == 401
        kernel = app.state.kernel

        async def setup():
            configured(kernel)
            await install_client(
                kernel,
                lambda request: httpx.Response(401, json={"error": {"message": "Client not accepted"}}),
            )

        client.portal.call(setup)
        password = (tmp_path / "initial-password").read_text().strip()
        login = client.post("/api/auth/login", json={"password": password})
        response = client.post("/api/control", json=command, headers={"X-CSRF-Token": login.json()["csrf"]})
        assert response.status_code == 502
        data = response.json()
        assert data["source"] == "provider" and data["upstream_status"] == 401 and data["api_status"] == 502
        assert data["details"]["provider_id"] == "openrouter"
        assert "Client not accepted" in data["error"]
        assert client.get("/api/status").status_code == 200
