---
name: council
description: Run this project's four-provider technical council through T3 Code, for requested investigations and required open-PR reviews. Requires T3 delegation; not a global Codex or Claude Code workflow.
---

# Project council

Adapted from [t3dotgg/tstack council](https://github.com/t3dotgg/tstack/blob/main/skills/council/SKILL.md)
under the owner's 2026-11-08 decision. This file is the shared entrypoint for all
four providers. Keep it in this repository; do not install a global copy.
Read [AGENTS.md](../../AGENTS.md) and the relevant feature contracts.

## Project ownership and precedence

This repository's `skills/council/SKILL.md` is the authoritative council workflow
for Astra Council Cabinet. Use it together with `AGENTS.md` and the project-local
[babysit-pr](../babysit-pr/SKILL.md). A discovered global `council` skill, including
one reached through `~/.codex/skills/council` or `~/.claude/skills/council`, does not
replace or supplement these project rules automatically.

Do not overwrite, synchronize, regenerate or replace this file with a global
skill, and do not turn this directory into a symlink to the global version.
Updating the global skill does not authorize updating this project. Do not
retarget global discovery symlinks to this repository as a workaround. Bring
selected improvements here only through an owner-authorized project change,
preserving project contracts and the normal PR workflow. Leave global files and
symlinks unchanged during project skill maintenance unless separately requested.
If this file is missing or unreadable, report the problem; do not fall back to the
global council or reconstruct it from the global copy.

## Members and transport

Use T3 `orchestrator_capabilities` to resolve the live catalog and option names,
then `delegate_task` for each member. Honor the harness's capability refresh rules.
The current configured selections are:

| Member | Provider instance | Exact model | Options |
| --- | --- | --- | --- |
| Claude Opus | `claudeAgent` | `claude-opus-5-5` | `effort: high` |
| Latest GPT Sol | `codex` | `gpt-6.2-sol` | `reasoningEffort: high` |
| Muse Wonder Full | `muse` | **`muse-wonder-full-1.0`** | `reasoningEffort: high` |
| Gemini Neon | `antigravity` | `gemini-4-neon` | `thought_level: high` |

Wonder Full is mandatory: **`muse-wonder-1.0` is a different model and is not a
substitute**. Contributor variants and the OpenCode/CLIProxyAPI routes are not
substitutes for these exact targets either; Neon means the Antigravity model,
not its orchestrator variant or Loki's runtime provider. The owner permits the
latest confirmed Sol 6.x successor. Do not
silently replace another member, model, provider or requested option. Report an
unavailable capability to the owner for resolution; do not silently omit a
member or bypass the review gate. Put settings in `target.options`, not merely the prompt;
verify the returned provider/model/options. Do not use native same-provider agents,
top-level threads or shell-launched models to stand in for this council.
Retain the caller's configured service tier; this skill does not force a tier.
If T3 delegation is unavailable, report the blocker; do not use the global skill
or another transport to bypass it.

## Round workflow

1. Fix already-confirmed in-scope problems and run relevant checks before requesting
   implementation review. Research rounds may precede a patch, but are labelled
   design/experiment review and never substitute for reviewing the actual open PR.
2. Supply each member a self-contained brief: user intent, scope, source revision,
   evidence paths, uncertainties, exclusions and the requested decision. Children
   receive no implicit parent history. For PR review include the URL and full base
   and head SHA; independently inspect the actual open GitHub diff. Keep initial
   reviews independent, including avoiding other members' reports and PR comments
   until each initial assessment is written.
3. Start one read-only delegate per member, normally in parallel. Shared checkout
   is sufficient for read-only review; one parent owns edits. Use `role: "review"`
   for PR review and `mode: "async"`. Keep the shared reviewed source stable while
   delegates read it: no tracked edits, checkouts or pushes during that round.
   If a correction must supersede a round, cancel its delegates before editing.
   Pin executable probes to the reviewed source. Delegate tools/paid provider
   experiments only when the brief explicitly authorizes them. Do not let
   reviewers repeat live experiments.
4. Ask for actionable findings with trigger, consequence and measured evidence;
   separate introduced bugs, existing weaknesses and hypotheses. Request a score
   out of 10 with its rationale, unresolved blockers and validation limits. Use
   the brief below for implementation review. For experiments also ask which result would falsify the proposed explanation.
5. Validate findings yourself. Run narrow tests of suggested changes, then return
   successes, failures, sample sizes and objections to **all four** members.
   Start a new `delegate_task` for each subsequent round, including the original
   brief and accumulated findings/responses. Keep a new stable `clientRequestId`
   and returned `taskId` per round. Never continue a delegate via `t3_thread_send`.
6. Follow a delegate's `next` instruction. Do not poll or keep an external wait
   loop. Cancel replaced/unneeded tasks. A delegated council member does its own
   review and starts no children unless the assigned task explicitly requests it.

Seek agreement on the scoped result, not an inflated score. Record every score
and round, including objections and failures; use `no score` when omitted. High
scores do not override failing checks or unresolved defects. Do not widen scope
to win approval. A stalled disagreement goes to the owner with concrete evidence.

## Implementation review brief

Give every member the same factual brief, without the parent's verdict or sibling
reports in the initial round. Fill in the task-specific fields below. Read-only
isolated probes may create scratch artifacts; they must not change the reviewed
source or touch the live runtime. Tests themselves remain reviewable for
correctness, relevance and side effects.

> Independently review the open PR. Repository: [absolute path]. PR: [URL].
> Base: [full SHA]. Head: [full SHA]. Read `AGENTS.md`, this project skill, the
> relevant feature contracts, the actual open GitHub diff and callers of changed
> code. Use pinned source (`git diff <base>...<head>`, `git show <head>:<path>`) and
> confirm that the PR head matches before reporting. Inspect base movement
> separately for integration conflicts; do not treat unrelated base changes as
> changes introduced by the PR.
> Write your initial assessment before reading sibling reports, PR comments or
> the PR description's scores/findings.
>
> Intent and acceptance: [requirements]. Non-goals: [exclusions]. Deployment
> assumptions: [relevant runtime facts]. Evidence: [commands, results, paths and
> limitations]. Allowed probes: [isolated checks and permitted evidence access].
> Stay read-only: no tracked edits, commits, checkout changes, pushes, merges, runtime
> actions or GitHub comments. No live/provider experiments or private-state
> access beyond what this brief explicitly authorizes. Start no agents.
>
> Trace changed behavior and failure paths, including ordering, configuration
> sent on the wire, data correctness, permissions and state isolation. Neither
> green CI nor the parent's summary proves correctness. Review the full scoped
> change in this pass; there is no finding quota and a clean review is valid.
>
> For each blocking finding, identify a reachable trigger in this deployment,
> the consequence, file/line and evidence, and the acceptance requirement,
> project contract, permission/isolation boundary or previously working behavior
> it breaks. Explain how the diff introduces or exposes the defect. Separate
> pre-existing issues, optional polish and hypotheses from introduced defects.
> Do not expand the PR to unrelated improvements to obtain a higher score.
>
> Return the full reviewed SHA, findings or “no actionable findings”, unresolved
> blockers, residual risks, validation performed and its limits, a score out of
> 10 with rationale, and an implementation verdict: APPROVE or BLOCKED. Report
> CI/review state separately; implementation approval is not merge readiness.
> End with: `PR <full URL>, head <SHA>, checks and reviews <state>`.

Do not blanket-exclude latency, races, filesystem failures or malformed persisted
data: assess their relevance to the changed behavior and project contracts.
Research rounds use the research workflow above, with hypotheses, falsifiers and
experimental limits instead of an implementation approval.

## Findings across rounds

Maintain a concise finding record in the PR description: ID, reviewer, first
round/head, affected code, evidence and disposition (fixed, rejected with reason,
nonblocking follow-up, or unresolved blocker). Send the accumulated record and
the full brief to all four reviewers in subsequent rounds.

For every reviewer, distinguish newly changed code from code already reviewed.
A late finding must meet the same evidence and scope requirements; earlier silence
does not invalidate a real defect. Ask for a complete scoped pass rather than a
new assortment of speculative edge cases each round. Do not fix out-of-scope
items just to obtain approval. Preserve objections and escalate an evidence-backed
stalemate to the owner, as above; a majority vote cannot waive a valid blocker.

## PR evidence and handoff

Put the council round/score table and concise disposition of findings in the PR
description, before the model/harness attribution. Do not post separate council
score comments. Label pre-PR research separately from current-head PR review.
Each pushed head needs council delta review; old scores remain historical.
The parent alone edits, pushes, links the PR to T3 and performs the authorized
GitHub squash merge after completed current-head review by all four members, no
unresolved valid blockers and passing required checks. Record evidence-based
dispositions; a stalled reviewer disagreement goes to the owner before merging. Preserve explicit owner holds and standing
merge authorization; this skill adds no per-merge permission request.
Pin the reviewed full head SHA in the merge request and verify the returned merge
result and commit. Missing, pending, cancelled or failed required checks do not pass.

[Babysitting](../babysit-pr/SKILL.md) uses Haiku and Luna for check/comment triage;
it does not replace the technical council. Children never watch or merge PRs.
Their final PR-work line is `PR <full URL>, head <SHA>, checks and reviews <state>`.

## Research history and prompt limits

The completed November 2026 Cheapseek/Loki trial and its inline-HTML reporting
instructions are recorded in [PLAN](../../docs/PLAN.md#2026-11-08--t3-council-trial-and-cheapseek-tool-call-experiments)
and [tool-call evaluation](../../docs/TOOL_CALL_EVALUATION.md). Those dated budgets
are not renewed by invoking this skill. Research reviews and new provider
experiments require the scope authorized for the current task; reviews must not
repeat paid experiments implicitly.

The owner authorized the measured shared final-message shell convention for all
eligible bots/providers, with monitoring and no bespoke Loki/Gemini behavior.
Unrelated prompt tuning remains frozen. See the current
[PROMPTS contract](../../docs/PROMPTS.md#shared-shell-call-convention) and `AGENTS.md`.
