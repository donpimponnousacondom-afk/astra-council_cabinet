import asyncio
import fcntl
import io
import json
import logging
import os
import pty
import re
import signal
import socket
import subprocess
import sys
import termios

import httpx
import pytest

from hortator import console as console_module
from hortator.console import OperationalConsole


def access(target="/api/status", status=200):
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:12345", "GET", target, "1.1", status
    )


def output(**kwargs):
    return OperationalConsole(stream=io.StringIO(), keys=False, color=False, **kwargs)


async def test_real_ledger_events_have_time_scope_identity_and_no_http_poll_spam(kernel):
    with output() as console:
        console.bind(kernel)
        console.stream.seek(0)
        console.stream.truncate()
        for _ in range(25):
            access()
            access("/api/stats")
        assert console.stream.getvalue() == ""
        event = kernel.store.emit(
            "request.failed",
            {"provider_id": "ollama", "error": "TimeoutError: Request timed out"},
            bot_id="hortator",
            turn_id="turn_example",
            request_id="req_example",
            level="error",
        )
        rendered = console.stream.getvalue()
        assert re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d", rendered)
        assert all(
            text in rendered
            for text in (
                "ERROR",
                "providers",
                "bot=hortator",
                "provider=ollama",
                f"#{event['seq']}",
                "Request timed out",
            )
        )
        assert "None" not in rendered and "\x1b" not in rendered
        assert (
            kernel.store.events(after=event["seq"] - 1)[0]["data"]["error"]
            == "TimeoutError: Request timed out"
        )


async def test_filters_do_not_affect_ledger_and_replay_respects_filters(kernel):
    with output() as console:
        console.bind(kernel)
        console.key("p")
        console.stream.seek(0)
        console.stream.truncate()
        event = kernel.store.emit(
            "provider.failure", {"error": "scoped failure", "provider_id": "sample"}, level="error"
        )
        assert not console.stream.getvalue()
        assert kernel.store.events(after=event["seq"] - 1)
        console.key("r")
        assert "scoped failure" not in console.stream.getvalue()
        console.key("a")
        console.key("e")
        assert "scoped failure" in console.stream.getvalue()
        console.key("-")
        console.key("-")
        assert console.threshold == logging.ERROR
        console.key("0")
        assert console.threshold == logging.INFO and "providers" in console.scopes


def test_http_debug_and_errors_never_print_queries_or_client_credentials():
    with output() as console:
        access("/api/status?password=query-test-secret")
        assert not console.stream.getvalue()
        console.key("+")
        access("/api/status?token=another-query-secret")
        access("/missing?credential=hidden", 404)
        text = console.stream.getvalue()
        assert "GET /api/status → 200" in text and "GET /missing → 404" in text
        assert (
            "query-test-secret" not in text
            and "another-query-secret" not in text
            and "credential" not in text
        )
        console.key("w")
        before = console.stream.getvalue()
        access("/hidden-scope", 500)
        assert console.stream.getvalue() == before


async def test_fold_expands_errors_and_ids_without_secrets_or_terminal_injection(kernel):
    secret = "console-credential-test-only"
    kernel.vault.put("provider/openrouter/api_key", secret)
    with output() as console:
        console.bind(kernel)
        kernel.store.emit(
            "tool.failed",
            {
                "name": "sample",
                "error": f"Bad response {secret}\x1b[2J\nforged",
                "api_key": "unknown-key",
                "reasoning_content": "hidden thought",
                "detail": "<think>hidden chain</think>visible",
            },
            bot_id="ada",
            turn_id="turn_scoped",
            level="error",
        )
        assert "turn_scoped" not in console.stream.getvalue()
        console.key("f")
        text = console.stream.getvalue()
        assert '"turn_id": "turn_scoped"' in text
        assert (
            secret not in text
            and "unknown-key" not in text
            and "hidden thought" not in text
            and "hidden chain" not in text
        )
        assert "\x1b" not in text and "\nforged" not in text
        assert "REDACTED" in text and "visible" in text
        console.key("f")
        assert not console.details


def test_python_exception_traceback_is_folded_and_transport_urls_are_sanitized():
    with output() as console:
        try:
            raise RuntimeError("Cannot reach https://user:pass@provider.test/v1?key=sample-credential")
        except RuntimeError:
            logging.getLogger("httpx").exception("Provider transport failed")
        assert "Traceback" not in console.stream.getvalue()
        console.key("f")
        text = console.stream.getvalue()
        assert "Traceback" in text and "RuntimeError" in text and "providers" in text
        assert "user:pass" not in text and "sample-credential" not in text


def test_transport_child_debug_never_bypasses_payload_filter():
    child = logging.getLogger("discord.gateway")
    previous = child.level
    try:
        child.setLevel(logging.DEBUG)
        with output(level="debug", details=True) as console:
            child.debug("raw wire message with unknown-credential and reasoning")
            assert not console.stream.getvalue()
            assert not console.history
    finally:
        child.setLevel(previous)


async def test_repeat_summary_preserves_all_occurrences_and_new_errors_are_immediate(kernel, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(console_module.time, "monotonic", lambda: clock[0])
    with output() as console:
        for n in range(3):
            clock[0] = 100 + n * 5
            kernel.store.emit(
                "provider.failure",
                {"provider_id": "sample", "error": "timeout", "consecutive_failures": n + 1},
                level="error",
            )
        assert console.stream.getvalue().count("provider.failure") == 1
        assert len([e for e in kernel.store.events() if e["kind"] == "provider.failure"]) == 3
        kernel.store.emit(
            "provider.failure", {"provider_id": "sample", "error": "authentication failed"}, level="error"
        )
        assert "authentication failed" in console.stream.getvalue()
        clock[0] = 131
        console.flush_repeats()
        assert "2 additional repeats in 31s" in console.stream.getvalue()
        assert "consecutive_failures=3" in console.stream.getvalue()
        console.key("e")
        assert console.stream.getvalue().count("timeout") == 5


async def test_replay_reads_pre_restart_events_and_bounds_memory(kernel):
    kernel.store.emit("turn.failed", {"error": "before console startup"}, bot_id="ada", level="error")
    with output() as console:
        console.bind(kernel)
        assert "before console startup" not in console.stream.getvalue()
        console.key("e")
        assert "before console startup" in console.stream.getvalue()
        for n in range(230):
            kernel.store.emit("tool.completed", {"name": "large", "result": ["x" * 6000] * 20}, bot_id="ada")
        assert len(console.history) == 200
        assert len(json.dumps(list(console.history))) < 2_500_000
        console.stream.seek(0)
        console.stream.truncate()
        console.key("e")
        assert "before console startup" in console.stream.getvalue()


async def test_runtime_inspection_does_not_modify_configuration(kernel):
    before = kernel.store.list("bots")
    with output() as console:
        console.bind(kernel)
        console.scopes.clear()
        console.key("i")
        assert "bot=ada enabled=False" in console.stream.getvalue()
        assert "provider=openrouter enabled=True" in console.stream.getvalue()
        assert "active_turn=none" in console.stream.getvalue()
    assert kernel.store.list("bots") == before


def test_no_color_and_tty_color_modes(monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setenv("TERM", "screen")
    monkeypatch.delenv("NO_COLOR", raising=False)
    with OperationalConsole(stream=Terminal(), keys=False) as console:
        logging.warning("color test")
        assert "\x1b[93m" in console.stream.getvalue()
        assert "1049" not in console.stream.getvalue()
    monkeypatch.setenv("NO_COLOR", "")
    with OperationalConsole(stream=Terminal(), keys=False) as console:
        logging.error("plain test")
        assert "\x1b" not in console.stream.getvalue()


async def test_broken_console_does_not_fail_event_storage(kernel):
    class Broken(io.StringIO):
        def write(self, _):
            raise BrokenPipeError()

    with OperationalConsole(stream=Broken(), keys=False) as console:
        event = kernel.store.emit("turn.started", {}, bot_id="ada")
        assert console.output_failed
        assert kernel.store.events(after=event["seq"] - 1)


def controlling_terminal():
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


@pytest.mark.parametrize("stop_signal", [signal.SIGINT, signal.SIGTERM])
async def test_real_cli_terminal_keys_and_signal_restore(tmp_path, stop_signal):
    # Isolated draft bots, temporary data, random local port. No live Discord/provider calls.
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    os.set_blocking(master, False)
    env = {k: v for k, v in os.environ.items() if not k.startswith("HORTATOR_")}
    env.update(HORTATOR_ADMIN_PASSWORD="console-test-password-only", TERM="screen")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hortator.cli",
            "--data-dir",
            str(tmp_path / "runtime"),
            "serve",
            "--port",
            str(port),
            "--no-color",
        ],
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        preexec_fn=controlling_terminal,
    )
    captured = ""

    def drain():
        nonlocal captured
        try:
            while chunk := os.read(master, 65536):
                captured += chunk.decode(errors="replace")
        except BlockingIOError:
            pass

    async def until(text):
        async with asyncio.timeout(10):
            while text not in captured:
                drain()
                await asyncio.sleep(0.02)

    try:
        await until("Uvicorn running")
        assert "Single-key controls active" in captured
        state = termios.tcgetattr(slave)
        assert not state[3] & (termios.ICANON | termios.ECHO)
        assert state[3] & termios.ISIG
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            assert (await client.get("/api/health")).status_code == 200
            drain()
            assert "GET /api/health" not in captured
            os.write(master, b"+")
            await until("CONSOLE DEBUG")
            assert (await client.get("/api/health?token=must-not-be-logged")).status_code == 200
            await until("GET /api/health")
            os.write(master, b"f")
            await until('"path": "/api/health"')
            assert "must-not-be-logged" not in captured
            os.write(master, b"d")
            await until("d:discord=off")
            # An actual Ctrl-C byte exercises ISIG; SIGTERM exercises Uvicorn's restored signal handler.
            if stop_signal == signal.SIGINT:
                os.write(master, b"\x03")
            else:
                process.send_signal(stop_signal)
            code = await asyncio.to_thread(process.wait, timeout=8)
            drain()
            assert code == (0 if stop_signal == signal.SIGINT else -signal.SIGTERM), captured
            assert "Application shutdown complete" in captured
            assert termios.tcgetattr(slave) == original
            assert "\x1b[?1049" not in captured and "\x1b[2J" not in captured
    finally:
        if process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.wait, timeout=5)
        os.close(master)
        os.close(slave)
