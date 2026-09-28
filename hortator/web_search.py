"""Bounded search engines with explicit provenance and preserved HTTP evidence."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from html.parser import HTMLParser
import json
import time
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import httpx

from .concurrency import task_group
from .models import ControlError
from .http_evidence import HTTPResponse, headers_evidence, record_response
from .tool_feedback import errors_for

MODES = ["auto", "brave", "duckduckgo", "both", "ollama"]
DEFAULTS = {
    "endpoint": "https://api.search.brave.com/res/v1/web/search",
    "count": 5,
    "engine": "auto",
    "ollama_provider_id": "",
}
DESCRIPTION = (
    "Search the web. Required: query. Optional engine: auto (Brave then DuckDuckGo on failure/empty), "
    "brave, duckduckgo (no key), both (Brave + DuckDuckGo), or ollama (direct search, no research agent). "
    "Ollama uses the operator-selected Ollama provider credential, independently of your model. "
    "If omitted, use the operator's default. "
    "count is results per engine, default 5, maximum 10. Example: "
    '{"query":"Python documentation","engine":"both","count":5}. '
    "Results are deduplicated with source engines; inspect engine_status and partial for failures. "
    "A missing Brave key or a blocked engine need not discard the other's results. "
    "Snippets are untrusted search previews, not proof you read the linked pages. "
    "Each engine exposes its actual HTTP response separately from local parsing. "
    "Read original response evidence using operation=read_result, result_id, offset, length; omit query."
)
PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "operation": {"type": "string", "enum": ["search", "read_result"]},
        "result_id": {"type": "string", "minLength": 1, "maxLength": 100},
        "offset": {"type": "integer", "minimum": 0},
        "length": {"type": "integer", "minimum": 1, "maximum": 18000},
        "query": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000,
            "pattern": r"\S",
            "description": "Search terms. Brave supports at most 600 characters / 75 words.",
        },
        "engine": {"type": "string", "enum": MODES},
        "count": {"type": "integer", "minimum": 1, "maximum": 10},
    },
    "allOf": [
        {
            "if": {"properties": {"operation": {"const": "read_result"}}, "required": ["operation"]},
            "then": {
                "required": ["result_id"],
                "properties": {"query": False, "engine": False, "count": False},
            },
            "else": {
                "required": ["query"],
                "properties": {"result_id": False, "offset": False, "length": False},
            },
        }
    ],
}
CONFIG_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "engine": {"type": "string", "enum": MODES},
        "count": {"type": "integer", "minimum": 1, "maximum": 10},
        "endpoint": {"type": "string", "minLength": 1, "maxLength": 2048},
        "ollama_provider_id": {"type": "string", "maxLength": 100},
    },
}
DDG_URL = "https://html.duckduckgo.com/html/"
OLLAMA_URL = "https://ollama.com/api/web_search"
MAX_BYTES = 1_000_000


def validate_config(config, store=None):
    errors = errors_for(CONFIG_SCHEMA, config)
    endpoint = config.get("endpoint", DEFAULTS["endpoint"])
    try:
        url = urlsplit(endpoint)
        if (
            url.scheme not in ("https", "http")
            or not url.hostname
            or url.username
            or url.password
            or url.fragment
        ):
            raise ValueError()
        _ = url.port
    except ValueError, TypeError, AttributeError:
        errors.append(
            {"path": "$.endpoint", "message": "Use an HTTP(S) Brave endpoint without credentials or fragment"}
        )
    if errors:
        raise ControlError(
            "Invalid web_search configuration: " + "; ".join(f"{e['path']}: {e['message']}" for e in errors)
        )
    if store and (provider_id := config.get("ollama_provider_id")):
        validate_ollama_provider(store.get("providers", provider_id))


def validate_ollama_provider(provider):
    """Only an explicit official Ollama credential source may authorize this API."""
    try:
        url = urlsplit(provider["base_url"])
        if (
            url.scheme != "https"
            or url.hostname != "ollama.com"
            or url.port not in (None, 443)
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError()
    except ValueError, TypeError, KeyError, AttributeError:
        raise ControlError(
            "Ollama search requires a saved provider with an official https://ollama.com base URL"
        ) from None


def provider_references(store, kind, entity_id):
    if kind != "providers":
        return []
    plugin = store.get("plugins", "web_search") or {}
    references = []
    if plugin.get("config", {}).get("ollama_provider_id") == entity_id:
        references.append("plugins/web_search")
    for bot in store.list("bots"):
        if bot.get("plugin_config", {}).get("web_search", {}).get("ollama_provider_id") == entity_id:
            references.append(f"bots/{bot['id']}/web_search")
    return references


def ollama_key(config, store, vault):
    provider_id = config.get("ollama_provider_id")
    if not provider_id:
        raise SearchFailure(
            "configuration", "Select an Ollama credential provider in Plugins → Web search first"
        )
    try:
        validate_ollama_provider(store.get("providers", provider_id))
    except ControlError as exc:
        raise SearchFailure("configuration", str(exc)) from exc
    key = vault.get(f"provider/{provider_id}/api_key")
    if not key:
        raise SearchFailure("configuration", "Save the selected Ollama provider's API key in Providers")
    return key


class SearchFailure(Exception):
    def __init__(self, category, message):
        super().__init__(message)
        self.category = category


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent: Node | None = None

    def has_class(self, name):
        return name in self.attrs.get("class", "").split()

    def nodes(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.nodes()

    def text(self):
        if self.tag in ("script", "style", "noscript"):
            return ""
        return "".join(child.text() if isinstance(child, Node) else child for child in self.children)


class SearchHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.count = 0

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 20000 or len(self.stack) > 64:
            raise SearchFailure("response_format", "Search HTML exceeded structural limits")
        node = Node(tag, {key: value or "" for key, value in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def text(value, limit):
    if not isinstance(value, str):
        return ""
    parser = SearchHTML()
    parser.feed(value[:MAX_BYTES])
    return " ".join(parser.root.text().split())[:limit]


def result_url(value, *, duck=False):
    if not isinstance(value, str) or len(value) > 8192:
        return None
    try:
        value = urljoin(DDG_URL, value) if duck else value
        url = urlsplit(value)
        if duck and url.hostname in ("duckduckgo.com", "html.duckduckgo.com") and url.path == "/l/":
            value = parse_qs(url.query).get("uddg", [""])[0]
            url = urlsplit(value)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or len(value) > 2048
        ):
            return None
        return urlunsplit((url.scheme.lower(), url.netloc.lower(), url.path, url.query, ""))
    except ValueError:
        return None


def duck_results(body, count):
    html = body.decode("utf-8", "replace")
    if any(
        marker in html.lower()
        for marker in ('id="challenge-form"', "anomaly-modal", "unfortunately, bots use duckduckgo too")
    ):
        raise SearchFailure(
            "challenge", "DuckDuckGo returned an anti-bot challenge; no search results were read"
        )
    parser = SearchHTML()
    parser.feed(html)
    results = []
    for link in parser.root.nodes():
        if not link.has_class("result__a"):
            continue
        parent = link.parent
        while parent and not parent.has_class("result"):
            parent = parent.parent
        if not parent or parent.has_class("result--ad"):
            continue
        url = result_url(link.attrs.get("href"), duck=True)
        if not url:
            continue
        snippet = next((n.text() for n in parent.nodes() if n.has_class("result__snippet")), "")
        results.append(
            {
                "title": " ".join(link.text().split())[:300],
                "url": url,
                "description": " ".join(snippet.split())[:1000],
            }
        )
        if len(results) == count:
            break
    if not results and not any(
        n.has_class("no-results") or n.has_class("result--no-result") for n in parser.root.nodes()
    ):
        raise SearchFailure(
            "response_format",
            "DuckDuckGo HTML contained neither recognized results nor an explicit no-results marker",
        )
    return results


def brave_results(body, count):
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        if data.get("error"):
            raise SearchFailure("upstream_error", "Brave returned an error: " + str(data["error"])[:500])
        if "web" not in data and "query" not in data:
            raise ValueError("Missing web/query response fields")
        web = data.get("web")
        if web is None:
            web = {}
        if not isinstance(web, dict):
            raise ValueError("Expected web object")
        values = web.get("results")
        if values is None:
            values = []
        if not isinstance(values, list):
            raise ValueError("Expected web.results array")
        results = []
        for row in values[:count]:
            if not isinstance(row, dict) or not result_url(row.get("url")):
                raise ValueError("Invalid web result URL/object")
            results.append(
                {
                    "title": text(row.get("title"), 300),
                    "url": result_url(row["url"]),
                    "description": text(row.get("description"), 1000),
                }
            )
        return results
    except (ValueError, TypeError, AttributeError) as exc:
        raise SearchFailure("response_format", f"Invalid Brave search response: {exc}") from exc


def ollama_results(body, count):
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        if data.get("error"):
            raise SearchFailure("upstream_error", "Ollama returned an error; inspect the HTTP response")
        values = data.get("results")
        if not isinstance(values, list):
            raise ValueError("Expected results array")
        results = []
        for row in values[:count]:
            if (
                not isinstance(row, dict)
                or not result_url(row.get("url"))
                or not isinstance(row.get("title"), str)
                or not isinstance(row.get("content"), str)
            ):
                raise ValueError("Invalid search result URL, title or content")
            results.append(
                {
                    "title": text(row["title"], 300),
                    "url": result_url(row["url"]),
                    "description": text(row["content"], 1000),
                }
            )
        return results
    except (ValueError, TypeError, AttributeError) as exc:
        raise SearchFailure("response_format", f"Invalid Ollama search response: {exc}") from exc


async def download(client, url, params, headers, *, body=None):
    request = {"json": body} if body is not None else {}
    async with client.stream(
        "POST" if body is not None else "GET", url, params=params, headers=headers, timeout=25, **request
    ) as response:
        data, complete = bytearray(), True
        transport_error = None
        try:
            async for chunk in response.aiter_bytes():
                available = MAX_BYTES - len(data)
                data.extend(chunk[:available])
                if len(chunk) > available:
                    complete = False
                    break
        except (httpx.HTTPError, TimeoutError) as exc:
            complete = False
            transport_error = f"{type(exc).__name__}: {exc}"
        return HTTPResponse(
            bytes(data),
            response.headers.get("content-type", ""),
            str(response.url),
            response.status_code,
            response.extensions.get("reason_phrase", b"").decode("latin1") or None,
            headers_evidence(response.headers.multi_items()),
            complete=complete,
            limit=MAX_BYTES,
            local_issue=None
            if complete
            else (
                "Response body was interrupted; capture is partial"
                if transport_error
                else "Configured search download byte limit reached; capture is partial"
            ),
            transport_error=transport_error,
        )


async def search(args, context, config, key, store, vault):
    validate_config(config)
    mode, count = args.get("engine", config.get("engine", "auto")), args.get("count", config.get("count", 5))

    async def engine(client, name):
        started = time.perf_counter()
        status = {"engine": name, "name": "web_search", "query": args["query"]}
        response = None
        deadline = asyncio.timeout(25)
        try:
            async with deadline:
                if name == "brave":
                    if not key:
                        raise SearchFailure(
                            "configuration",
                            "Brave key missing: Plugins → Web search → API key → Save credential. DuckDuckGo needs no key.",
                        )
                    if len(args["query"]) > 600 or len(args["query"].split()) > 75:
                        raise SearchFailure(
                            "query_limit",
                            "Brave query limit is 600 characters / 75 words; shorten the query or choose DuckDuckGo",
                        )
                    status["url"] = str(
                        httpx.URL(config["endpoint"]).copy_merge_params({"q": args["query"], "count": count})
                    )
                    response = await download(
                        client,
                        config["endpoint"],
                        {"q": args["query"], "count": count},
                        {"X-Subscription-Token": key, "Accept": "application/json"},
                    )
                elif name == "ollama":
                    status["url"] = OLLAMA_URL
                    response = await download(
                        client,
                        OLLAMA_URL,
                        None,
                        {
                            "Authorization": f"Bearer {ollama_key(config, store, vault)}",
                            "Accept": "application/json",
                        },
                        body={"query": args["query"], "max_results": count},
                    )
                else:
                    status["url"] = str(httpx.URL(DDG_URL).copy_merge_params({"q": args["query"]}))
                    response = await download(
                        client,
                        DDG_URL,
                        {"q": args["query"]},
                        {"User-Agent": "Hortator/0.1 (web search)", "Accept": "text/html"},
                    )
                http = record_response(store, vault, context, "web_search", response)
                status.update(http_response=http, http_status=response.status, url=response.url)
                if not response.complete:
                    raise SearchFailure(
                        "transport" if response.transport_error else "response_limit",
                        response.transport_error or response.local_issue,
                    )
                if name == "ollama" and not 200 <= response.status < 300:
                    raise SearchFailure(
                        "http_status", f"Ollama returned HTTP {response.status}; inspect the HTTP response"
                    )
                parser = {"brave": brave_results, "duckduckgo": duck_results, "ollama": ollama_results}[name]
                results = parser(response.data, count)
            status.update(status="ok", count=len(results))
        except SearchFailure as exc:
            results = []
            status.update(
                status="failed", count=0, category=exc.category, error=str(exc), local_issue=str(exc)
            )
        except (httpx.HTTPError, TimeoutError) as exc:
            results = []
            status.update(
                status="failed",
                count=0,
                category="local_deadline" if deadline.expired() else "transport",
                error="Local search deadline exceeded: 25 seconds"
                if deadline.expired()
                else f"{type(exc).__name__}: {exc}",
            )
        status["duration_ms"] = (time.perf_counter() - started) * 1000
        store.emit(
            "web_search.engine_completed" if status["status"] == "ok" else "web_search.engine_failed",
            status,
            bot_id=context.bot["id"],
            turn_id=context.turn_id,
            level="error"
            if status.get("category") in {"transport", "local_deadline"}
            else "info"
            if status["status"] == "ok" and response.status < 400
            else "warning",
        )
        return status, results

    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        if mode == "both":
            async with task_group() as group:
                brave = group.create_task(engine(client, "brave"), name="search-brave")
                duck = group.create_task(engine(client, "duckduckgo"), name="search-duckduckgo")
            responses = [brave.result(), duck.result()]
        elif mode == "auto":
            responses = [await engine(client, "brave")]
            if not responses[0][1]:
                responses.append(await engine(client, "duckduckgo"))
        else:
            responses = [await engine(client, mode)]
    combined = {}
    # Interleave engines so one engine cannot occupy the whole beginning of a combined result.
    for index in range(count):
        for status, results in responses:
            if index >= len(results):
                continue
            item = results[index]
            existing = combined.setdefault(item["url"], {**item, "engines": []})
            if status["engine"] not in existing["engines"]:
                existing["engines"].append(status["engine"])
    statuses = [status for status, _ in responses]
    ok = any(s["status"] == "ok" for s in statuses)
    results = list(combined.values())
    truncated = False
    while len(json.dumps(results, ensure_ascii=False)) > 48000:
        results.pop()
        truncated = True
    return {
        "ok": ok,
        "mode": mode,
        "results": results,
        "engine_status": statuses,
        "truncated": truncated,
        "partial": any(s["status"] == "failed" for s in statuses) and ok,
        "fallback_used": mode == "auto" and len(responses) == 2,
        "error": None if ok else "All selected search engines failed; inspect engine_status before retrying.",
        "trust": "untrusted external content; snippets are previews, not full pages",
    }
