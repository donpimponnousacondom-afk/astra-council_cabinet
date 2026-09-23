"""Shared runtime feedback test helpers."""

import json
from unittest.mock import AsyncMock

import httpx
from conftest import configured, ingest


def tool(name, arguments, call_id="call"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
        },
    }


def reply(*calls):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"tool_calls": list(calls)},
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        },
    )


def responses(body):
    return [json.loads(message["content"]) for message in body["messages"] if message["role"] == "tool"]


def ready(kernel, **changes):
    bot = configured(kernel, **changes)
    ingest(kernel)
    transport = AsyncMock()
    transport.send.return_value = "888888888888888888"
    kernel.engine.transport = transport
    return bot, transport


def enable_documents(kernel):
    plugin = kernel.store.get("plugins", "document_site")
    plugin.pop("revision")
    kernel.store.put("plugins", {**plugin, "enabled": True})
