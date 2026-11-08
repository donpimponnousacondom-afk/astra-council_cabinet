"""Replay a saved Cheapseek inference without dispatching any returned tools.

Run with the repository Python, e.g. .venv/bin/python scripts/replay_tool_request.py.
The owner must authorize paid inference. Raw evidence belongs in ignored audit/.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

import httpx
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hortator.chat_response import (  # noqa: E402
    ChatResponse,
    ResponseFormatError,
    ResponseLimitError,
    SSEReader,
    UpstreamResponseError,
)
from hortator.provider import Completion  # noqa: E402
from hortator.request_payload import encode_request  # noqa: E402
from hortator.tool_feedback import feedback, full_schema, parse_arguments  # noqa: E402


MODEL = "cheapseek/deepseek-flash"
VARIANTS = ("baseline", "description", "required", "combined", "op_field", "root_required", "instruction")
CHANGES = {
    "baseline": "Unchanged saved body",
    "description": "Prepend shell function.description; preserves the original screened YOUR_COMMAND text",
    "required": "Add redundant nonempty-object conditional to shell parameters.allOf",
    "combined": "Apply description + required",
    "op_field": "Annotate shell operation in both root and full-schema properties",
    "root_required": "DIAGNOSTIC ONLY: add root required=[operation]; advertised {} help becomes invalid",
    "instruction": "Append a system message with the complete shell call convention; tools unchanged",
}
SHELL_PREFIX = (
    "Shell arguments must be one JSON object. To execute a command, use "
    '{"operation":"run","command":"YOUR_COMMAND"} and optionally timeout_seconds or cwd. '
    "Every nonempty shell call must include operation: run, status, read, cancel, or read_result. "
    "Command alone does not select run. {} returns usage only and executes nothing. "
)
OPERATION_DESCRIPTION = (
    "Required in every nonempty call; only {} may omit it. run executes command; "
    "status/read/cancel take job_id; read_result takes result_id."
)
SHELL_INSTRUCTION = (
    "Tool calling convention: shell is one tool, not separate shell.run or shell.status tools. "
    'Every nonempty shell call must explicitly include the JSON field "operation". '
    'To execute a command, send {"operation":"run","command":"pwd"}, replacing pwd with your actual command. '
    "For status/read/cancel use the matching operation plus a real returned job_id. "
    "For read_result use operation read_result plus a real returned result_id. "
    "A command field alone does not select run and will be rejected without execution. "
    "{} returns usage only and executes nothing. Preserve the task and all other tool constraints."
)


def variant_body(source, variant):
    body = copy.deepcopy(source)
    if variant == "baseline":
        return body
    if variant not in VARIANTS:
        raise ValueError(f"Unknown experimental variant: {variant}")
    shell = next(t["function"] for t in body["tools"] if t["function"]["name"] == "shell")
    if "operation" not in full_schema(shell["parameters"]).get("required", []):
        raise ValueError("Experiment requires the original mandatory operation selector")
    if variant in {"description", "combined"}:
        shell["description"] = SHELL_PREFIX + shell["description"]
    if variant in {"required", "combined"}:
        shell["parameters"].setdefault("allOf", []).append(
            {"if": {"minProperties": 1}, "then": {"required": ["operation"]}}
        )
    if variant == "op_field":
        shell["parameters"]["properties"]["operation"]["description"] = OPERATION_DESCRIPTION
        full_schema(shell["parameters"])["properties"]["operation"]["description"] = OPERATION_DESCRIPTION
    if variant == "root_required":
        shell["parameters"]["required"] = ["operation"]
    if variant == "instruction":
        body["messages"].append({"role": "system", "content": SHELL_INSTRUCTION})
    return body


def schedule(variants, repeats):
    """Rotate each block, reversing alternate cycles; balance every n blocks."""
    planned = []
    for block in range(repeats):
        order = list(variants if (block // len(variants)) % 2 == 0 else reversed(variants))
        offset = block % len(variants)
        planned.extend({"block": block + 1, "variant": v} for v in order[offset:] + order[:offset])
    return planned


def classify(calls, original_tools):
    schemas = {t["function"]["name"]: t["function"]["parameters"] for t in original_tools}
    results = []
    for call in calls:
        function = call.get("function", {})
        name = function.get("name")
        try:
            arguments = parse_arguments(function.get("arguments", ""))
            if name not in schemas:
                results.append({"tool": name, "category": "other_invalid", "error": "Unknown tool"})
                continue
            report = feedback(name, arguments, schemas[name])
            help_only = bool(report and report.get("usage_only"))
            errors = report.get("errors", []) if report else []
            missing_operation = any(
                e.get("rule") == "required" and e.get("message") == "'operation' is a required property"
                for e in errors
            )
            category = (
                "help"
                if help_only
                else "valid_action"
                if report is None
                else "missing_operation"
                if missing_operation
                else "other_invalid"
            )
            results.append(
                {
                    "tool": name,
                    "category": category,
                    "schema_valid": report is None or help_only,
                    "nonempty_object": isinstance(arguments, dict) and bool(arguments),
                    "argument_keys": list(arguments) if isinstance(arguments, dict) else None,
                    "errors": errors,
                    "copied_command_placeholder": isinstance(arguments, dict)
                    and "YOUR_COMMAND" in str(arguments.get("command", "")),
                }
            )
        except (ValueError, TypeError) as exc:
            results.append({"tool": name, "category": "other_invalid", "error": str(exc)})
    return results


def outcomes(calls):
    counts = {
        category: sum(c["category"] == category for c in calls)
        for category in ("valid_action", "help", "missing_operation", "other_invalid")
    }
    return {
        "call_counts": counts,
        "per_tool": {
            name: {
                category: sum(c["tool"] == name and c["category"] == category for c in calls)
                for category in counts
            }
            for name in sorted({c["tool"] for c in calls}, key=str)
        },
        "first_shell_category": next((c["category"] for c in calls if c["tool"] == "shell"), None),
        "any_valid_shell_action": any(
            c["tool"] == "shell" and c["category"] == "valid_action" for c in calls
        ),
        "any_missing_shell_operation": any(
            c["tool"] == "shell" and c["category"] == "missing_operation" for c in calls
        ),
        "any_other_invalid_shell": any(
            c["tool"] == "shell" and c["category"] == "other_invalid" for c in calls
        ),
        "no_tool_calls": not calls,
        "help_only": bool(calls) and counts["help"] == len(calls),
        "any_valid_action": bool(counts["valid_action"]),
        "all_valid_actions": bool(calls) and counts["valid_action"] == len(calls),
        "mixed_categories": len({c["category"] for c in calls}) > 1,
        "nonempty_shell_objects": sum(
            c["tool"] == "shell" and c.get("nonempty_object", False) for c in calls
        ),
        "quality": "unassessed; schema validity does not establish task progress or execution success",
    }


class CapturedResponse:
    """Retain consumed, decompressed bytes before the shared parser interprets them."""

    def __init__(self, response, captured):
        self.response, self.captured = response, captured

    async def aiter_bytes(self):
        async for chunk in self.response.aiter_bytes():
            self.captured.extend(chunk)
            yield chunk


def load_source(data_dir, request_id):
    with sqlite3.connect((data_dir / "council.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown saved request")
        body = json.loads(row["body"])
        if row["bot_id"] not in {"sun", "zelda"} or body.get("model") != MODEL:
            raise ValueError("This owner-authorized experiment covers Sun/Zelda Cheapseek only")
        provider_row = db.execute(
            "SELECT body,revision FROM entities WHERE kind='providers' AND id=?", (row["provider_id"],)
        ).fetchone()
        provider = json.loads(provider_row["body"])
        encrypted = None
        for scope in (f"bot/{row['bot_id']}/provider_key", f"provider/{row['provider_id']}/api_key"):
            secret = db.execute("SELECT value FROM secrets WHERE scope=?", (scope,)).fetchone()
            if secret:
                encrypted = secret[0]
                break
        key = ""
        if encrypted:
            master = os.environ.get("HORTATOR_MASTER_KEY") or (data_dir / "master.key").read_text().strip()
            key = Fernet(master.encode()).decrypt(encrypted).decode()
        if provider.get("requires_key", True) and not key:
            raise ValueError("Configured credential is unavailable")
        meta = {k: row[k] for k in ("id", "turn_id", "bot_id", "model", "started_at", "provider_id")}
        meta["historical_context"] = {
            k: json.loads(row["context"]).get(k) for k in ("round", "profile_revision", "provider_revision")
        }
        meta["current_provider_revision"] = provider_row["revision"]
        return body, provider, key, meta


async def replay(args):
    source, provider, key, meta = load_source(args.data_dir, args.request_id)
    bodies = {variant: variant_body(source, variant) for variant in args.variant}
    endpoint = provider["base_url"].rstrip("/") + "/chat/completions"
    headers = httpx.Headers({"Content-Type": "application/json"})
    headers.update(provider.get("headers", {}))
    if key:
        headers[provider.get("auth_header", "Authorization")] = (
            provider.get("auth_scheme", "Bearer") + " " + key
        ).strip()
    payloads = {variant: encode_request(body, endpoint)[0] for variant, body in bodies.items()}
    source_payload, _ = encode_request(source, endpoint)
    args.output.mkdir(parents=True, exist_ok=False)
    planned = schedule(args.variant, args.repeats)
    root = Path(__file__).resolve().parents[1]
    source_hashes = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in (
            "scripts/replay_tool_request.py",
            "hortator/tool_feedback.py",
            "hortator/chat_response.py",
            "hortator/provider.py",
            "hortator/request_payload.py",
        )
    }
    for name in source_hashes:
        target = args.output / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / name).read_bytes())

    def redact(value):
        text = json.dumps(value, ensure_ascii=False, indent=2)
        return text.replace(key, "[REDACTED]") if len(key) >= 6 else text

    (args.output / "manifest.json").write_text(
        redact(
            {
                **meta,
                "variants": args.variant,
                "changes": {v: CHANGES[v] for v in args.variant},
                "schedule": planned,
                "position_balanced": args.repeats % len(args.variant) == 0,
                "versions": {name: version(name) for name in ("jsonschema", "httpx")},
                "source_hashes": source_hashes,
                "source_payload_sha256": hashlib.sha256(source_payload).hexdigest(),
                "payload_sha256": {v: hashlib.sha256(p).hexdigest() for v, p in payloads.items()},
                "baseline_json_equal": bodies.get("baseline") == source if "baseline" in bodies else None,
                "tools_executed": False,
                "limits": "Saved request is already redacted; not a reconstruction of hidden reasoning/media. "
                "Current provider transport/credentials are used. No retries or tool-result continuation. "
                "Wire evidence is consumed decompressed UTF-8 response data (replacement on bad encoding), "
                "with the configured key redacted; not complete network packets or response headers.",
            }
        )
    )
    for variant, body in bodies.items():
        (args.output / f"request-{variant}.json").write_text(redact(body))
    summaries = []
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        for index, item in enumerate(planned):
            started = time.time()
            result = Completion()
            state = ChatResponse(result)
            captured = bytearray()
            summary = {"sample": index + 1, **item, "started_at": started, "outcome": "not_completed"}
            try:
                async with asyncio.timeout(args.timeout):
                    async with client.stream(
                        "POST",
                        endpoint,
                        headers=headers,
                        content=payloads[item["variant"]],
                        timeout=args.timeout,
                    ) as response:
                        summary["http_status"] = response.status_code
                        response.raise_for_status()
                        if "text/event-stream" in response.headers.get("content-type", ""):
                            async for frame in SSEReader(result.response_diagnostics).frames(
                                CapturedResponse(response, captured)
                            ):
                                if state.frame(frame):
                                    break
                        else:
                            captured.extend(await response.aread())
                            state.packet(json.loads(captured), streamed=False)
                        state.finish()
                classified = classify(result.tool_calls, source["tools"])
                summary.update(
                    outcome="complete" if result.finish_reason in {"stop", "tool_calls"} else "incomplete",
                    finish_reason=result.finish_reason,
                    reported_model=result.metadata.get("model"),
                    calls=classified,
                    **outcomes(classified),
                    usage=result.usage,
                )
            except (httpx.HTTPError, TimeoutError, ValueError) as exc:
                summary.update(
                    outcome="transport_error"
                    if isinstance(exc, (httpx.HTTPError, TimeoutError))
                    else "format_error",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            except (ResponseFormatError, ResponseLimitError, UpstreamResponseError) as exc:
                summary.update(
                    outcome="provider_error" if isinstance(exc, UpstreamResponseError) else "format_error",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            finally:
                summary["elapsed_seconds"] = round(time.time() - started, 3)
                (args.output / f"response-{index + 1:02}.json").write_text(
                    redact(
                        {
                            "summary": summary,
                            "response": result.message(),
                            "diagnostics": result.response_diagnostics,
                        }
                    )
                )
                summaries.append(summary)
                (args.output / "summary.json").write_text(redact(summaries))
                (args.output / f"wire-{index + 1:02}.json").write_text(
                    redact({"consumed_utf8": captured.decode("utf-8", "replace")})
                )
            print(redact(summary), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".local/share/hortator")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--variant", choices=VARIANTS, nargs="+", default=["baseline"])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (
        not 1 <= args.repeats <= 20
        or not 0 < args.timeout <= 600
        or len(set(args.variant)) != len(args.variant)
    ):
        parser.error("Use 1–20 repetitions and a timeout of at most 600 seconds")
    asyncio.run(replay(args))


if __name__ == "__main__":
    main()
