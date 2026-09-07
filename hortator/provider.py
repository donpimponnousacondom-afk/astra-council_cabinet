from __future__ import annotations

import asyncio
import copy
import json
import math
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

from .models import ControlError
from .store import dumps, uid


class ProviderError(ControlError):
    def __init__(self, message, status=502, *, http_status=None, retry_after=0, provider_fault=True):
        super().__init__(message, status)
        self.http_status, self.retry_after, self.provider_fault = http_status, retry_after, provider_fault


def strip_reasoning(content: str) -> str:
    # Never serialize vendor reasoning fields. Also defend against commonly tagged inline thought blocks.
    content = re.sub(
        r"<(think|thinking|analysis|reasoning)\b[^>]*>.*?(?:</\1\s*>|$)", "", content, flags=re.S | re.I
    )
    return content.strip()


def safe_payload(value):
    if isinstance(value, dict):
        return {
            k: ("[reasoning omitted]" if k in {"reasoning_content", "reasoning_details"} else safe_payload(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [safe_payload(v) for v in value]
    return value


def number(value):
    return (
        value
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
        else None
    )


def normalized_usage(usage, profile):
    inp, out = number(usage.get("prompt_tokens")), number(usage.get("completion_tokens"))
    completion_details = usage.get("completion_tokens_details") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    reasoning = number(completion_details.get("reasoning_tokens"))
    cached = number(prompt_details.get("cached_tokens", usage.get("prompt_cache_hit_tokens")))
    cost, cost_source = number(usage.get("cost")), "reported"
    if cost is None:
        cost_source = None
        ip, op = profile.get("input_price_per_million"), profile.get("output_price_per_million")
        if inp is not None and out is not None and ip is not None and op is not None:
            cost, cost_source = (inp * ip + out * op) / 1_000_000, "estimated"
    return dict(
        input_tokens=inp,
        output_tokens=out,
        reasoning_tokens=reasoning,
        cached_tokens=cached,
        cost=cost,
        cost_source=cost_source,
    )


@dataclass
class Completion:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    # Some compatible APIs require replaying the assistant reasoning alongside tool results.
    # It lives only in the active task; it is scrubbed from stored request snapshots and never delivered.
    reasoning_content: str = ""
    reasoning_details: list[dict] = field(default_factory=list)
    request_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def message(self):
        result = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            result["tool_calls"] = self.tool_calls
        if self.reasoning_content:
            result["reasoning_content"] = self.reasoning_content
        if self.reasoning_details:
            result["reasoning_details"] = self.reasoning_details
        return result


class ProviderPool:
    def __init__(self, store, vault, client=None):
        self.store, self.vault = store, vault
        self.client = client or httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=40),
        )
        self.active: dict[str, int] = {}
        self.probing: set[str] = set()
        self.changed = asyncio.Condition()

    @asynccontextmanager
    async def slot(self, provider):
        pid = provider["id"]
        async with self.changed:
            await self.changed.wait_for(lambda: self.active.get(pid, 0) < provider["max_concurrency"])
            current = self.store.get("providers", pid)
            if not current or not current["enabled"]:
                raise ProviderError("Provider is disabled", provider_fault=False)
            health = self.store.health(pid)
            if health["circuit_until"] > time.time() or pid in self.probing:
                raise ProviderError(
                    "Provider circuit is open; waiting for recovery probe", provider_fault=False
                )
            if health["consecutive_failures"] >= provider["failure_threshold"]:
                self.probing.add(pid)
            self.active[pid] = self.active.get(pid, 0) + 1
        try:
            yield
        finally:
            async with self.changed:
                self.active[pid] -= 1
                self.probing.discard(pid)
                self.changed.notify_all()

    def key(self, provider, bot_id):
        return self.vault.get(f"bot/{bot_id}/provider_key") or self.vault.get(
            f"provider/{provider['id']}/api_key"
        )

    def headers(self, provider, bot_id):
        headers = {"Content-Type": "application/json", **provider["headers"]}
        key = self.key(provider, bot_id)
        if provider["requires_key"] and not key:
            raise ProviderError("Provider API key is missing; add it in the dashboard", provider_fault=False)
        if key:
            headers[provider.get("auth_header", "Authorization")] = (
                provider.get("auth_scheme", "Bearer") + " " + key
            ).strip()
        return headers

    def success(self, provider):
        old = self.store.health(provider["id"])
        self.store.execute(
            "UPDATE provider_health SET consecutive_failures=0,circuit_until=0,last_error=NULL,last_success=? WHERE provider_id=?",
            (time.time(), provider["id"]),
        )
        if old["consecutive_failures"]:
            self.store.emit(
                "provider.recovered",
                {"provider_id": provider["id"], "previous_failures": old["consecutive_failures"]},
            )

    def failure(self, provider, exc):
        if not exc.provider_fault:
            return
        old = self.store.health(provider["id"])
        count = old["consecutive_failures"] + 1
        delay = max(
            exc.retry_after, provider["circuit_seconds"] if count >= provider["failure_threshold"] else 0
        )
        error = self.vault.redact(str(exc))[:2000]
        self.store.execute(
            "UPDATE provider_health SET consecutive_failures=?,circuit_until=?,last_error=? WHERE provider_id=?",
            (count, time.time() + delay if delay else 0, error, provider["id"]),
        )
        if delay and old["circuit_until"] <= time.time():
            self.store.emit(
                "provider.circuit_open",
                {
                    "provider_id": provider["id"],
                    "consecutive_failures": count,
                    "retry_in_seconds": delay,
                    "error": error,
                },
                level="error",
            )
        self.store.emit(
            "provider.failure",
            {
                "provider_id": provider["id"],
                "consecutive_failures": count,
                "retry_in_seconds": delay,
                "error": error,
            },
            level="error",
        )

    async def complete(
        self, *, bot, profile, messages, tools, turn_id, context, purpose="generation", output_limit=None
    ):
        provider = self.store.get("providers", profile["provider_id"])
        if not provider:
            raise ProviderError("Provider no longer exists", provider_fault=False)
        queued = time.perf_counter()
        async with self.slot(provider):
            limit = bot.get("daily_cost_limit")
            if limit is not None:
                now = time.time()
                budget = self.store.one(
                    "SELECT coalesce(sum(cost),0) AS cost,sum(CASE WHEN status='completed' AND cost IS NULL THEN 1 ELSE 0 END) AS unknown FROM requests WHERE bot_id=? AND started_at>=?",
                    (bot["id"], now - now % 86400),
                )
                if budget["cost"] >= limit or budget["unknown"]:
                    raise ProviderError(
                        "Daily model cost threshold reached or a completed request has unknown cost",
                        provider_fault=False,
                    )
            headers = self.headers(provider, bot["id"])
            body = copy.deepcopy(profile["request_json"])
            if purpose == "compaction":
                body.update(copy.deepcopy(profile.get("compaction_request_json", {})))
            body.update(model=profile["model"], messages=messages, stream=profile["stream"])
            if output_limit:
                cap_key = "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"
                body[cap_key] = output_limit
            if tools:
                body["tools"] = tools
                body["tool_choice"] = "auto"
            if profile["stream"] and profile["include_usage"]:
                body.setdefault("stream_options", {"include_usage": True})
            request_id, started, clock = uid("req_"), time.time(), time.perf_counter()
            meta = {
                **context,
                "queue_ms": (clock - queued) * 1000,
                "profile_revision": profile["revision"],
                "provider_revision": provider["revision"],
                "endpoint": provider["base_url"] + "/chat/completions",
            }
            self.store.execute(
                """INSERT INTO requests(id,turn_id,bot_id,provider_id,profile_id,model,purpose,
              started_at,status,body,context) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    request_id,
                    turn_id,
                    bot["id"],
                    provider["id"],
                    profile["id"],
                    profile["model"],
                    purpose,
                    started,
                    "running",
                    dumps(self.vault.redact(safe_payload(body))),
                    dumps(self.vault.redact(meta)),
                ),
            )
            self.store.emit(
                "request.started",
                {
                    "purpose": purpose,
                    "profile_id": profile["id"],
                    "model": profile["model"],
                    "estimated_tokens": context.get("estimated_tokens"),
                },
                bot_id=bot["id"],
                turn_id=turn_id,
                request_id=request_id,
            )
            result = Completion(request_id=request_id)
            ttft = visible = None
            calls: dict[int, dict] = {}
            total_bytes = 0
            status_code = None
            try:
                # Total deadline covers streamed bodies as well as the connection, not just inactivity.
                async with asyncio.timeout(provider["timeout_seconds"]):
                    async with self.client.stream(
                        "POST",
                        provider["base_url"] + "/chat/completions",
                        headers=headers,
                        json=body,
                        timeout=provider["timeout_seconds"],
                    ) as response:
                        status_code = response.status_code
                        if response.status_code >= 300:
                            error_bytes = bytearray()
                            async for chunk in response.aiter_bytes():
                                error_bytes.extend(chunk[: 12000 - len(error_bytes)])
                                if len(error_bytes) >= 12000:
                                    break
                            raw = error_bytes.decode(errors="replace")
                            try:
                                retry = float(response.headers.get("retry-after", "0"))
                            except ValueError:
                                retry = 0
                            raise ProviderError(
                                f"HTTP {response.status_code}: {self.vault.redact(raw)[:2000]}",
                                http_status=response.status_code,
                                retry_after=min(max(retry, 0), 3600),
                                provider_fault=(
                                    response.status_code in (401, 403, 429)
                                    and not self.vault.get(f"bot/{bot['id']}/provider_key")
                                )
                                or response.status_code >= 500,
                            )
                        if "text/event-stream" in response.headers.get("content-type", ""):

                            async def packets():
                                lines = []
                                size = 0
                                async for line in response.aiter_lines():
                                    size += len(line)
                                    if size > 8_000_000:
                                        raise ProviderError(
                                            "Provider stream exceeded the 8 MB response limit",
                                            provider_fault=False,
                                        )
                                    if line == "":
                                        if lines:
                                            yield "\n".join(lines)
                                            lines = []
                                    elif line.startswith("data:"):
                                        lines.append(line[5:].lstrip())
                                if lines:
                                    yield "\n".join(lines)

                            finished = False
                            async for payload in packets():
                                if payload == "[DONE]":
                                    finished = True
                                    break
                                packet = json.loads(payload)
                                result.metadata.update(
                                    {
                                        key: packet[key]
                                        for key in (
                                            "id",
                                            "model",
                                            "created",
                                            "system_fingerprint",
                                            "provider",
                                            "service_tier",
                                        )
                                        if key in packet
                                    }
                                )
                                if packet.get("error"):
                                    raise ProviderError(str(packet["error"]))
                                if packet.get("usage"):
                                    result.usage.update(packet["usage"])
                                for choice in packet.get("choices", []):
                                    if choice.get("index", 0) != 0:
                                        continue
                                    delta = choice.get("delta") or {}
                                    token = delta.get("content") or ""
                                    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                                    details = delta.get("reasoning_details") or []
                                    if (
                                        token
                                        or reasoning
                                        or details
                                        or any(
                                            c.get("function", {}).get("arguments")
                                            for c in delta.get("tool_calls", [])
                                        )
                                    ):
                                        if ttft is None:
                                            ttft = (time.perf_counter() - clock) * 1000
                                            self.store.emit(
                                                "request.first_token",
                                                {"ttft_ms": ttft},
                                                bot_id=bot["id"],
                                                turn_id=turn_id,
                                                request_id=request_id,
                                            )
                                    if token and visible is None:
                                        visible = (time.perf_counter() - clock) * 1000
                                    result.content += token
                                    result.reasoning_content += reasoning
                                    result.reasoning_details.extend(details)
                                    for call in delta.get("tool_calls", []):
                                        entry = calls.setdefault(
                                            call["index"],
                                            {
                                                "id": "",
                                                "type": "function",
                                                "function": {"name": "", "arguments": ""},
                                            },
                                        )
                                        if call.get("id"):
                                            entry["id"] = call["id"]
                                        fn = call.get("function") or {}
                                        entry["function"]["name"] += fn.get("name") or ""
                                        entry["function"]["arguments"] += fn.get("arguments") or ""
                                    if choice.get("finish_reason"):
                                        result.finish_reason = choice["finish_reason"]
                            if not finished and not result.finish_reason:
                                raise ProviderError("Stream disconnected before a completion boundary")
                            result.tool_calls = [calls[k] for k in sorted(calls)]
                        else:
                            chunks = []
                            async for chunk in response.aiter_bytes():
                                total_bytes += len(chunk)
                                if total_bytes > 8_000_000:
                                    raise ProviderError(
                                        "Provider response exceeded the 8 MB limit", provider_fault=False
                                    )
                                chunks.append(chunk)
                            packet = json.loads(b"".join(chunks))
                            result.metadata.update(
                                {
                                    key: packet[key]
                                    for key in (
                                        "id",
                                        "model",
                                        "created",
                                        "system_fingerprint",
                                        "provider",
                                        "service_tier",
                                    )
                                    if key in packet
                                }
                            )
                            if packet.get("error"):
                                raise ProviderError(str(packet["error"]))
                            choices = packet.get("choices") or []
                            if not choices:
                                raise ProviderError("Response has no choices", provider_fault=False)
                            msg = choices[0]["message"]
                            result.content = msg.get("content") or ""
                            result.tool_calls = msg.get("tool_calls") or []
                            result.reasoning_content = msg.get("reasoning_content") or ""
                            result.reasoning_details = msg.get("reasoning_details") or []
                            result.usage = packet.get("usage") or {}
                            result.finish_reason = choices[0].get("finish_reason")
                            # A buffered response has no measurable time-to-first-token.
                result.content = strip_reasoning(result.content)
                if len(result.tool_calls) > 100 or any(
                    not c.get("id") or c.get("type") != "function" for c in result.tool_calls
                ):
                    raise ProviderError("Invalid or excessive tool calls", provider_fault=False)
                self.success(provider)
                metrics = normalized_usage(result.usage, profile)
                duration = (time.perf_counter() - clock) * 1000
                self.store.execute(
                    """UPDATE requests SET ended_at=:ended_at,status='completed',ttft_ms=:ttft,
                  first_visible_ms=:visible,duration_ms=:duration,input_tokens=:input_tokens,output_tokens=:output_tokens,
                  reasoning_tokens=:reasoning_tokens,cached_tokens=:cached_tokens,cost=:cost,cost_source=:cost_source,
                  usage=:usage,response=:response,http_status=:http_status WHERE id=:id""",
                    {
                        **metrics,
                        "ended_at": time.time(),
                        "ttft": ttft,
                        "visible": visible,
                        "duration": duration,
                        "usage": dumps(self.vault.redact(result.usage)),
                        "response": dumps(
                            self.vault.redact(
                                {
                                    "content": result.content,
                                    "tool_calls": result.tool_calls,
                                    "finish_reason": result.finish_reason,
                                    "provider_metadata": result.metadata,
                                }
                            )
                        ),
                        "http_status": status_code,
                        "id": request_id,
                    },
                )
                self.store.emit(
                    "request.completed",
                    {
                        **metrics,
                        "duration_ms": duration,
                        "ttft_ms": ttft,
                        "finish_reason": result.finish_reason,
                    },
                    bot_id=bot["id"],
                    turn_id=turn_id,
                    request_id=request_id,
                )
                return result
            except BaseException as error:
                cancelled = isinstance(error, asyncio.CancelledError)
                if not isinstance(error, (Exception, asyncio.CancelledError)):
                    raise
                exc = (
                    error
                    if isinstance(error, ProviderError)
                    else ProviderError(
                        "Request cancelled; provider usage may be incomplete"
                        if cancelled
                        else f"{type(error).__name__}: {str(error) or 'Request timed out'}"
                    )
                )
                message = self.vault.redact(str(exc))[:3000]
                metrics = normalized_usage(result.usage, profile)
                self.store.execute(
                    """UPDATE requests SET ended_at=:ended,status=:status,error=:error,
                    duration_ms=:duration,ttft_ms=:ttft,http_status=:http_status,usage=:usage,response=:response,
                    input_tokens=:input_tokens,output_tokens=:output_tokens,reasoning_tokens=:reasoning_tokens,
                    cached_tokens=:cached_tokens,cost=:cost,cost_source=:cost_source WHERE id=:id""",
                    {
                        **metrics,
                        "ended": time.time(),
                        "status": "cancelled" if cancelled else "failed",
                        "error": message,
                        "duration": (time.perf_counter() - clock) * 1000,
                        "ttft": ttft,
                        "http_status": status_code,
                        "usage": dumps(self.vault.redact(result.usage)),
                        "response": dumps(
                            self.vault.redact(
                                {
                                    "partial": True,
                                    "content": strip_reasoning(result.content),
                                    "tool_calls": result.tool_calls or list(calls.values()),
                                    "finish_reason": result.finish_reason,
                                    "provider_metadata": result.metadata,
                                }
                            )
                        ),
                        "id": request_id,
                    },
                )
                self.store.emit(
                    "request.cancelled" if cancelled else "request.failed",
                    {
                        "error": message,
                        "provider_id": provider["id"],
                        "profile_id": profile["id"],
                        "http_status": status_code,
                    },
                    bot_id=bot["id"],
                    turn_id=turn_id,
                    request_id=request_id,
                    level="warning" if cancelled else "error",
                )
                if not cancelled:
                    self.failure(provider, exc)
                    raise exc from error
                raise

    async def probe(self, provider):
        start = time.perf_counter()
        try:
            response = await self.client.get(
                provider["base_url"] + "/models", headers=self.headers(provider, ""), timeout=20
            )
            if response.status_code >= 300:
                raise ProviderError(
                    f"Model discovery returned HTTP {response.status_code}", http_status=response.status_code
                )
            raw = response.json()
            models = [
                {
                    "id": item.get("id"),
                    "context_length": item.get("context_length"),
                    "pricing": item.get("pricing"),
                }
                for item in raw.get("data", [])[:3000]
                if isinstance(item, dict)
            ]
            # Some catalogs are public. Discovery cannot establish credential validity or reset health.
            result = {
                "provider_id": provider["id"],
                "latency_ms": (time.perf_counter() - start) * 1000,
                "models": models,
                "authentication_verified": False,
                "note": "Model discovery succeeded. Catalogs may be public: this does not verify credentials, chat completions, account limits, or tool support.",
            }
            self.store.emit("provider.discovery", {k: v for k, v in result.items() if k != "models"})
            return result
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(self.vault.redact(str(exc))) from exc

    async def close(self):
        await self.client.aclose()
