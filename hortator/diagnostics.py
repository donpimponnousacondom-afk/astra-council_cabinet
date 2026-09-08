"""Operator-only provider evidence; never part of model inspection or Discord exports."""

from __future__ import annotations

import json
import re
import time

from .store import dumps


INLINE_REASONING = re.compile(r"<(think|thinking|analysis|reasoning)\b[^>]*>(.*?)(?:</\1\s*>|$)", re.S | re.I)


def text_content(value):
    """Compatible assistant text can be a string or an ordered list of text parts."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            part["text"]
            for part in value
            if isinstance(part, dict)
            and part.get("type") in ("text", "output_text")
            and isinstance(part.get("text"), str)
        )
    raise ValueError("Assistant text must be a string, text-part array, or null")


def record_diagnostics(store, vault, request_id, result, body, *, status, error=None):
    """Preserve returned reasoning even on an interrupted/failed stream.

    Requests remain safe for the model's inspector. Replay fields live separately,
    with their message indices, so private diagnostics can reconstruct the request.
    No image bytes, transport headers or credentials are copied here.
    """
    replay = []
    for index, message in enumerate(body.get("messages", [])):
        fields = {key: message[key] for key in ("reasoning_content", "reasoning_details") if key in message}
        if fields:
            replay.append({"message_index": index, **fields})
    output_limit = {key: body[key] for key in ("max_tokens", "max_completion_tokens") if key in body}
    data = {
        "capture": "recorded",
        "status": status,
        "finish_reason": result.finish_reason,
        "output_limits": output_limit,
        "output_tokens": result.usage.get("completion_tokens"),
        "visible_content_chars": len(INLINE_REASONING.sub("", result.content).strip()),
        "reasoning_content": result.reasoning_content,
        "reasoning_details": result.reasoning_details,
        "inline_reasoning": [
            {"tag": match[1], "text": match[2]} for match in INLINE_REASONING.finditer(result.content)
        ],
        "request_reasoning": replay,
        "provider_error": error,
    }
    # Keep provider-produced diagnostic text, while applying credential masking
    # independently of the reasoning filter used for public/model-facing output.
    data = vault.redact(data)
    store.execute(
        "INSERT INTO request_diagnostics VALUES(?,?,?) ON CONFLICT(request_id) "
        "DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at",
        (request_id, dumps(data), time.time()),
    )


def read_diagnostics(store, request_id, *, limit=None):
    row = store.one(
        "SELECT substr(body,1,?) AS body,updated_at FROM request_diagnostics WHERE request_id=?",
        (limit + 1 if limit is not None else 2_147_483_647, request_id),
    )
    if not row:
        return {
            "capture": "unavailable",
            "note": "Reasoning was not recorded for this request. Older discarded reasoning cannot be recovered.",
        }
    if limit is not None and len(row["body"]) > limit:
        return {
            "capture": "console_limit",
            "note": "Private diagnostics exceed the console snapshot limit. Open this request in the authenticated dashboard.",
        }
    return {**json.loads(row["body"]), "updated_at": row["updated_at"]}
