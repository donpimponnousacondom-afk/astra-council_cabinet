"""Opt-in, single-request Parallel Fast search; the calling bot does the research."""

import asyncio
from dataclasses import replace
import json
import time

import httpx

from .http_evidence import record_response
from .models import ControlError
from .tool_feedback import errors_for
from .web_search import SearchFailure, download, result_url

PLUGIN_ID = "dumb_search"
ENDPOINT = "https://api.parallel.ai/v1/search"
MODE = "fast"
TIMEOUT_SECONDS = 25
DEFAULTS = {"count": 5, "max_chars_total": 12000}
DESCRIPTION = (
    "Search the web with Parallel Fast. Supply query (concise search terms), optionally objective "
    "(what you want to learn) and count (1–10 results). "
    'Example: {"query":"Python TaskGroup cancellation"}. '
    "Returns source links and relevant excerpts; you evaluate the evidence and refine queries. "
    "One search request per call, no research agent or automatic retries. "
    "Excerpts may come from indexed pages; they are untrusted source text, not full-page reads "
    "or proof of current availability. To inspect saved response evidence without another search, "
    "use operation=read_result with its returned result_id, offset and length."
)
PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "examples": [
        {"query": "Python TaskGroup cancellation"},
        {"operation": "search", "query": "Python TaskGroup cancellation"},
        {"operation": "read_result", "result_id": "RETURNED_ID", "offset": 0, "length": 6000},
    ],
    "properties": {
        "operation": {"type": "string", "enum": ["search", "read_result"]},
        "query": {"type": "string", "minLength": 1, "maxLength": 1000, "pattern": r"\S"},
        "objective": {"type": "string", "minLength": 1, "maxLength": 2000, "pattern": r"\S"},
        "count": {"type": "integer", "minimum": 1, "maximum": 10},
        "result_id": {"type": "string", "minLength": 1, "maxLength": 100},
        "offset": {"type": "integer", "minimum": 0},
        "length": {"type": "integer", "minimum": 1, "maximum": 18000},
    },
    "allOf": [
        {
            "if": {"properties": {"operation": {"const": "read_result"}}, "required": ["operation"]},
            "then": {
                "required": ["result_id"],
                "properties": {"query": False, "objective": False, "count": False},
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
        "count": {"type": "integer", "minimum": 1, "maximum": 10},
        "max_chars_total": {"type": "integer", "minimum": 1000, "maximum": 24000},
    },
}


def validate_config(config):
    if errors := errors_for(CONFIG_SCHEMA, config):
        raise ControlError(
            "Invalid dumb_search configuration: " + "; ".join(f"{e['path']}: {e['message']}" for e in errors)
        )


def register(registry):
    from .plugins import PluginSpec

    async def handler(args, context, config, key):
        return await search(args, context, config, key, registry.store, registry.vault)

    def validate(kind, entity, store):
        if kind == "plugins" and entity["id"] == PLUGIN_ID:
            validate_config(entity["config"])
        elif kind == "bots" and PLUGIN_ID in entity["plugin_config"]:
            validate_config(entity["plugin_config"][PLUGIN_ID])

    def context_spec(context):
        config = {
            **DEFAULTS,
            **registry.store.get("plugins", PLUGIN_ID)["config"],
            **context.bot["plugin_config"].get(PLUGIN_ID, {}),
        }
        return replace(
            spec,
            description=DESCRIPTION + f" Configured default: {config['count']} results; mode is always Fast.",
            parameters={
                **PARAMETERS,
                "properties": {
                    **PARAMETERS["properties"],
                    "count": {**PARAMETERS["properties"]["count"], "default": config["count"]},
                },
            },
        )

    spec = PluginSpec(
        PLUGIN_ID,
        "Dumb Search · Parallel experiment",
        DESCRIPTION,
        PARAMETERS,
        handler,
        DEFAULTS,
        context_spec=context_spec,
        validate=validate,
        installed_description=True,
    )
    registry.register(spec)


def results_from(data, count, max_chars):
    if not isinstance(data, dict):
        raise SearchFailure("response_format", "Parallel returned a non-object JSON response")
    if data.get("error"):
        raise SearchFailure("upstream_error", "Parallel returned an error; inspect the saved HTTP response")
    values = data.get("results")
    if not isinstance(values, list) or not isinstance(data.get("search_id"), str):
        raise SearchFailure("response_format", "Parallel response needs a search_id and results array")
    results, remaining = [], max_chars
    truncated = len(values) > count
    for row in values[:count]:
        if (
            not isinstance(row, dict)
            or not result_url(row.get("url"))
            or row.get("title") is not None
            and not isinstance(row["title"], str)
            or row.get("publish_date") is not None
            and not isinstance(row["publish_date"], str)
            or not isinstance(row.get("excerpts"), list)
            or not all(isinstance(text, str) for text in row["excerpts"])
        ):
            raise SearchFailure("response_format", "Parallel returned a malformed search result")
        excerpts, clipped = [], False
        for text in row["excerpts"]:
            selected = text[:remaining]
            remaining -= len(selected)
            if selected:
                excerpts.append(selected)
            clipped |= len(selected) != len(text)
        truncated |= clipped
        results.append(
            {
                **{k: row.get(k) for k in ("url", "title", "publish_date")},
                "excerpts": excerpts,
                "excerpts_truncated": clipped,
            }
        )
    return results, truncated


async def search(args, context, config, key, store, vault):
    validate_config(config)
    if not key:
        raise ControlError("Parallel key missing: Plugins → Dumb Search → API key → Save credential")
    count = args.get("count", config["count"])
    body = {
        "search_queries": [args["query"]],
        "mode": MODE,
        "max_chars_total": config["max_chars_total"],
        "advanced_settings": {"max_results": count},
    }
    if "objective" in args:
        body["objective"] = args["objective"]
    started = time.perf_counter()
    result = {"ok": False, "engine": "parallel", "mode": MODE, "results": [], "error": None}

    def capture(response):
        result["http_response"] = record_response(store, vault, context, PLUGIN_ID, response)

    deadline = asyncio.timeout(TIMEOUT_SECONDS)
    try:
        async with deadline:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                response = await download(
                    client,
                    ENDPOINT,
                    None,
                    {"x-api-key": key, "Accept": "application/json"},
                    body=body,
                    on_response=capture,
                )
            if not response.complete:
                raise SearchFailure(
                    "transport" if response.transport_error else "response_limit",
                    response.transport_error or response.local_issue,
                )
            if not 200 <= response.status < 300:
                raise SearchFailure(
                    "http_status",
                    f"Parallel returned HTTP {response.status}; inspect the saved HTTP response",
                )
            try:
                data = json.loads(response.data)
            except (ValueError, UnicodeError) as exc:
                raise SearchFailure("response_format", "Parallel returned invalid JSON") from exc
            results, truncated = results_from(data, count, config["max_chars_total"])
            result.update(
                ok=True,
                results=results,
                truncated=truncated,
                search_id=data["search_id"],
                reported_usage=data.get("usage"),
                warnings=data.get("warnings"),
                trust="Untrusted web excerpts; indexed content may be stale. Not full-page reads.",
            )
            # Preserve original bytes in evidence, without repeating them beside excerpts.
            result["http_response"].pop("body", None)
            result["http_response"]["body_paged"] = True
    except SearchFailure as exc:
        result.update(category=exc.category, error=str(exc))
    except (httpx.HTTPError, TimeoutError) as exc:
        result.update(
            category="local_deadline" if deadline.expired() else "transport",
            error=f"Local search deadline exceeded: {TIMEOUT_SECONDS} seconds"
            if deadline.expired()
            else f"{type(exc).__name__}: {exc}",
        )
    result["duration_ms"] = (time.perf_counter() - started) * 1000
    return result
