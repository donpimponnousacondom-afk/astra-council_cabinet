"""Literal, bounded Discord diagnostic footers; never executable templates."""

import math
import re

from .discord_text import preview


DEFAULT_FOOTER_TEMPLATE = "TTFT: {{TTFT}} | TPS: {{TPS}}"
FOOTER_PLACEHOLDERS = ("TTFT", "TPS", "PROVIDER", "CONTEXT", "MODEL", "MODEL SELECTED", "BOT")
MAX_FOOTER_UNITS = 500
PLACEHOLDER = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def footer_settings(bot):
    # Old configurations acquire effective defaults without rewriting the database.
    return {
        "footer_enabled": bot.get("footer_enabled", bot.get("role") == "hortator"),
        "footer_template": bot.get("footer_template", DEFAULT_FOOTER_TEMPLATE),
    }


def validate_template(value):
    if any(ord(c) < 32 or ord(c) == 127 or c in "\u2028\u2029" for c in value):
        raise ValueError("Footer template must be a single line without control characters")
    value = value.strip()
    if value.startswith("-# "):
        value = value[3:].strip()
    if not value or len(value) > 300 or "```" in value:
        raise ValueError("Footer template must contain 1–300 characters, without code fences")
    for match in PLACEHOLDER.finditer(value):
        if match[1].upper() not in FOOTER_PLACEHOLDERS:
            raise ValueError("Supported footer placeholders: " + ", ".join(FOOTER_PLACEHOLDERS))
    if "{" in PLACEHOLDER.sub("", value) or "}" in PLACEHOLDER.sub("", value):
        raise ValueError("Use double braces for footer placeholders, for example {{TTFT}}")
    return value


def measurement(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def render_footer(bot, profile=None, provider=None, request=None, *, redact=lambda value: value):
    settings = footer_settings(bot)
    if not settings["footer_enabled"]:
        return ""
    profile, provider, request = profile or {}, provider or {}, request or {}
    ttft, duration, output = (request.get(key) for key in ("ttft_ms", "duration_ms", "output_tokens"))
    tps = None
    if all(measurement(v) for v in (ttft, duration, output)) and duration > ttft and output > 0:
        tps = output * 1000 / (duration - ttft)
    used, window = request.get("input_tokens"), profile.get("context_window")
    values = {
        "TTFT": f"{ttft:.0f}ms" if measurement(ttft) else "—",
        "TPS": f"{tps:.1f}" if measurement(tps) else "—",
        "PROVIDER": provider.get("name") or provider.get("id") or "—",
        "CONTEXT": f"{used:.0f}" if measurement(used) else "—",
        "MODEL": request.get("model") or profile.get("model") or "—",
        "BOT": bot.get("name") or bot.get("id") or "—",
    }
    if measurement(window):
        values["CONTEXT"] += f"/{window:.0f}"
    values["MODEL SELECTED"] = values["MODEL"]

    def substitute(match):
        # Redact before escaping/truncating so formatting cannot disguise a stored secret.
        value = str(redact(values[match[1].upper()]))
        value = " ".join(value.split())
        value = "".join(c for c in value if ord(c) >= 32 and ord(c) != 127)
        return re.sub(r"([\\`*_~|<>\[\]])", r"\\\1", value)

    template = redact(settings["footer_template"])
    text = "-# " + PLACEHOLDER.sub(substitute, template)
    if len(text.encode("utf-16-le")) // 2 > MAX_FOOTER_UNITS:
        text = preview(text, MAX_FOOTER_UNITS - 1).rstrip("\\") + "…"
    return text
