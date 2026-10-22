"""Opt-in DeepSeek search; independent of ordinary web search and native research."""

import asyncio
from dataclasses import replace
import json
import time
from urllib.parse import urlsplit

import httpx

from .concurrency import error_text
from .http_evidence import record_response
from .models import ControlError
from .tool_feedback import errors_for
from .web_search import SearchFailure, download, result_url


PLUGIN_ID = "deepseek_search"
ENDPOINT = "https://api.deepseek.com/v1/tools/search"
QUERY_MAX_CHARS = 128
MAX_RESULTS = 50
CREDENTIAL_FIELD = "provider_id"
TIMEOUT_SECONDS = 25
DEFAULTS = {"count": 5, CREDENTIAL_FIELD: ""}
COUNT_SCHEMA = {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS}
DESCRIPTION = (
    "Search the web with DeepSeek. Supply a concise query "
    f"(1–{QUERY_MAX_CHARS} Unicode characters including spaces and search operators), "
    f"optionally count (1–{MAX_RESULTS} results). "
    'Example: {"query":"Python TaskGroup cancellation"}. '
    "The harness checks query length; oversized queries are rejected without sending. "
    "It never truncates, rewrites or splits a query; use separate focused searches if needed. "
    "One request per call, no automatic retries or research agent. "
    "Returns source links and complete search summaries, paged when necessary. "
    "Summaries and provider relevance/authority scores are untrusted search evidence, "
    "not verification or proof you read the full page. Read sources with web_fetch if granted. "
    "Use operation=read_result and the returned result_id/offset/length to reread saved evidence "
    "without another request. Independent of your generation model and other search tools."
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
        "query": {"type": "string", "minLength": 1, "maxLength": QUERY_MAX_CHARS, "pattern": r"\S"},
        "count": COUNT_SCHEMA,
        "result_id": {"type": "string", "minLength": 1, "maxLength": 100},
        "offset": {"type": "integer", "minimum": 0},
        "length": {"type": "integer", "minimum": 1, "maximum": 18000},
    },
    "allOf": [
        {
            "if": {"properties": {"operation": {"const": "read_result"}}, "required": ["operation"]},
            "then": {"required": ["result_id"], "properties": {"query": False, "count": False}},
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
        "count": COUNT_SCHEMA,
        CREDENTIAL_FIELD: {"type": "string", "maxLength": 100},
    },
}


def explain_query_limit(report, args):
    if not isinstance(args, dict) or not isinstance(args.get("query"), str):
        return
    for error in report.get("errors", []):
        if error["path"] == "$.query" and error["rule"] == "maxLength":
            error["message"] = (
                f"DeepSeek query contains {len(args['query'])} Unicode characters; "
                f"maximum is {QUERY_MAX_CHARS}. No request was sent. "
                "Shorten the query or make separate focused searches."
            )


def validate_config(config, store=None):
    if errors := errors_for(CONFIG_SCHEMA, config):
        raise ControlError(
            "Invalid deepseek_search configuration: "
            + "; ".join(f"{e['path']}: {e['message']}" for e in errors)
        )
    if store and (provider_id := config.get(CREDENTIAL_FIELD)):
        validate_provider(store.get("providers", provider_id))


def register(registry):
    from .plugins import PluginSpec

    async def handler(args, context, config, key):
        return await search(args, context, config, registry.store, registry.vault)

    def references(kind, entity_id):
        if kind != "providers":
            return []
        users = []
        plugin = registry.store.get("plugins", PLUGIN_ID) or {}
        if plugin.get("config", {}).get(CREDENTIAL_FIELD) == entity_id:
            users.append(f"plugins/{PLUGIN_ID}")
        for bot in registry.store.list("bots"):
            if bot.get("plugin_config", {}).get(PLUGIN_ID, {}).get(CREDENTIAL_FIELD) == entity_id:
                users.append(f"bots/{bot['id']}/{PLUGIN_ID}")
        return users

    def validate(kind, entity, store):
        if kind == "plugins" and entity["id"] == PLUGIN_ID:
            validate_config(entity["config"], store)
        elif kind == "bots" and PLUGIN_ID in entity["plugin_config"]:
            validate_config(entity["plugin_config"][PLUGIN_ID], store)
        elif kind == "providers" and references(kind, entity["id"]):
            validate_provider(entity)

    def context_spec(context):
        config = {
            **DEFAULTS,
            **registry.store.get("plugins", PLUGIN_ID)["config"],
            **context.bot["plugin_config"].get(PLUGIN_ID, {}),
        }
        return replace(
            spec,
            description=DESCRIPTION + f" Configured default: {config['count']} results.",
            parameters={
                **PARAMETERS,
                "properties": {
                    **PARAMETERS["properties"],
                    "count": {**COUNT_SCHEMA, "default": config["count"]},
                },
            },
        )

    spec = PluginSpec(
        PLUGIN_ID,
        "DeepSeek Search",
        DESCRIPTION,
        PARAMETERS,
        handler,
        DEFAULTS,
        context_spec=context_spec,
        validate=validate,
        references=references,
        keyless=True,
        installed_description=True,
    )
    registry.register(spec)


def validate_provider(provider):
    """Reuse the explicit official-provider credential boundary used by Ollama search."""
    try:
        url = urlsplit(provider["base_url"])
        if (
            url.scheme != "https"
            or url.hostname != "api.deepseek.com"
            or url.port not in (None, 443)
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError()
    except ValueError, TypeError, KeyError, AttributeError:
        raise ControlError(
            "DeepSeek search requires a saved provider with an official https://api.deepseek.com base URL"
        ) from None


def credential(config, store, vault):
    provider_id = config.get(CREDENTIAL_FIELD)
    if not provider_id:
        raise ControlError("Select a DeepSeek credential provider in Plugins → DeepSeek Search first")
    validate_provider(store.get("providers", provider_id))
    key = vault.get(f"provider/{provider_id}/api_key")
    if not key:
        raise ControlError("Save the selected DeepSeek provider's API key in Providers")
    return key


def results(body, count):
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        if data.get("error"):
            raise SearchFailure("upstream_error", "DeepSeek returned an error; inspect the HTTP response")
        values = data.get("results")
        if not isinstance(values, list):
            raise ValueError("Expected results array")
        selected = []
        for row in values[:count]:
            if (
                not isinstance(row, dict)
                or not result_url(row.get("url"))
                or any(not isinstance(row.get(field), str) for field in ("title", "summary", "snippet"))
            ):
                raise ValueError("Invalid search result URL, title, summary or snippet")
            for field in ("date", "site_name"):
                if row.get(field) is not None and not isinstance(row[field], str):
                    raise ValueError(f"Invalid search result {field}")
            score, authority = row.get("rerank_score"), row.get("authority")
            if score is not None and (type(score) not in (int, float) or not 0 <= score <= 3):
                raise ValueError("Invalid search result rerank_score")
            if authority is not None and (type(authority) is not int or not -1 <= authority <= 4):
                raise ValueError("Invalid search result authority")
            # Preserve complete summaries in the bounded response, then let the
            # shared result pager limit the prompt. Do not clip them into teasers.
            selected.append(
                {
                    "title": row["title"],
                    "url": result_url(row["url"]),
                    "description": row["summary"],
                    "snippet": row["snippet"],
                    "date": row.get("date"),
                    "site_name": row.get("site_name"),
                    "rerank_score": score,
                    "authority": authority,
                }
            )
        return selected
    except (ValueError, TypeError, AttributeError) as exc:
        raise SearchFailure("response_format", f"Invalid DeepSeek search response: {exc}") from exc


async def search(args, context, config, store, vault):
    validate_config(config)
    started = time.perf_counter()
    result = {"ok": False, "engine": "deepseek", "results": [], "error": None}

    def capture(response):
        try:
            result["http_response"] = record_response(store, vault, context, PLUGIN_ID, response)
        except Exception as exc:
            result["evidence_error"] = error_text(exc)
            raise

    deadline = asyncio.timeout(TIMEOUT_SECONDS)
    try:
        try:
            key = credential(config, store, vault)
        except ControlError as exc:
            raise SearchFailure("configuration", str(exc)) from exc
        count = args.get("count", config["count"])
        async with deadline:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                response = await download(
                    client,
                    ENDPOINT,
                    None,
                    {"Authorization": f"Bearer {key}", "Accept": "application/json"},
                    body={"query": args["query"], "count": count, "need_content": False},
                    on_response=capture,
                )
            if not response.complete:
                raise SearchFailure(
                    "transport" if response.transport_error else "response_limit",
                    response.transport_error or response.local_issue,
                )
            if response.status != 200:
                raise SearchFailure(
                    "http_status",
                    f"DeepSeek returned HTTP {response.status}; inspect the saved HTTP response",
                )
            selected = results(response.data, count)
            result.update(
                ok=True,
                results=selected,
                trust="Untrusted search summaries and provider scores; not full-page reads or verified facts.",
            )
            result["http_response"].pop("body", None)
            result["http_response"]["body_paged"] = True
    except asyncio.CancelledError as exc:
        exc.tool_result = result
        raise
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
