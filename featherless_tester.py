#!/usr/bin/env python3
"""Operator-only Featherless discovery/benchmark CLI. See docs/FEATHERLESS_TESTER.md.

Uses the existing Python environment, never starts Kernel or writes council state.
All completion calls (including warm-up/filler) share one weighted concurrency gate.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import copy
import csv
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import sys
import time
import traceback
from urllib.parse import quote
import uuid

import httpx
import tiktoken
from cryptography.fernet import Fernet

from hortator.chat_response import (
    ChatResponse,
    SSEReader,
    UpstreamResponseError,
    ResponseFormatError,
    ResponseLimitError,
)
from hortator.provider import Completion
from hortator.footer_tokens import measure_footer_tokens

API = "https://api.featherless.ai"
DATA = Path(os.environ.get("HORTATOR_DATA_DIR", Path.home() / ".local/share/hortator"))
ENCODER = tiktoken.get_encoding("cl100k_base")
MAX_METADATA = 2 * 1024 * 1024
MAX_RESPONSE = 128 * 1024 * 1024
METRICS = [
    "model",
    "mode",
    "purpose",
    "repeat",
    "status",
    "http_status",
    "finish_reason",
    "input_tokens",
    "input_count_source",
    "output_tokens",
    "output_count_source",
    "reasoning_tokens",
    "reasoning_count_source",
    "visible_tokens_estimate",
    "tool_call_count",
    "ttft_ms",
    "first_visible_ms",
    "elapsed_s",
    "stream_tps",
    "e2e_tps",
    "error",
    "upstream_error_code",
    "record",
]


def local_tokens(text):
    return len(ENCODER.encode(text, disallowed_special=()))


def parse_selection(text, count):
    if text.strip().lower() == "all":
        return list(range(count))
    indices = set()
    for item in text.split(","):
        match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", item)
        if not match:
            raise ValueError("Selection must use numbers/ranges, e.g. 2-8,11,14-16 or all")
        first, last = int(match[1]), int(match[2] or match[1])
        if not 1 <= first <= last <= count:
            raise ValueError(f"Selection {item!r} is outside 1–{count}")
        indices.update(range(first - 1, last))
    return sorted(indices)


def object_json(value):
    result = json.loads(value)
    if not isinstance(result, dict):
        raise argparse.ArgumentTypeError("Expected a JSON object")
    return result


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Use a positive integer")
    return number


def credentials(data_dir, provider_id):
    """Read the vault in read-only mode; env key also works without any council DB."""
    key = os.environ.get("FEATHERLESS_API_KEY", "")
    headers = {"Content-Type": "application/json", "User-Agent": "Hortator-Featherless-Tester/1"}
    hidden = [key] if key else []
    database = data_dir / "council.sqlite3"
    if not key and database.exists():
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as db:
            row = db.execute(
                "SELECT body FROM entities WHERE kind='providers' AND id=?", (provider_id,)
            ).fetchone()
            if row:
                config = json.loads(row[0])
                # This tool is specifically for Featherless, never forward its key to another origin.
                if config["base_url"].rstrip("/") != API + "/v1":
                    raise ValueError("Selected provider is not https://api.featherless.ai/v1")
                headers.update(config.get("headers", {}))
            master = os.environ.get("HORTATOR_MASTER_KEY") or (data_dir / "master.key").read_text().strip()
            vault = Fernet(master.encode())
            for scope, encrypted in db.execute("SELECT scope,value FROM secrets"):
                secret = vault.decrypt(encrypted).decode()
                if not scope.endswith("/user_id"):
                    hidden.append(secret)
                if scope == f"provider/{provider_id}/api_key":
                    key = secret
    if not key:
        raise ValueError(
            "Set FEATHERLESS_API_KEY in the environment, or configure the Featherless provider in Hortator"
        )
    headers = httpx.Headers(headers)
    headers["Authorization"] = "Bearer " + key
    return headers, [v for v in hidden if len(v) >= 6]


class Evidence:
    def __init__(self, directory, hidden=()):
        self.root = directory
        self.root.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.hidden = sorted(hidden, key=len, reverse=True)
        self.rows = []

    def redact(self, value):
        if isinstance(value, str):
            for secret in self.hidden:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {
                k: (
                    "[REDACTED]"
                    if k.lower() in {"authorization", "api_key", "cookie", "set-cookie"}
                    else self.redact(v)
                )
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        return value

    def log(self, text):
        text = self.redact(str(text))
        text = re.sub(r"[\x00-\x1f\x7f-\x9f]", lambda m: f"\\u{ord(m[0]):04x}", text)
        print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {text}", flush=True)

    def write(self, name, value):
        path = self.root / name
        with path.open("x", encoding="utf-8") as output:
            os.chmod(path, 0o600)
            json.dump(self.redact(value), output, ensure_ascii=True, indent=2, allow_nan=False)
            output.write("\n")
        return str(path)

    def result(self, row):
        path = self.write(f"request-{uuid.uuid4().hex[:12]}.json", row)
        row["record"] = path
        summary = {k: row.get(k) for k in METRICS}
        summary["record"] = path
        self.rows.append(summary)
        path = self.root / "results.csv"
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as output:
            os.chmod(path, 0o600)
            writer = csv.DictWriter(output, fieldnames=METRICS)
            if new:
                writer.writeheader()
            writer.writerow(self.redact(summary))
        ttft = f"{row['ttft_ms']:.1f}ms" if row.get("ttft_ms") is not None else "unknown"
        speed = f"{row['stream_tps']:.1f}" if row.get("stream_tps") is not None else "unknown"
        e2e = f"{row['e2e_tps']:.1f}" if row.get("e2e_tps") is not None else "unknown"
        self.log(
            f"{row['model']} {row['mode']} {row['purpose']} {row['status']} "
            f"HTTP={row.get('http_status')} finish={row.get('finish_reason')} TTFT={ttft} "
            f"TPS={speed} E2E_TPS={e2e} elapsed={row.get('elapsed_s')}s "
            f"tokens={row.get('input_tokens')}/{row.get('output_tokens')} "
            f"reasoning={row.get('reasoning_tokens')}"
            + (f" error={row['error']}" if row.get("error") else "")
        )


class Units:
    def __init__(self, workers, total, ignore=False):
        self.workers, self.total, self.ignore = workers, total, ignore
        self.active = self.used = 0
        self.condition = asyncio.Condition()

    @asynccontextmanager
    async def slot(self, cost):
        cost = 1 if self.ignore else int(cost or self.total)
        if cost > self.total:
            raise ValueError(f"Model requires {cost} units; configured allowance is {self.total}")
        async with self.condition:
            await self.condition.wait_for(
                lambda: self.active < self.workers and self.used + cost <= self.total
            )
            self.active += 1
            self.used += cost
        try:
            yield
        finally:
            async with self.condition:
                self.active -= 1
                self.used -= cost
                self.condition.notify_all()


def tier(model):
    return (model.get("availability") or {}).get("tier", "unknown")


def not_hot(model):
    availability = model.get("availability") or {}
    return availability.get("is_hot_live") is False and availability.get("is_hot_recent") is False


def needs_warmup(model):
    return tier(model) in {"cold", "loading", "offline"} or not_hot(model)


def availability_matches(model, selected):
    return selected == "all" or (not_hot(model) if selected == "not-hot" else tier(model) == selected)


def ceiling(model, plan, override=None):
    limits = [
        n
        for n in (model.get("context_length"), plan.get("max_context_length"), override)
        if isinstance(n, int) and n > 0
    ]
    return min(limits) if limits else None


class Tester:
    def __init__(self, client, evidence, options):
        self.client, self.evidence, self.options = client, evidence, options
        self.gate = Units(options.workers, options.workers, True)
        self.plan = {}

    async def api(self, method, path, *, params=None, body=None):
        # Unfiltered catalog downloads are forbidden, even if a caller makes a mistake.
        if path == "/v1/models" and (not params or not params.get("q") or not params.get("per_page")):
            raise ValueError("Catalog requests require a search filter and explicit page size")
        async with asyncio.timeout(30):
            async with self.client.stream(method, API + path, params=params, json=body) as response:
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_METADATA:
                        raise ValueError(
                            "Metadata response exceeds 2 MiB; server may have ignored pagination"
                        )
                    chunks.append(chunk)
                raw = b"".join(chunks).decode("utf-8", errors="replace")
                self.evidence.write(
                    f"metadata-{uuid.uuid4().hex[:12]}.json",
                    {
                        "method": method,
                        "url": str(response.url),
                        "http_status": response.status_code,
                        "headers": dict(response.headers),
                        "body": raw,
                    },
                )
                if response.status_code >= 400:
                    raise ValueError(f"HTTP {response.status_code}: {raw}")
                return json.loads(raw)

    async def detail(self, model_id):
        model = await self.api("GET", "/v1/models/" + quote(model_id, safe="/"))
        if not isinstance(model, dict) or model.get("id") != model_id:
            raise ValueError(f"Unexpected metadata for {model_id}")
        return model

    async def discover(self):
        self.plan = await self.api("GET", "/v1/plan")
        total = int(self.options.units or self.plan.get("concurrency") or 1)
        if self.options.ignore_plan_units:
            total = self.options.workers
        elif self.plan.get("concurrency"):
            total = min(total, int(self.plan["concurrency"]))
        self.gate = Units(self.options.workers, total, self.options.ignore_plan_units)
        ids = list(self.options.model or [])
        for query in self.options.filter or []:
            if query.lower() == "qween":
                query = "qwen"
                self.evidence.log("Search spelling: qween → qwen")
            result = await self.api(
                "GET",
                "/v1/models",
                params={
                    "q": query,
                    "page": self.options.page,
                    "per_page": self.options.limit,
                    "available_on_current_plan": "true",
                    "sort": "-popularity",
                },
            )
            entries = result.get("data", [])
            if not isinstance(entries, list):
                raise ValueError("Catalog data is not a list")
            if len(entries) > self.options.limit:
                self.evidence.log(
                    f"Catalog returned {len(entries)} filtered entries despite per_page={self.options.limit}; "
                    "applying the requested limit locally (download remains bounded to 2 MiB)"
                )
            ids.extend(m["id"] for m in entries[: self.options.limit])
        models = []
        for model_id in dict.fromkeys(ids):
            try:
                model = await self.detail(model_id)
                if availability_matches(model, self.options.availability):
                    models.append(model)
            except (ValueError, httpx.HTTPError, TimeoutError) as exc:
                self.evidence.log(f"Metadata failed {model_id}: {exc}")
        self.evidence.write("catalog.json", {"plan": self.plan, "models": models})
        self.evidence.log(
            f"Plan context={self.plan.get('max_context_length')} units={self.plan.get('concurrency')}; "
            f"workers={self.options.workers} inference-unit allowance={total}"
        )
        print(" #  Tier      Live/Recent  Units  Model context / effective  Vision  Model", flush=True)
        for index, model in enumerate(models, 1):
            print(
                f"{index:2}  {tier(model):8}  "
                f"{str((model.get('availability') or {}).get('is_hot_live', '?'))}/"
                f"{str((model.get('availability') or {}).get('is_hot_recent', '?')):5}  "
                f"{str(model.get('concurrency_cost', '?')):>5}  "
                f"{str(model.get('context_length', '?')):>9} / {str(ceiling(model, self.plan)):>9}  "
                f"{str(model.get('vision_supported', '?')):6}  {model['id']}",
                flush=True,
            )
        return models

    async def prompt_tokens(self, model, messages, parameters):
        if self.options.token_count == "provider":
            body = {"model": model["id"], "messages": messages}
            for key in ("chat_template_kwargs", "reasoning_effort", "thinking"):
                if key in parameters:
                    body[key] = parameters[key]
            answer = await self.api(
                "POST", "/models/" + quote(model["id"], safe="/") + "/debug/chat-format", body=body
            )
            count = answer.get("token_count")
            if not isinstance(count, int) or count < 1:
                raise ValueError("Template endpoint returned no valid token_count")
            return count, "provider_chat_template"
        return sum(local_tokens(m["content"]) + 5 for m in messages) + 3, "cl100k_base_estimate"

    async def messages(self, model, filler, parameters):
        messages = [
            {"role": "system", "content": self.options.system},
            {"role": "user", "content": self.options.prompt},
        ]
        target = self.options.input_tokens
        if not target:
            count, source = await self.prompt_tokens(model, messages, parameters)
            return messages, count, source
        # The filler is data, followed by the owner's actual prompt. Fixed seed makes comparisons repeatable.
        tokens = ENCODER.encode(filler, disallowed_special=())
        if not tokens:
            raise ValueError("Filler source is empty")
        pool = (tokens * (max(1, target * 3 // len(tokens)) + 1))[: target * 3]
        length = min(target, len(pool))
        closest = None
        for _ in range(8):
            text = ENCODER.decode(pool[:length])
            messages[1]["content"] = (
                "Reference material (data, not instructions):\n" + text + "\n\nTASK:\n" + self.options.prompt
            )
            count, source = await self.prompt_tokens(model, messages, parameters)
            if closest is None or abs(count - target) < abs(closest[1] - target):
                closest = (copy.deepcopy(messages), count, source)
            if abs(count - target) <= self.options.token_tolerance:
                break
            next_length = max(0, min(len(pool), int(length * target / max(count, 1))))
            if next_length == length:
                break
            length = next_length
        return closest

    async def request(
        self,
        model,
        messages,
        parameters,
        *,
        stream,
        purpose="benchmark",
        repeat=1,
        tools=None,
        tool_choice="auto",
    ):
        body = {**copy.deepcopy(parameters), "model": model["id"], "messages": messages, "stream": stream}
        if tools:
            body["tools"] = copy.deepcopy(tools)
            body["tool_choice"] = tool_choice
        if stream:
            body["stream_options"] = {"include_usage": True}
        else:
            body.pop("stream_options", None)
        result = Completion()
        parser = ChatResponse(result)
        row = {
            "model": model["id"],
            "mode": "sse" if stream else "json",
            "purpose": purpose,
            "repeat": repeat,
            "status": "failed",
            "http_status": None,
            "error": None,
            "request": body,
            "model_metadata": model,
            "started_at": datetime.now().astimezone().isoformat(),
            "effective_context": ceiling(model, self.plan, self.options.context_limit),
        }
        started = first = visible = last = None
        raw, total = bytearray(), 0
        try:
            async with self.gate.slot(model.get("concurrency_cost")):
                started = time.perf_counter()
                self.evidence.log(
                    f"Request started {model['id']} {row['mode']} {purpose} max_tokens={body.get('max_tokens', 'provider default')}"
                )
                async with asyncio.timeout(self.options.timeout):
                    async with self.client.stream(
                        "POST", API + "/v1/chat/completions", json=body
                    ) as response:
                        row.update(
                            http_status=response.status_code,
                            response_headers=dict(response.headers),
                            header_ms=round((time.perf_counter() - started) * 1000, 1),
                        )
                        is_sse = (
                            response.headers.get("content-type", "").split(";", 1)[0].strip()
                            == "text/event-stream"
                        )
                        row["response_format"] = "sse" if is_sse else "json"
                        if is_sse and response.status_code < 400:
                            reader = SSEReader(result.response_diagnostics)
                            async for frame in reader.frames(response):
                                packet = f"event: {frame.event}\ndata: {frame.data}\n\n".encode()
                                total += len(packet)
                                if total > MAX_RESPONSE:
                                    raise ValueError("Local diagnostic capture exceeded 128 MiB")
                                raw.extend(packet)
                                before = (
                                    len(result.content),
                                    len(result.reasoning_content),
                                    len(str(result.reasoning_details)),
                                    len(str(result.tool_calls)),
                                )
                                done = parser.frame(frame)
                                after = (
                                    len(result.content),
                                    len(result.reasoning_content),
                                    len(str(result.reasoning_details)),
                                    len(str(result.tool_calls)),
                                )
                                if before != after and parser.activity():
                                    last = time.perf_counter()
                                    first = first or last
                                    if result.content:
                                        visible = visible or last
                                if done:
                                    break
                            if not result.finish_reason:
                                raise ValueError("SSE ended without a finish_reason; output remains partial")
                        else:
                            async for chunk in response.aiter_bytes():
                                raw.extend(chunk)
                                if len(raw) > MAX_RESPONSE:
                                    raise ValueError("Local diagnostic capture exceeded 128 MiB")
                            if response.status_code >= 400:
                                try:
                                    envelope = json.loads(raw).get("error")
                                    if isinstance(envelope, dict):
                                        row["upstream_error_code"] = envelope.get("code")
                                except ValueError, AttributeError:
                                    pass
                                raise ValueError(
                                    f"HTTP {response.status_code}: {raw.decode('utf-8', errors='replace')}"
                                )
                            parser.packet(parser.decode(raw.decode("utf-8")), streamed=False)
                        parser.finish()
                        if not result.finish_reason:
                            raise ValueError("Response has no finish_reason; completion unconfirmed")
                        row["status"] = "length" if result.finish_reason == "length" else "completed"
        except asyncio.CancelledError:
            row.update(status="cancelled", error="Cancelled by operator; upstream may still be finishing")
            raise
        except Exception as exc:
            if isinstance(exc, UpstreamResponseError) and isinstance(exc.envelope, dict):
                row["upstream_error_code"] = exc.envelope.get("code")
            row["error"] = (
                json.dumps(exc.envelope, ensure_ascii=False)
                if isinstance(exc, UpstreamResponseError)
                else f"{type(exc).__name__}: {str(exc) or 'request deadline exceeded'}"
            )
            row["error_origin"] = (
                "upstream_error"
                if isinstance(exc, UpstreamResponseError)
                else "response_format"
                if isinstance(exc, (ResponseFormatError, ResponseLimitError))
                else "local_deadline"
                if isinstance(exc, TimeoutError)
                else "transport"
                if isinstance(exc, httpx.HTTPError)
                else "upstream_http"
                if row.get("http_status") and row["http_status"] >= 400
                else "response_format"
                if isinstance(exc, ValueError) and row.get("http_status") == 200
                else "http_or_local"
            )
            row["exception_traceback"] = traceback.format_exc()
        finally:
            elapsed = time.perf_counter() - started if started else 0
            usage = result.usage
            counts = await asyncio.to_thread(
                measure_footer_tokens,
                body,
                usage,
                result.content,
                result.reasoning_content,
                result.reasoning_details,
                result.tool_calls,
            )
            output = counts["completion"]["value"] if parser.activity() or usage else None
            source = (
                "provider_usage" if counts["completion"]["source"] == "reported" else "cl100k_base_estimate"
            )
            reasoning = counts["reasoning"]["value"]
            row.update(
                elapsed_s=round(elapsed, 3),
                finish_reason=result.finish_reason,
                ttft_ms=round((first - started) * 1000, 1) if first and stream else None,
                first_visible_ms=round((visible - started) * 1000, 1) if visible and stream else None,
                stream_tps=round(max(0, output - 1) / (last - first), 2)
                if output is not None and first and last and last > first and stream
                else None,
                e2e_tps=round(output / elapsed, 2) if output is not None and elapsed else None,
                output_tokens=output,
                output_count_source=source if output is not None else "unknown",
                reasoning_tokens=reasoning,
                reasoning_count_source=counts["reasoning"]["source"],
                visible_tokens_estimate=local_tokens(result.content),
                reasoning_tokens_estimate=local_tokens(result.reasoning_content),
                input_tokens=usage.get("prompt_tokens"),
                input_count_source="provider_usage" if usage.get("prompt_tokens") is not None else "unknown",
                usage=usage,
                content=result.content,
                reasoning_content=result.reasoning_content,
                reasoning_details=result.reasoning_details,
                tool_calls=result.tool_calls,
                tool_call_count=len(result.tool_calls),
                assistant_message=result.message(),
                token_evidence=counts,
                diagnostics=result.response_diagnostics,
                raw_response=raw.decode("utf-8", errors="replace"),
                raw_response_note="SSE events reconstructed after framing; JSON body as received. Local limit 128 MiB.",
            )
            self.evidence.result(row)
        return row

    async def ready(self, model):
        if not needs_warmup(model) or not self.options.warm:
            return model
        deadline = time.monotonic() + self.options.warm_timeout
        # A valid one-token inference request requests loading; an empty/invalid body would not.
        result = await self.request(
            model,
            [{"role": "user", "content": "Reply OK."}],
            {"max_tokens": 1},
            stream=False,
            purpose="warmup",
        )
        if result["status"] in {"completed", "length"}:
            return model
        if result["http_status"] in {401, 403}:
            return None
        previous = None
        while time.monotonic() < deadline:
            await asyncio.sleep(min(self.options.poll_seconds, max(0, deadline - time.monotonic())))
            try:
                current = await self.detail(model["id"])
                state = json.dumps(current.get("availability") or {}, sort_keys=True)
                if state != previous:
                    self.evidence.log(f"Warm-up status {model['id']}: {state}; metadata may lag five minutes")
                    previous = state
                if not needs_warmup(current) and tier(current) == "warm":
                    return current
            except (ValueError, httpx.HTTPError, TimeoutError) as exc:
                self.evidence.log(f"Warm-up metadata {model['id']}: {exc}")
        self.evidence.log(f"Warm-up wait expired for {model['id']}; making one final readiness probe")
        result = await self.request(
            model,
            [{"role": "user", "content": "Reply OK."}],
            {"max_tokens": 1},
            stream=False,
            purpose="warmup_final",
        )
        return model if result["status"] in {"completed", "length"} else None

    async def benchmark(self, model, filler, parameters):
        try:
            model = await self.ready(model)
            if model is None:
                return
            messages, count, source = await self.messages(model, filler, parameters)
            limit = ceiling(model, self.plan, self.options.context_limit)
            cap = parameters.get("max_tokens")
            risk = limit is not None and (count + (cap or 0) > limit or count >= limit)
            self.evidence.log(
                f"Input {model['id']}: {count} ({source}), output cap={cap or 'provider default'}, "
                f"effective window={limit}, over-budget={risk}"
            )
            self.evidence.write(
                f"prompt-{uuid.uuid4().hex[:12]}.json",
                {
                    "model": model["id"],
                    "messages": messages,
                    "parameters": parameters,
                    "input_tokens": count,
                    "source": source,
                    "target": self.options.input_tokens,
                    "over_budget": risk,
                    "effective_context": limit,
                },
            )
            if risk and not self.options.allow_overflow:
                self.evidence.log(
                    f"Skipped {model['id']}: prompt + output exceeds window; use --allow-overflow to test rejection intentionally"
                )
                return
            modes = [True, False] if self.options.mode == "both" else [self.options.mode == "sse"]
            for repeat in range(1, self.options.repeat + 1):
                for stream in modes:
                    for attempt in range(self.options.retries + 1):
                        result = await self.request(model, messages, parameters, stream=stream, repeat=repeat)
                        if result["status"] in {"completed", "length"}:
                            break
                        retry = (
                            result["http_status"] in {408, 429, 500, 502, 503, 504}
                            or result.get("error_origin") in {"transport", "local_deadline"}
                            or result.get("upstream_error_code")
                            in {"capacity_exhausted", "rate_limit_exceeded", "server_error"}
                        )
                        if not retry or attempt == self.options.retries:
                            break
                        await asyncio.sleep(self.options.retry_delay)
                    if self.options.show_reasoning and result.get("reasoning_content"):
                        self.evidence.log(f"Reasoning {model['id']}: {result['reasoning_content']}")
                    if self.options.show_output and result.get("content"):
                        self.evidence.log(f"Output {model['id']}: {result['content']}")
        except Exception as exc:
            self.evidence.log(
                f"Model preparation failed {model['id'] if model else 'unknown'}: {type(exc).__name__}: {exc}"
            )
            self.evidence.result(
                {
                    "model": model["id"] if model else "unknown",
                    "mode": self.options.mode,
                    "purpose": "preparation",
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "exception_traceback": traceback.format_exc(),
                }
            )


def make_filler(seed, count):
    rng = random.Random(seed)
    words = "river forest city engineer archive copper clock distant report experiment railway garden signal history measurement satellite library valley weather lantern machine bridge".split()
    return "\n".join(
        f"Record {i}: " + " ".join(rng.choices(words, k=30)) + "." for i in range(max(1, count // 30))
    )


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--filter", action="append", help="Server-side model search; repeat to combine filters")
    p.add_argument("--model", action="append", help="Exact model ID; bypasses catalog search; repeatable")
    p.add_argument("--limit", type=positive, default=30, help="Results per filtered page (maximum 100)")
    p.add_argument("--page", type=positive, default=1)
    p.add_argument(
        "--availability", choices=["all", "warm", "cold", "loading", "unknown", "not-hot"], default="all"
    )
    p.add_argument("--list", action="store_true", help="Metadata only; never start inference")
    p.add_argument("--select", help="1-based selection: 2-8,11 or all")
    p.add_argument("--yes", action="store_true", help="Use CLI values without interactive selection/settings")
    p.add_argument("--workers", type=positive, default=1)
    p.add_argument("--units", type=positive, help="Additional local concurrency-unit ceiling")
    p.add_argument(
        "--ignore-plan-units", action="store_true", help="Intentionally oversubscribe using --workers only"
    )
    p.add_argument("--mode", choices=["sse", "json", "both"], default="sse")
    p.add_argument(
        "--no-stream", action="store_const", dest="mode", const="json", help="Alias for --mode json"
    )
    p.add_argument("--max-tokens", type=positive, default=512, help="Combined output/reasoning request cap")
    p.add_argument(
        "--omit-max-tokens", action="store_true", help="Test the provider default output allowance"
    )
    p.add_argument("--temperature", type=float)
    p.add_argument("--top-p", type=float)
    p.add_argument("--reasoning-effort", help="Exact top-level provider value, not translated per model")
    p.add_argument("--thinking", choices=["default", "on", "off"], default="default")
    p.add_argument(
        "--params", type=object_json, default={}, help="Additional native request JSON; CLI controls win"
    )
    p.add_argument("--system", default="You are a concise technical assistant.")
    p.add_argument(
        "--prompt",
        default="Explain why a database uses transactions. Give a concrete example and discuss isolation in about 250 words.",
    )
    p.add_argument(
        "--input-tokens", type=positive, help="Target total prompt tokens including system/template/filler"
    )
    p.add_argument("--context-limit", type=positive, help="Additional local model-context ceiling")
    p.add_argument("--token-count", choices=["provider", "local"], default="provider")
    p.add_argument("--token-tolerance", type=positive, default=32)
    p.add_argument("--filler-file", type=Path, help="Reuse a text corpus; repeated/trimmed for each model")
    p.add_argument(
        "--generate-filler", metavar="MODEL_ID", help="Generate a reusable corpus with this model first"
    )
    p.add_argument("--filler-output-tokens", type=positive, default=4096)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--allow-overflow", action="store_true", help="Send deliberately oversized context experiments"
    )
    p.add_argument("--warm", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--warm-timeout", type=positive, default=600)
    p.add_argument("--poll-seconds", type=positive, default=15)
    p.add_argument("--timeout", type=positive, default=180, help="Total seconds for each inference request")
    p.add_argument("--repeat", type=positive, default=1)
    p.add_argument(
        "--retries", type=int, default=0, help="Additional transient attempts, separately recorded"
    )
    p.add_argument("--retry-delay", type=positive, default=10)
    p.add_argument("--show-reasoning", action="store_true")
    p.add_argument("--show-output", action="store_true")
    p.add_argument(
        "--tools",
        default="",
        help="Simulated tools, comma-separated; all enables the eight fixtures. Omit for no tools.",
    )
    p.add_argument("--disable-tools", default="", help="Comma-separated simulated tool names to remove")
    p.add_argument(
        "--tool-suite",
        choices=["custom", "baseline"],
        default="custom",
        help="Custom uses --prompt; baseline runs identical memory CRUD/web/email tasks",
    )
    p.add_argument(
        "--tool-exposure",
        choices=["isolated", "all"],
        default="isolated",
        help="Baseline exposes one selected name per case, or all enabled tools",
    )
    p.add_argument("--tool-guidance", choices=["runtime", "simple", "none"], default="runtime")
    p.add_argument(
        "--tool-system-layout",
        choices=["merged", "separate"],
        default="merged",
        help="Merge adjacent system layers like Hortator; separate reproduces early laboratory trials",
    )
    p.add_argument(
        "--tool-prompt-file", type=Path, help="Replace only the tester's tool guidance with editable text"
    )
    p.add_argument(
        "--tool-schema",
        choices=["runtime", "conditional", "simple"],
        default="runtime",
        help="Runtime includes the {} usage wrapper; conditional omits it; simple also omits conditionals. Validation stays strict.",
    )
    p.add_argument(
        "--tool-choice",
        choices=["auto", "required"],
        default="auto",
        help="Initial tool choice; later rounds use auto so the model can finish with text",
    )
    p.add_argument("--tool-rounds", type=positive, default=6)
    p.add_argument(
        "--tool-template-probe",
        action="store_true",
        help="Capture provider-rendered initial and first continuation templates (metadata only)",
    )
    p.add_argument("--tool-calls-per-round", type=positive, default=4)
    p.add_argument(
        "--model-switch-delay",
        type=int,
        default=16,
        help="Tool mode: seconds between model blocks to avoid model-switch throttling",
    )
    p.add_argument(
        "--tool-memory-file",
        type=Path,
        help="JSON baseline copied into independent per-case notebooks; original is never edited",
    )
    p.add_argument(
        "--tool-fixtures-file",
        type=Path,
        help="JSON overrides for simulated web/email results; no external effects",
    )
    p.add_argument("--data-dir", type=Path, default=DATA)
    p.add_argument("--provider-id", default="featherless")
    p.add_argument("--output", type=Path, help="New private report directory outside source Git")
    return p


def parameters(options):
    body = copy.deepcopy(options.params)
    reserved = {"messages", "model", "stream", "stream_options", "tools", "tool_choice", "n"} & body.keys()
    if reserved:
        raise ValueError("Use CLI controls, not --params, for: " + ", ".join(sorted(reserved)))
    body.pop("max_completion_tokens", None)
    body.pop("max_output_tokens", None)
    if options.omit_max_tokens:
        body.pop("max_tokens", None)
    else:
        body["max_tokens"] = options.max_tokens
    for flag in ("temperature", "top_p", "reasoning_effort"):
        if getattr(options, flag) is not None:
            body[flag] = getattr(options, flag)
    if options.thinking != "default":
        body["chat_template_kwargs"] = {
            **body.get("chat_template_kwargs", {}),
            "enable_thinking": options.thinking == "on",
        }
    return body


async def main_async(options):
    headers, hidden = credentials(options.data_dir.resolve(), options.provider_id)
    root = options.output or options.data_dir / "benchmarks" / (
        datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    )
    root = root.resolve()
    if root.is_relative_to(Path(__file__).resolve().parent):
        raise ValueError("Put benchmark evidence outside the source checkout (--output /tmp/...) ")
    evidence = Evidence(root, hidden)
    evidence.log(f"Evidence: {root}")
    async with httpx.AsyncClient(
        headers=headers, timeout=None, trust_env=False, follow_redirects=False
    ) as client:
        tester = Tester(client, evidence, options)
        models = await tester.discover()
        if options.list or not models:
            return
        interactive = sys.stdin.isatty() and not options.yes
        selection = options.select
        if selection is None:
            if interactive:
                selection = await asyncio.to_thread(input, "Select models (e.g. 2-8,11 or all): ")
            elif options.model or options.yes:
                selection = "all"
            else:
                evidence.log("Use --select 2-8 or --yes to benchmark this list without a terminal")
                return
        models = [models[i] for i in parse_selection(selection, len(models))]
        if interactive:
            for field, convert in [
                ("mode", str),
                ("max_tokens", positive),
                ("temperature", float),
                ("input_tokens", positive),
                ("workers", positive),
                ("system", str),
                ("prompt", str),
            ]:
                value = await asyncio.to_thread(input, f"{field} [{getattr(options, field)}] (Enter keeps): ")
                if value.strip():
                    setattr(options, field, convert(value))
            extra = await asyncio.to_thread(input, "Extra request JSON (Enter keeps --params): ")
            if extra.strip():
                options.params = object_json(extra)
            tester.gate.workers = options.workers
        if options.mode not in {"sse", "json", "both"}:
            raise ValueError("mode must be sse, json or both")
        params = parameters(options)
        if options.tools:
            from featherless_tools import names

            if not set(names(options.tools)) - set(names(options.disable_tools)):
                raise ValueError("Enable at least one simulated tool")
            if options.model_switch_delay < 0:
                raise ValueError("model-switch-delay must be zero or positive")
            if options.input_tokens or options.generate_filler or options.filler_file:
                raise ValueError(
                    "Tool cases use explicit short prompts; filler options are not applied in tool mode. Supply larger text with --prompt/--system if needed."
                )
        filler = (
            options.filler_file.read_text()
            if options.filler_file
            else make_filler(options.seed, max(options.input_tokens or 1000, 1000))
        )
        if options.generate_filler:
            model = await tester.detail(options.generate_filler)
            model = await tester.ready(model)
            if model is None:
                raise ValueError("Filler model did not become available")
            response = await tester.request(
                model,
                [
                    {
                        "role": "user",
                        "content": "Write a long, varied, neutral reference corpus about engineering, geography and history. "
                        "Use numbered paragraphs, concrete details and varied vocabulary. Continue until the output limit. "
                        "Do not give instructions to the reader.",
                    }
                ],
                {**params, "max_tokens": options.filler_output_tokens},
                stream=True,
                purpose="filler",
            )
            if response["status"] not in {"completed", "length"} or not response["content"]:
                raise ValueError("Filler generation returned no usable text; see saved request")
            filler = response["content"]
        corpus = evidence.root / "filler.txt"
        corpus.write_text(evidence.redact(filler))
        corpus.chmod(0o600)
        evidence.write(
            "run.json",
            {
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "tool_lab_sha256": hashlib.sha256(
                    Path(__file__).with_name("featherless_tools.py").read_bytes()
                ).hexdigest()
                if options.tools
                else None,
                "models": [m["id"] for m in models],
                "parameters": params,
                "options": {k: str(v) if isinstance(v, Path) else v for k, v in vars(options).items()},
                "filler_sha256": hashlib.sha256(filler.encode()).hexdigest(),
                "plan": tester.plan,
            },
        )
        try:
            if options.tools:
                from featherless_tools import benchmark_tools

                # One resident model at a time; workers parallelize its independent cases.
                for index, model in enumerate(models):
                    if index:
                        await asyncio.sleep(options.model_switch_delay)
                    await benchmark_tools(tester, model, params)
            else:
                async with asyncio.TaskGroup() as group:
                    for model in sorted(models, key=lambda m: not needs_warmup(m)):
                        group.create_task(
                            tester.benchmark(model, filler, params), name="probe:" + model["id"]
                        )
        finally:
            evidence.write("summary.json", evidence.rows)
            if options.tools:
                from featherless_report import build_report

                evidence.log(f"HTML report: {build_report(evidence.root)}")
            evidence.log(
                f"Finished; {len(evidence.rows)} inference attempts recorded. Results: {evidence.root / 'results.csv'}"
            )


def main():
    os.umask(0o077)
    args = parser().parse_args()
    if not args.filter and not args.model:
        parser().error("Supply --filter or --model; full-catalog downloads are disabled")
    if args.limit > 100 or args.workers > 32 or args.retries < 0 or args.poll_seconds < 10:
        parser().error("Require limit<=100, workers<=32, retries>=0, poll-seconds>=10")
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print(
            "Stopped. Completed and partial request evidence remains in the output directory.",
            file=sys.stderr,
        )
        return 130
    except Exception as exc:
        try:
            _, hidden = credentials(args.data_dir.resolve(), args.provider_id)
        except Exception:
            hidden = [os.environ.get("FEATHERLESS_API_KEY", "")]
        message = f"Tester stopped: {type(exc).__name__}: {exc}"
        for secret in hidden:
            if secret:
                message = message.replace(secret, "[REDACTED]")
        message = re.sub(r"[\x00-\x1f\x7f-\x9f]", lambda m: f"\\u{ord(m[0]):04x}", message)
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
