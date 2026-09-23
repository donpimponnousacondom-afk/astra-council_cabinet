"""Regression checks for verification exit status and evidence, without service access."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

from verify import commands, pytest_counts, run_checks


class VerificationTests(unittest.TestCase):
    def test_real_child_failure_is_preserved_and_remaining_checks_not_run(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            evidence = {"commands": []}
            code = run_checks(
                [
                    ("success", [sys.executable, "-c", "pass"]),
                    ("failure", [sys.executable, "-c", "raise SystemExit(7)"]),
                    ("not-run", [sys.executable, "-c", "raise SystemExit(99)"]),
                ],
                evidence,
                output,
            )
            saved = json.loads(output.read_text())
            self.assertEqual(code, 7)
            self.assertEqual(saved["exit_code"], 7)
            self.assertEqual([row["exit_code"] for row in saved["commands"]], [0, 7, None])
            self.assertEqual(saved["commands"][-1]["status"], "not_run")
            self.assertGreaterEqual(saved["commands"][1]["duration_seconds"], 0)

    def test_missing_command_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = {"commands": []}
            self.assertEqual(
                run_checks([("missing", [directory + "/absent"])], evidence, Path(directory) / "result.json"),
                127,
            )
            self.assertTrue(evidence["commands"][0]["launch_error"])

    def test_full_default_keeps_browser_and_ci_collects_entire_backend(self):
        for suite in ("full", "ci"):
            checks = dict(commands(suite, Path("results.xml")))
            self.assertEqual("playwright" in checks, suite == "full")
            self.assertEqual(
                checks["pytest"], ["uv", "run", "--frozen", "pytest", "-q", "-ra", "--junitxml=results.xml"]
            )
            self.assertIn("scripts", checks["lint"])

    def test_counts_include_skips_without_copying_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.xml"
            path.write_text(
                '<testsuites><testsuite tests="3" failures="1" errors="0" skipped="1">'
                '<testcase classname="test_shell" name="test_os"><skipped message="private"/></testcase>'
                "</testsuite></testsuites>"
            )
            counts = pytest_counts(path)
            self.assertEqual(counts["passed"], 1)
            self.assertEqual(counts["skipped_tests"], ["test_shell.test_os"])
            self.assertNotIn("private", json.dumps(counts))


if __name__ == "__main__":
    unittest.main()
