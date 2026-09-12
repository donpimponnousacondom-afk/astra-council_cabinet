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
from .diagnostics import record_diagnostics, reasoning_settings
from .chat_response import (
    ChatResponse,
    Frame,
    ResponseFormatError,
    SSEReader,
    UpstreamResponseError,
    RESPONSE_LIMIT,
    ResponseLimitError,
)
from .store import dumps, uid
from .vision import ImageCache


class ProviderError(ControlError):
    def __init__(
        self, message, status=502, *, http_status=None, retry_after=0, provider_fault=True, details=None
    ):
        super().__init__(message, status)
        self.http_status, self.retry_after, self.provider_fault = http_status, retry_after, provider_fault
        self.details = details or {}


def transport_error(error, timeout_seconds):
    """Identify the observed HTTP phase without treating local capacity as an outage."""
    phases = {
        httpx.ConnectTimeout: ("connect", "Timed out establishing the provider connection", True),
        httpx.ReadTimeout: ("read", "Timed out waiting for provider response data", True),
        httpx.WriteTimeout: ("write", "Timed out sending request data to the provider", True),
        httpx.PoolTimeout: ("pool", "Timed out waiting for a local HTTP connection slot", False),
    }
    for kind, (phase, explanation, fault) in phases.items():
        if isinstance(error, kind):
            return ProviderError(
                f"{explanation} ({phase} timeout: {timeout_seconds:g} seconds; {kind.__name__})",
                provider_fault=fault,
                details={
                    "origin": "transport" if fault else "local_client",
                    "timeout_kind": phase,
                    "timeout_seconds": timeout_seconds,
                    "exception_type": kind.__name__,
                    "transport_message": str(error) or None,
                },
            )
    return ProviderError(
        f"Provider connection/protocol failure: {type(error).__name__}: "
        f"{str(error) or 'HTTP transport supplied no further detail'}",
        details={"origin": "transport", "exception_type": type(error).__name__},
    )


def strip_reasoning(content: str) -> str:
    # Public/model-facing content excludes tagged reasoning. Private diagnostics
    # retain the original provider fields separately, before this filter runs.
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


def envelope_error(error, *, private_key=False):
    """HTTP-200 error envelopes still carry request/auth/rate/server semantics.

    Keep the actual HTTP status in the ledger. Do not trip every bot's shared
    provider circuit for a single invalid model request.
    """
    fields = error if isinstance(error, dict) else {}
    code = str(fields.get("code") or fields.get("type") or "").lower()
    status = fields.get("status") or fields.get("status_code")
    try:
        status = int(status if status is not None else code)
    except ValueError, TypeError:
        status = None
    if code in {
        "bad_request",
        "invalid_request",
        "invalid_request_error",
        "invalid_argument",
        "context_length_exceeded",
        "unsupported_parameter",
        "model_not_found",
    }:
        status = 400
    elif code in {"unauthorized", "authentication_error", "invalid_api_key"}:
        status = 401
    elif code in {"permission_denied", "permission_error", "forbidden"}:
        status = 403
    elif code in {"rate_limit_exceeded", "rate_limit_error", "too_many_requests"}:
        status = 429
    elif code in {"capacity_exhausted", "overloaded", "overloaded_error", "service_unavailable"}:
        status = 503
    fault = status is None or status >= 500 or (status in (401, 403, 429) and not private_key)
    return ProviderError(f"Provider error: {error}", http_status=status, provider_fault=fault)


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
    """Normalize reported counts and snapshot the estimate's exact billing basis.

    Cache counts are parts of prompt_tokens; reasoning is already part of
    completion_tokens. Missing or inconsistent split data is never guessed.
    """
    inp, out = number(usage.get("prompt_tokens")), number(usage.get("completion_tokens"))
    completion_details = usage.get("completion_tokens_details") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    reasoning = (
        number(completion_details.get("reasoning_tokens")) if isinstance(completion_details, dict) else None
    )
    standard_hit = number(prompt_details.get("cached_tokens")) if isinstance(prompt_details, dict) else None
    vendor_hit = number(usage.get("prompt_cache_hit_tokens"))
    cached = standard_hit if standard_hit is not None else vendor_hit
    miss = number(usage.get("prompt_cache_miss_tokens"))
    split_error = None
    if standard_hit is not None and vendor_hit is not None and standard_hit != vendor_hit:
        split_error = "Reported cache-hit counts disagree."
    if inp is not None:
        if cached is not None and miss is None and cached <= inp:
            miss = inp - cached
        elif miss is not None and cached is None and miss <= inp:
            cached = inp - miss
        if (cached is not None and cached > inp) or (miss is not None and miss > inp):
            split_error = "Reported cache count exceeds total input tokens."
        elif cached is not None and miss is not None and cached + miss != inp:
            split_error = "Reported cache-hit and cache-miss counts do not sum to total input tokens."
    rates = {
        name: number(profile.get(name))
        for name in (
            "input_price_per_million",
            "output_price_per_million",
            "cache_hit_input_price_per_million",
            "cache_miss_input_price_per_million",
        )
    }
    split_configured = any(
        profile.get(key) is not None
        for key in ("cache_hit_input_price_per_million", "cache_miss_input_price_per_million")
    )
    mode = "cache_split" if split_configured else "flat"
    cost, cost_source = number(usage.get("cost")), "reported"
    note = None
    if cost is None:
        cost_source = None
        ip, op = rates["input_price_per_million"], rates["output_price_per_million"]
        hit_rate, miss_rate = (
            rates["cache_hit_input_price_per_million"],
            rates["cache_miss_input_price_per_million"],
        )
        if inp is None or out is None:
            note = "Provider did not report both total input and output token counts."
        elif split_configured:
            if hit_rate is None or miss_rate is None or op is None:
                note = "Cache-aware estimates require cache-hit, cache-miss and output rates."
            elif split_error:
                note = split_error
            elif cached is None or miss is None:
                note = "Provider did not report a cache split; no hit or miss count is assumed."
            else:
                cost, cost_source = (cached * hit_rate + miss * miss_rate + out * op) / 1_000_000, "estimated"
        elif ip is not None and op is not None:
            cost, cost_source = (inp * ip + out * op) / 1_000_000, "estimated"
        else:
            note = "Flat estimates require input and output rates."
    pricing = {
        "version": 1,
        "basis": "provider_reported" if cost_source == "reported" else mode,
        "profile_id": profile.get("id"),
        "profile_revision": profile.get("revision"),
        "currency": "USD",
        "rate_schedule": "manual_profile_snapshot",
        "rates_per_million": rates,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_hit_tokens": cached,
        "cache_miss_tokens": miss,
        "output_includes_reasoning": True,
        "cost": cost,
        "cost_source": cost_source,
        "note": note,
        "cache_split_error": split_error,
    }
    return dict(
        input_tokens=inp,
        output_tokens=out,
        reasoning_tokens=reasoning,
        cached_tokens=cached if split_error is None else None,
        cost=cost,
        cost_source=cost_source,
        pricing=pricing,
    )


@dataclass
class Completion:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    # Some compatible APIs require replaying the assistant reasoning alongside tool results.
    # Replay stays intact in the active task and private diagnostics, but is scrubbed
    # from model-readable request snapshots and never delivered to Discord.
    reasoning_content: str = ""
    reasoning_details: list[dict] = field(default_factory=list)
    request_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    response_diagnostics: dict[str, Any] = field(default_factory=dict)

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
        # Header names are case-insensitive; custom spelling must not create a
        # second User-Agent or defeat the vault-owned authentication header.
        headers = httpx.Headers({"Content-Type": "application/json"})
        headers.update(provider["headers"])
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

    def failure(self, provider, exc, *, bot_id=None, turn_id=None, request_id=None):
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
                    "reason": "Provider circuit is open; requests wait until the retry delay expires",
                },
                bot_id=bot_id,
                turn_id=turn_id,
                request_id=request_id,
                level="error",
            )
        self.store.emit(
            "provider.failure",
            {
                "provider_id": provider["id"],
                "consecutive_failures": count,
                "retry_in_seconds": delay,
                "error": error,
                "error_origin": exc.details.get("origin")
                or ("upstream_http" if exc.http_status is not None else "unknown"),
            },
            bot_id=bot_id,
            turn_id=turn_id,
            request_id=request_id,
            level="error",
        )

    async def complete(self, *, bot, profile, messages, tools, turn_id, context, purpose="generation"):
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
            omitted_caps = []
            if purpose == "compaction":
                body.update(copy.deepcopy(profile.get("compaction_request_json", {})))
                # Compaction budgets only the retained summary. These wire caps
                # combine thinking and final text, so neither generation JSON
                # nor compaction overrides may impose them on summarization.
                for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
                    if key in body:
                        omitted_caps.append(key)
                        body.pop(key)
            body.update(model=profile["model"], messages=messages, stream=profile["stream"])
            if not profile["stream"]:
                body.pop("stream_options", None)
            if tools:
                body["tools"] = tools
                body["tool_choice"] = "auto"
            if profile["stream"] and profile["include_usage"]:
                body.setdefault("stream_options", {"include_usage": True})
            request_id, started, clock = uid("req_"), time.time(), time.perf_counter()
            meta = {
                **context,
                "diagnostic_capture_version": 2,
                "queue_ms": (clock - queued) * 1000,
                "profile_revision": profile["revision"],
                "provider_revision": provider["revision"],
                "endpoint": provider["base_url"] + "/chat/completions",
            }
            if purpose == "compaction":
                meta.update(
                    compaction_output_policy="provider_default",
                    omitted_output_cap_fields=omitted_caps,
                    retained_summary_token_limit=profile["summary_tokens"],
                    summary_tokenizer="cl100k_base",
                )
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
            request_settings = {
                "reasoning": reasoning_settings(body, self.vault.redact),
                "profile_revision": profile["revision"],
            }
            self.store.emit(
                "request.started",
                {
                    "provider_id": provider["id"],
                    "purpose": purpose,
                    "profile_id": profile["id"],
                    "model": profile["model"],
                    "estimated_tokens": context.get("estimated_tokens"),
                    **request_settings,
                },
                bot_id=bot["id"],
                turn_id=turn_id,
                request_id=request_id,
            )
            result = Completion(request_id=request_id)
            ttft = visible = None
            response_state = ChatResponse(result)
            result.response_diagnostics["requested_stream"] = profile["stream"]
            total_bytes = 0
            status_code = None
            error_data = None
            diagnostic_at = clock
            deadline = asyncio.timeout(provider["timeout_seconds"])
            phase = "prepare_request"
            try:
                record_diagnostics(self.store, self.vault, request_id, result, body, status="running")
                try:
                    body["messages"] = ImageCache(self.store).wire_messages(messages, profile)
                except ControlError as exc:
                    raise ProviderError(str(exc), provider_fault=False) from exc
                # Total deadline covers streamed bodies as well as the connection, not just inactivity.
                async with deadline:
                    phase = "await_response_headers"
                    async with self.client.stream(
                        "POST",
                        provider["base_url"] + "/chat/completions",
                        headers=headers,
                        json=body,
                        timeout=provider["timeout_seconds"],
                    ) as response:
                        status_code = response.status_code
                        phase = "read_error_response" if status_code >= 300 else "read_response"
                        if response.status_code >= 300:
                            error_bytes = bytearray()
                            async for chunk in response.aiter_bytes():
                                error_bytes.extend(chunk[: 12000 - len(error_bytes)])
                                if len(error_bytes) >= 12000:
                                    break
                            raw = error_bytes.decode(errors="replace")
                            try:
                                error_data = json.loads(raw)
                            except ValueError:
                                error_data = raw
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
                        content_type = (
                            response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        )
                        is_sse = content_type == "text/event-stream"
                        phase = "read_sse" if is_sse else "read_json"
                        result.response_diagnostics.update(
                            response_format="sse" if is_sse else "json", content_type=content_type
                        )
                        if is_sse:
                            reader = SSEReader(result.response_diagnostics)
                            finished = False
                            try:
                                async for frame in reader.frames(response):
                                    finished = response_state.frame(frame)
                                    if profile["stream"] and response_state.activity():
                                        if ttft is None:
                                            ttft = (time.perf_counter() - clock) * 1000
                                            self.store.emit(
                                                "request.first_token",
                                                {"ttft_ms": ttft},
                                                bot_id=bot["id"],
                                                turn_id=turn_id,
                                                request_id=request_id,
                                            )
                                        if result.content and visible is None:
                                            visible = (time.perf_counter() - clock) * 1000
                                    if time.perf_counter() - diagnostic_at >= 2:
                                        record_diagnostics(
                                            self.store, self.vault, request_id, result, body, status="running"
                                        )
                                        diagnostic_at = time.perf_counter()
                                    if finished:
                                        break
                            except httpx.TransportError:
                                if not result.finish_reason:
                                    raise
                                # The final choice is complete; a lost usage/trailer
                                # connection cannot make that full answer incomplete.
                                response_state.note("transport_closed_after_finish")
                            if not finished and not result.finish_reason:
                                raise ProviderError(
                                    "Stream disconnected before a completion boundary",
                                    details={"origin": "transport", "reason": "incomplete_stream"},
                                )
                            result.response_diagnostics.setdefault("completion_boundary", "finish_reason")
                        else:
                            chunks = []
                            async for chunk in response.aiter_bytes():
                                total_bytes += len(chunk)
                                result.response_diagnostics["response_bytes"] = total_bytes
                                if total_bytes > RESPONSE_LIMIT:
                                    raise ResponseLimitError(
                                        "buffered JSON response", total_bytes, RESPONSE_LIMIT
                                    )
                                chunks.append(chunk)
                            raw = b"".join(chunks).decode("utf-8-sig", errors="strict")
                            result.response_diagnostics["response_bytes"] = total_bytes
                            response_state.current = Frame(raw)
                            response_state.packet(response_state.decode(raw), streamed=False)
                            # Buffered responses have no measurable TTFT/TPS.
                response_state.finish()
                record_diagnostics(self.store, self.vault, request_id, result, body, status="completed")
                result.content = strip_reasoning(result.content)
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
                                    "pricing": metrics["pricing"],
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
                        **request_settings,
                        "provider_id": provider["id"],
                        "profile_id": profile["id"],
                        "model": profile["model"],
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
                if isinstance(error, UpstreamResponseError):
                    error_data = error.envelope
                    exc = envelope_error(
                        error_data, private_key=bool(self.vault.get(f"bot/{bot['id']}/provider_key"))
                    )
                    exc.details["origin"] = "upstream_error"
                elif isinstance(error, ResponseLimitError):
                    exc = ProviderError(
                        str(error),
                        provider_fault=False,
                        details={"origin": "local_client", "reason": "response_limit", **error.details},
                    )
                    result.response_diagnostics["last_frame"] = response_state.failure_evidence(
                        self.vault.redact
                    )
                elif isinstance(error, (ResponseFormatError, UnicodeError, httpx.DecodingError)):
                    exc = ProviderError(
                        f"Response format error: {error}",
                        provider_fault=False,
                        details={"origin": "response_format", **getattr(error, "details", {})},
                    )
                    evidence_key = (
                        "last_frame"
                        if exc.details.get("field") in ("SSE body", "body")
                        or not isinstance(error, ResponseFormatError)
                        else "offending_frame"
                    )
                    result.response_diagnostics[evidence_key] = response_state.failure_evidence(
                        self.vault.redact
                    )
                elif isinstance(error, ProviderError):
                    exc = error
                elif cancelled:
                    exc = ProviderError(
                        "Request cancelled by its owning turn or runtime; partial output withheld, provider usage may be incomplete",
                        provider_fault=False,
                        details={"origin": "cancelled"},
                    )
                elif isinstance(error, TimeoutError) and deadline.expired():
                    exc = ProviderError(
                        f"Local total request deadline exceeded: {provider['timeout_seconds']:g} seconds. "
                        "Partial output withheld; adjust Providers → Total request timeout for longer requests.",
                        provider_fault=False,
                        details={
                            "origin": "local_deadline",
                            "timeout_kind": "total",
                            "timeout_seconds": provider["timeout_seconds"],
                        },
                    )
                elif isinstance(error, httpx.TransportError):
                    exc = transport_error(error, provider["timeout_seconds"])
                else:
                    # A Python/adapter bug must never masquerade as upstream downtime.
                    import traceback

                    frames = traceback.extract_tb(error.__traceback__)
                    exc = ProviderError(
                        f"Local provider-client failure: {type(error).__name__}: {error}",
                        provider_fault=False,
                        details={
                            "origin": "local_client",
                            "exception_type": type(error).__name__,
                            "location": [{"function": f.name, "line": f.lineno} for f in frames[-4:]],
                        },
                    )
                origin = exc.details.get("origin") or (
                    "upstream_http" if exc.http_status is not None else "request_configuration"
                )
                exc.request_id = request_id
                if cancelled:
                    error.request_id = request_id
                failure_context = {
                    "purpose": purpose,
                    "model": profile["model"],
                    "phase": phase,
                    "duration_ms": (time.perf_counter() - clock) * 1000,
                    **{
                        k: exc.details[k]
                        for k in ("timeout_kind", "timeout_seconds", "observed_bytes", "limit_bytes")
                        if k in exc.details
                    },
                }
                message = self.vault.redact(str(exc))[:3000]
                record_diagnostics(
                    self.store,
                    self.vault,
                    request_id,
                    result,
                    body,
                    status="cancelled" if cancelled else "failed",
                    error={
                        "envelope": error_data,
                        "message": message,
                        "error_status": exc.http_status,
                        "provider_fault": exc.provider_fault,
                        "transport_http_status": status_code,
                        "origin": origin,
                        "details": {**exc.details, **failure_context},
                    },
                )
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
                                    "tool_calls": result.tool_calls,
                                    "finish_reason": result.finish_reason,
                                    "provider_metadata": result.metadata,
                                    "pricing": metrics["pricing"],
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
                        **failure_context,
                        "provider_id": provider["id"],
                        "profile_id": profile["id"],
                        "http_status": status_code,
                        "error_status": exc.http_status,
                        "provider_fault": exc.provider_fault,
                        "error_origin": origin,
                        "response_format": result.response_diagnostics.get("response_format"),
                        "frame_index": result.response_diagnostics.get("frame_index"),
                        "field": exc.details.get("field"),
                    },
                    bot_id=bot["id"],
                    turn_id=turn_id,
                    request_id=request_id,
                    level="warning" if cancelled else "error",
                )
                if not cancelled:
                    self.failure(provider, exc, bot_id=bot["id"], turn_id=turn_id, request_id=request_id)
                    raise exc from error
                raise

    async def probe(self, provider):
        start = time.perf_counter()
        response = None
        request_headers = None
        try:
            request_headers = self.headers(provider, "")
            response = await self.client.get(
                provider["base_url"] + "/models", headers=request_headers, timeout=20
            )
            if response.status_code >= 300:
                raise ProviderError(
                    f"Provider returned HTTP {response.status_code}", http_status=response.status_code
                )
            raw = response.json()
            if not isinstance(raw, dict) or raw.get("error") or not isinstance(raw.get("data"), list):
                raise ProviderError(
                    "Provider did not return a valid model catalog", http_status=response.status_code
                )
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
        except (ProviderError, httpx.HTTPError, ValueError) as exc:
            upstream_status = response.status_code if response is not None else None
            evidence = {}
            provider_message = ""
            if response is not None:
                # Only diagnostic response headers: never cookies, authorization,
                # full outgoing headers, or transport wire logs.
                evidence["response_headers"] = {
                    key: self.vault.redact(response.headers[key])[:300]
                    for key in (
                        "content-type",
                        "server",
                        "cf-mitigated",
                        "cf-ray",
                        "x-request-id",
                        "request-id",
                        "retry-after",
                    )
                    if key in response.headers
                }
                try:
                    raw = safe_payload(self.vault.redact(response.json()))
                    if isinstance(raw, dict):
                        error = raw.get("error")
                        provider_message = error.get("message") if isinstance(error, dict) else error
                        provider_message = (
                            provider_message or raw.get("message") or raw.get("msg") or raw.get("detail")
                        )
                        raw = {
                            key: raw[key]
                            for key in (
                                "error",
                                "message",
                                "msg",
                                "detail",
                                "code",
                                "type",
                                "success",
                                "request_id",
                            )
                            if key in raw
                        }
                    preview = dumps(raw)
                except ValueError:
                    preview = self.vault.redact(response.text)
                # Redact before truncation, so a credential cannot be split and leak.
                evidence["response_excerpt"] = preview[:2000]
                evidence["response_truncated"] = len(preview) > 2000
            summary = (
                f"Provider returned HTTP {upstream_status}"
                if upstream_status is not None and upstream_status >= 300
                else str(transport_error(exc, 20))
                if isinstance(exc, httpx.TransportError)
                else str(exc) or type(exc).__name__
            )
            if isinstance(provider_message, str) and provider_message:
                summary += ": " + provider_message[:600]
            message = self.vault.redact(
                f"Model discovery for {provider['id']} failed (Hortator API HTTP 502): {summary}"
            )
            details = self.vault.redact(
                {
                    "provider_id": provider["id"],
                    "operation": "model_discovery",
                    "api_status": 502,
                    "upstream_status": upstream_status,
                    "endpoint": provider["base_url"] + "/models",
                    "duration_ms": (time.perf_counter() - start) * 1000,
                    "user_agent": request_headers.get("User-Agent", self.client.headers.get("User-Agent"))
                    if request_headers is not None
                    else None,
                    **evidence,
                }
            )
            self.store.emit("provider.discovery_failed", {**details, "error": message}, level="error")
            # Discovery is separate from completion health, on failure as on success.
            raise ProviderError(
                message, http_status=upstream_status, provider_fault=False, details=details
            ) from exc

    async def close(self):
        await self.client.aclose()
