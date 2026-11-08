---
name: council
description: Run this project's three-provider technical council through T3 Code, for requested investigations and required open-PR reviews. Requires T3 delegation; not a global Codex or Claude Code workflow.
---

# Project council

Adapted from [t3dotgg/tstack council](https://github.com/t3dotgg/tstack/blob/main/skills/council/SKILL.md)
under the owner's 2026-11-08 decision. This file is the shared entrypoint for all
three providers. Keep it in this repository; do not install a global copy.
Read [AGENTS.md](../../AGENTS.md) and the relevant feature contracts.

## Members and transport

Use T3 `orchestrator_capabilities` to resolve the live catalog and option names,
then `delegate_task` for each member. Honor the harness's capability refresh rules.
The current configured selections are:

| Member | Provider instance | Exact model | Options |
| --- | --- | --- | --- |
| Claude Opus | `claudeAgent` | `claude-opus-5-5` | `effort: high` |
| Latest GPT Sol | `codex` | `gpt-6.1-sol` | `reasoningEffort: high` |
| Muse Wonder Full | `muse` | **`muse-wonder-full-1.0`** | `reasoningEffort: high` |

Wonder Full is mandatory: **`muse-wonder-1.0` is a different model and is not a
substitute**. The owner permits the latest confirmed Sol 6.x successor. Do not
silently replace another member, model, provider or requested option. Report an
unavailable capability. Put settings in `target.options`, not merely the prompt;
verify the returned provider/model/options. Do not use native same-provider agents,
top-level threads or shell-launched models to stand in for this council.
Retain the caller's configured service tier; this skill does not force a tier.

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
   is sufficient for read-only review; one parent owns edits. Pin executable probes
   to the reviewed source. Delegate tools/paid provider experiments only when the
   brief explicitly authorizes them. Do not let reviewers repeat live experiments.
4. Ask for actionable findings with trigger, consequence and measured evidence;
   separate introduced bugs, existing weaknesses and hypotheses. Request a score
   out of 10 with its rationale, unresolved blockers and validation limits. For
   experiments also ask which result would falsify the proposed explanation.
5. Validate findings yourself. Run narrow tests of suggested changes, then return
   successes, failures, sample sizes and objections to **all three** members.
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

## PR evidence and handoff

Put the council round/score table and concise disposition of findings in the PR
description, before the model/harness attribution. Do not post separate council
score comments. Label pre-PR research separately from current-head PR review.
Each pushed head needs council delta review; old scores remain historical.
The parent alone edits, pushes, links the PR to T3 and performs the authorized
GitHub squash merge after current-head review and required checks pass.
Pin the reviewed full head SHA in the merge request and verify the returned merge
result and commit. Missing, pending, cancelled or failed required checks do not pass.

[Babysitting](../babysit-pr/SKILL.md) uses Haiku and Luna for check/comment triage;
it does not replace the technical council. Children never watch or merge PRs.
Their final PR-work line is `PR <full URL>, head <SHA>, checks and reviews <state>`.

The initial tool-call trial authorized Cheapseek inference replays of Sun/Zelda
failures and measured offline variants. After three rounds and the owner's
2026-11-08 “Go for loki”, the two-case, 40-call Loki inference benchmark is also
authorized. Follow its predeclared plan and assess task relevance independently
of syntax. The experiments alone did not authorize live rollout. After their
results, the owner explicitly authorized the shared final-message shell
convention for all eligible bots/providers, with monitoring and no bespoke
Loki/Gemini behavior. That narrow change is authorized; unrelated prompt tuning
remains frozen. See [PROMPTS](../../docs/PROMPTS.md#shared-shell-call-convention).
For this trial, report steps, measured results and council feedback inline in the
T3 Code chat using `html_preview` then `html_render`. The owner explicitly wants
the visible chat view, not an HTML file. Refresh it when feedback or phase results
arrive, clearly distinguishing live progress, research scores and final PR review.
