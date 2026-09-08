import asyncio
import base64
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


def read_all_evidence(console):
    for _ in range(100):
        if console.evidence.get("next") is None:
            return
        console.key("n")
    raise AssertionError("Synthetic evidence should fit within 100 bounded pages")


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
                "data_base64": base64.b64encode(secret.encode()).decode(),
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
            and base64.b64encode(secret.encode()).decode() not in text
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
        assert "\n  │ Traceback (most recent call last):\n" in text
        assert "\\n  File" not in text


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


async def test_different_jobs_and_tool_calls_do_not_hide_each_other_as_repeats(kernel):
    before = len(kernel.store.events(limit=100))
    with output() as console:
        for job_id in ("job-a", "job-b", "job-b"):
            kernel.store.emit(
                "job.failed", {"job_id": job_id, "status": "failed"}, bot_id="ada", level="warning"
            )
        for call_id in ("call-a", "call-b", "call-b"):
            kernel.store.emit(
                "tool.failed",
                {"call_id": call_id, "error": "The same validation error on separate calls"},
                bot_id="ada",
                level="warning",
            )
        assert console.stream.getvalue().count("job.failed") == 2
        assert console.stream.getvalue().count("tool.failed") == 2
        console.flush_repeats(force=True)
        assert console.stream.getvalue().count("1 additional repeats") == 2
        assert len(kernel.store.events(limit=100)) == before + 6


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


async def test_scope_depths_are_independent_and_selectable_without_changing_filters_or_ledger(kernel):
    with output() as console:
        console.bind(kernel)
        first = kernel.store.emit("document.updated", {"site": "example", "revision": 1}, bot_id="ada")
        second = kernel.store.emit("publishing.delivered", {"site": "example", "revision": 1}, bot_id="ada")
        kernel.store.emit(
            "provider.failure", {"provider_id": "example", "error": "synthetic failure"}, level="error"
        )
        before = kernel.store.events(limit=100)
        console.key("T")
        assert console.scope_depths == {"tools": 1, "providers": 0}
        console.key("T")
        assert console.evidence["seq"] == second["seq"]
        console.key("[")
        assert console.evidence["seq"] == first["seq"]
        console.key("]")
        assert console.evidence["seq"] == second["seq"]
        console.key("P")
        assert console.scope_depths == {"tools": 2, "providers": 1}
        console.key("P")
        assert "No stored request is linked" in console.stream.getvalue()
        console.key("t")
        assert "tools" not in console.scopes and console.scope_depths["tools"] == 2
        console.key("f")
        assert console.details
        console.key("0")
        assert console.scope_depths == {"tools": 0, "providers": 0}
        assert console.evidence is None and not console.details and "tools" in console.scopes
        assert kernel.store.events(limit=100) == before


async def test_job_deep_evidence_recovers_command_output_and_nonzero_exit_from_minimal_event(kernel):
    job_id = "job_" + "a" * 32
    directory = kernel.directory / "jobs" / job_id
    directory.mkdir(mode=0o700)
    secret = "later-vault-test-value"
    command = "printf 'synthetic only' # " + "x" * 6500 + " COMMAND-END"
    stdout = ("chunk 🙂 " + "x" * 80 + "\n") * 120 + f"STDOUT-END {secret}\x1b[2J\n"
    stderr = "expected diagnostic\nSTDERR-END\n"
    metadata = {
        "job_id": job_id,
        "bot_id": "ada",
        "channel_id": "channel",
        "turn_id": "turn-job",
        "task": "console-fixture",
        "status": "failed",
        "exit_code": 1,
        "workspace_committed": True,
        "started_at": 1,
        "stdout_bytes": len(stdout.encode()),
        "stderr_bytes": len(stderr.encode()),
    }
    for name, value in (("meta.json", json.dumps(metadata)), ("stdout.log", stdout), ("stderr.log", stderr)):
        (directory / name).write_text(value)
        (directory / name).chmod(0o600)
    kernel.store.emit(
        "tool.started",
        {
            "name": "shell",
            "call_id": "call-job",
            "arguments": {
                "operation": "run",
                "task": "console-fixture",
                "command": command,
            },
        },
        bot_id="ada",
        turn_id="turn-job",
    )
    event = kernel.store.emit(
        "job.failed",
        {"job_id": job_id, "status": "failed"},
        bot_id="ada",
        turn_id="turn-job",
        level="warning",
    )
    before = kernel.store.events(limit=100)
    with output() as console:
        console.bind(kernel)
        console.key("T")
        console.key("T")
        assert console.evidence["seq"] == event["seq"]
        assert "STDOUT-END" not in console.stream.getvalue()  # First page is bounded.
        kernel.vault.put("provider/test/api_key", secret)  # A later credential is scrubbed on reread.
        read_all_evidence(console)
        text = console.stream.getvalue()
        assert "COMMAND-END" in text and "STDOUT-END" in text and "STDERR-END" in text
        assert "command_exit (nonzero exit; valid workspace changes saved)" in text
        assert '"exit_code": 1' in text and "call-job" in text and job_id in text
        assert secret not in text and "\x1b" not in text and "\\u001b[2J" in text
        assert "n next page" in text and "End of snapshot" in text
        page = console.evidence["page"]
        console.key("N")
        assert console.evidence["page"] == page - 1
        kernel.store.emit("document.updated", {"site": "newer"}, bot_id="ada")
        assert console.evidence["seq"] == event["seq"]
        assert f"n/N still page selected event #{event['seq']}" in console.stream.getvalue()
    assert (directory / "stdout.log").read_text() == stdout
    assert kernel.store.events(before=event["seq"] + 1, limit=100) == before


async def test_provider_depth_reads_complete_stored_request_and_preserves_redactions(kernel):
    secret = "provider-console-secret-only"
    kernel.vault.put("provider/openrouter/api_key", secret)
    body = {
        "model": "fixture-model",
        "messages": [{"role": "user", "content": "x" * 7000 + " PROMPT-END"}]
        + [{"role": "user", "content": f"message-{n}"} for n in range(45)],
        "reasoning": {"effort": "low"},
        "headers": {"Authorization": secret},
    }
    response = {
        "content": "y" * 7000 + f" RESPONSE-END {secret}\x1b[2J",
        "reasoning_content": "never-display-reasoning",
    }
    kernel.store.execute(
        "INSERT INTO requests(id,turn_id,bot_id,provider_id,profile_id,model,purpose,started_at,status,body,context,response,ttft_ms,duration_ms) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "req-console",
            "turn-console",
            "ada",
            "openrouter",
            "balanced",
            "fixture-model",
            "generation",
            1,
            "completed",
            json.dumps(body),
            json.dumps({"queue_ms": 12.5}),
            json.dumps(response),
            1234,
            5678,
        ),
    )
    event = kernel.store.emit(
        "request.completed",
        {"provider_id": "openrouter"},
        bot_id="ada",
        turn_id="turn-console",
        request_id="req-console",
    )
    before = kernel.store.one("SELECT * FROM requests WHERE id='req-console'")
    with output() as console:
        console.bind(kernel)
        console.key("P")
        console.key("P")
        assert console.evidence["seq"] == event["seq"]
        read_all_evidence(console)
        text = console.stream.getvalue()
        assert "PROMPT-END" in text and "RESPONSE-END" in text and "message-44" in text
        assert all(
            value in text
            for value in (
                "fixture-model",
                "generation",
                '"ttft_ms": 1234',
                '"duration_ms": 5678',
                '"queue_ms": 12.5',
            )
        )
        assert secret not in text and "never-display-reasoning" not in text and "\x1b" not in text
        assert '"reasoning"' in text and '"effort": "low"' in text
        assert '"input_tokens": null' in text and "null means unknown" in text
    assert kernel.store.one("SELECT * FROM requests WHERE id='req-console'") == before


async def test_deep_job_error_is_distinct_and_expired_missing_output_is_explicit(kernel):
    job_id = "job_" + "b" * 32
    directory = kernel.directory / "jobs" / job_id
    directory.mkdir(mode=0o700)
    (directory / "meta.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "bot_id": "ada",
                "channel_id": "channel",
                "turn_id": "turn-job",
                "task": "console-fixture",
                "status": "failed",
                "workspace_committed": False,
                "error": "Unsafe output tree; sandbox changes discarded",
                "output_expired": True,
            }
        )
    )
    kernel.store.emit(
        "job.failed",
        {"job_id": job_id, "status": "failed"},
        bot_id="ada",
        turn_id="turn-job",
        level="warning",
    )
    with output() as console:
        console.bind(kernel)
        console.key("T")
        console.key("T")
        read_all_evidence(console)
        text = console.stream.getvalue()
        assert "runner_or_validation_failure" in text and "Unsafe output tree" in text
        assert "Output expired under retention" in text and "cannot reconstruct a missing command" in text
        assert "command_exit (nonzero" not in text


async def test_console_json_and_evidence_survive_malformed_tool_arguments(kernel):
    kernel.store.emit(
        "tool.started",
        {"name": "shell", "arguments": None, "call_id": "malformed"},
        bot_id="ada",
        turn_id="turn-malformed",
    )
    kernel.store.emit(
        "tool.failed",
        {"name": "shell", "error": "Arguments must be an object", "call_id": "malformed"},
        bot_id="ada",
        turn_id="turn-malformed",
        level="warning",
    )
    with output() as console:
        console.bind(kernel)
        console.key("T")
        console.key("T")
        read_all_evidence(console)
        assert "Arguments must be an object" in console.stream.getvalue()
        assert '"arguments": null' in console.stream.getvalue()
        assert "Stored evidence unavailable" not in console.stream.getvalue()


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
            os.write(master, b"T")
            await until("T tools=json")
            os.write(master, b"T")
            await until("T tools=evidence")
            os.write(master, b"P")
            await until("P providers=json")
            os.write(master, b"n")
            await until("Select stored evidence with T or P first")
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
