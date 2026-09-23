# Verification entry template

Use this block at the top of each dated entry in `VERIFICATION.md`. Replace placeholders with measured evidence; retain failed, skipped and unrun checks. A focused selection is never a full-suite pass.

```text
Commit: <full revision>; dirty at start/end: <true/false>/<true/false>
Backend scope: <full collection | focused: exact paths/filter | not run>
Backend result: <passed/failed/error/skipped counts>; exit <code>; duration <seconds>
Frontend format/build: <passed/failed/not run>; exits <codes>; durations <seconds>
Browser tests: <passed/failed/not run>; scope <selection>; duration <seconds>
Evidence: <local artifact path or Actions run/artifact link>
Classification: <mock fixtures / local integration / live checks, explicitly distinguished>
Live checks: <none | exact services and actions>
Limitations: <skips with reasons, incomplete runs, untested environments>
```

`./scripts/check.sh` retains the full local sequence, including Playwright. Install Chromium separately with `cd web && npx playwright install chromium` when needed. Playwright starts an isolated seeded test server on port 18000; it never reuses the shared production server. `./scripts/check.sh --suite ci` runs the full backend collection, Ruff lint/format including scripts, and locked frontend install/format/build, without browser tests. Both stop at the first failure and return its nonzero exit status. When piping output, use `set -o pipefail` so `tee` cannot hide failure.

The ignored `test-results/verification/result.json` records source revision and dirty state at start/end, classification, command argument arrays, real child exit codes, durations, test counts and skipped IDs. Negative child return codes identify signals; the runner returns the conventional `128 + signal`. Remaining commands are `not_run`, never passed. Each invocation replaces these local files; archive required metadata outside Git before the next run. Abrupt termination may leave status `running`, which is incomplete evidence. JUnit details remain in `test-results/verification/pytest.xml`; inspect these and `pytest -ra` output for skip reasons. CI uploads only metadata JSON, not JUnit assertion output, logs, environments, credentials, runtime databases, screenshots or traces.

Full backend collection includes mocked adapters and real local integration. Namespace tests may skip when host isolation is unavailable; the public package download test is opt-in. A green CI job with these skips does not validate the production sandbox, Discord or provider behavior. Dedicated sandbox validation can set `HORTATOR_REQUIRE_SANDBOX=1` on a prepared host. Do not set this blindly on a hosted runner.

The workflow check is named **Backend and frontend**. After the workflow is pushed and has run, a repository administrator must select that exact check in the main branch's required status checks/ruleset, with no bypass that defeats the intended gate. Adding workflow YAML alone does not enforce merging. This change neither pushes the workflow nor changes repository settings. Setup failures before the verification command starts have no result JSON; the failed Actions step remains authoritative.

References: [uv GitHub Actions setup](https://docs.astral.sh/uv/guides/integration/github/), [GitHub protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches), [artifact behavior](https://github.com/actions/upload-artifact).
