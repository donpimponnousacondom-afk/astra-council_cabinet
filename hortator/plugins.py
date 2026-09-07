from __future__ import annotations

import asyncio
import base64
import importlib.metadata
import ipaddress
import json
import socket
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlparse

import aiohttp
import httpx
from jsonschema import Draft202012Validator

from .models import ControlError
from .store import dumps, uid


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


STR = {"type": "string"}


def function(name, description, parameters):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


SPEAK = function("council_speak", "Publish one considered council contribution, optionally replying to a message ID in this context. This ends your turn. Never include reasoning or analysis traces.",
                 schema({"content": {"type": "string", "minLength": 1, "maxLength": 12000}, "reply_to": STR,
                         "artifact_ids": {"type": "array", "items": STR, "maxItems": 4}}, ("content",)))
SILENCE = function("council_silence", "Choose to listen without posting. Ends this activation. Give only a short operational label, not private reasoning.",
                   schema({"label": {"type": "string", "maxLength": 200}}, ("label",)))


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Validate and pin the actual DNS answers used by the socket (including redirects)."""
    async def resolve(self, host, port=0, family=socket.AF_INET):
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
        out = []
        for fam, _, proto, _, address in infos:
            ip = ipaddress.ip_address(address[0])
            if not ip.is_global or ip.is_multicast:
                raise ControlError("web_fetch cannot access private, local, reserved, or multicast addresses")
            out.append({"hostname": host, "host": str(ip), "port": port, "family": fam, "proto": proto,
                        "flags": socket.AI_NUMERICHOST})
        return out

    async def close(self):
        pass


def public_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ControlError("Only public HTTP(S) URLs without credentials are allowed")
    if parsed.port not in (None, 80, 443):
        raise ControlError("Web tools allow ports 80 and 443")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        if parsed.hostname.lower() == "localhost" or parsed.hostname.lower().endswith(".localhost"):
            raise ControlError("Local addresses are not allowed")
    else:
        if not address.is_global or address.is_multicast:
            raise ControlError("Private addresses are not allowed")
    return url


async def fetch_public(url, limit=1_000_000):
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=False,
                                     cookie_jar=aiohttp.DummyCookieJar()) as session:
        for _ in range(6):
            public_url(url)
            async with session.get(url, allow_redirects=False, headers={"User-Agent": "Hortator/0.1"}) as response:
                if response.status in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                if response.status >= 400:
                    raise ControlError(f"Web request returned HTTP {response.status}")
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > limit:
                        raise ControlError(f"Response exceeds the {limit} byte limit")
                return bytes(data), response.headers.get("Content-Type", ""), str(response.url)
    raise ControlError("Too many redirects")


class ExtractText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


@dataclass
class ToolContext:
    bot: dict
    channel_id: str
    turn_id: str
    owner_verified: bool = False


@dataclass
class PluginSpec:
    id: str
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict, ToolContext, dict, str], Awaitable[Any]]
    defaults: dict
    owner_only: bool = False


class Registry:
    def __init__(self, store, vault, directory, inspect):
        self.store, self.vault = store, vault
        self.directory = directory / "artifacts"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.inspect = inspect
        self.specs: dict[str, PluginSpec] = {}
        self.register(PluginSpec("web_fetch", "Web fetch", "Retrieve readable text from a public web page.",
                      schema({"url": STR}, ("url",)), self.web_fetch, {}))
        self.register(PluginSpec("web_search", "Web search", "Search the web using Brave Search.",
                      schema({"query": {"type": "string", "minLength": 1, "maxLength": 1000}}, ("query",)), self.web_search,
                      {"endpoint": "https://api.search.brave.com/res/v1/web/search", "count": 5}))
        self.register(PluginSpec("image_generation", "Image generation", "Generate an image from a prompt. Returns an artifact ID that you can attach with council_speak.",
                      schema({"prompt": {"type": "string", "minLength": 1, "maxLength": 8000}}, ("prompt",)), self.image_generation,
                      {"endpoint": "https://api.openai.com/v1/images/generations", "request_json": {"model": "gpt-image-1", "size": "1024x1024"}}))
        self.register(PluginSpec("tts", "Text to speech", "Generate an audio attachment from text. Returns an artifact ID.",
                      schema({"text": {"type": "string", "minLength": 1, "maxLength": 4000}}, ("text",)), self.tts,
                      {"endpoint": "https://api.openai.com/v1/audio/speech", "request_json": {"model": "tts-1", "voice": "alloy", "response_format": "mp3"}}))
        self.register(PluginSpec("memory", "Private memory", "Read or write your own persistent notes for this channel. Notes from other bots/channels are inaccessible.",
                      schema({"operation": {"type": "string", "enum": ["read", "write", "delete"]}, "key": {"type": "string", "maxLength": 100},
                              "value": {"type": "string", "maxLength": 8000}}, ("operation",)), self.memory, {}))
        self.register(PluginSpec("council_inspect", "Council inspector", "Read council status, statistics, configuration, events, trajectory or context for The Boss. No mutations or credentials.",
                      schema({"resource": {"type": "string", "enum": ["status", "stats", "bots", "providers", "profiles", "prompts", "rooms", "plugins", "settings", "events", "turn", "context"]},
                              "id": STR, "channel_id": STR}, ("resource",)), self.council_inspect, {}, True))
        for entry in importlib.metadata.entry_points(group="hortator.plugins"):
            entry.load()(self)

    def register(self, spec):
        if spec.id in self.specs or spec.id in ("council_speak", "council_silence"):
            raise ValueError("Duplicate/reserved plugin ID")
        Draft202012Validator.check_schema(spec.parameters)
        self.specs[spec.id] = spec

    def allowed(self, name, context):
        spec = self.specs.get(name)
        config = self.store.get("plugins", name)
        return bool(spec and config and config["enabled"] and name in context.bot["enabled_plugins"] and
                    (not spec.owner_only or (context.bot["role"] == "hortator" and context.owner_verified)))

    def schemas(self, context):
        return [function(name, spec.description, spec.parameters) for name, spec in self.specs.items() if self.allowed(name, context)]

    async def call(self, name, args, context, call_id):
        start = time.perf_counter()
        self.store.emit("tool.started", {"name": name, "call_id": call_id, "arguments": args}, bot_id=context.bot["id"], turn_id=context.turn_id)
        try:
            if not self.allowed(name, context):
                raise ControlError("Tool is not enabled for this bot and trusted request")
            spec = self.specs[name]
            errors = list(Draft202012Validator(spec.parameters).iter_errors(args))
            if errors:
                raise ControlError(errors[0].message)
            config = {**spec.defaults, **self.store.get("plugins", name)["config"], **context.bot["plugin_config"].get(name, {})}
            key = self.vault.get(f"bot/{context.bot['id']}/plugin:{name}") or self.vault.get(f"plugin/{name}/api_key")
            async with asyncio.timeout(120):
                result = self.vault.redact(await spec.handler(args, context, config, key))
            if len(dumps(result)) > 60000:
                result = {"truncated": True, "text": dumps(result)[:50000]}
            self.store.emit("tool.completed", {"name": name, "call_id": call_id, "result": result,
                            "duration_ms": (time.perf_counter()-start)*1000}, bot_id=context.bot["id"], turn_id=context.turn_id)
            return result
        except asyncio.CancelledError:
            self.store.emit("tool.cancelled", {"name": name, "call_id": call_id}, bot_id=context.bot["id"], turn_id=context.turn_id, level="warning")
            raise
        except Exception as exc:
            error = self.vault.redact(f"{type(exc).__name__}: {exc}")[:2000]
            self.store.emit("tool.failed", {"name": name, "call_id": call_id, "error": error,
                            "duration_ms": (time.perf_counter()-start)*1000}, bot_id=context.bot["id"], turn_id=context.turn_id, level="error")
            return {"error": error}

    async def web_fetch(self, args, context, config, key):
        data, content_type, url = await fetch_public(args["url"])
        if not any(t in content_type for t in ("text/", "application/json", "application/xml")):
            raise ControlError("web_fetch supports text documents; binary files are not injected into context")
        text = data.decode("utf-8", errors="replace")
        if "html" in content_type:
            parser = ExtractText()
            parser.feed(text)
            text = "".join(parser.parts)
        return {"url": url, "text": text[:18000], "truncated": len(text) > 18000, "trust": "untrusted external content"}

    async def web_search(self, args, context, config, key):
        if not key:
            raise ControlError("Add a Brave Search API key in the dashboard")
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            result = await client.get(config["endpoint"], params={"q": args["query"], "count": min(int(config.get("count", 5)), 10)},
                                      headers={"X-Subscription-Token": key, "Accept": "application/json"}, timeout=25)
            result.raise_for_status()
            values = result.json().get("web", {}).get("results", [])
            return {"results": [{"title": r.get("title"), "url": r.get("url"), "description": r.get("description")} for r in values[:10]],
                    "trust": "untrusted external content"}

    async def media_request(self, config, key, body):
        # Endpoints are operator configuration, never model-supplied URLs. No redirect credential forwarding.
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            async with client.stream("POST", config["endpoint"], json=body, headers=headers, timeout=100) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 20_000_000:
                        raise ControlError("Media response exceeds 20 MB")
                return bytes(data), response.headers.get("content-type", "application/octet-stream")

    def artifact(self, data, mime, suffix, context):
        if len(data) > 8_000_000:
            raise ControlError("Generated attachment exceeds the 8 MB delivery limit")
        artifact_id = uid("art_")
        filename = artifact_id + suffix
        path = self.directory / filename
        path.write_bytes(data)
        path.chmod(0o600)
        self.store.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)", (artifact_id, context.bot["id"], context.turn_id, filename, mime, len(data), time.time()))
        return {"artifact_id": artifact_id, "mime": mime, "bytes": len(data)}

    async def image_generation(self, args, context, config, key):
        data, _ = await self.media_request(config, key, {**config.get("request_json", {}), "prompt": args["prompt"], "n": 1})
        result = json.loads(data)
        item = result["data"][0]
        mime = "image/png"
        if item.get("b64_json"):
            image = base64.b64decode(item["b64_json"], validate=True)
        elif item.get("url"):
            image, mime, _ = await fetch_public(item["url"], 8_000_000)
        else:
            raise ControlError("Image endpoint returned neither b64_json nor a public image URL")
        suffix = ".png"
        if image.startswith(b"\xff\xd8\xff"):
            mime, suffix = "image/jpeg", ".jpg"
        elif image[:4] == b"RIFF" and image[8:12] == b"WEBP":
            mime, suffix = "image/webp", ".webp"
        elif not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ControlError("Generated image is not PNG, JPEG or WebP")
        return {**self.artifact(image, mime, suffix, context), "usage": result.get("usage")}

    async def tts(self, args, context, config, key):
        body = {**config.get("request_json", {}), "input": args["text"]}
        fmt = body.get("response_format", "mp3")
        if fmt not in ("mp3", "opus", "aac", "flac", "wav", "pcm"):
            raise ControlError("Unsupported audio response format")
        data, mime = await self.media_request(config, key, body)
        if not data or "json" in mime or "html" in mime:
            raise ControlError("TTS endpoint did not return audio")
        return self.artifact(data, mime, "." + fmt, context)

    async def memory(self, args, context, config, key):
        scope = (context.bot["id"], context.channel_id)
        if args["operation"] == "read":
            return {"notes": self.store.rows("SELECT key,value,updated_at FROM memories WHERE bot_id=? AND channel_id=? ORDER BY key", scope)}
        name = args.get("key", "").strip()
        if not name:
            raise ControlError("A memory key is required")
        if args["operation"] == "delete":
            self.store.execute("DELETE FROM memories WHERE bot_id=? AND channel_id=? AND key=?", (*scope, name))
        else:
            value = args.get("value", "")
            size = self.store.one("SELECT coalesce(sum(length(value)),0) AS n FROM memories WHERE bot_id=? AND channel_id=? AND key<>?", (*scope, name))["n"]
            if size + len(value) > 24000:
                raise ControlError("Channel memory limit is 24,000 characters; consolidate notes first")
            self.store.execute("INSERT INTO memories VALUES(?,?,?,?,?) ON CONFLICT(bot_id,channel_id,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                               (*scope, name, self.vault.redact(value), time.time()))
        return {"saved": True, "key": name}

    async def council_inspect(self, args, context, config, key):
        return self.inspect(args["resource"], args.get("id"), args.get("channel_id"))

    def resolve_artifact(self, artifact_id, context):
        item = self.store.one("SELECT * FROM artifacts WHERE id=? AND bot_id=? AND turn_id=?", (artifact_id, context.bot["id"], context.turn_id))
        if not item:
            raise ControlError("Attachment does not belong to this bot and turn")
        return self.directory / item["filename"]
