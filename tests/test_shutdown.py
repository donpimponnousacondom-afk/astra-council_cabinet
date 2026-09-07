import asyncio
import os
import signal
import socket
import sqlite3
import subprocess
import sys

import httpx
import pytest


@pytest.mark.parametrize("stop_signal", [signal.SIGINT, signal.SIGTERM])
async def test_cli_shutdown_drains_open_dashboard_stream(tmp_path, stop_signal):
    # A real server process catches Uvicorn's ordering: connections drain before lifespan cleanup.
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    directory = tmp_path / "runtime"
    env = {k: v for k, v in os.environ.items() if not k.startswith("HORTATOR_")}
    password = "isolated-shutdown-test-password"
    env["HORTATOR_ADMIN_PASSWORD"] = password
    log_path = tmp_path / "server.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hortator.cli",
                "--data-dir",
                str(directory),
                "serve",
                "--port",
                str(port),
            ],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=3) as client:
                async with asyncio.timeout(10):
                    while True:
                        assert process.poll() is None, log_path.read_text()
                        try:
                            if (await client.get("/api/health")).status_code == 200:
                                break
                        except httpx.ConnectError:
                            pass
                        await asyncio.sleep(0.05)
                login = await client.post("/api/auth/login", json={"password": password})
                assert login.status_code == 200
                async with client.stream("GET", "/api/events/stream") as response:
                    assert response.status_code == 200
                    lines = response.aiter_lines()
                    assert await anext(lines) == "event: connected"
                    process.send_signal(stop_signal)
                    # Keep the SSE connection open while waiting for process exit.
                    code = await asyncio.to_thread(process.wait, timeout=8)
                assert code == (0 if stop_signal == signal.SIGINT else -signal.SIGTERM)
        finally:
            if process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)
    output = log_path.read_text()
    assert "Application shutdown complete" in output
    assert "Traceback" not in output and "timeout graceful shutdown" not in output
    with sqlite3.connect(f"file:{directory / 'council.sqlite3'}?mode=ro&immutable=1", uri=True) as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='runtime.stopped'").fetchone()[0] == 1
