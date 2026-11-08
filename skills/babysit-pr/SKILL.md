---
name: babysit-pr
description: Triage this project's PR checks and feedback with Haiku and Luna, using one T3-owned watch. Complements the Opus/Sol/Wonder Full technical council.
---

# Project PR babysitting

Adapted from [t3dotgg/tstack babysit-pr](https://github.com/t3dotgg/tstack/blob/main/skills/babysit-pr/SKILL.md)
under the owner's 2026-11-08 decision. Read [AGENTS.md](../../AGENTS.md).
This repository uses T3 Code; the upstream standalone `pr-wait` fallback and its
automatic rebase advice do not apply. Never reset, rebase, force-push or merge
local task branches to resolve base changes. Follow the repository's linear
history rules and report an actual branch-transition blocker.

## Assignments

Resolve the live T3 catalog and use `delegate_task` with explicit options:

- **Claude Haiku**, currently `claudeAgent` / `claude-haiku-5-5`, `effort: high`.
- **GPT Luna**, currently `codex` / `gpt-6-luna`, `reasoningEffort: high` and
  `serviceTier: priority`.

Report unavailable selections; do not silently substitute. These agents check
current-head CI results, logs, review threads and actionable feedback. They are
not substitutes for the [technical council](../council/SKILL.md). Assign distinct
triage scopes to avoid duplicate work; both return measured evidence and pending
state. The parent owns edits unless it explicitly assigns a fix to one writer.

## One watch, owned by the parent

Link every PR created, taken over or pushed to with `link_pull_request` immediately
after that operation. Read current head/base/checks and existing comments first.
Resolve already-known issues before waiting. When checks or reviews remain pending,
the **parent** calls `watch_pull_request` and ends its turn. Follow T3's wake and
rewatch instructions. A wake is new evidence, not permission to bypass merge gates.

Delegated Haiku/Luna tasks take a bounded snapshot, investigate actionable failures
and return. They **never watch or poll PRs**, run `gh --watch`, use `pr-wait`, start
sleep loops or create recurring tasks. Do not claim a finished child is monitoring.
New evidence needing another triage pass gets a fresh `delegate_task` with the
original scope and previous findings/dispositions. Cancel superseded tasks.

On wakes, validate findings against the current source. Keep unresolved older
findings open until verified fixed; an older comment is not obsolete merely
because a newer commit exists. Distinguish code failures from infrastructure
failures before rerunning checks. Current-head checks include all check runs and
commit statuses, not only one workflow or the most convenient green result.
Use REST endpoints if GraphQL is rate-limited; inspect rate-limit reset evidence
and avoid tight retry loops. Do not bypass required checks.

## Resolve and finish

Fix confirmed in-scope issues and push ordinary task-branch commits. Explain why
an investigated finding is dismissed; use attributed replies under the owner's
standing authorization, not invented human approval. Update an existing comment
only by its recorded ID; shared GitHub credentials make `--edit-last` unsafe.
Read any repository-specific review-bot rules before addressing their findings.
Keep unrelated improvements as follow-ups. No repeated green test runs without
new changes or unresolved evidence. Council scores belong in the PR description.

Before readiness, refresh the exact head, required checks, review threads and
base. Investigate a changed base and overlapping changes; do not certify stale
test/review evidence or merge conflicts. Every new head requires council delta
review. The parent verifies the gate and uses GitHub **Squash and merge** under
standing authorization, unless the owner explicitly reserves that action.

At a user handoff or investigated blocker, call `unwatch_pull_request` for any
active parent watch, then report actual status. Never post repetitive waiting
messages. A child ends with:
`PR <full URL>, head <SHA>, checks and reviews <state>`.
