"""Fixed repository checks with metadata-only evidence and failure propagation."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def source_state():
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()

    return {
        "revision": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain", "--untracked-files=normal")),
    }


def commands(suite, junit):
    checks = [
        ("dependencies", ["uv", "sync", "--frozen", "--python", "3.14"]),
        ("lint", ["uv", "run", "--frozen", "ruff", "check", "hortator", "tests", "scripts"]),
        (
            "format",
            ["uv", "run", "--frozen", "ruff", "format", "--check", "--quiet", "hortator", "tests", "scripts"],
        ),
        ("evidence-tests", ["uv", "run", "--frozen", "python", "scripts/test_verify.py"]),
        ("pytest", ["uv", "run", "--frozen", "pytest", "-q", "-ra", f"--junitxml={junit}"]),
        ("frontend-dependencies", ["npm", "ci", "--prefix", "web"]),
        ("frontend-format", ["npm", "run", "format:check", "--prefix", "web"]),
        ("frontend-build", ["npm", "run", "build", "--prefix", "web"]),
    ]
    if suite == "full":
        checks.append(("playwright", ["npm", "test", "--prefix", "web"]))
    return checks


def pytest_counts(path):
    if not path.exists():
        return None
    root = ET.parse(path).getroot()
    suites = list(root.iter("testsuite"))
    counts = {
        key: sum(int(s.get(key, 0)) for s in suites) for key in ("tests", "failures", "errors", "skipped")
    }
    counts["passed"] = counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"]
    # Only identities are copied; assertion output and skip messages remain local in JUnit.
    counts["skipped_tests"] = [
        f"{case.get('classname')}.{case.get('name')}"
        for case in root.iter("testcase")
        if case.find("skipped") is not None
    ]
    return counts


def run_checks(checks, evidence, output):
    exit_code = 0
    for name, command in checks:
        row = {
            "name": name,
            "command": command,
            "status": "not_run",
            "exit_code": None,
            "duration_seconds": None,
        }
        evidence["commands"].append(row)
        if not exit_code:
            print("+ " + " ".join(command), flush=True)
            started = time.monotonic()
            try:
                code = subprocess.run(command, cwd=ROOT, check=False).returncode
            except OSError:
                code = 127
                row["launch_error"] = True
            except KeyboardInterrupt:
                code = 130
            row.update(exit_code=code, duration_seconds=round(time.monotonic() - started, 3))
            row["status"] = "passed" if code == 0 else "failed"
            exit_code = code if code >= 0 else 128 - code
        evidence["exit_code"] = exit_code
        evidence["status"] = "failed" if exit_code else "running"
        output.write_text(json.dumps(evidence, indent=2) + "\n")
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("full", "ci"), default="full")
    args = parser.parse_args()
    evidence_dir = ROOT / "test-results" / "verification"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    output = evidence_dir / "result.json"
    junit = evidence_dir / "pytest.xml"
    junit.unlink(missing_ok=True)
    evidence = {
        "schema_version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source": source_state(),
        "suite": args.suite,
        "backend_scope": "full",
        "classification": "automated fixtures and local integration; no live Discord/provider checks",
        "live_checks": [],
        "playwright": "included" if args.suite == "full" else "not_run",
        "commands": [],
    }
    started = time.monotonic()
    code = run_checks(commands(args.suite, junit.relative_to(ROOT)), evidence, output)
    evidence.update(
        status="passed" if code == 0 else "failed",
        duration_seconds=round(time.monotonic() - started, 3),
        finished_at=datetime.now(timezone.utc).isoformat(),
        source_after=source_state(),
        pytest=pytest_counts(junit),
    )
    output.write_text(json.dumps(evidence, indent=2) + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a") as stream:
            stream.write(f"Verification `{args.suite}`: **{evidence['status']}** (exit {code}).\n\n")
            stream.write(
                "Full backend collection; local fixtures/integration only. No live Discord/provider checks.\n\n"
            )
            stream.write(
                "See result.json for per-command exits, durations, revision, dirty state and skipped test IDs.\n"
            )
    print(f"Evidence: {output.relative_to(ROOT)}; exit {code}", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
