"""Read-only shell-call and cache accounting from the existing request ledger.

No provider calls, vault access, tool execution or raw prompt/command output.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from statistics import median
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.replay_tool_request import classify  # noqa: E402


def summarize(rows):
    groups = {}
    for row in rows:
        body = json.loads(row["body"])
        tools = body.get("tools", [])
        if not any(t.get("function", {}).get("name") == "shell" for t in tools):
            continue
        context = json.loads(row["context"])
        guidance = next(
            (p for p in context.get("prompt_layers", []) if p["id"] == "shell_call_guidance"), None
        )
        key = (
            row["bot_id"],
            row["provider_id"],
            row["profile_id"],
            row["model"],
            guidance["sha256"] if guidance else "absent",
        )
        group = groups.setdefault(key, {"counts": Counter(), "input": [], "cache": [], "ttft": []})
        count = group["counts"]
        count["attempts"] += 1
        count["status_" + row["status"]] += 1
        if row["input_tokens"] is not None:
            group["input"].append(row["input_tokens"])
        cached, total = row["cached_tokens"], row["input_tokens"]
        if cached is not None and total is not None and total > 0 and 0 <= cached <= total:
            group["cache"].append((total, cached))
        else:
            count["cache_unknown_attempts"] += 1
        if row["ttft_ms"] is not None:
            group["ttft"].append(row["ttft_ms"])
        response = json.loads(row["response"] or "null")
        if (
            row["status"] != "completed"
            or not response
            or response.get("finish_reason") not in ("stop", "tool_calls")
        ):
            count["not_complete"] += 1
            continue
        count["complete_responses"] += 1
        calls = response.get("tool_calls") or []
        shell = [c for c in calls if c.get("function", {}).get("name") == "shell"]
        count["shell_responses"] += bool(shell)
        count["no_shell_responses"] += not shell
        count["no_tool_responses"] += not calls
        results = classify(shell, [t for t in tools if t.get("type") == "function"])
        count["omission_responses"] += any(c["category"] == "missing_operation" for c in results)
        count["other_invalid_responses"] += any(c["category"] == "other_invalid" for c in results)
        count["help_only_shell_responses"] += bool(results) and all(c["category"] == "help" for c in results)
        count["example_match_responses"] += any(c.get("example_command_match") for c in results)
        for call in results:
            count["shell_calls"] += 1
            count["nonempty_shell_objects"] += bool(call.get("nonempty_object"))
            count[call["category"] + "_calls"] += 1
    output = []
    for key, group in sorted(groups.items()):
        count, cache = group["counts"], group["cache"]
        counts = {
            name: count[name]
            for name in (
                "attempts",
                "complete_responses",
                "not_complete",
                "shell_responses",
                "no_shell_responses",
                "no_tool_responses",
                "omission_responses",
                "other_invalid_responses",
                "help_only_shell_responses",
                "example_match_responses",
                "shell_calls",
                "nonempty_shell_objects",
                "missing_operation_calls",
                "valid_action_calls",
                "help_calls",
                "other_invalid_calls",
                "cache_unknown_attempts",
            )
        }
        counts.update({name: value for name, value in count.items() if name.startswith("status_")})
        output.append(
            {
                **dict(zip(("bot_id", "provider_id", "profile_id", "model", "guidance_sha256"), key)),
                **counts,
                "cache_reported_attempts": len(cache),
                "median_input_tokens": median(group["input"]) if group["input"] else None,
                "median_cached_share": median(c / t for t, c in cache) if cache else None,
                "min_cached_share": min((c / t for t, c in cache), default=None),
                "reported_input_tokens": sum(t for t, _ in cache),
                "reported_cached_tokens": sum(c for _, c in cache),
                "reported_uncached_tokens": sum(t - c for t, c in cache),
                "ttft_reported_attempts": len(group["ttft"]),
                "median_ttft_ms": median(group["ttft"]) if group["ttft"] else None,
            }
        )
    return output


def instant(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise argparse.ArgumentTypeError("Include a timezone offset or Z")
    return result.timestamp()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--since", type=instant, required=True)
    parser.add_argument("--until", type=instant, default=datetime.now(timezone.utc).timestamp())
    args = parser.parse_args()
    if args.until <= args.since:
        parser.error("--until must follow --since")
    with closing(sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT bot_id,provider_id,profile_id,model,status,body,context,response,input_tokens,"
            "cached_tokens,ttft_ms FROM requests WHERE purpose='generation' AND started_at>=? AND started_at<?",
            (args.since, args.until),
        )
        groups = summarize(rows)
    print(
        json.dumps(
            {
                "since": datetime.fromtimestamp(args.since, timezone.utc).isoformat(),
                "until": datetime.fromtimestamp(args.until, timezone.utc).isoformat(),
                "scope": "Generation attempts advertising shell; retries are separate attempts. Complete responses require stop/tool_calls. Original saved schemas judge syntax, not execution or task relevance.",
                "caveat": "Observational counts, not a controlled comparison. Compare context size, timing, schema and task mix; missing cache counts are unknown. Review task relevance separately.",
                "groups": groups,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
