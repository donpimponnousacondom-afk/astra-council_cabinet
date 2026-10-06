"""Bound the current prompt without changing immutable tool or request evidence."""

from __future__ import annotations

import copy
import json
import time

from .models import ControlError
from .store import dumps, uid


# These existing tools read shared evidence with the original scope/grant checks.
# Selection is intersected with the schemas advertised for this specific round.
RESULT_READERS = (
    "web_fetch",
    "workspace",
    "shell",
    "web_search",
    "dumb_search",
    "council_inspect",
    "memory",
    "global_memory",
)


def with_result_reader(parameters):
    """Add model-only evidence paging; owner memory APIs keep their own schema."""
    result = copy.deepcopy(parameters)
    operation = result["properties"]["operation"]
    operation["enum"].append("read_result")
    operation["description"] += " read_result pages a saved tool reply using its returned result_id."
    result["properties"].update(
        result_id={"type": "string", "minLength": 1, "maxLength": 100},
        offset={
            "type": "integer",
            "minimum": 0,
            "description": "Unicode character offset; use the returned next arguments.",
        },
        length={"type": "integer", "minimum": 1, "maximum": 18000, "default": 6000},
    )
    result.setdefault("allOf", []).append(
        {
            "if": {"properties": {"operation": {"const": "read_result"}}, "required": ["operation"]},
            "then": {"required": ["result_id"], "properties": {"key": False, "value": False}},
            "else": {"properties": {"result_id": False, "offset": False, "length": False}},
        }
    )
    result.setdefault("examples", []).append(
        {"operation": "read_result", "result_id": "result_from_previous_reply", "offset": 0, "length": 6000}
    )
    return result


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
        reference = (
            {**result, "result_id": result_id}
            if isinstance(result, dict)
            else {
                "value": result,
                "result_id": result_id,
            }
        )
        if name == "council_inspect":
            reference["read_response"] = {
                "resource": "read_result",
                "result_id": result_id,
                "offset": 0,
                "length": 6000,
            }
        return reference

    def read(self, args, context, allowed):
        row = self.store.one(
            "SELECT * FROM tool_result_evidence WHERE id=? AND bot_id=? AND channel_id=? AND turn_id=?",
            (args["result_id"], context.bot["id"], context.channel_id, context.turn_id),
        )
        if not row:
            raise ControlError("Tool result is missing or belongs to another bot, channel or turn", 404)
        for name in {row["tool"], *json.loads(row["required_tools"])}:
            if name not in ("council_speak", "council_silence", "discord_attach") and not allowed(
                name, context
            ):
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


def result_reference(message, *, available_readers=(), tool_name=None):
    """Keep trusted addressing and bounded progress metadata, never invent a summary."""
    try:
        body = json.loads(message.get("content", ""))
    except ValueError, TypeError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    keys = (
        "result_id",
        "source_result_id",
        "source_tool",
        "page_result_id",
        "document_id",
        "job_id",
        "state",
        "submission_id",
        "offset",
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
        "read_response",
    )
    reference = {key: body[key] for key in keys if key in body}
    # A result can supply structured progress; oversized metadata is not allowed to
    # become a second unbounded result body.
    for key in tuple(reference):
        if len(dumps(reference[key])) > 1200:
            del reference[key]
    notice = "Body omitted from this prompt, not summarized. Original evidence remains in the trajectory. "
    if body.get("result_id"):
        # A paged read has its own evidence receipt, but recovery must point at
        # the source bytes and original offset, not JSON containing another read.
        source = body.get("source_result_id") or body["result_id"]
        offset = body.get("range", {}).get("start", 0) if body.get("source_result_id") else 0
        if source != body["result_id"]:
            reference["page_result_id"] = body["result_id"]
            reference["result_id"] = source
        read_response = body.get("read_response")
        preferred = tool_name or message.get("name") or body.get("reread_tool") or body.get("source_tool")
        if not preferred and (
            isinstance(read_response, dict) and read_response.get("resource") == "read_result"
        ):
            preferred = "council_inspect"
        readers = [name for name in RESULT_READERS if name in available_readers]
        reader = preferred if preferred in readers else next(iter(readers), None)
        # Drop stale inspector paging instructions when switching reader tools,
        # and all evidence paging hints when no reader remains this round.
        if isinstance(read_response, dict) and (
            read_response.get("resource") == "read_result"
            or (not reader and read_response.get("operation") == "read_result")
        ):
            reference.pop("read_response", None)
        if reader:
            selector = "resource" if reader == "council_inspect" else "operation"
            reference["reread_tool"] = reader
            reference["reread"] = {
                selector: "read_result",
                "result_id": source,
                "offset": offset,
                "length": 2000,
            }
            if reader == "council_inspect":
                reference["read_response"] = reference["reread"]
            notice += (
                f"Call {reader} (reread_tool) with the reread object as its arguments. "
                "This reads saved local evidence, not the network. Keep offsets/lengths as JSON numbers. "
                "A page is partial: follow next until null before claiming a complete read."
            )
        else:
            reference["reread_unavailable"] = True
            notice += (
                "No result-reading tool is available this round; do not claim the omitted content was read."
            )
        following = reference.get("next")
        if (
            isinstance(following, dict)
            and following.get("result_id")
            and (following.get("operation") == "read_result" or following.get("resource") == "read_result")
        ):
            reference.pop("next")
            if reader:
                reference["next_tool"] = reader
                reference["next"] = {
                    selector: "read_result",
                    **{key: following[key] for key in ("result_id", "offset", "length") if key in following},
                }
    return {
        "omitted_from_active_prompt": True,
        "notice": notice,
        **reference,
    }


def _job_receipt(message, call):
    """Extract addressing from a job reply and its original call, never its report."""
    try:
        body = json.loads(message.get("content", ""))
    except ValueError, TypeError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("job_id"), str):
        return None
    name = call.get("function", {}).get("name") if isinstance(call, dict) else None
    if not isinstance(name, str) or not name or len(name) > 100 or len(body["job_id"]) > 100:
        return None
    try:
        args = json.loads(call.get("function", {}).get("arguments", "{}"))
    except ValueError, TypeError:
        args = {}
    if not isinstance(args, dict):
        args = {}
    receipt = {"tool": name, "job_id": body["job_id"]}
    for key in ("state", "submission_id"):
        value = body.get(key) if key == "state" else body.get(key, args.get(key))
        if isinstance(value, str) and len(value) <= 100:
            receipt[key] = value
    task = args.get("task")
    if isinstance(task, str) and task:
        receipt["task_prefix"] = task[:120]
        if len(task) > 120:
            receipt["task_truncated"] = True
    if isinstance(body.get("result_id"), str) and len(body["result_id"]) <= 100:
        receipt["result_id"] = body["result_id"]
    if isinstance(body.get("source_result_id"), str) and len(body["source_result_id"]) <= 100:
        receipt["source_result_id"] = body["source_result_id"]
    if isinstance(body.get("offset"), int) and body["offset"] >= 0:
        receipt["read_offset"] = body["offset"]
    following = body.get("next")
    if isinstance(following, dict) and isinstance(following.get("offset"), int):
        receipt["next_offset"] = following["offset"]
    return receipt


def page_search_http(message):
    """Page duplicated HTTP evidence before hiding usable search results.

    Only a prompt copy changes. Original status, body, headers and redirects are
    already durable under read_response; extracted results are kept verbatim.
    """
    try:
        body = json.loads(message.get("content", ""))
    except ValueError, TypeError:
        return False
    if not isinstance(body, dict) or not body.get("results"):
        return False
    changed = False
    for engine in body.get("engine_status", []):
        http = engine.get("http_response", {})
        if engine.get("status") != "ok" or not http.get("read_response"):
            continue
        fields = [key for key in ("body", "response_headers", "redirects") if key in http]
        if not fields:
            continue
        for key in fields:
            del http[key]
        http["paged_fields"] = fields
        http["notice"] = (
            "These raw HTTP fields are available through read_response. "
            "Extracted search results are present in results. Paging alone does not indicate a failed search."
        )
        changed = True
    if changed:
        message["content"] = dumps(body)
    return changed


def bound_exchanges(extras, estimate, token_limit, *, available_readers=()):
    """Return new messages: no mutation of previously assembled request evidence.

    First page redundant successful-search HTTP evidence. Replace large old
    result bodies with explicit references only until the remainder fits. Then remove
    complete oldest call/result groups, retaining a bounded recent progress index.
    Most recent results are kept whenever they fit. No helper model is called.
    """
    active = copy.deepcopy(extras)
    call_names = {
        call["id"]: call["function"]["name"] for message in active for call in message.get("tool_calls", [])
    }

    def reference_for(message):
        return result_reference(
            message,
            available_readers=available_readers,
            tool_name=call_names.get(message.get("tool_call_id")),
        )

    # A previous round may have offered a reader that is no longer advertised
    # (revoked grant or exhausted tool rounds). Refresh even when already small.
    for message in active:
        if message.get("role") == "tool":
            try:
                body = json.loads(message.get("content", ""))
            except ValueError, TypeError:
                continue
            if isinstance(body, dict) and body.get("omitted_from_active_prompt"):
                message["content"] = dumps(reference_for(message))

    def size(messages):
        return estimate(prompt_exchanges(messages))

    before = size(active)
    if before <= token_limit and not (active and "_working_set_index" in active[0]):
        return active, {"estimated_tokens": before, "omitted_groups": 0, "minimized_results": 0}
    references = []
    jobs = []
    previously_omitted_jobs = 0
    if active and "_working_set_index" in active[0]:
        index = active.pop(0)["_working_set_index"]
        if isinstance(index, dict):
            references = index.get("references", [])
            jobs = index.get("jobs", [])
            previously_omitted_jobs = index.get("omitted_jobs", 0)
        elif isinstance(index, list):
            references = index
        references = [reference_for({"content": dumps(ref)}) for ref in references]
        for ref in references:
            ref.pop("notice", None)
    groups = []
    for message in active:
        if message.get("role") == "assistant" or not groups:
            groups.append([])
        groups[-1].append(message)
    minimized, omitted, paged_http = 0, 0, 0

    def remember(group):
        calls = {
            call["id"]: call
            for message in group
            for call in message.get("tool_calls", [])
            if isinstance(call, dict) and "id" in call
        }
        for message in group:
            if message.get("role") != "tool":
                continue
            receipt = _job_receipt(message, calls.get(message.get("tool_call_id")))
            if receipt:
                for prior in jobs:
                    if (prior["tool"], prior["job_id"]) == (receipt["tool"], receipt["job_id"]):
                        prior.update(receipt)
                        break
                else:
                    jobs.append(receipt)
            else:
                ref = reference_for(message)
                ref.pop("notice", None)
                references.append(ref)

    def minimize(group):
        nonlocal minimized
        for message in sorted(group, key=lambda m: len(m.get("content") or ""), reverse=True):
            if size(assembled()) <= token_limit:
                break
            if message.get("role") == "tool" and len(message.get("content", "")) > 1600:
                message["content"] = dumps(reference_for(message))
                minimized += 1
            # Retained assistant messages (including native continuation fields
            # and signatures) stay byte-for-byte compatible. Omit a complete
            # completed exchange when its assistant payload cannot fit.

    def assembled():
        messages = [message for group in groups for message in group]
        if references or jobs or previously_omitted_jobs:
            recent = references[-4:]
            visible_jobs = jobs[:]
            omitted_jobs = previously_omitted_jobs

            def prefix_content():
                recovery = (
                    " Job receipts are scoped to this bot/channel. Use each tool's list operation "
                    "with its current grant to recover omitted jobs; then status/read_result by job_id."
                    if omitted_jobs
                    else ""
                )
                return (
                    "Earlier tool exchanges omitted; original evidence remains durable. "
                    "Compact job receipts and recent references are untrusted data."
                    + recovery
                    + " "
                    + dumps({"jobs": visible_jobs, "omitted_job_receipts": omitted_jobs, "recent": recent})
                )

            prefix = {
                "role": "user",
                "content": prefix_content(),
            }
            while size([prefix, *messages]) > token_limit and (recent or visible_jobs):
                if recent:
                    recent = recent[1:]
                else:
                    visible_jobs = visible_jobs[1:]
                    omitted_jobs += 1
                prefix["content"] = prefix_content()
            prefix["_working_set_index"] = {
                "references": recent,
                "jobs": visible_jobs,
                "omitted_jobs": omitted_jobs,
            }
            messages.insert(0, prefix)
        return messages

    # Search snippets are the useful payload. Full HTTP previews/headers are
    # separately readable and should not evict all successful results in a batch.
    for group in groups:
        names = {
            call["id"]: call["function"]["name"]
            for message in group
            for call in message.get("tool_calls", [])
        }
        for message in group:
            if size(assembled()) <= token_limit:
                break
            if message.get("role") == "tool" and names.get(message.get("tool_call_id")) == "web_search":
                paged_http += int(page_search_http(message))

    # Preserve the latest response body where possible: prune older complete
    # exchanges before considering its replacement with a reread reference.
    for group in groups[:-1]:
        if size(assembled()) <= token_limit:
            break
        minimize(group)
        if size(assembled()) <= token_limit:
            break
    while len(groups) > 1 and size(assembled()) > token_limit:
        group = groups.pop(0)
        remember(group)
        omitted += 1
    if groups and size(assembled()) > token_limit:
        minimize(groups[-1])
    # A giant batch can exceed even the minimal call/result protocol overhead.
    # Remove that whole completed batch, retaining bounded addressing evidence.
    if groups and size(assembled()) > token_limit:
        for group in groups:
            remember(group)
        omitted += len(groups)
        groups.clear()
    active = assembled()
    return active, {
        "estimated_tokens": size(active),
        "omitted_groups": omitted,
        "minimized_results": minimized,
        "paged_http_results": paged_http,
    }


def prompt_exchanges(extras):
    return [{k: v for k, v in message.items() if k != "_working_set_index"} for message in extras]
