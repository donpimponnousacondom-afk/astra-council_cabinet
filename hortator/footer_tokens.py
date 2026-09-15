"""Numeric footer evidence, separate from reported billing and private reasoning."""

import json
import math
from functools import lru_cache

import tiktoken

from .diagnostics import INLINE_REASONING, text_content


@lru_cache(maxsize=1)
def encoder():
    return tiktoken.get_encoding("cl100k_base")


def count(text):
    return len(encoder().encode(text, disallowed_special=()))


def reported(*values):
    for value in values:
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
            and int(value) == value
        ):
            return int(value)
    return None


def detail(usage, key):
    value = usage.get(key)
    return value if isinstance(value, dict) else {}


def reasoning_text(content, reasoning, details):
    # Providers can mirror the same reasoning in multiple representations. Use
    # one complete text representation; never tokenize encrypted data/signatures
    # or a provider's short reasoning summary as though it were the full trace.
    if reasoning:
        return reasoning
    text = "".join(
        item["text"]
        for item in details
        if item.get("type") in (None, "text", "reasoning.text") and isinstance(item.get("text"), str)
    )
    return text or "".join(match[2] for match in INLINE_REASONING.finditer(content))


def sample(value, source):
    return {"value": value, "source": source if value is not None else "unavailable"}


def measure_footer_tokens(body, usage, content, reasoning, details, tool_calls):
    """Run on a worker with detached request values; return counts only.

    Prefer reported usage. Fallbacks use the exact visible/reasoning text and
    tool names/arguments with one fixed tokenizer, without planning multipliers.
    Input estimates cover serialized textual messages/tools, never image bytes
    or opaque reasoning payloads. Unobservable provider tokens cannot be counted.
    """
    reasoning_count = reported(
        detail(usage, "completion_tokens_details").get("reasoning_tokens"),
        usage.get("reasoning_tokens"),
        detail(usage, "output_tokens_details").get("reasoning_tokens"),
    )
    reasoning_source = "reported"
    if reasoning_count is None:
        text = reasoning_text(content, reasoning, details)
        reasoning_count = count(text) if text else None
        reasoning_source = "estimated"

    completion_count = reported(usage.get("completion_tokens"), usage.get("output_tokens"))
    completion_source = "reported"
    if completion_count is None:
        completion_count = count(INLINE_REASONING.sub("", content).strip())
        for call in tool_calls:
            function = call.get("function") or {}
            completion_count += count(function.get("name") or "")
            completion_count += count(function.get("arguments") or "")
        completion_count += reasoning_count or 0
        completion_source = "estimated"

    total_count = reported(usage.get("total_tokens"))
    total_source = "reported"
    if total_count is None:
        input_count = reported(usage.get("prompt_tokens"), usage.get("input_tokens"))
        total_source = "derived" if completion_source == "reported" else "estimated"
        if input_count is None:
            messages = []
            for message in body.get("messages", []):
                text = text_content(message.get("content"))
                entry = {"role": message.get("role"), "content": text}
                for key in ("name", "tool_calls", "tool_call_id"):
                    if key in message:
                        entry[key] = message[key]
                replay = reasoning_text(
                    "", message.get("reasoning_content"), message.get("reasoning_details") or []
                )
                if replay:
                    entry["reasoning_content"] = replay
                messages.append(entry)
            input_count = count(
                json.dumps(
                    {"messages": messages, "tools": body.get("tools", [])},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            total_source = "estimated"
        total_count = input_count + completion_count

    return {
        "tokenizer": "cl100k_base",
        "estimate_basis": "Observed text only; excludes unseen reasoning, media tokens and native chat framing",
        "reasoning": sample(reasoning_count, reasoning_source),
        "completion": sample(completion_count, completion_source),
        "total": sample(total_count, total_source),
    }
