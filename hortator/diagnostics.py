"""Operator-only provider evidence; never part of model inspection or Discord exports."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

from .store import dumps


INLINE_REASONING = re.compile(r"<(think|thinking|analysis|reasoning)\b[^>]*>(.*?)(?:</\1\s*>|$)", re.S | re.I)


def reasoning_settings(body, redact=lambda value: value):
    """Summarize native controls, never provider-produced reasoning or inferred defaults."""
    paths = (
        "reasoning_effort",
        "reasoning.effort",
        "reasoning.enabled",
        "reasoning.max_tokens",
        "chat_template_kwargs.enable_thinking",
        "chat_template_kwargs.thinking",
        "chat_template_kwargs.do_reasoning",
        "chat_template_kwargs.thinking_budget",
        "chat_template_kwargs.preserve_thinking",
        "chat_template_kwargs.clear_thinking",
        "thinking.type",
        "thinking.budget_tokens",
        "thinking",
    )
    fields = []
    for path in paths:
        value = body
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                break
            value = value[part]
        else:
            if value is None or isinstance(value, (str, bool, int, float)):
                fields.append(f"{path}:{json.dumps(redact(value), ensure_ascii=True)[:80]}")
    return ",".join(fields) if fields else "unspecified"


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
        "capture_version": 2,
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
        "response": result.response_diagnostics,
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
    request = store.one(
        "SELECT id,provider_id,model,started_at,status,reasoning_tokens,context FROM requests WHERE id=?",
        (request_id,),
    )
    identity = {
        "request_id": request_id,
        "provider_id": request["provider_id"] if request else None,
        "model": request["model"] if request else None,
        "request_started_at": datetime.fromtimestamp(request["started_at"], timezone.utc).isoformat()
        if request
        else None,
    }
    row = store.one(
        "SELECT substr(body,1,?) AS body,updated_at FROM request_diagnostics WHERE request_id=?",
        (limit + 1 if limit is not None else 2_147_483_647, request_id),
    )
    if not row:
        enabled = request and json.loads(request["context"]).get("diagnostic_capture_version")
        return {
            **identity,
            "capture": "unavailable",
            "reasoning_status": "capture_missing" if enabled else "not_recorded",
            "note": "Diagnostic capture was enabled for this request, but its record is missing. Inspect capture/storage errors."
            if enabled
            else "No private diagnostic capture is stored for this request. Whether its provider returned reasoning is unknown.",
        }
    if limit is not None and len(row["body"]) > limit:
        return {
            **identity,
            "capture": "console_limit",
            "reasoning_status": "inspect_dashboard",
            "note": "Private diagnostics exceed the console snapshot limit. Open this request in the authenticated dashboard.",
        }
    data = json.loads(row["body"])
    present = bool(
        (data.get("reasoning_content") or "").strip()
        or data.get("reasoning_details")
        or any((item.get("text") or "").strip() for item in data.get("inline_reasoning", []))
    )
    status = data.get("status") or (request["status"] if request else None)
    if present:
        reasoning_status, note = "present", "Provider reasoning is captured below."
    elif status == "running":
        reasoning_status, note = "pending", "Capture is active; no reasoning text has arrived yet."
    elif status in ("failed", "cancelled", "interrupted"):
        reasoning_status, note = (
            "none_received",
            f"No reasoning text was received before this request {status}. Capture was active.",
        )
    else:
        reasoning_status, note = "not_returned", "The provider returned no reasoning text in this response."
    if not present and request and request["reasoning_tokens"]:
        note += f" It reported {request['reasoning_tokens']} reasoning tokens without exposing their text."
    return {
        **identity,
        **data,
        "reasoning_status": reasoning_status,
        "note": note,
        "updated_at": row["updated_at"],
    }
