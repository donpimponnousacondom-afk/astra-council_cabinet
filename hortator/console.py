"""Append-only operational console. Filters never change the durable event ledger."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import sys
import termios
import time
from collections import OrderedDict, deque
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from .models import SECRET_FIELDS
from .provider import strip_reasoning

LEVELS = {"error": 40, "warning": 30, "info": 20, "debug": 10}
SCOPES = {
    "s": "system",
    "b": "bots",
    "p": "providers",
    "d": "discord",
    "t": "tools",
    "c": "context",
    "w": "dashboard",
}
COLORS = {"error": "91", "warning": "93", "info": "92", "debug": "90"}
SCOPE_COLORS = {
    "system": "97",
    "bots": "95",
    "providers": "96",
    "discord": "94",
    "tools": "93",
    "context": "92",
    "dashboard": "36",
}
CONSOLE_TOKENS = re.compile(
    r"(?P<field>\b(?:bot|provider|enabled|gateway|status|profile|active_turn|failures|circuit_remaining|details|"
    r"s:system|b:bots|p:providers|d:discord|t:tools|c:context|w:dashboard)=)(?P<value>[^\s|]+)"
    r"|(?P<scope>\b(?i:system|bots|providers|discord|tools|context|dashboard)\b)"
    r"|(?P<level>\b(?:ERROR|WARNING|INFO|DEBUG)\b)"
    r"|(?P<key>(?:^|(?<=\| ))(?:\+/-|f|r|e|i|0|\?)(?=\s))"
    r"|(?P<scope_key>(?:(?<=Scopes: )|(?<=, ))(?:[sbdtcw]|p/a)(?= ))"
    r"|(?P<control>\bCtrl-C\b)"
)
QUIET_EVENTS = {
    "context.assembled",
    "request.first_token",
    "message.received",
    "message.edited",
    "message.deleted",
    "decision.silence",
    "delivery.queued",
    "delivery.cooldown",
    "delivery.sending",
    "discord.history_imported",
}
SUMMARY_FIELDS = (
    "error",
    "reason",
    "message",
    "status",
    "decision",
    "model",
    "purpose",
    "name",
    "command",
    "action",
    "source",
    "close_code",
    "reconnect_duration_ms",
    "duration_ms",
    "ttft_ms",
    "input_tokens",
    "output_tokens",
    "consecutive_failures",
    "retry_in_seconds",
    "before_tokens",
    "after_tokens",
)


def scope_for(kind):
    prefix = kind.split(".", 1)[0]
    if prefix in {"provider", "request"}:
        return "providers"
    if prefix in {"discord", "message", "delivery", "notification"}:
        return "discord"
    if prefix in {"turn", "decision", "activation"}:
        return "bots"
    if prefix in {"tool", "workspace", "job", "shell", "web_fetch"}:
        return "tools"
    if prefix in {"context", "compaction", "memory"}:
        return "context"
    if prefix == "http":
        return "dashboard"
    return "system"


def plain(value):
    # Untrusted log text must not issue terminal controls, forge lines, or change Screen titles.
    return "".join(c if c.isprintable() else f"\\u{ord(c):04x}" for c in str(value))


def safe_text(value):
    value = strip_reasoning(value)
    value = re.sub(r"(?i)\b(bearer\s+)[\w.~+/-]+=*", r"\1[REDACTED]", value)
    value = re.sub(
        r"(?i)\b(authorization|api[_-]?key|token|password|secret|cookie|csrf)([\s\"']*[:=][\s\"']*)[^\s,;\"']+",
        r"\1\2[REDACTED]",
        value,
    )

    def safe_url(match):
        try:
            url = urlsplit(match[0])
            host = url.netloc.rsplit("@", 1)[-1]
            return urlunsplit((url.scheme, host, url.path, "", ""))
        except ValueError:
            return "[invalid URL omitted]"

    return re.sub(r"https?://[^\s\"'<>]+", safe_url, value)


def safe_value(value, depth=0):
    if depth > 8:
        return "[nested detail omitted]"
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:80]:
            name = str(key)
            if name.lower() in SECRET_FIELDS | {
                "csrf",
                "cookie",
                "set-cookie",
                "headers",
                "reasoning_content",
                "reasoning_details",
            }:
                result[plain(name)] = "[REDACTED]"
            else:
                result[plain(name)] = safe_value(item, depth + 1)
        if len(value) > 80:
            result["…"] = "Additional fields are available in the event ledger"
        return result
    if isinstance(value, (list, tuple)):
        return [safe_value(v, depth + 1) for v in value[:40]] + (
            ["[additional items omitted]"] if len(value) > 40 else []
        )
    if isinstance(value, str):
        text = safe_text(value)
        return text if len(text) <= 6000 else text[:6000] + "… [truncated; inspect the event ledger]"
    return value if value is None or isinstance(value, (int, float, bool)) else safe_text(str(value))


class OperationalConsole(logging.Handler):
    def __init__(
        self,
        *,
        level="info",
        scopes=None,
        details=False,
        keys=True,
        color=None,
        stream=None,
        stdin=None,
        repeat_seconds=30,
    ):
        super().__init__(logging.DEBUG)
        self.threshold = LEVELS[level]
        self.scopes = set(SCOPES.values()) if scopes is None else set(scopes)
        self.details = details
        self.keys_enabled = keys
        self.stream = stream if stream is not None else sys.stderr
        self.stdin = stdin if stdin is not None else sys.stdin
        self.color = (
            (self.stream.isatty() and "NO_COLOR" not in os.environ and os.getenv("TERM") != "dumb")
            if color is None
            else color
        )
        self.repeat_seconds = repeat_seconds
        self.history = deque(maxlen=200)
        self.error_history = deque(maxlen=50)
        self.repeats = OrderedDict()
        self.store = None
        self.redact = lambda value: value
        self.loop = self.timer = self.tty_state = self.input_fd = None
        self.saved_loggers = []
        self.output_failed = False

    def __enter__(self):
        # Library wire-debug logs can contain credentials, payloads and provider reasoning.
        # App DEBUG uses the safe event ledger; transport internals stay at WARNING.
        for name in (
            "",
            "hortator",
            "hortator.events",
            "uvicorn",
            "uvicorn.error",
            "uvicorn.access",
            "discord",
            "httpx",
            "httpcore",
            "aiohttp",
        ):
            logger = logging.getLogger(name)
            self.saved_loggers.append(
                (logger, logger.handlers[:], logger.level, logger.propagate, logger.disabled)
            )
            logger.handlers = [self] if not name else []
            logger.propagate = bool(name)
            logger.disabled = False
            logger.setLevel(
                logging.WARNING if name in {"discord", "httpx", "httpcore", "aiohttp"} else logging.DEBUG
            )
        return self

    def __exit__(self, *_):
        self.stop()
        for logger, handlers, level, propagate, disabled in reversed(self.saved_loggers):
            logger.handlers, logger.level, logger.propagate, logger.disabled = (
                handlers,
                level,
                propagate,
                disabled,
            )
        self.saved_loggers.clear()

    def bind(self, kernel):
        self.redact = kernel.vault.redact
        self.store = kernel.store
        # Make recent persisted incidents inspectable immediately after a restart, without replay spam.
        known = {e.get("seq") for e in self.history if e.get("seq")}
        older = [self.event(e) for e in reversed(kernel.store.events(limit=100)) if e["seq"] not in known]
        self.history = deque([*older, *self.history], maxlen=200)
        incidents = kernel.store.events(level="error", limit=25) + kernel.store.events(
            level="warning", limit=25
        )
        self.error_history = deque(
            (self.event(e) for e in sorted(incidents, key=lambda e: e["seq"])), maxlen=50
        )
        self.loop = asyncio.get_running_loop()
        self.start_keys()
        self.help()
        self.timer = self.loop.call_later(1, self.tick)

    def start_keys(self):
        if not self.keys_enabled or not self.stdin.isatty() or not self.stream.isatty():
            return
        try:
            fd = self.stdin.fileno()
            if os.tcgetpgrp(fd) != os.getpgrp():
                return  # Never consume an unrelated foreground job's terminal input.
            saved = termios.tcgetattr(fd)
            state = copy.deepcopy(saved)
            state[3] &= ~(termios.ICANON | termios.ECHO)
            # ISIG remains set: Ctrl-C is still the normal cooperative server shutdown.
            state[6][termios.VMIN], state[6][termios.VTIME] = 1, 0
            termios.tcsetattr(fd, termios.TCSANOW, state)
            self.input_fd, self.tty_state = fd, saved
            self.loop.add_reader(fd, self.read_key)
        except (OSError, ValueError, termios.error):
            self.stop_keys()

    def stop_keys(self):
        if self.input_fd is not None:
            if self.loop and not self.loop.is_closed():
                self.loop.remove_reader(self.input_fd)
            try:
                termios.tcsetattr(self.input_fd, termios.TCSANOW, self.tty_state)
            except (OSError, termios.error):
                pass
        self.input_fd = self.tty_state = None

    def stop(self):
        self.stop_keys()
        if self.timer:
            self.timer.cancel()
            self.timer = None
        self.flush_repeats(force=True)

    def read_key(self):
        try:
            data = os.read(self.input_fd, 1024)
        except OSError:
            self.stop_keys()
            return
        if not data:
            self.stop_keys()
        elif len(data) == 1:
            self.key(data.decode("ascii", errors="ignore"))
        # Ignore escape sequences / pasted commands. This console is not a shell.

    def key(self, key):
        with self.lock:
            self.handle_key(key)

    def handle_key(self, key):
        if key in {"+", "=", "-"}:
            levels = [40, 30, 20, 10]
            index = max(0, min(3, levels.index(self.threshold) + (-1 if key == "-" else 1)))
            self.threshold = levels[index]
        elif key in SCOPES or key == "a":
            scope = SCOPES["p" if key == "a" else key]
            self.scopes.symmetric_difference_update({scope})
        elif key == "f":
            self.details = not self.details
            self.state()
            self.replay(limit=5)
            return
        elif key in {"r", "e"}:
            self.replay(errors=key == "e")
            return
        elif key == "i":
            self.inspect()
            return
        elif key in {"?", "h"}:
            self.help()
            return
        elif key == "0":
            self.threshold, self.details = logging.INFO, False
            self.scopes = set(SCOPES.values())
        else:
            return
        self.state()

    def tick(self):
        self.flush_repeats()
        self.timer = self.loop.call_later(1, self.tick)

    def paint(self, text, code):
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def highlight(self, text):
        """Apply semantic colors only after redaction and terminal-control escaping."""
        if not self.color:
            return text

        def token(match):
            if match["field"]:
                field, value = match["field"][:-1], match["value"]
                label_color = SCOPE_COLORS.get(field.split(":")[-1], "90")
                if value in {"on", "True", "online", "ready", "sent", "completed", "recovered"}:
                    color = "1;92"
                elif value in {"off", "False", "failed", "error"}:
                    color = "1;91"
                elif (
                    value in {"offline", "unknown", "none", "folded", "silent", "cancelled", "suppressed"}
                    or field == "profile"
                ):
                    color = "90"
                elif field == "failures":
                    color = "92" if value == "0" else "1;91"
                elif field == "circuit_remaining":
                    color = "92" if value == "0s" else "1;93"
                else:
                    color = {"bot": "1;95", "provider": "1;96"}.get(field, "1;93")
                return self.paint(field, label_color) + self.paint("=", "90") + self.paint(value, color)
            if match["scope"]:
                return self.paint(match[0], SCOPE_COLORS[match[0].lower()])
            if match["level"]:
                return self.paint(match[0], "1;" + COLORS[match[0].lower()])
            return self.paint(match[0], "1;93")

        return CONSOLE_TOKENS.sub(token, text)

    def write(self, text):
        if self.output_failed:
            return
        try:
            self.stream.write(text + "\n")
            self.stream.flush()
        except (OSError, ValueError):
            self.output_failed = True  # A broken log sink must never fail a model turn.

    def notice(self, text):
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        self.write(
            f"{self.paint(stamp, '90')} {self.paint('CONSOLE', '1;96')} {self.highlight(plain(safe_text(self.redact(text))))}"
        )

    def state(self):
        level = next(k for k, v in LEVELS.items() if v == self.threshold)
        scopes = " ".join(f"{k}:{v}={'on' if v in self.scopes else 'off'}" for k, v in SCOPES.items())
        self.notice(f"{level.upper()} | details={'expanded' if self.details else 'folded'} | {scopes}")

    def help(self):
        self.state()
        self.notice(
            "+/- verbosity | f fold/expand JSON and errors (replays last 5) | r recent 20 | e recent errors | i inspect runtime | 0 reset | ? help"
        )
        self.notice(
            "Scopes: s system, b bots, p/a providers, d Discord, t tools, c context, w web/dashboard. HTTP successes require DEBUG (+)."
        )
        self.notice(
            f"{'Single-key controls active' if self.input_fd is not None else 'Keyboard controls inactive (noninteractive terminal or --no-console-keys)'}; Ctrl-C stops the server. Repeats summarized every {self.repeat_seconds:g}s; full events remain in the dashboard."
        )

    def event(self, event):
        event = safe_value(self.redact(event))
        kind = event["kind"]
        level = event.get("level", "info")
        if level == "info" and kind in QUIET_EVENTS:
            level = "debug"
        encoded = json.dumps(event["data"], ensure_ascii=False, default=str)
        if len(encoded) > 12000:
            event["data"] = {
                **{
                    k: event["data"][k]
                    for k in ("provider_id", "error", "status", "name")
                    if k in event["data"]
                },
                "detail_preview": encoded[:4000],
                "truncated": "Full detail remains in the event ledger",
            }
        return {**event, "scope": scope_for(kind), "level": level}

    def emit(self, record):
        try:
            if (
                record.name.startswith(("discord", "httpx", "httpcore", "aiohttp"))
                and record.levelno < logging.WARNING
            ):
                return  # Also reject child loggers with an explicitly enabled wire-debug level.
            if hasattr(record, "council_event"):
                event = self.event(record.council_event)
            else:
                level = (
                    "error"
                    if record.levelno >= 40
                    else "warning"
                    if record.levelno >= 30
                    else "info"
                    if record.levelno >= 20
                    else "debug"
                )
                if (
                    record.name == "uvicorn.access"
                    and isinstance(record.args, tuple)
                    and len(record.args) == 5
                ):
                    _, method, target, _, status = record.args
                    # Never log request query strings, headers, cookies, bodies, or client credentials.
                    path = str(target).split("?", 1)[0].split("#", 1)[0]
                    data = {
                        "message": f"{method} {path} → {status}",
                        "method": method,
                        "path": path,
                        "status_code": status,
                    }
                    level = "error" if int(status) >= 500 else "warning" if int(status) >= 400 else "debug"
                    kind = "http.access"
                else:
                    kind = (
                        "discord.library"
                        if record.name.startswith("discord")
                        else "provider.library"
                        if record.name.startswith(("httpx", "httpcore", "aiohttp"))
                        else "runtime.log"
                    )
                    data = {"message": record.getMessage(), "logger": record.name}
                    if record.exc_info:
                        data["traceback"] = logging.Formatter().formatException(record.exc_info)
                event = self.event({"at": record.created, "kind": kind, "level": level, "data": data})
            self.accept(event)
        except Exception:
            # logging.Handler.handleError prints raw records/args; never use it for credential-bearing logs.
            self.notice("Could not format one log entry; inspect the durable event ledger.")

    def visible(self, event):
        return event["scope"] in self.scopes and LEVELS.get(event["level"], 20) >= self.threshold

    def accept(self, event):
        self.history.append(event)
        if LEVELS.get(event["level"], 20) >= 30:
            self.error_history.append(event)
        if not self.visible(event):
            return
        self.flush_repeats()
        data = event["data"]
        repeatable = (
            event["kind"] == "http.access"
            or event["level"] in {"warning", "error"}
            or event["kind"] in {"discord.online", "discord.reconnecting", "provider.recovered"}
        )
        if repeatable and self.repeat_seconds > 0:
            key = (
                event["scope"],
                event["kind"],
                event.get("bot_id"),
                data.get("provider_id"),
                data.get("error") or data.get("reason") or data.get("message"),
            )
            if key in self.repeats:
                group = self.repeats[key]
                group["count"] += 1
                group["event"] = event
                return
            if len(self.repeats) >= 200:
                _, group = self.repeats.popitem(last=False)
                self.repeat_summary(group)
            self.repeats[key] = {"since": time.monotonic(), "event": event, "count": 0}
        self.render(event)

    def flush_repeats(self, force=False):
        with self.lock:
            now = time.monotonic()
            for key, group in list(self.repeats.items()):
                if force or now - group["since"] >= self.repeat_seconds:
                    self.repeat_summary(group)
                    del self.repeats[key]

    def repeat_summary(self, group):
        event = group["event"]
        if group["count"] and self.visible(event):
            self.render(
                event,
                suffix=f"[{group['count']} additional repeats in {time.monotonic() - group['since']:.0f}s; latest occurrence shown]",
                details=False,
            )

    def replay(self, *, errors=False, limit=20):
        selected = [
            e
            for e in list(self.error_history if errors else self.history)
            if self.visible(e) and (not errors or LEVELS.get(e["level"], 20) >= 30)
        ][-limit:]
        self.notice(
            f"Replay: {len(selected)} {'warnings/errors' if errors else 'recent entries'} matching current filters (original timestamps)."
        )
        for event in selected:
            self.render(event)

    def inspect(self):
        if self.store is None:
            self.notice("Runtime inspection is available after application startup.")
            return
        self.notice("Runtime snapshot (read-only; unaffected by console filters):")
        settings = self.store.get("settings", "global")
        self.notice(f"Council enabled={settings['enabled']}")
        runtimes = {row["bot_id"]: row for row in self.store.rows("SELECT * FROM bot_runtime")}
        turns = {
            row["bot_id"]: row
            for row in self.store.rows("SELECT bot_id,id FROM turns WHERE status='running'")
        }
        for bot in self.store.list("bots"):
            state = runtimes.get(bot["id"], {})
            self.notice(
                f"bot={bot['id']} enabled={bot['enabled']} gateway={state.get('gateway_status', 'unknown')} profile={bot['model_profile_id']} active_turn={turns.get(bot['id'], {}).get('id', 'none')}"
            )
        health = {row["provider_id"]: row for row in self.store.rows("SELECT * FROM provider_health")}
        for provider in self.store.list("providers"):
            state = health.get(provider["id"], {})
            self.notice(
                f"provider={provider['id']} enabled={provider['enabled']} failures={state.get('consecutive_failures', 0)} circuit_remaining={max(0, state.get('circuit_until', 0) - time.time()):.0f}s"
            )

    def render(self, event, *, suffix="", details=None):
        # Re-redact history at display time too, including credentials added since capture.
        event = safe_value(self.redact(event))
        data = event["data"]
        stamp = datetime.fromtimestamp(event["at"]).astimezone().isoformat(timespec="milliseconds")
        level = event["level"]
        identity = " ".join(
            f"{label}={plain(value)}"
            for label, value in (("bot", event.get("bot_id")), ("provider", data.get("provider_id")))
            if value
        )
        fields = [
            f"{key}={plain(data[key])}" if key not in {"error", "message", "reason"} else plain(data[key])
            for key in SUMMARY_FIELDS
            if data.get(key) is not None
        ]
        summary = " · ".join(fields)
        if len(summary) > 320:
            summary = summary[:317] + "…"
        reference = f" #{event['seq']}" if event.get("seq") else ""
        scope_color = SCOPE_COLORS.get(event["scope"], "97")
        summary = (
            self.paint(summary, COLORS[level]) if level in {"warning", "error"} else self.highlight(summary)
        )
        line = f"{self.paint(stamp, '90')} {self.paint(level.upper().ljust(7), COLORS.get(level, '90'))} {self.paint(event['scope'].ljust(9), scope_color)} {self.paint(plain(event['kind']), '1;' + scope_color)}{self.paint(reference, '90')} {self.highlight(identity)} {summary} {self.paint(suffix, '90')}"
        self.write(line.rstrip())
        if self.details if details is None else details:
            body = {k: event[k] for k in ("turn_id", "request_id", "data") if event.get(k)}
            multiline = {
                key: value
                for key, value in data.items()
                if key in {"traceback", "error"} and isinstance(value, str) and "\n" in value
            }
            if multiline:
                body["data"] = {key: value for key, value in data.items() if key not in multiline}
            encoded = json.dumps(body, ensure_ascii=False, indent=2, default=str)
            if len(encoded) > 12000:
                encoded = encoded[:12000] + "\n… [details truncated; inspect the event ledger]"
            for row in encoded.splitlines():
                row = plain(row)
                if self.color:
                    row = re.sub(
                        r'("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)',
                        lambda m: (
                            self.paint(m[1], "96" if m[2] else "92") + (m[2] or "")
                            if m[1]
                            else self.paint(
                                m[0], {"true": "1;92", "false": "1;91", "null": "90"}.get(m[0], "94")
                            )
                        ),
                        row,
                    )
                self.write(self.paint("  │ ", "90") + row)
            for key, value in multiline.items():
                self.write(self.paint(f"  │ {key}:", COLORS.get(level, "90")))
                rows = value.splitlines()
                for row in rows[:80]:
                    self.write(self.paint("  │ ", "90") + self.paint(plain(row), COLORS.get(level, "90")))
                if len(rows) > 80:
                    self.write("  │ … [additional lines omitted]")
