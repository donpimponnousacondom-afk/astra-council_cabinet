import io
import json

import httpx
import pytest

from conftest import configured
from hortator.console import OperationalConsole
from hortator.http_evidence import headers_evidence
from hortator.plugins import ToolContext
from support.web_search import setup, call as search_call


@pytest.mark.parametrize(
    "status",
    [
        200,
        201,
        202,
        204,
        206,
        300,
        301,
        302,
        303,
        304,
        307,
        308,
        400,
        401,
        403,
        404,
        408,
        419,
        429,
        500,
        501,
        502,
        503,
        599,
    ],
)
async def test_fetch_status_headers_body_and_paged_evidence(kernel, monkeypatch, status):
    import hortator.plugins as plugin

    bot = configured(kernel, enabled_plugins=["web_fetch"])
    context = ToolContext(bot, "channel", "turn")
    payload = "" if status == 204 else "<p>actual upstream response</p>" + "x" * 5000
    url = "https://example.com/page?q=" + ("y" * 3000) + "&page=2#section"

    class Response:
        def __init__(self):
            self.status = status
            self.reason = "Fixture upstream reason"
            self.url = url
            self.headers = {"Content-Type": "text/html", "Retry-After": "12", "X-Request-ID": "upstream-id"}
            self.content = self

        async def iter_chunked(self, size):
            yield payload.encode()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        def __init__(self, **kwargs):
            self.connector = kwargs["connector"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.connector.close()

        def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(plugin.aiohttp, "ClientSession", Session)
    result = await kernel.registry.call("web_fetch", {"url": url}, context, "call-one")
    evidence = result["http_response"]
    assert evidence["http_status"] == status and evidence["http_reason"] == "Fixture upstream reason"
    assert {"name": "Retry-After", "value": "12"} in evidence["response_headers"]
    assert evidence["url"] == url
    raw = await kernel.registry.call("web_fetch", evidence["read_response"], context, "call-read")
    original = json.loads(raw["text"])
    assert original["body"] == payload and original["http_status"] == status
    assert "ControlError" not in original["body"]
    if status not in {301, 302, 303, 307, 308}:
        assert result["status"] == "ready"
        later = ToolContext(bot, "channel", "later-turn")
        page = await kernel.registry.call(
            "web_fetch", {"operation": "read_response", "document_id": result["document_id"]}, later, "later"
        )
        assert json.loads(page["text"])["body"] == payload
        foreign = await kernel.registry.call(
            "web_fetch",
            {"operation": "read_response", "document_id": result["document_id"]},
            ToolContext(bot, "other", "later"),
            "foreign",
        )
        assert foreign["ok"] is False
    event = kernel.store.one(
        "SELECT level,data FROM events WHERE kind='tool.completed' AND json_extract(data,'$.call_id')='call-one'"
    )
    assert event["level"] == ("warning" if status >= 400 or status in {301, 302, 303, 307, 308} else "info")
    assert json.loads(event["data"])["url"] == url
    if status == 404:
        with OperationalConsole(stream=io.StringIO(), keys=False, color=False) as console:
            console.bind(kernel)
            stored = next(
                e
                for e in kernel.store.events()
                if e["kind"] == "tool.completed" and e["data"].get("call_id") == "call-one"
            )
            original = console.evidence_text({**stored, "scope": "tools"})
            assert "Original HTTP response" in original
            assert payload in original


async def test_search_202_challenge_has_body_and_brave_results(kernel, monkeypatch):
    challenge = '<form id="challenge-form">actual challenge text</form>'

    def handler(request):
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(202, text=challenge, headers={"Retry-After": "30"})
        return httpx.Response(200, json={"web": {"results": [{"url": "https://example.com/brave"}]}})

    context = setup(kernel, monkeypatch, handler, key="fixture-key")
    result = await search_call(kernel, context, engine="both")
    assert result["ok"] and result["partial"]
    ddg = next(s for s in result["engine_status"] if s["engine"] == "duckduckgo")
    assert ddg["http_status"] == 202 and ddg["category"] == "challenge"
    assert ddg["http_response"]["body"] == challenge
    read = await kernel.registry.call("web_search", ddg["http_response"]["read_response"], context, "read")
    assert json.loads(read["text"])["body"] == challenge
    assert result["results"][0]["url"] == "https://example.com/brave"


async def test_search_received_error_body_and_interrupted_body_are_retained(kernel, monkeypatch):
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"actual partial body"
            raise httpx.ReadError("fixture stream loss")

    def handler(request):
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(503, headers={"content-type": "text/plain"}, stream=Broken())
        return httpx.Response(419, json={"message": "actual expiry explanation"})

    context = setup(kernel, monkeypatch, handler, key="fixture-key")
    result = await search_call(kernel, context, engine="both")
    assert not result["ok"]
    brave, ddg = result["engine_status"]
    assert brave["http_response"]["http_status"] == 419
    assert json.loads(brave["http_response"]["body"]) == {"message": "actual expiry explanation"}
    assert ddg["http_response"]["http_status"] == 503
    assert ddg["http_response"]["body"] == "actual partial body"
    assert ddg["category"] == "transport" and not ddg["http_response"]["capture_complete"]


def test_response_headers_preserve_duplicates_but_not_credentials():
    result = headers_evidence([("X-ID", "one"), ("X-ID", "two"), ("Set-Cookie", "private-session")])
    assert result == [
        {"name": "X-ID", "value": "one"},
        {"name": "X-ID", "value": "two"},
        {"name": "Set-Cookie", "value": "[REDACTED]"},
    ]


async def test_console_field_order_full_urls_precision_and_evidence(kernel):
    url = "https://example.com/page?q=" + ("a" * 7000) + "&normal=value#anchor"
    stream = io.StringIO()
    with OperationalConsole(stream=stream, keys=False, color=False) as console:
        console.bind(kernel)
        stream.seek(0)
        stream.truncate()
        for level, kind in [("info", "tool.completed"), ("warning", "tool.failed"), ("error", "tool.failed")]:
            kernel.store.emit(
                kind,
                {
                    "name": "web_fetch",
                    "http_status": 404,
                    "duration_ms": 440.466889180243,
                    "call_id": "call-hidden-full-identifier",
                    "url": url,
                    "error": "upstream body retained",
                },
                bot_id="ada",
                level=level,
            )
        lines = [line for line in stream.getvalue().splitlines() if "name=web_fetch" in line]
        assert len(lines) == 3
        for line in lines:
            assert url in line and "440.5" in line and "440.466" not in line
            assert "call-hidden" not in line
            assert (
                line.index("name=web_fetch")
                < line.index("http_status=404")
                < line.index("duration_ms=440.5")
                < line.index("url=")
                < line.index("error=")
            )
        console.key("T")
        console.key("T")
        assert "call-hidden-full-identifier" in stream.getvalue()
        assert url in stream.getvalue()
