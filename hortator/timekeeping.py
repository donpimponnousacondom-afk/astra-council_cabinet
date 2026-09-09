"""Council-local presentation; stored instants and source evidence stay unchanged."""

from datetime import datetime
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Europe/Madrid"
TIME_FIELDS = frozenset(
    {
        "at",
        "now",
        "created_at",
        "updated_at",
        "started_at",
        "ended_at",
        "sent_at",
        "fetched_at",
        "expires_at",
        "delivered_at",
        "published_at",
        "committed_at",
        "built_at",
        "request_started_at",
        "last_sent",
        "last_success",
        "next_at",
        "retry_at",
        "retry_until",
        "circuit_until",
        "remote_observed_at",
        "claimed_at",
    }
)
SOURCE_FIELDS = frozenset(
    {
        "content",
        "value",
        "command",
        "arguments",
        "body",
        "response",
        "request_json",
        "compaction_request_json",
        "summary",
        "previous_summary",
        "before_summary",
        "after_summary",
        "prompt_layers",
        "schema",
        "parameters",
        "example",
        "examples",
    }
)


def council_timezone(store):
    return (store.get("settings", "global") or {}).get("timezone", DEFAULT_TIMEZONE)


def local_timestamp(value, timezone=DEFAULT_TIMEZONE, *, timespec="seconds"):
    if isinstance(value, str):
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            return value  # Never invent an offset for historical text.
    else:
        moment = datetime.fromtimestamp(value, ZoneInfo(timezone))
    return moment.astimezone(ZoneInfo(timezone)).isoformat(timespec=timespec)


def present_times(value, timezone, *, field=None):
    """Format known result metadata, never parse/rewrite untrusted source text."""
    if field in SOURCE_FIELDS:
        return value
    if field in TIME_FIELDS and isinstance(value, (str, int, float)) and not isinstance(value, bool):
        try:
            if not isinstance(value, str) and value <= 0:
                return value  # Preserve unset deadline/last-seen sentinels.
            return local_timestamp(value, timezone)
        except ValueError, OverflowError, OSError:
            return value
    if isinstance(value, dict):
        return {key: present_times(item, timezone, field=key) for key, item in value.items()}
    if isinstance(value, list):
        return [present_times(item, timezone) for item in value]
    return value
