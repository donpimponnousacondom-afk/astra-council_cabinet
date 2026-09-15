"""Real temporary Git histories and a separate Screen session; no live mutations."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest


spec = importlib.util.spec_from_file_location(
    "next_feature", Path(__file__).parents[1] / "scripts/next_feature.py"
)
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL, text=True).strip()


def commit(root, name, content=None):
    (root / name).write_text(content or name)
    git(root, "add", name)
    git(root, "commit", "-qm", name)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    upstream = tmp_path / "upstream.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(upstream)], check=True)
    root = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", str(upstream), str(root)], check=True, capture_output=True)
    git(root, "config", "user.name", "Workflow test")
    git(root, "config", "user.email", "workflow@example.invalid")
    git(root, "config", "core.hooksPath", "/dev/null")
    git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text("/web/dist/\n")
    git(root, "add", ".gitignore")
    commit(root, "baseline")
    git(root, "push", "-q", "origin", "main")  # Only the isolated temporary bare repository.
    git(root, "switch", "-qc", "feat/task")
    return root


def fake_prs(monkeypatch, records):
    original = workflow.run

    def run(args, *positional, **kwargs):
        if args[:3] == ["gh", "pr", "list"]:
            return json.dumps(records)
        return original(args, *positional, **kwargs)

    monkeypatch.setattr(workflow, "run", run)
    original_which = workflow.shutil.which
    monkeypatch.setattr(
        workflow.shutil, "which", lambda name: "/synthetic/gh" if name == "gh" else original_which(name)
    )


def test_fresh_placeholder_and_repeat_reuse(repo):
    before = git(repo, "rev-parse", "HEAD")
    plan = workflow.preflight(repo)
    assert workflow.transition(plan) is None
    assert git(repo, "branch", "--show-current") == "feat/next_feature"
    assert git(repo, "rev-parse", "HEAD") == before
    assert workflow.transition(workflow.preflight(repo)) is None
    assert not git(repo, "branch", "--list", "archive/*")
    assert git(repo, "rev-list", "--count", "HEAD") == "1"


def test_previous_placeholder_preserved_when_main_advances(repo):
    old = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "feat/next_feature")
    git(repo, "switch", "main")
    current = commit(repo, "another-merged-change")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "switch", "feat/task")
    archived = workflow.transition(workflow.preflight(repo))
    assert archived.startswith("archive/next_feature-")
    assert git(repo, "rev-parse", archived) == old
    assert git(repo, "rev-parse", "HEAD") == current
    assert git(repo, "rev-list", "--count", "main") == "2"


def squash(repo, monkeypatch):
    git(repo, "branch", "-m", "feat/next_feature")
    tip = commit(repo, "feature-change")
    git(repo, "switch", "main")
    git(repo, "merge", "--squash", "feat/next_feature")
    git(repo, "commit", "-qm", "Feature squashed (#42)")
    merge = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "switch", "feat/next_feature")
    fake_prs(monkeypatch, [{"number": 42, "headRefOid": tip, "mergeCommit": {"oid": merge}}])
    return tip, merge


def test_squash_merge_requires_exact_head_and_preserves_old_commits(repo, monkeypatch):
    tip, merge = squash(repo, monkeypatch)
    assert not workflow.ancestor(repo, tip, merge)
    plan = workflow.preflight(repo)
    assert plan["proof"] == "merged PR #42"
    archived = workflow.transition(plan)
    assert git(repo, "rev-parse", archived) == tip
    assert git(repo, "rev-parse", "HEAD") == merge


def test_commits_after_a_squashed_pr_are_not_discarded(repo, monkeypatch):
    squash(repo, monkeypatch)
    tip = commit(repo, "unfinished-addition")
    with pytest.raises(workflow.Stop, match="not verified as merged"):
        workflow.preflight(repo)
    assert git(repo, "rev-parse", "HEAD") == tip
    assert (repo / "unfinished-addition").exists()


def test_unmerged_existing_placeholder_blocks_another_merged_branch(repo, monkeypatch):
    git(repo, "switch", "-c", "feat/next_feature")
    commit(repo, "unfinished-placeholder")
    git(repo, "switch", "feat/task")
    fake_prs(monkeypatch, [])
    with pytest.raises(workflow.Stop, match="feat/next_feature contains work"):
        workflow.preflight(repo)
    assert git(repo, "branch", "--show-current") == "feat/task"


@pytest.mark.parametrize("tracked", [True, False])
def test_dirty_work_stops_before_fetch(repo, tracked, monkeypatch):
    (repo / ("baseline" if tracked else "untracked")).write_text("preserve this")

    def forbidden(*args):
        pytest.fail("Dirty work must stop before any ancestry/merge check")

    monkeypatch.setattr(workflow, "merged", forbidden)
    with pytest.raises(workflow.Stop, match="uncommitted or untracked"):
        workflow.preflight(repo)
    assert git(repo, "branch", "--show-current") == "feat/task"


def test_diverged_local_main_is_not_merged_or_rebased(repo):
    git(repo, "switch", "main")
    divergent = commit(repo, "local-only-main")
    git(repo, "switch", "feat/task")
    with pytest.raises(workflow.Stop, match="diverged"):
        workflow.preflight(repo)
    assert git(repo, "rev-parse", "main") == divergent
    assert git(repo, "branch", "--show-current") == "feat/task"


def test_another_worktree_owning_main_is_left_alone(repo):
    other = repo.parent / "other-worktree"
    git(repo, "worktree", "add", str(other), "main")
    with pytest.raises(workflow.Stop, match="another worktree"):
        workflow.preflight(repo)
    assert git(other, "branch", "--show-current") == "main"


def test_checkout_race_is_refused(repo):
    plan = workflow.preflight(repo)
    git(repo, "switch", "-c", "feat/another-agent")
    with pytest.raises(workflow.Stop, match="Checkout changed"):
        workflow.transition(plan)
    assert git(repo, "branch", "--show-current") == "feat/another-agent"


def test_refresh_does_not_require_merge_or_change_branch(repo, monkeypatch):
    tip = commit(repo, "not-yet-merged")
    fake_prs(monkeypatch, [])
    plan = workflow.preflight(repo, refresh=True)
    assert workflow.transition(plan) is None
    assert git(repo, "rev-parse", "HEAD") == tip
    assert git(repo, "branch", "--show-current") == "feat/task"
    git(repo, "switch", "main")
    with pytest.raises(workflow.Stop, match="task branch"):
        workflow.preflight(repo, refresh=True)


@pytest.mark.parametrize("failure", [None, "dependencies", "mismatched_stamp", "interrupted"])
def test_screen_worker_build_and_start_order_is_fail_closed(repo, monkeypatch, failure):
    data = repo.parent / "runtime"
    data.mkdir()
    path = repo.parent / "job.json"
    path.write_text(
        json.dumps({**workflow.preflight(repo, refresh=True), "data": str(data), "state": "queued"})
    )
    calls, executed = [], []
    real_run = workflow.run

    def run(args, root=repo, **kwargs):
        if args[0] in ("uv", "npm"):
            calls.append(args)
            if failure == "dependencies":
                raise workflow.Stop("Synthetic dependency failure")
            if failure == "interrupted":
                raise KeyboardInterrupt()
            if args[:3] == ["npm", "run", "build"]:
                target = repo / "web/dist"
                target.mkdir(parents=True)
                (target / "build-info.json").write_text(
                    json.dumps(
                        {
                            "commit": "0" * 40
                            if failure == "mismatched_stamp"
                            else git(repo, "rev-parse", "HEAD"),
                            "dirty": False,
                        }
                    )
                )
            return ""
        return real_run(args, root, **kwargs)

    monkeypatch.setattr(workflow, "run", run)
    monkeypatch.setattr(workflow, "listener", lambda: None)
    monkeypatch.setattr(workflow.os, "execv", lambda path, args: executed.append(args))
    if failure:
        with pytest.raises(KeyboardInterrupt if failure == "interrupted" else workflow.Stop):
            workflow.worker(path)
        assert not executed
        assert json.loads(path.read_text())["state"] == "failed"
        if failure in ("dependencies", "interrupted"):
            assert len(calls) == 1
    else:
        workflow.worker(path)
        assert calls == [
            ["uv", "sync", "--frozen"],
            ["npm", "ci", "--prefix", "web"],
            ["npm", "run", "build", "--prefix", "web"],
        ]
        assert executed == [
            [str(workflow.LAUNCHER), "serve", "--host", "127.0.0.1", "--port", "8000", "--color"]
        ]
        assert json.loads(path.read_text())["state"] == "starting"
    os.close(workflow.lock(data / ".next-feature-worker.lock"))


def test_second_controller_or_worker_cannot_overlap(tmp_path):
    fd = workflow.lock(tmp_path / "maintenance.lock")
    try:
        with pytest.raises(workflow.Stop, match="still running"):
            workflow.lock(tmp_path / "maintenance.lock")
    finally:
        os.close(fd)
    os.close(workflow.lock(tmp_path / "maintenance.lock"))


def test_subprocess_timeout_kills_spawned_group(tmp_path):
    marker = tmp_path / "child.pid"
    script = "import subprocess,time,sys;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);open(sys.argv[1],'w').write(str(p.pid));time.sleep(60)"
    with pytest.raises(workflow.Stop, match="timed out"):
        workflow.run([sys.executable, "-c", script, str(marker)], tmp_path, timeout=0.3)
    pid = int(marker.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        path = Path(f"/proc/{pid}/stat")
        try:
            state = path.read_text().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            break  # The child can exit between checking /proc and opening stat.
        if state == "Z":
            break
        time.sleep(0.05)
    else:
        pytest.fail("Timed-out build left a running child")


def test_real_screen_stop_preserves_shell_and_ignores_unrelated_commands(tmp_path, monkeypatch):
    if not shutil.which("screen"):
        pytest.skip("GNU Screen is required for terminal integration")
    root = tmp_path
    name = f"hortator-next-test-{os.getpid()}"
    subprocess.run(
        ["screen", "-c", "/home/codexy/.screenrc", "-dmS", name, "-t", "dashboard", "bash", "--login", "-i"],
        cwd=root,
        check=True,
    )
    original = workflow.screen_state
    monkeypatch.setattr(workflow, "listener", lambda: None)
    monkeypatch.setattr(workflow, "screen_state", lambda root: original(root, name))
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                state = workflow.screen_state(root)
                break
            except workflow.Stop:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        fake = root / "hortator"
        marker = root / "pid"
        fake.write_text(
            "import os,time\nfrom pathlib import Path\nPath('pid').write_text(str(os.getpid()))\ntry:time.sleep(60)\nexcept KeyboardInterrupt:pass\n"
        )
        workflow.stuff(state, f"{sys.executable} {fake} serve\r")
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        pid = int(marker.read_text())
        monkeypatch.setattr(workflow, "listener", lambda: pid if workflow.process(pid) else None)
        running = workflow.screen_state(root)
        assert running["server"] == pid
        workflow.stop_server(root, running)
        assert workflow.process(state["shell"])
        assert workflow.screen_state(root)["server"] is None
        workflow.stuff(state, "sleep 60\r")
        time.sleep(0.2)
        with pytest.raises(workflow.Stop, match="Another command"):
            workflow.screen_state(root)
    finally:
        assert name.startswith("hortator-next-test-") and name != "hortator"
        subprocess.run(["screen", "-S", name, "-X", "quit"], check=False, capture_output=True)
