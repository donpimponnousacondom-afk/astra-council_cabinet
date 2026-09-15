import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from hortator.publishing_receiver import HTACCESS, Receiver, ReceiverError, canonical, validate_envelope


def envelope(*, bot="dirac", slug="cat-report", revision=1, job=None, files=None):
    files = (
        files
        if files is not None
        else {
            "index.html": b'<link rel="stylesheet" href="assets/style.css"><script src="app.js"></script><img src="chart.svg">',
            "assets/style.css": b"body { color: green; }",
            "app.js": b"document.body.dataset.ready = 'yes'",
            "chart.svg": b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="10"/></svg>',
        }
    )
    return {
        "version": 1,
        "bot_id": bot,
        "slug": slug,
        "revision": revision,
        "job_id": job or f"job-{bot}-{slug}-{revision}",
        "turn_id": "turn-1",
        "created_at": 1788849900.0,
        "local_snapshot_commit": "a" * 40,
        "files": {
            path: {
                "blob": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "mime": "text/plain",
                "content": base64.b64encode(data).decode(),
            }
            for path, data in files.items()
        },
    }


@pytest.fixture
def receiver(tmp_path):
    if not shutil.which("git"):
        pytest.skip("The receiver requires real Git snapshots")
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("existing unrelated domain page")
    (web / ".dh-diag").symlink_to(tmp_path / "unrelated-dreamhost-path")
    return Receiver(web, tmp_path / "state")


def git(receiver, *args):
    return subprocess.check_output(["git", "-C", str(receiver.history), *args], text=True).strip()


def public(receiver, bot="dirac", slug="cat-report"):
    return receiver.web / bot / slug


def test_complete_release_is_atomic_static_and_snapshotted_before_it_is_visible(receiver, monkeypatch):
    replace = os.replace
    publication_checks = []

    def checked_replace(source, target):
        if Path(target) == public(receiver):
            assert git(receiver, "rev-list", "--count", "HEAD") == "1"
            staged = Path(source).resolve()
            assert (staged / "index.html").read_bytes().startswith(b"<link")
            assert (staged / "assets/style.css").read_bytes() == b"body { color: green; }"
            assert not public(receiver).exists()
            publication_checks.append(True)
        return replace(source, target)

    monkeypatch.setattr(os, "replace", checked_replace)
    payload = envelope()
    result = receiver.deploy(payload)
    assert publication_checks == [True]
    assert result["delivered"] and result["delivery_current"]
    assert result["remote_commit"] == git(receiver, "rev-parse", "HEAD")
    assert (
        result["manifest_hash"]
        == hashlib.sha256(
            canonical(
                {
                    path: {k: v for k, v in data.items() if k != "content"}
                    for path, data in payload["files"].items()
                }
            )
        ).hexdigest()
    )
    assert public(receiver).is_symlink()
    assert (receiver.web / "dirac" / ".htaccess").read_text() == HTACCESS
    assert "Options -Indexes -ExecCGI -Includes" in HTACCESS
    assert "RewriteRule ^$ - [R=404,L]" in HTACCESS
    assert "SetHandler default-handler" in HTACCESS
    assert (receiver.web / "index.html").read_text() == "existing unrelated domain page"
    assert (receiver.web / ".dh-diag").is_symlink()
    assert not (receiver.web / ".htaccess").exists()
    assert not (receiver.web / "dirac/index.html").exists()
    for path in (
        receiver.state,
        receiver.releases,
        public(receiver).resolve(),
        public(receiver).resolve().parent,
    ):
        assert path.stat().st_mode & 0o777 == 0o755
    for path in (receiver.metadata, receiver.history, receiver.metadata / "jobs"):
        assert path.stat().st_mode & 0o777 == 0o700
    assert (public(receiver) / "chart.svg").stat().st_mode & 0o777 == 0o644
    assert (receiver.web / "dirac/.hortator-owner.json").stat().st_mode & 0o777 == 0o600
    assert (receiver.history / "sites/dirac/cat-report/index.html").stat().st_mode & 0o777 == 0o600
    assert git(receiver, "branch", "--show-current") == "snapshots"
    assert git(receiver, "remote") == ""


def test_retries_are_idempotent_and_old_delivered_jobs_do_not_rollback(receiver):
    first = envelope()
    receipt = receiver.deploy(first)
    assert receiver.deploy(first) == receipt
    assert git(receiver, "rev-list", "--count", "HEAD") == "1"
    second = envelope(revision=2)
    second["files"]["index.html"] = envelope(files={"index.html": b"updated"})["files"]["index.html"]
    newer = receiver.deploy(second)
    assert newer["current_revision"] == 2
    old = receiver.deploy(first)
    assert old["delivered"] and not old["delivery_current"]
    assert old["current_revision"] == 2
    assert (public(receiver) / "index.html").read_text() == "updated"
    assert git(receiver, "rev-list", "--count", "HEAD") == "2"
    assert git(receiver, "show", receipt["remote_commit"] + ":sites/dirac/cat-report/index.html").startswith(
        "<link"
    )
    with pytest.raises(ReceiverError, match="stale"):
        receiver.deploy(envelope(revision=1, job="different-stale-job"))
    assert (public(receiver) / "index.html").read_text() == "updated"


def test_cannot_reuse_a_job_for_other_content_or_another_bot(receiver):
    payload = envelope()
    receiver.deploy(payload)
    for different in (
        envelope(bot="hortator", job=payload["job_id"]),
        envelope(job=payload["job_id"], files={"index.html": b"different"}),
    ):
        with pytest.raises(ReceiverError, match="identifier is already reserved"):
            receiver.deploy(different)
    assert not (receiver.web / "hortator").exists()


def test_no_deletion_and_other_bot_namespace_stays_separate(receiver):
    receiver.deploy(envelope())
    with pytest.raises(ReceiverError, match="cannot be deleted"):
        receiver.deploy(envelope(revision=2, files={"index.html": b"lost assets"}))
    receiver.deploy(envelope(bot="hortator", files={"index.html": b"Hortator's copy"}))
    assert (public(receiver) / "chart.svg").exists()
    assert (public(receiver, "hortator") / "index.html").read_text() == "Hortator's copy"
    assert (public(receiver) / "index.html").read_bytes().startswith(b"<link")
    assert (
        receiver.status(
            {"version": 1, "bot_id": "hortator", "slug": "cat-report", "job_id": "job-dirac-cat-report-1"}
        )["status"]
        == "unknown"
    )


@pytest.mark.parametrize("kind", ["directory", "symlink", "file"])
def test_unmanaged_namespace_collision_never_replaces_any_content(receiver, tmp_path, kind):
    path = receiver.web / "dirac"
    if kind == "directory":
        path.mkdir()
        (path / "keep.txt").write_text("mine")
    elif kind == "symlink":
        path.symlink_to(tmp_path / "someone-else")
    else:
        path.write_text("mine")
    with pytest.raises(ReceiverError, match="namespace already exists"):
        receiver.deploy(envelope())
    assert not (receiver.metadata / "bots/dirac.json").exists()
    if kind == "directory":
        assert (path / "keep.txt").read_text() == "mine"
    elif kind == "symlink":
        assert os.readlink(path) == str(tmp_path / "someone-else")
    else:
        assert path.read_text() == "mine"


def test_unmanaged_site_collision_and_foreign_owner_markers_refuse(receiver):
    receiver.deploy(envelope())
    occupied = receiver.web / "dirac/occupied"
    occupied.mkdir()
    (occupied / "index.html").write_text("unmanaged")
    with pytest.raises(ReceiverError, match="name is already taken"):
        receiver.deploy(envelope(slug="occupied"))
    assert (occupied / "index.html").read_text() == "unmanaged"
    namespace = receiver.web / "dirac/.hortator-owner.json"
    original = namespace.read_text()
    value = json.loads(original)
    value["bot_id"] = "hortator"
    namespace.write_text(json.dumps(value))
    with pytest.raises(ReceiverError, match="foreign ownership"):
        receiver.deploy(envelope(revision=2))
    namespace.write_text(original)
    site = receiver.metadata / "sites/dirac/cat-report.json"
    value = json.loads(site.read_text())
    value["owner"] = "hortator"
    site.write_text(json.dumps(value))
    with pytest.raises(ReceiverError, match="another bot"):
        receiver.deploy(envelope(revision=2))


@pytest.mark.parametrize(
    "path",
    [
        "../outside.html",
        "/root.html",
        ".htaccess",
        "assets/.secret.txt",
        "a/../x.html",
        "bad.php",
        "bad.php.html",
        "run.sh",
        "bad.py.html",
        "a\\index.html",
        "bad\nindex.html",
        "assets//file.css",
        "a/.hidden/index.html",
    ],
)
def test_unsafe_paths_are_rejected_before_any_remote_state_is_created(receiver, path):
    with pytest.raises(ReceiverError, match="Invalid publish request"):
        receiver.deploy(envelope(files={path: b"untrusted"}))
    assert not receiver.state.exists()
    assert not (receiver.web / "dirac").exists()


def test_all_manifest_errors_are_reported_together(receiver):
    payload = envelope(files={"index.html": b"hello"})
    payload["revision"] = True
    payload["slug"] = ""
    payload["files"]["index.html"]["bytes"] = 17
    payload["files"]["index.html"]["blob"] = "b" * 64
    with pytest.raises(ReceiverError) as error:
        receiver.deploy(payload)
    assert all(text in str(error.value) for text in ("revision", "slug", "bytes does not match", "SHA256"))
    assert not receiver.state.exists()


def test_file_directory_collision_and_invalid_json_values_are_rejected(receiver):
    with pytest.raises(ReceiverError, match="parent path is already a file"):
        receiver.deploy(envelope(files={"x.html": b"file", "x.html/index.html": b"child"}))
    for value in (float("nan"), float("inf"), "not-a-time", "2026-09-08T00:00:00"):
        payload = envelope()
        payload["created_at"] = value
        with pytest.raises(ReceiverError, match="created_at"):
            validate_envelope(payload)


def test_git_failure_leaves_existing_site_unchanged_and_same_job_can_retry(receiver, monkeypatch):
    receiver.deploy(envelope())
    old_target = os.readlink(public(receiver))
    original = receiver._snapshot

    def fail(*args):
        raise ReceiverError("synthetic Git failure")

    monkeypatch.setattr(receiver, "_snapshot", fail)
    with pytest.raises(ReceiverError, match="synthetic Git failure"):
        receiver.deploy(envelope(revision=2))
    assert os.readlink(public(receiver)) == old_target
    assert git(receiver, "rev-list", "--count", "HEAD") == "1"
    monkeypatch.setattr(receiver, "_snapshot", original)
    assert receiver.deploy(envelope(revision=2))["delivery_current"]
    assert git(receiver, "rev-list", "--count", "HEAD") == "2"


def test_crash_before_atomic_link_swap_preserves_old_content_and_recovers_on_retry(receiver, monkeypatch):
    receiver.deploy(envelope())
    original = os.replace
    old_target = os.readlink(public(receiver))

    def fail_link(source, target):
        if Path(target) == public(receiver):
            raise OSError("synthetic swap failure")
        return original(source, target)

    monkeypatch.setattr(os, "replace", fail_link)
    with pytest.raises(OSError, match="synthetic swap failure"):
        receiver.deploy(envelope(revision=2))
    assert os.readlink(public(receiver)) == old_target
    assert git(receiver, "rev-list", "--count", "HEAD") == "2"
    with pytest.raises(ReceiverError, match="earlier publication is incomplete"):
        receiver.deploy(envelope(revision=3))
    monkeypatch.setattr(os, "replace", original)
    assert receiver.deploy(envelope(revision=2))["delivery_current"]
    assert git(receiver, "rev-list", "--count", "HEAD") == "2"


def test_crash_after_atomic_link_swap_can_finish_receipt_without_duplicate_snapshot(receiver, monkeypatch):
    receiver.deploy(envelope())
    original = receiver._save_site

    def fail_site(bot, slug, site):
        if site["current"] == "job-dirac-cat-report-2":
            raise OSError("synthetic final receipt failure")
        return original(bot, slug, site)

    monkeypatch.setattr(receiver, "_save_site", fail_site)
    with pytest.raises(OSError, match="synthetic final receipt failure"):
        receiver.deploy(envelope(revision=2))
    new_target = os.readlink(public(receiver))
    old_status = receiver.status(
        {"version": 1, "bot_id": "dirac", "slug": "cat-report", "job_id": "job-dirac-cat-report-1"}
    )
    assert old_status["delivered"] and not old_status["delivery_current"]
    assert old_status["current_revision"] == 2
    monkeypatch.setattr(receiver, "_save_site", original)
    receipt = receiver.deploy(envelope(revision=2))
    assert receipt["delivery_current"] and receipt["current_revision"] == 2
    assert os.readlink(public(receiver)) == new_target
    assert git(receiver, "rev-list", "--count", "HEAD") == "2"


@pytest.mark.parametrize("failure_point", ["mkdir", "marker", "serving-rules"])
def test_interrupted_namespace_registration_recovers_only_with_proven_ownership(
    receiver, monkeypatch, failure_point
):
    import hortator.publishing_receiver as module

    original_mkdir = Path.mkdir
    original_write = module._atomic_write

    def fail_mkdir(path, *args, **kwargs):
        if failure_point == "mkdir" and path == receiver.web / "dirac":
            raise OSError("synthetic registration failure")
        return original_mkdir(path, *args, **kwargs)

    def fail_write(path, *args, **kwargs):
        target = (
            receiver.web / "dirac" / (".hortator-owner.json" if failure_point == "marker" else ".htaccess")
        )
        if failure_point != "mkdir" and path == target:
            raise OSError("synthetic registration failure")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    monkeypatch.setattr(module, "_atomic_write", fail_write)
    with pytest.raises(OSError, match="synthetic registration failure"):
        receiver.deploy(envelope())
    assert (receiver.metadata / "bots/dirac.json").exists()
    monkeypatch.setattr(Path, "mkdir", original_mkdir)
    monkeypatch.setattr(module, "_atomic_write", original_write)
    if failure_point == "marker":
        with pytest.raises(ReceiverError, match="operator must inspect"):
            receiver.deploy(envelope())
        assert not list((receiver.web / "dirac").iterdir())
    else:
        assert receiver.deploy(envelope())["delivery_current"]
        assert git(receiver, "rev-list", "--count", "HEAD") == "1"


def test_deleted_established_namespace_cannot_be_silently_recreated(receiver):
    receiver.deploy(envelope())
    shutil.rmtree(receiver.web / "dirac")
    with pytest.raises(ReceiverError, match="operator must inspect"):
        receiver.deploy(envelope(revision=2))
    assert not (receiver.web / "dirac").exists()


def test_changed_public_pointer_and_release_symlinks_cannot_be_followed_or_overwritten(receiver, tmp_path):
    receiver.deploy(envelope())
    path = public(receiver)
    release = path.resolve()
    path.unlink()
    path.symlink_to(tmp_path / "foreign-content")
    with pytest.raises(ReceiverError, match="destination changed"):
        receiver.deploy(envelope(revision=2))
    assert os.readlink(path) == str(tmp_path / "foreign-content")
    path.unlink()
    path.symlink_to(release)
    (release / "index.html").unlink()
    secret = tmp_path / "outside-secret"
    secret.write_text("synthetic secret must never be imported")
    (release / "index.html").symlink_to(secret)
    with pytest.raises(ReceiverError, match="symlink"):
        receiver.deploy(envelope(revision=2))
    audit = receiver.audit()
    assert audit["drift_count"] == 1
    assert "symlink" in str(audit)
    assert "synthetic secret" not in str(audit)
    assert not list((receiver.history / "drift").rglob("index.html"))


def test_audit_records_content_drift_in_git_without_repair_or_repeated_commits(receiver):
    first = receiver.deploy(envelope())
    clean = receiver.audit()
    assert clean["changed"] and clean["drift_count"] == 0
    assert not receiver.audit()["changed"]
    assert git(receiver, "rev-list", "--count", "HEAD") == "2"
    asset = public(receiver) / "index.html"
    asset.write_text("manual change on remote host")
    drift = receiver.audit()
    assert drift["changed"] and drift["drift_count"] == 1
    assert "content hash changed" in str(drift)
    assert asset.read_text() == "manual change on remote host"
    captured = receiver.history / "drift" / drift["observation_hash"] / "dirac/cat-report/index.html"
    assert captured.read_text() == "manual change on remote host"
    assert git(receiver, "show", first["remote_commit"] + ":sites/dirac/cat-report/index.html").startswith(
        "<link"
    )
    assert not receiver.audit()["changed"]
    assert git(receiver, "rev-list", "--count", "HEAD") == "3"
    with pytest.raises(ReceiverError, match="drift"):
        receiver.deploy(envelope(revision=2))
    assert asset.read_text() == "manual change on remote host"


def test_audit_does_not_restore_or_adopt_foreign_namespace_metadata(receiver):
    receiver.deploy(envelope())
    marker = receiver.web / "dirac/.hortator-owner.json"
    marker.write_text('{"bot_id":"another-owner"}')
    report = receiver.audit()
    assert report["drift_count"] == 1
    assert marker.read_text() == '{"bot_id":"another-owner"}'
    assert "foreign ownership" in str(report)


def test_receiver_is_standalone_and_uses_bounded_json_protocol(receiver, tmp_path):
    import hortator.publishing_receiver as module

    installed = tmp_path / "standalone.py"
    installed.write_text(Path(module.__file__).read_text())
    command = [
        sys.executable,
        "-I",
        str(installed),
        "--web-root",
        str(receiver.web),
        "--state-root",
        str(receiver.state),
    ]
    result = subprocess.run(
        command + ["deploy"],
        input=json.dumps(envelope()),
        text=True,
        capture_output=True,
        timeout=30,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["delivered"]
    status = subprocess.run(
        command + ["status"],
        input=json.dumps(
            {
                "version": 1,
                "bot_id": "dirac",
                "slug": "cat-report",
            }
        ),
        text=True,
        capture_output=True,
        timeout=30,
        cwd=tmp_path,
    )
    assert json.loads(status.stdout)["delivery_current"]
    audit = subprocess.run(
        command + ["audit"],
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        timeout=30,
        cwd=tmp_path,
    )
    assert json.loads(audit.stdout)["drift_count"] == 0
    bad = subprocess.run(
        command + ["deploy"],
        input='{"version":1,"version":2}',
        text=True,
        capture_output=True,
        timeout=30,
        cwd=tmp_path,
    )
    assert bad.returncode == 1
    assert "Duplicate JSON" in json.loads(bad.stdout)["error"]
    assert "Traceback" not in bad.stdout + bad.stderr


def test_state_and_web_roots_must_not_be_nested_or_symlinked(receiver, tmp_path):
    with pytest.raises(ReceiverError, match="separate directories"):
        Receiver(receiver.web, receiver.web / "private-state")
    linked = tmp_path / "linked-web"
    linked.symlink_to(receiver.web)
    with pytest.raises(ReceiverError, match="symbolic links"):
        Receiver(linked, receiver.state)


def test_configuring_state_for_another_web_root_cannot_transfer_namespace_ownership(receiver, tmp_path):
    receiver.deploy(envelope())
    second_web = tmp_path / "other-web"
    second_web.mkdir()
    other = Receiver(second_web, receiver.state)
    with pytest.raises(ReceiverError, match="another web root"):
        other.deploy(envelope())
    assert not list(second_web.iterdir())
