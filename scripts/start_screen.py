"""Start the existing installation in shared Screen without duplicate servers."""

import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.request import urlopen

import next_feature as workflow


def main():
    data = Path(os.environ.get("HORTATOR_DATA_DIR", "")).resolve()
    if data.is_relative_to(workflow.ROOT) or not (data / "council.sqlite3").is_file():
        raise workflow.Stop("Expected the existing external data directory; nothing was initialized.")
    os.umask(0o077)
    controller = workflow.lock(data / ".start-screen.lock")
    try:
        sessions = subprocess.run(["screen", "-ls"], capture_output=True, text=True).stdout
        found = re.findall(r"^\s*\d+\.hortator\s", sessions, re.M)
        if not found:
            if workflow.listener() is not None:
                raise workflow.Stop("Port 8000 is occupied outside shared Screen; no server started.")
            logs = data / "logs"
            logs.mkdir(mode=0o700, exist_ok=True)
            logs.chmod(0o700)
            subprocess.run(
                [
                    "screen",
                    "-c",
                    "/home/codexy/.screenrc",
                    "-dmS",
                    "hortator",
                    "-t",
                    "dashboard",
                    "-L",
                    "-Logfile",
                    str(logs / "hortator.screen.log"),
                    "bash",
                    "--login",
                    "-i",
                ],
                cwd=workflow.ROOT,
                check=True,
            )
            deadline = time.monotonic() + 10
            while True:
                try:
                    state = workflow.screen_state(workflow.ROOT)
                    break
                except workflow.Stop:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.2)
            subprocess.run(
                ["screen", "-S", state["session"], "-p", state["window"], "-X", "logfile", "flush", "1"],
                check=True,
            )
        else:
            state = workflow.screen_state(workflow.ROOT)
        if not state["server"]:
            # Recheck the foreground shell immediately before sending any input.
            if workflow.screen_state(workflow.ROOT) != state:
                raise workflow.Stop("Shared terminal changed; no input was sent.")
            workflow.stuff(
                state,
                str(workflow.LAUNCHER) + " serve --host 127.0.0.1 --port 8000 --color\r",
            )
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                with urlopen("http://127.0.0.1:8000/api/health", timeout=2) as response:
                    healthy = response.status == 200 and json.load(response).get("status") == "ok"
                current = workflow.screen_state(workflow.ROOT)
                if healthy and current["server"] and current["shell"] == state["shell"]:
                    print(f"Hortator ready: http://127.0.0.1:8000 · Screen {state['session']}")
                    print("Join the terminal: screen -x hortator")
                    return
            except OSError, ValueError, workflow.Stop:
                pass
            time.sleep(0.5)
        raise workflow.Stop(
            "Startup was not confirmed. Inspect screen -x hortator; no second start attempted."
        )
    finally:
        os.close(controller)


if __name__ == "__main__":
    try:
        main()
    except (workflow.Stop, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None
