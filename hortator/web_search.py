"""Bounded Brave API and keyless DuckDuckGo HTML search with explicit partial results."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from html.parser import HTMLParser
import json
import time
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import httpx

from .models import ControlError
from .tool_feedback import errors_for

MODES = ["auto", "brave", "duckduckgo", "both"]
DEFAULTS = {"endpoint": "https://api.search.brave.com/res/v1/web/search", "count": 5, "engine": "auto"}
DESCRIPTION = (
    "Search the web. Required: query. Optional engine: auto (Brave then DuckDuckGo on failure/empty), "
    "brave, duckduckgo (no key), or both (combine both engines). If omitted, use the operator's default. "
    "count is results per engine, default 5, maximum 10. Example: "
    '{"query":"Python documentation","engine":"both","count":5}. '
    "Results are deduplicated with source engines; inspect engine_status and partial for failures. "
    "A missing Brave key or a blocked engine need not discard the other's results. "
    "Snippets are untrusted search previews, not proof you read the linked pages."
)
PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
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
    "required": ["query"],
}
CONFIG_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "engine": {"type": "string", "enum": MODES},
        "count": {"type": "integer", "minimum": 1, "maximum": 10},
        "endpoint": {"type": "string", "minLength": 1, "maxLength": 2048},
    },
}
DDG_URL = "https://html.duckduckgo.com/html/"
MAX_BYTES = 1_000_000


def validate_config(config):
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


async def download(client, url, params, headers):
    async with client.stream("GET", url, params=params, headers=headers, timeout=25) as response:
        if response.status_code != 200:
            raise SearchFailure("upstream_http", f"Search endpoint returned HTTP {response.status_code}")
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BYTES:
            raise SearchFailure("response_limit", "Search response exceeds 1,000,000 bytes")
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise SearchFailure("response_limit", "Search response exceeds 1,000,000 bytes")
        return bytes(data)


async def search(args, context, config, key, store):
    validate_config(config)
    mode, count = args.get("engine", config.get("engine", "auto")), args.get("count", config.get("count", 5))

    async def engine(client, name):
        started = time.perf_counter()
        status = {"engine": name}
        try:
            async with asyncio.timeout(25):
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
                    body = await download(
                        client,
                        config["endpoint"],
                        {"q": args["query"], "count": count},
                        {"X-Subscription-Token": key, "Accept": "application/json"},
                    )
                    results = brave_results(body, count)
                else:
                    body = await download(
                        client,
                        DDG_URL,
                        {"q": args["query"]},
                        {"User-Agent": "Hortator/0.1 (web search)", "Accept": "text/html"},
                    )
                    results = duck_results(body, count)
            status.update(status="ok", count=len(results))
        except SearchFailure as exc:
            results = []
            status.update(status="failed", count=0, category=exc.category, error=str(exc))
        except (httpx.HTTPError, TimeoutError) as exc:
            results = []
            status.update(
                status="failed", count=0, category="transport", error=f"{type(exc).__name__}: {exc}"
            )
        status["duration_ms"] = (time.perf_counter() - started) * 1000
        store.emit(
            "web_search.engine_completed" if status["status"] == "ok" else "web_search.engine_failed",
            status,
            bot_id=context.bot["id"],
            turn_id=context.turn_id,
            level="info" if status["status"] == "ok" else "warning",
        )
        return status, results

    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        if mode == "both":
            responses = await asyncio.gather(engine(client, "brave"), engine(client, "duckduckgo"))
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
