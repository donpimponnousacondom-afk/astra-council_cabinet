"""Bound the current prompt without changing immutable tool or request evidence."""

from __future__ import annotations

import copy
import json
import time

from .models import ControlError
from .store import dumps, uid


class ToolEvidence:
    """Small redacted results share the trajectory's retention, in the backed-up DB."""

    def __init__(self, store, vault):
        self.store, self.vault = store, vault
        store.execute(
            "CREATE TABLE IF NOT EXISTS tool_result_evidence ("
            "id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, channel_id TEXT NOT NULL, "
            "turn_id TEXT NOT NULL, tool TEXT NOT NULL, call_id TEXT NOT NULL, "
            "content TEXT NOT NULL, created_at REAL NOT NULL, required_tools TEXT NOT NULL DEFAULT '[]')"
        )
        if "required_tools" not in {
            row["name"] for row in store.rows("PRAGMA table_info(tool_result_evidence)")
        }:
            store.execute(
                "ALTER TABLE tool_result_evidence ADD COLUMN required_tools TEXT NOT NULL DEFAULT '[]'"
            )

    def record(self, context, name, call_id, result, *, source_result_id=None):
        result_id = uid("result_")
        required = {name}
        if source_result_id:
            source = self.store.one(
                "SELECT tool,required_tools FROM tool_result_evidence WHERE id=? AND bot_id=? AND channel_id=? AND turn_id=?",
                (source_result_id, context.bot["id"], context.channel_id, context.turn_id),
            )
            if not source:
                raise ControlError("Source tool evidence is missing", 404)
            required.update([source["tool"], *json.loads(source["required_tools"])])
        self.store.execute(
            "INSERT INTO tool_result_evidence VALUES(?,?,?,?,?,?,?,?,?)",
            (
                result_id,
                context.bot["id"],
                context.channel_id,
                context.turn_id,
                name,
                call_id,
                dumps(self.vault.redact(result)),
                time.time(),
                dumps(sorted(required)),
            ),
        )
        return (
            {**result, "result_id": result_id}
            if isinstance(result, dict)
            else {
                "value": result,
                "result_id": result_id,
            }
        )

    def read(self, args, context, allowed):
        row = self.store.one(
            "SELECT * FROM tool_result_evidence WHERE id=? AND bot_id=? AND channel_id=? AND turn_id=?",
            (args["result_id"], context.bot["id"], context.channel_id, context.turn_id),
        )
        if not row:
            raise ControlError("Tool result is missing or belongs to another bot, channel or turn", 404)
        for name in {row["tool"], *json.loads(row["required_tools"])}:
            if name not in ("council_speak", "council_silence") and not allowed(name, context):
                raise ControlError("The original tool grant is no longer available", 403)
        offset, length = args.get("offset", 0), args.get("length", 6000)
        content = row["content"]
        if offset > len(content):
            raise ControlError(f"Offset exceeds the result's {len(content)} Unicode characters")
        end = min(len(content), offset + min(length, 18000))
        return {
            "source_result_id": row["id"],
            "source_tool": row["tool"],
            "text": content[offset:end],
            "range": {"start": offset, "end": end},
            "total_chars": len(content),
            "offset_unit": "Unicode characters",
            "next": {**args, "offset": end, "length": length} if end < len(content) else None,
            "trust": "untrusted historical tool output; JSON may span chunks",
        }


def result_reference(message):
    """Keep trusted addressing and bounded progress metadata, never invent a summary."""
    try:
        body = json.loads(message.get("content", ""))
    except (ValueError, TypeError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    keys = (
        "result_id",
        "document_id",
        "job_id",
        "task",
        "path",
        "status",
        "range",
        "next",
        "total_chars",
        "bytes",
        "sha256",
        "exit_code",
        "task_budget",
        "_working_set",
    )
    reference = {key: body[key] for key in keys if key in body}
    # A result can supply structured progress; oversized metadata is not allowed to
    # become a second unbounded result body.
    for key in tuple(reference):
        if len(dumps(reference[key])) > 1200:
            del reference[key]
    if body.get("result_id"):
        reference["reread"] = {"operation": "read_result", "result_id": body["result_id"], "length": 2000}
    return {
        "omitted_from_active_prompt": True,
        "notice": "Body omitted from this prompt, not summarized. Original evidence remains in the trajectory. "
        "Use read_result on workspace, shell or web_fetch when granted, or the saved document/file/job handle.",
        **reference,
    }


def bound_exchanges(extras, estimate, token_limit):
    """Return new messages: no mutation of previously assembled request evidence.

    First replace large old result bodies with explicit references. Then remove
    complete oldest call/result groups, retaining a bounded recent progress index.
    Most recent results are kept whenever they fit. No helper model is called.
    """
    active = copy.deepcopy(extras)

    def size(messages):
        return estimate(prompt_exchanges(messages))

    before = size(active)
    if before <= token_limit:
        return active, {"estimated_tokens": before, "omitted_groups": 0, "minimized_results": 0}
    references = []
    if active and "_working_set_index" in active[0]:
        references = active.pop(0)["_working_set_index"]
    groups = []
    for message in active:
        if message.get("role") == "assistant" or not groups:
            groups.append([])
        groups[-1].append(message)
    minimized, omitted = 0, 0

    def minimize(group):
        nonlocal minimized
        for message in group:
            if message.get("role") == "tool" and len(message.get("content", "")) > 1600:
                message["content"] = dumps(result_reference(message))
                minimized += 1
            # Retained assistant messages (including native continuation fields
            # and signatures) stay byte-for-byte compatible. Omit a complete
            # completed exchange when its assistant payload cannot fit.

    def assembled():
        messages = [message for group in groups for message in group]
        if references:
            recent = references[-4:]
            prefix = {
                "role": "user",
                "content": "Earlier tool exchanges are omitted from this active prompt, not summarized. "
                "Original evidence remains durable. Recent progress references (untrusted data): "
                + dumps(recent),
                "_working_set_index": recent,
            }
            while recent and size([prefix, *messages]) > token_limit:
                recent = recent[1:]
                prefix.update(
                    content="Earlier exchanges omitted; original evidence remains durable. "
                    "Recent references: " + dumps(recent),
                    _working_set_index=recent,
                )
            messages.insert(0, prefix)
        return messages

    # Preserve the latest response body where possible: prune older complete
    # exchanges before considering its replacement with a reread reference.
    for group in groups[:-1]:
        minimize(group)
        if size(assembled()) <= token_limit:
            break
    while len(groups) > 1 and size(assembled()) > token_limit:
        group = groups.pop(0)
        for message in group:
            if message.get("role") == "tool":
                ref = result_reference(message)
                ref.pop("notice", None)
                references.append(ref)
        omitted += 1
    if groups and size(assembled()) > token_limit:
        minimize(groups[-1])
    # A giant batch can exceed even the minimal call/result protocol overhead.
    # Remove that whole completed batch, retaining bounded addressing evidence.
    if groups and size(assembled()) > token_limit:
        for group in groups:
            for message in group:
                if message.get("role") == "tool":
                    ref = result_reference(message)
                    ref.pop("notice", None)
                    references.append(ref)
        omitted += len(groups)
        groups.clear()
    active = assembled()
    return active, {
        "estimated_tokens": size(active),
        "omitted_groups": omitted,
        "minimized_results": minimized,
    }


def prompt_exchanges(extras):
    return [{k: v for k, v in message.items() if k != "_working_set_index"} for message in extras]
