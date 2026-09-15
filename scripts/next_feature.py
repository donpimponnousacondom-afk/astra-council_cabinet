"""Local operator workflow: verified merge -> shared Screen -> build -> runtime.

Standard-library only: dependency installation and checkout must not unload this
controller. The worker runs a private copy of this file outside the checkout.
"""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from urllib.request import urlopen

ROOT = Path("/home/codexy/codex/astra-council_cabinet")
LAUNCHER = Path("/home/codexy/.local/bin/hortator")
TARGET = "feat/next_feature"


class Stop(Exception):
    pass


def say(message):
    print(f"[hortator-next-feature] {message}", flush=True)


def run(args, root=ROOT, *, visible=False, timeout=60):
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
    environment.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    child = subprocess.Popen(
        args,
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=None if visible else subprocess.PIPE,
        stderr=None if visible else subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = child.communicate(timeout=timeout)
    except BaseException as error:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.communicate()
        if isinstance(error, subprocess.TimeoutExpired):
            raise Stop(f"{args[0]} timed out; no later workflow step was started.") from None
        raise
    if child.returncode:
        # Do not print credential-bearing remote URLs or authentication output.
        raise Stop(f"{args[0]} failed (exit {child.returncode}); no later workflow step was started.")
    return (output or "").strip()


def git(root, *args):
    return run(["git", *args], root)


def ancestor(root, old, new):
    result = subprocess.run(["git", "merge-base", "--is-ancestor", old, new], cwd=root, capture_output=True)
    if result.returncode not in (0, 1):
        raise Stop("Cannot establish Git ancestry; checkout was not changed.")
    return result.returncode == 0


def ref(root, name):
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "refs/heads/" + name],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise Stop("Cannot inspect local branch references.")
    return result.stdout.strip() if result.returncode == 0 else None


def clean(root):
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root.resolve():
        raise Stop("This is not the expected Hortator repository.")
    if git(root, "status", "--porcelain"):
        raise Stop(
            "Working tree has uncommitted or untracked files. Finish that work first; nothing was stashed or discarded."
        )


def merged(root, branch, head, remote_main):
    if ancestor(root, head, remote_main):
        return "included in main"
    if not shutil.which("gh"):
        raise Stop(f"{branch} needs squash-merge verification. Install/authenticate GitHub CLI, then retry.")
    candidates = json.loads(
        run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--base",
                "main",
                "--head",
                branch,
                "--limit",
                "100",
                "--json",
                "number,headRefOid,mergeCommit",
            ],
            root,
        )
    )
    for pr in candidates:
        commit = (pr.get("mergeCommit") or {}).get("oid")
        if pr.get("headRefOid") == head and commit and ancestor(root, commit, remote_main):
            return f"merged PR #{pr['number']}"
    raise Stop(
        f"{branch} contains work not verified as merged into origin/main. Merge its PR first; Screen was not interrupted."
    )


def preflight(root, refresh=False):
    clean(root)
    branch = git(root, "branch", "--show-current")
    if not branch:
        raise Stop("Detached HEAD: switch to the intended task branch first.")
    head = git(root, "rev-parse", "HEAD")
    if refresh:
        if branch == "main":
            raise Stop("--refresh expects a task branch; use the normal workflow to leave main.")
        return {"root": str(root), "branch": branch, "head": head, "refresh": True}
    say("Fetching origin/main and checking the previous task…")
    git(root, "fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main")
    remote_main = git(root, "rev-parse", "refs/remotes/origin/main")
    main = ref(root, "main")
    if not main or not ancestor(root, main, remote_main):
        raise Stop("Local main is missing or has diverged. No merge/rebase/reset was attempted.")
    proof = merged(root, branch, head, remote_main)
    placeholder = ref(root, TARGET)
    if placeholder and branch != TARGET:
        merged(root, TARGET, placeholder, remote_main)
    for worktree in git(root, "worktree", "list", "--porcelain").split("\n\n"):
        lines = worktree.splitlines()
        if any(line in ("branch refs/heads/main", "branch refs/heads/" + TARGET) for line in lines):
            location = next(line[9:] for line in lines if line.startswith("worktree "))
            if Path(location).resolve() != root.resolve():
                raise Stop("main or feat/next_feature is checked out in another worktree; it was left alone.")
    return {
        "root": str(root),
        "branch": branch,
        "head": head,
        "main": main,
        "remote_main": remote_main,
        "placeholder": placeholder,
        "proof": proof,
        "refresh": False,
    }


def transition(plan):
    root = Path(plan["root"])
    clean(root)
    if (
        git(root, "branch", "--show-current") != plan["branch"]
        or git(root, "rev-parse", "HEAD") != plan["head"]
    ):
        raise Stop("Checkout changed after preflight; refusing to switch another task.")
    if plan["refresh"]:
        return None
    if ref(root, "main") != plan["main"] or ref(root, TARGET) != plan["placeholder"]:
        raise Stop("A local branch changed after preflight; repeat the checks.")
    git(root, "switch", "main")
    try:
        git(root, "pull", "--ff-only", "origin", "main")
    except Stop:
        git(root, "switch", plan["branch"])
        raise
    clean(root)
    if git(root, "branch", "--show-current") != "main" or ref(root, TARGET) != plan["placeholder"]:
        raise Stop("Another checkout/branch change occurred during the pull; it was left alone.")
    new_main = git(root, "rev-parse", "HEAD")
    if not ancestor(root, plan["remote_main"], new_main):
        git(root, "switch", plan["branch"])
        raise Stop("Remote main moved outside the verified history; original task branch restored.")
    archived = None
    if plan["placeholder"] and plan["placeholder"] != new_main:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archived = f"archive/next_feature-{stamp}-{plan['placeholder'][:8]}"
        git(root, "branch", "-m", TARGET, archived)
        say(f"Preserved the previous placeholder as {archived}.")
    if ref(root, TARGET):
        git(root, "switch", TARGET)
    else:
        git(root, "switch", "-c", TARGET)
    return archived


def process(pid):
    try:
        directory = Path("/proc") / str(pid)
        fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
        return {
            "pid": pid,
            "ppid": int(fields[1]),
            "pgid": int(fields[2]),
            "foreground": int(fields[5]),
            "comm": (directory / "comm").read_text().strip(),
            "cwd": (directory / "cwd").resolve(),
            "argv": (directory / "cmdline").read_bytes().decode(errors="replace").split("\0"),
        }
    except OSError, ValueError:
        return None


def descends(pid, parent):
    for _ in range(32):
        if pid == parent:
            return True
        entry = process(pid)
        if not entry or entry["ppid"] == pid:
            break
        pid = entry["ppid"]
    return False


def listener():
    output = run(["ss", "-H", "-ltnp", "sport = :8000"])
    if not output:
        return None
    pids = set(map(int, re.findall(r"pid=(\d+)", output)))
    if len(pids) != 1 or "127.0.0.1:8000" not in output:
        raise Stop("Port 8000 is occupied by an unidentified process; it was left alone.")
    return next(iter(pids))


def screen_state(root, session_name="hortator"):
    result = subprocess.run(["screen", "-ls"], capture_output=True, text=True)
    sessions = re.findall(r"^\s*(\d+\." + re.escape(session_name) + r")\s", result.stdout, re.M)
    if len(sessions) != 1:
        raise Stop(
            "Expected one existing Screen session named hortator. It was not created or detached automatically."
        )
    session = sessions[0]
    screen_pid = int(session.split(".")[0])
    shells = []
    children = Path(f"/proc/{screen_pid}/task/{screen_pid}/children").read_text().split()
    for child in children:
        candidate = process(int(child))
        if (
            candidate
            and candidate["ppid"] == screen_pid
            and candidate["comm"] == "bash"
            and candidate["cwd"] == root
        ):
            shells.append(candidate)
    if len(shells) != 1:
        raise Stop("Cannot uniquely identify the shared repository Bash in Screen; no input was sent.")
    shell = shells[0]
    # Pin input to the verified shell's actual window number, even if another
    # Screen window is selected on a user's attached display. Never use -Q.
    environment = (Path("/proc") / str(shell["pid"]) / "environ").read_bytes().split(b"\0")
    window = next((item[7:].decode() for item in environment if item.startswith(b"WINDOW=")), "")
    if not window.isdigit():
        raise Stop("Cannot identify the shared Bash Screen window; no input was sent.")
    pid = listener()
    inside = descends(os.getpid(), shell["pid"])
    if pid:
        server = process(pid)
        is_serve = server and any(
            Path(arg).name == "hortator" and server["argv"][i + 1 : i + 2] == ["serve"]
            for i, arg in enumerate(server["argv"])
        )
        if (
            not server
            or not is_serve
            or server["cwd"] != root
            or not descends(pid, shell["pid"])
            or server["pgid"] != shell["foreground"]
        ):
            raise Stop("The listener is not the verified foreground Hortator server; no Ctrl-C was sent.")
    elif shell["foreground"] != shell["pgid"] and not (inside and shell["foreground"] == os.getpgrp()):
        raise Stop("Another command is running in the shared Screen window; it was left alone.")
    return {"session": session, "window": window, "shell": shell["pid"], "server": pid, "inside": inside}


def stuff(state, text):
    run(["screen", "-S", state["session"], "-p", state["window"], "-X", "stuff", text])


def stop_server(root, state):
    if not state["server"]:
        return
    if screen_state(root) != state:
        raise Stop("Screen/process state changed; no Ctrl-C was sent.")
    say("Stopping the foreground Hortator server in Screen; attachments stay connected…")
    stuff(state, "\x03")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        shell = process(state["shell"])
        if shell and shell["foreground"] == shell["pgid"] and listener() is None:
            return
        time.sleep(0.25)
    raise Stop(
        "Hortator did not return to its Bash prompt within 45 seconds. No kill or checkout was attempted."
    )


def lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise Stop("Another Hortator maintenance command is still running.") from None
    return fd


def write_status(path, state, **values):
    value = {**json.loads(path.read_text()), **values, "state": state}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.chmod(0o600)
    temporary.replace(path)
    return value


def worker(path):
    job = json.loads(path.read_text())
    root, data = Path(job["root"]), Path(job["data"])
    worker_lock = lock(data / ".next-feature-worker.lock")
    try:
        if listener() is not None:
            raise Stop("A server started before maintenance; no checkout was attempted.")
        write_status(path, "updating")
        archived = transition(job)
        selected_commit = git(root, "rev-parse", "HEAD")
        write_status(path, "dependencies", archive=archived)
        say("Updating locked dependencies…")
        run(["uv", "sync", "--frozen"], root, visible=True, timeout=300)
        run(["npm", "ci", "--prefix", "web"], root, visible=True, timeout=300)
        write_status(path, "building")
        say("Building the dashboard for the selected commit…")
        run(["npm", "run", "build", "--prefix", "web"], root, visible=True, timeout=300)
        clean(root)
        commit = git(root, "rev-parse", "HEAD")
        branch = git(root, "branch", "--show-current")
        expected_branch = job["branch"] if job["refresh"] else TARGET
        if branch != expected_branch or commit != selected_commit:
            raise Stop("Checkout changed during the build; the server was not started.")
        build = json.loads((root / "web/dist/build-info.json").read_text())
        if build.get("commit") != commit or build.get("dirty") is not False:
            raise Stop("Dashboard build stamp does not match the clean checkout; the server was not started.")
        write_status(path, "starting", commit=commit, final_branch=branch)
        say(f"Starting {branch} at {commit[:12]} in this Screen window.")
        # exec keeps the server in the same foreground process group and closes
        # the maintenance lock before runtime startup; normal Ctrl-C returns to Bash.
        os.execv(str(LAUNCHER), [str(LAUNCHER), "serve", "--host", "127.0.0.1", "--port", "8000", "--color"])
    except BaseException as error:
        write_status(
            path,
            "failed",
            error=str(error) if isinstance(error, Stop) else f"{type(error).__name__}: maintenance failed",
        )
        raise
    finally:
        os.close(worker_lock)


def ready(job):
    """Compare HTTP assets and the startup-captured event, without logging in."""
    try:
        with urlopen("http://127.0.0.1:8000/api/health", timeout=2) as response:
            if json.load(response).get("status") != "ok":
                return False
        with urlopen("http://127.0.0.1:8000/build-info.json", timeout=2) as response:
            build = json.load(response)
        pid = listener()
        db = sqlite3.connect(f"file:{job['data']}/council.sqlite3?mode=ro", uri=True)
        try:
            rows = db.execute(
                "SELECT data FROM events WHERE kind='runtime.started' ORDER BY seq DESC LIMIT 5"
            ).fetchall()
        finally:
            db.close()
        event = next((e for (raw,) in rows if (e := json.loads(raw)).get("pid") == pid), None)
        return bool(
            event
            and event["build"]["commit"] == build["commit"] == job["commit"]
            and event["build"]["dirty"] is False
            and build["dirty"] is False
        )
    except OSError, ValueError, KeyError, sqlite3.Error, Stop:
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Update merged Hortator code, prepare feat/next_feature, build and restart in shared Screen."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check Git/Screen prerequisites; fetch metadata only, no stop/checkout/build.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Rebuild/restart the clean current task branch, without switching or pulling.",
    )
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
        return
    data = Path(os.environ.get("HORTATOR_DATA_DIR", "/home/codexy/.local/share/hortator")).resolve()
    if not (data / "council.sqlite3").is_file() or data.is_relative_to(ROOT):
        raise Stop("Expected the existing external Hortator data directory; nothing was initialized.")
    for name in ("git", "screen", "uv", "npm", "python3.14", "ss"):
        if not shutil.which(name):
            raise Stop(f"Required command is missing: {name}")
    if not LAUNCHER.is_file():
        raise Stop("The external Hortator runtime launcher is missing.")
    control_lock = lock(data / ".next-feature-controller.lock")
    try:
        probe = lock(data / ".next-feature-worker.lock")
        os.close(probe)
        plan = preflight(ROOT, args.refresh)
        state = screen_state(ROOT)
        if args.check:
            say(f"Checks passed for {plan['branch']}; Screen {state['session']} was left running.")
            return
        directory = Path(tempfile.mkdtemp(prefix="hortator-next-feature-"))
        script = directory / "worker.py"
        shutil.copyfile(__file__, script)
        path = directory / "job.json"
        path.write_text(json.dumps({**plan, "data": str(data), "state": "queued"}))
        path.chmod(0o600)
        if state["inside"]:
            worker(path)
            return
        stop_server(ROOT, state)
        fresh = screen_state(ROOT)
        if fresh["shell"] != state["shell"] or fresh["server"]:
            raise Stop("Screen changed before the worker launch; no shell command was sent.")
        stuff(state, shlex.join(["python3.14", str(script), "--worker", str(path)]) + "\r")
        deadline, previous = time.monotonic() + 1000, None
        while time.monotonic() < deadline:
            job = json.loads(path.read_text())
            if job["state"] != previous:
                previous = job["state"]
                say(f"Screen: {previous}.")
                if previous == "starting":
                    deadline = min(deadline, time.monotonic() + 60)
            if previous == "failed":
                raise Stop(job["error"] + f" Inspect Screen; workflow record: {path}")
            if previous == "starting" and ready(job):
                receipt = data / "logs/next-feature.json"
                receipt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                receipt.write_text(
                    json.dumps(
                        {**job, "state": "ready", "verified_at": datetime.now(timezone.utc).isoformat()},
                        indent=2,
                    )
                    + "\n"
                )
                receipt.chmod(0o600)
                say(f"Ready: {job['final_branch']} · {job['commit'][:12]} · dashboard/server match.")
                say("Refresh your browser with Ctrl-Shift-R. Join the terminal with screen -x hortator.")
                shutil.rmtree(directory)
                return
            shell = process(state["shell"])
            if previous != "queued" and shell and shell["foreground"] == shell["pgid"]:
                raise Stop(f"The Screen worker exited before a healthy startup. Inspect Screen and {path}.")
            time.sleep(0.5)
        raise Stop(
            f"Timed out waiting for the Screen worker. Inspect Screen and {path}; no process was killed."
        )
    finally:
        os.close(control_lock)


if __name__ == "__main__":
    try:
        main()
    except Stop as error:
        say(str(error))
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        say(
            "Controller interrupted. Any Screen worker keeps its own lifecycle; inspect Screen before retrying."
        )
        raise SystemExit(130) from None
