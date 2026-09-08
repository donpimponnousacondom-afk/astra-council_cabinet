"""Append-only operational console. Filters never change the durable event ledger."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import stat
import sys
import termios
import textwrap
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
DETAIL_LEVELS = ("concise", "json", "evidence")
EVIDENCE_PAGE_CHARS = 6000
EVIDENCE_PAGE_LINES = 80
EVIDENCE_LIMIT = 8 * 1024 * 1024
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
    r"|(?P<key>(?:^|(?<=\| ))(?:\+/-|f|r|e|i|0|\?|T|P|n|N|\[|\])(?=\s))"
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
    "outcome",
    "exit_code",
    "job_id",
    "call_id",
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
    "stdout_bytes",
    "stderr_bytes",
    "workspace_committed",
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
    if prefix in {"tool", "workspace", "job", "shell", "web_fetch", "document", "publishing"}:
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


def safe_value(value, depth=0, *, bounded=True):
    if depth > (8 if bounded else 32):
        return "[nested detail omitted]"
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:80] if bounded else value.items():
            name = str(key)
            if name.lower() == "data_base64":
                result[plain(name)] = "[binary bytes omitted; the decoded text view is redacted]"
            elif name.lower() in SECRET_FIELDS | {
                "csrf",
                "cookie",
                "set-cookie",
                "headers",
                "reasoning_content",
                "reasoning_details",
            }:
                result[plain(name)] = "[REDACTED]"
            else:
                result[plain(name)] = safe_value(item, depth + 1, bounded=bounded)
        if bounded and len(value) > 80:
            result["…"] = "Additional fields are available in the event ledger"
        return result
    if isinstance(value, (list, tuple)):
        return [safe_value(v, depth + 1, bounded=bounded) for v in (value[:40] if bounded else value)] + (
            ["[additional items omitted]"] if bounded and len(value) > 40 else []
        )
    if isinstance(value, str):
        text = safe_text(value)
        return (
            text
            if not bounded or len(text) <= 6000
            else text[:6000] + "… [truncated; inspect the event ledger]"
        )
    return value if value is None or isinstance(value, (int, float, bool)) else safe_text(str(value))


def job_outcome(data):
    """A nonzero model command exit is distinct from failure to run/commit it."""
    if (
        data.get("status") == "failed"
        and type(data.get("exit_code")) is int
        and data["exit_code"] != 0
        and data.get("workspace_committed") is True
        and not data.get("error")
    ):
        return "command_exit (nonzero exit; valid workspace changes saved)"
    if data.get("error") and data.get("status") == "failed":
        return "runner_or_validation_failure (inspect retained error)"
    if data.get("status") == "failed":
        return "failure_unclassified (inspect stored job evidence)"
    return data.get("status", "unknown")


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
        self.scope_depths = {"tools": 0, "providers": 0}
        self.focus_scope = None
        self.evidence = None
        self.runner = None
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
        self.runner = getattr(getattr(getattr(kernel, "registry", None), "agentic", None), "runner", None)
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
        elif key in {"T", "P"}:
            scope = "tools" if key == "T" else "providers"
            self.focus_scope = scope
            self.scope_depths[scope] = (self.scope_depths[scope] + 1) % len(DETAIL_LEVELS)
            self.evidence = None
            self.state()
            self.replay(limit=1, scope=scope)
            return
        elif key in {"n", "N"}:
            self.page_evidence(1 if key == "n" else -1)
            return
        elif key in {"[", "]"}:
            self.select_evidence(-1 if key == "[" else 1)
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
            self.scope_depths = {"tools": 0, "providers": 0}
            self.focus_scope = self.evidence = None
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
        self.notice(
            f"{level.upper()} | details={'expanded' if self.details else 'folded'} | "
            f"T tools={DETAIL_LEVELS[self.scope_depths['tools']]} | "
            f"P providers={DETAIL_LEVELS[self.scope_depths['providers']]} | {scopes}"
        )

    def help(self):
        self.state()
        self.notice(
            "+/- verbosity | f fold/expand JSON and errors (replays last 5) | r recent 20 | e recent errors | i inspect runtime | 0 reset | ? help"
        )
        self.notice(
            "Scopes: s system, b bots, p/a providers, d Discord, t tools, c context, w web/dashboard. HTTP successes require DEBUG (+)."
        )
        self.notice(
            "T tools depth | P providers depth: concise → JSON → stored evidence (replays latest). "
            "[ older event | ] newer event | n next evidence page | N previous page. Lowercase keys still filter."
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
                data.get("job_id"),
                data.get("call_id"),
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

    def replay(self, *, errors=False, limit=20, scope=None):
        selected = [
            e
            for e in list(self.error_history if errors else self.history)
            if self.visible(e)
            and (not errors or LEVELS.get(e["level"], 20) >= 30)
            and (scope is None or e["scope"] == scope)
        ][-limit:]
        self.notice(
            f"Replay: {len(selected)} {'warnings/errors' if errors else 'recent entries'} matching current filters (original timestamps)."
        )
        for event in selected:
            self.render(event, select_evidence=True)

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

    def select_evidence(self, direction):
        if self.focus_scope is None or self.scope_depths[self.focus_scope] != 2:
            self.notice("Select stored evidence with T or P first; [ and ] then select an event.")
            return
        candidates = {
            event["seq"]: event
            for event in [*self.history, *self.error_history]
            if event.get("seq") and event["scope"] == self.focus_scope and self.visible(event)
        }
        ordered = sorted(candidates)
        if not ordered:
            self.notice("No retained event matches that scope and the current filters.")
            return
        current = self.evidence.get("seq") if self.evidence else None
        index = ordered.index(current) if current in candidates else len(ordered) - 1
        target = index + direction
        if not 0 <= target < len(ordered):
            self.notice("No more retained events in that direction; older evidence remains in the dashboard.")
            return
        self.render(candidates[ordered[target]], select_evidence=True)
        if self.evidence:
            self.evidence["pinned"] = True

    def stored_event(self, event):
        if self.store is None or not event.get("seq"):
            return event
        row = self.store.one(
            "SELECT seq,id,at,kind,level,bot_id,turn_id,request_id,substr(data,1,?) AS data, "
            "length(data) AS stored_chars FROM events WHERE seq=?",
            (EVIDENCE_LIMIT, event["seq"]),
        )
        if not row:
            return {**event, "stored_event": "No longer available"}
        size = row.pop("stored_chars")
        row["data"] = (
            json.loads(row["data"])
            if size <= EVIDENCE_LIMIT
            else {
                **event["data"],
                "omitted": f"Stored event exceeds {EVIDENCE_LIMIT} characters; inspect event #{event['seq']} in the dashboard",
            }
        )
        return row

    def tool_start(self, event, *, job=None):
        data = event.get("data", {})
        if event["kind"] == "tool.started":
            return event
        if data.get("call_id"):
            where = "json_extract(data,'$.call_id')=?"
            args = (data["call_id"],)
        elif job:
            where = (
                "json_extract(data,'$.name')='shell' AND "
                "json_extract(data,'$.arguments.operation')='run' AND json_extract(data,'$.arguments.task')=?"
            )
            args = (job["task"],)
        else:
            return None
        row = self.store.one(
            "SELECT seq FROM events WHERE kind='tool.started' AND bot_id=? AND turn_id=? "
            f"AND seq<=? AND {where} ORDER BY seq DESC LIMIT 1",
            (event.get("bot_id"), event.get("turn_id"), event.get("seq", 0), *args),
        )
        return self.stored_event({"seq": row["seq"], "data": {}}) if row else None

    def evidence_text(self, event):
        """Read existing evidence only. Bounded pages never enlarge retained event history."""
        source = self.stored_event(event)
        sections = [("Stored event (original timestamp)", source)]
        if self.store is None:
            sections.append(("Evidence availability", "Runtime storage is not bound to this console."))
        elif event["scope"] == "providers":
            request_id = source.get("request_id")
            request = (
                self.store.one(
                    "SELECT id,turn_id,bot_id,provider_id,profile_id,model,purpose,started_at,ended_at,"
                    "ttft_ms,first_visible_ms,duration_ms,input_tokens,output_tokens,reasoning_tokens,"
                    "cached_tokens,cost,cost_source,status,error,http_status,"
                    "substr(body,1,?) AS body,substr(response,1,?) AS response,"
                    "substr(context,1,?) AS context,substr(usage,1,?) AS usage "
                    "FROM requests WHERE id=?",
                    (EVIDENCE_LIMIT + 1,) * 4 + (request_id,),
                )
                if request_id
                else None
            )
            if request:
                payloads = {name: request.pop(name) for name in ("body", "response", "context", "usage")}
                sections.append(("Stored request state and timings (null means unknown)", request))
                for name, encoded in payloads.items():
                    if encoded is None:
                        value = "Not recorded; raw reasoning and omitted fields cannot be recovered."
                    elif len(encoded) > EVIDENCE_LIMIT:
                        value = f"This field exceeds {EVIDENCE_LIMIT} characters; inspect request {request_id} in the dashboard."
                    else:
                        try:
                            value = json.loads(encoded)
                        except ValueError:
                            value = encoded
                    sections.append((f"Request {name}", value))
            else:
                sections.append(
                    (
                        "Request evidence",
                        "No stored request is linked to this event; select a request.* event with [ or ].",
                    )
                )
        elif event["scope"] == "tools":
            data = source.get("data", {})
            result = data.get("result") if isinstance(data.get("result"), dict) else {}
            job_id = data.get("job_id") or result.get("job_id")
            job = None
            if job_id and re.fullmatch(r"job_[a-f0-9]{32}", str(job_id)):
                try:
                    if self.runner is None:
                        raise ValueError("Isolated runner is not bound to this console")
                    # _load checks real private regular files and never executes or recovers a job.
                    job = self.runner._load(job_id)
                    if job.get("bot_id") != source.get("bot_id") or job.get("turn_id") != source.get(
                        "turn_id"
                    ):
                        raise ValueError("Job ownership does not match this event")
                    sections.append(("Stored job state", {**job, "outcome": job_outcome(job)}))
                except Exception as error:
                    job = None
                    sections.append(("Job evidence unavailable", str(error)))
            started = self.tool_start(source, job=job)
            if started:
                sections.append(("Original tool call", started))
                arguments = started.get("data", {}).get("arguments")
                command = arguments.get("command") if isinstance(arguments, dict) else None
                if isinstance(command, str):
                    sections.append(("Full stored command (never executed by the console)", command))
            elif job:
                sections.append(
                    (
                        "Original command",
                        "No matching tool.started record remains; the console cannot reconstruct a missing command.",
                    )
                )
            result_id = data.get("result_id") or result.get("result_id")
            if result_id:
                evidence = self.store.one(
                    "SELECT id,tool,call_id,created_at,substr(content,1,?) AS content FROM tool_result_evidence "
                    "WHERE id=? AND bot_id=? AND turn_id=?",
                    (EVIDENCE_LIMIT + 1, result_id, source.get("bot_id"), source.get("turn_id")),
                )
                if evidence:
                    content = evidence.pop("content")
                    evidence["content"] = (
                        json.loads(content)
                        if len(content) <= EVIDENCE_LIMIT
                        else "Stored result exceeds console limit; inspect this result ID in the dashboard."
                    )
                    sections.append(("Immutable tool result", evidence))
            if job:
                for stream in ("stdout", "stderr"):
                    try:
                        if job.get("output_expired"):
                            raise ValueError("Output expired under retention; no log bytes remain available")
                        path = self.runner.root / job_id / f"{stream}.log"
                        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as log:
                            info = os.fstat(log.fileno())
                            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                                raise ValueError("Output is not a private regular file")
                            raw = log.read(EVIDENCE_LIMIT + 1)
                        text = raw[:EVIDENCE_LIMIT].decode("utf-8", errors="replace")
                        if len(raw) > EVIDENCE_LIMIT:
                            text += "\n[Console limit reached; inspect the job's byte-paged output in the dashboard.]"
                        sections.append(
                            (
                                f"{stream} ({info.st_size} stored bytes; UTF-8 display)",
                                text or "[empty output]",
                            )
                        )
                    except (OSError, ValueError) as error:
                        sections.append((f"{stream} unavailable", str(error)))
        output = []
        remaining = EVIDENCE_LIMIT
        for title, value in sections:
            value = safe_value(self.redact(value), bounded=False)
            encoded = (
                value
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False, indent=2, default=str)
            )
            section = f"{title}:\n{encoded}\n\n"
            output.append(section[:remaining])
            remaining -= len(section)
            if remaining < 0:
                output.append(
                    f"\n[Console snapshot truncated at {EVIDENCE_LIMIT} characters. Original event #{event.get('seq', '?')}, request/job/result IDs above remain available through dashboard inspection.]\n"
                )
                break
        return "".join(output)

    def show_evidence(self, event, *, selected=False):
        try:
            text = self.evidence_text(event)
        except Exception as error:
            text = f"Stored evidence unavailable: {safe_text(self.redact(str(error)))}"
        snapshot = {
            "text": text,
            "seq": event.get("seq"),
            "kind": event["kind"],
            "offsets": [0],
            "page": 0,
            "pinned": False,
        }
        if selected or self.evidence is None or not self.evidence.get("pinned"):
            self.evidence = snapshot
            self.focus_scope = event["scope"]
        self.print_evidence_page(snapshot)

    def print_evidence_page(self, snapshot):
        # Redact again before slicing so a newly added secret cannot span page boundaries.
        text = safe_text(self.redact(snapshot["text"]))
        start = snapshot["offsets"][snapshot["page"]]
        end = min(len(text), start + EVIDENCE_PAGE_CHARS)
        rows = text[start:end].splitlines(keepends=True)
        if len(rows) > EVIDENCE_PAGE_LINES:
            end = start + sum(len(row) for row in rows[:EVIDENCE_PAGE_LINES])
        snapshot["next"] = end if end < len(text) else None
        navigation = (
            ("n next page | " if snapshot["next"] is not None else "End of snapshot | ")
            + "N previous page | [ older event | ] newer event."
            if snapshot is self.evidence
            else f"Live preview; n/N still page selected event #{self.evidence.get('seq')}. r selects recent evidence."
        )
        self.notice(
            f"Stored evidence #{snapshot.get('seq') or '?'} {snapshot['kind']} · "
            f"characters {start}..{end}/{len(text)} (redacted snapshot). "
            f"{navigation} Missing or redacted data is not recoverable here."
        )
        for row in text[start:end].splitlines():
            for line in textwrap.wrap(
                plain(row), width=160, replace_whitespace=False, drop_whitespace=False
            ) or [""]:
                self.write(self.paint("  │ ", "90") + self.highlight(line))

    def page_evidence(self, direction):
        snapshot = self.evidence
        if not snapshot:
            self.notice("Select stored evidence with T or P first, then n/N page that snapshot.")
            return
        snapshot["pinned"] = True
        if direction < 0:
            if snapshot["page"] == 0:
                self.notice("Already at the first evidence page.")
                return
            snapshot["page"] -= 1
        else:
            if snapshot.get("next") is None:
                self.notice("Already at the end of this evidence snapshot.")
                return
            snapshot["offsets"] = snapshot["offsets"][: snapshot["page"] + 1] + [snapshot["next"]]
            snapshot["page"] += 1
        self.print_evidence_page(snapshot)

    def render(self, event, *, suffix="", details=None, select_evidence=False):
        # Re-redact history at display time too, including credentials added since capture.
        event = safe_value(self.redact(event))
        data = event["data"]
        if event["kind"].startswith("job.") and data.get("status") == "failed":
            data = {**data, "outcome": job_outcome(data)}
            event = {**event, "data": data}
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
        depth = (
            max(int(self.details), self.scope_depths.get(event["scope"], 0))
            if details is None
            else int(details)
        )
        if depth == 2:
            self.show_evidence(event, selected=select_evidence)
        elif depth == 1:
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
