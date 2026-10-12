"""Read-only, standard-library reporting of retained response shapes.

This reports observed normalized visible content, not raw wire bytes, reasoning,
Discord delivery or a guarantee about the next request's provider capabilities.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time


SHAPES = ("text_only", "tool_only", "text_and_tools", "empty")
MAX_HOURS = 720
MAX_ROWS = 10000


def _utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def parse_until(value):
    """Require an explicit offset so a saved report can be reproduced elsewhere."""
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.timestamp()
    except TypeError, ValueError, OverflowError:
        raise ValueError("--until must be an ISO 8601 timestamp with an explicit UTC offset") from None


def _counts():
    return {
        "requests": 0,
        "non_interrupted": dict.fromkeys(SHAPES, 0),
        "interrupted": dict.fromkeys(SHAPES, 0),
        "unreadable": 0,
    }


def _object(value):
    try:
        parsed = json.loads(value)
    except TypeError, ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _classify(value):
    response = _object(value)
    if response is None:
        return None
    content, calls = response.get("content"), response.get("tool_calls")
    if content is not None and not isinstance(content, str):
        return None
    if calls is not None and not isinstance(calls, list):
        return None
    text, tools = bool(content and content.strip()), bool(calls)
    shape = "text_and_tools" if text and tools else "text_only" if text else "tool_only" if tools else "empty"
    boundary = (
        "interrupted" if response.get("finish_reason") in ("length", "content_filter") else "non_interrupted"
    )
    return boundary, shape


def _add(counts, classified):
    counts["requests"] += 1
    if classified is None:
        counts["unreadable"] += 1
    else:
        boundary, shape = classified
        counts[boundary][shape] += 1


def _revision(context, key):
    value = context.get(key)
    return value if type(value) is int and value >= 0 else None


def response_protocol_report(database, *, hours=24, max_rows=1000, until=None):
    """Read newest bounded generation records in one snapshot without a Vault.

    Only selected rows have response/context JSON loaded. Historical identity is
    taken from each request; current bot/profile configuration is never joined.
    """
    if type(hours) is not int or not 1 <= hours <= MAX_HOURS:
        raise ValueError(f"--hours must be an integer from 1 to {MAX_HOURS}")
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_ROWS:
        raise ValueError(f"--max-rows must be an integer from 1 to {MAX_ROWS}")
    end = time.time() if until is None else until
    start = end - hours * 3600
    try:
        window = {"since": _utc(start), "until": _utc(end), "hours": hours, "bounds": "[since, until)"}
    except ValueError, OverflowError, OSError, TypeError:
        raise ValueError("Report time window must contain valid UTC timestamps") from None
    path = Path(database).resolve()
    totals, groups = _counts(), {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        # Avoid fetching wide evidence columns during the ordered candidate scan.
        rows = db.execute(
            "SELECT id,bot_id,provider_id,profile_id,model,started_at FROM requests "
            "WHERE status='completed' AND purpose='generation' AND started_at>=? AND started_at<? "
            "ORDER BY started_at DESC,id DESC LIMIT ?",
            (start, end, max_rows + 1),
        ).fetchall()
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        for row in rows:
            evidence = db.execute("SELECT context,response FROM requests WHERE id=?", (row["id"],)).fetchone()
            context = _object(evidence["context"]) or {}
            identity = {
                **{key: row[key] for key in ("bot_id", "provider_id", "profile_id", "model")},
                "profile_revision": _revision(context, "profile_revision"),
                "provider_revision": _revision(context, "provider_revision"),
            }
            key = tuple(identity.values())
            if key not in groups:
                groups[key] = {
                    **identity,
                    "counts": _counts(),
                    "sample_first_at": _utc(row["started_at"]),
                    "sample_last_at": _utc(row["started_at"]),
                }
            group = groups[key]
            group["sample_first_at"] = _utc(row["started_at"])
            classified = _classify(evidence["response"])
            _add(group["counts"], classified)
            _add(totals, classified)
    for group in groups.values():
        counts = group["counts"]
        group["mixed_output_evidence"] = (
            "observed"
            if counts["non_interrupted"]["text_and_tools"]
            else "observed_interrupted_only"
            if counts["interrupted"]["text_and_tools"]
            else "unknown"
        )
    return {
        "window": window,
        "max_rows": max_rows,
        "sampled_rows": len(rows),
        "truncated": truncated,
        "selection": "newest completed generation requests in the window",
        "sample_first_at": _utc(rows[-1]["started_at"]) if rows else None,
        "sample_last_at": _utc(rows[0]["started_at"]) if rows else None,
        "counts": totals,
        "groups": list(groups.values()),
        "interpretation": (
            "Counts describe retained normalized visible content and tool calls, independent of Discord "
            "publication. Interrupted means finish_reason length or content_filter. Other finish reasons "
            "are grouped as non_interrupted, not proof of successful delivery. Unknown means no observed "
            "non-interrupted mixed output in this sample, not provider incapability. Observed output is "
            "historical evidence, not a capability guarantee. Null revisions mean historical revision "
            "metadata was absent or invalid. Truncated samples omit older matching records."
        ),
    }
