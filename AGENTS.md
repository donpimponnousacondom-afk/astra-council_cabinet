# Council project continuity

## Repository boundary and branch policy

- The sole working repository is `/home/codexy/codex/astra-council_cabinet`. Its top level is the application root; there is no nested `council/` directory here.
- The user explicitly retired the previous workspace for this project. Do not run Git, edit files, restore files, synchronize changes, or clean up in `/home/codexy/codex/t3-code`. Do not use that repository as a worktree or source of truth.
- Inspect actual Git state and continue the user's active task branch. The user uses GitHub **Squash and merge**. After the prior PR is merged, require a clean working tree and prepare a new `feat/<name>`, `hotfix/<name>` or `dev/<name>` branch from verified, fetched `origin/main`, following the single-restart procedure below. Fast-forward local main at a scheduled stop; do not restart unchanged code just to walk through main. Use the user's specified base/name if provided. Never put new implementation commits on main. The original `dev/initial_phase` baseline is historical. Verify `git rev-parse --show-toplevel`, branch and working tree before Git mutations; preserve existing user changes. If the prior task is unmerged or main has diverged, stop that branch transition rather than merging/rebasing automatically or dropping work.
- Keep new work linear: ordinary commits on the task branch, no merge commits or local merges between branches. The agent owns task-branch publication, independent Luna/Sol PR review and check coordination, and GitHub **Squash and merge** after the merge gates pass. This has standing owner authorization; do not stop at merge readiness or ask again for ordinary merges unless the owner explicitly pauses or reserves a particular merge. Do not rewrite historical commits or force-push. Do not infer the current branch from a dated verification/design entry. Delete local task branches only when requested and after checking for unmerged work (a squash merge may require tree/PR evidence). Extra hook automation is deferred.
- The user deliberately reuses the local placeholder `feat/next_feature` after a merged PR, then renames it before publishing. The installed `hortator-next-feature` shell command automates verified merge checks, shared-Screen stop, fast-forward main update, placeholder preparation, locked dependency installation, dashboard build and foreground restart. It may archive an older verified-merged placeholder under `archive/next_feature-...` before recreating it; it must never discard unmerged commits or reset branches. Its `--refresh` mode rebuilds/restarts the clean current task branch without a Git transition. Run from a shell prompt, not the active console; see OPERATIONS. The helper itself does not publish; the agent must complete the publication workflow before deploying a completed change.
- Owner decision, 2026-10-22: normally use one runtime stop/start cycle per feature or hotfix, preferably after the final reviewed changes are ready. Do not restart unchanged code at task preparation or merely to replace a deployed PR head with its identical squash-merge tree. With a clean tree, verified merged prior PR, and identical running/checkout/fetched-main source trees, create the next task branch directly from `origin/main` without restarting; defer local main fast-forward until the next planned stop. If checkout-time code or worker/server compatibility requires stopping before edits, keep that stop and the final start within one cycle rather than starting an intermediate build. The existing `hortator-next-feature` helper still performs a full restart; do not invoke its normal mode automatically at task start. If another restart is genuinely needed for a correction, explain why. Preserve startup-captured version identity and report committed-but-not-loaded changes explicitly. See [OPERATIONS](docs/OPERATIONS.md#publication-and-local-development-deployment).
- Owner decision, 2026-10-12: every completed change must be committed on its task branch, pushed, and included in an open GitHub PR targeting `main`. This is standing authorization for task-branch pushes, PR creation, routine review/CI fixes, review comments and GitHub squash merges after both reviews and required checks pass; do not ask separately for each push or merge. Do not leave a deployed backlog of unpublished changes. Local development deployment may proceed once its PR exists and appropriate local checks pass, while GitHub CI and the independent reviews continue. Pending remote checks/reviews do not establish merge readiness.
- **NEVER PUSH DIRECTLY TO MAIN.** Do not make local implementation commits or local merges on `main`, create merge commits, rewrite history or force-push. GitHub PR **Squash and merge** into `main` after the required reviews/checks is the authorized completion path, not a prohibited direct push. The local `core.hooksPath=.githooks` setting installs an additional pre-push guard for `refs/heads/main`; do not bypass it. On a new clone, install it with `git config core.hooksPath .githooks`.
- Narrow recovery exception, authorized 2026-10-12: publish the eleven unpublished commits following PR #94 as eleven ordered single-commit PRs, followed by the protocol PR, using GitHub's native stack dependencies. The owner subsequently authorized combining the faulty intermediate PRs #95, #97 and #99 into their corrected successors #96, #98 and #100, and closing the intermediate PRs as superseded without merging. Preserve their original commits and review evidence; review the combined successor diffs. The bottom PR targets `main`; each higher PR initially targets the preceding PR's branch and ultimately reaches `main` through bottom-up GitHub squash merges performed by the agent. This exception permits the recovery stack to be published while lower PRs remain unmerged. Preserve the original local history: no local rebase or force-push. GitHub may restack unmerged PR heads after a lower squash merge; those changed heads require renewed reviews/checks. This does not authorize rewriting `main` or historical merged commits, or general stacking of future tasks.
- Never stage runtime data, credentials, logs, caches, dependencies, build output or test screenshots. The migration commit removes previously tracked copies from the current tree while preserving local files. Earlier commits still contain those copies; do not rewrite history or rotate credentials without a separately scoped task.
- Owner decision, 2026-10-08: do not create backups for each feature, hotfix or ordinary deployment. Full backups and Storage Box offloads run only on explicit request. On requested cleanup, retain one newest verified usable recovery copy locally, archive completed copies remotely, and remove older local copies only after source comparison and remote checksum verification. Follow [backup and archival operations](docs/OPERATIONS.md#backup-and-restore); this supersedes older generic pre-deployment backup instructions. The dashboard's existing recovery capture during an explicitly requested restore is unchanged.
- Owner relocation decision, 2026-10-18: `/opt/remote-backup` is the existing Storage Box SSHFS mount (formerly viewed as `asdfg`). Move all Hortator backup copies there when the task finishes, verifying remote checksums and source equivalence before local removal. This explicit relocation supersedes retaining one complete local recovery copy for this operation. Preserve the latest-requested pointer as a reference to the verified remote archive; no automatic new backup, other-project cleanup, or live-data relocation is authorized. See OPERATIONS for the archive layout and restoration procedure.

- Check scratch space before full verification. Pytest removes successful temporary fixtures by default and preserves failed ones for diagnosis. At task completion, remove confirmed, inactive project-owned browser/test/build scratch; archive useful diagnostics in the ignored audit folder or external data storage first. Inspect process use before cleanup, preserve current runs, and never blanket-delete `/tmp` or another application's files. See the temporary-file procedure in OPERATIONS.

- After publishing each completed change, spawn both independent PR-review
  sub-agents: **gpt-6-luna at max reasoning** and **gpt-6.1-sol at high reasoning**.
  A latest confirmed available successor within each family is allowed; do not
  substitute the older `gpt-6-sol`. Both must independently inspect the actual
  **open GitHub PR** diff, monitor all Actions/checks for its current head, and
  post their own PR comments with measured evidence. A local-commit review is
  preparation only and never substitutes for either required PR review. Keep
  historical local reviews honestly labelled as dated local evidence. Follow the local
  [council-pr-babysitter skill](/home/codexy/.codex/skills/council-pr-babysitter/SKILL.md)
  for reviewer settings, independent A/B briefs, evidence and escalation rules, and
  [PR review procedure](docs/OPERATIONS.md#independent-pr-review-and-check-monitoring).
  The parent validates findings and coordinates fixes; either reviewer may be
  delegated a fix. Keep one writer per affected branch to prevent checkout races.
  Resolve ordinary CI problems under standing authorization; escalate genuine
  owner decisions or investigated blockers. Every pushed head requires delta
  re-review and its own check evidence. After both reviews, resolved blocking
  findings and passing required checks, the parent squash-merges through GitHub
  and verifies the merge result without another permission request. Never bypass
  checks. Improve the skill from demonstrated review
  problems. This PR-specific protocol does not restrict useful sub-agent work
  elsewhere; delegation and parallel agent work are welcome within task scope.

- Owner decision, 2026-10-18: proactively recommend external audits for changes
  with substantial stability impact, including scheduling, cancellation, shell
  execution and state lifetime. When the auditor is outside this harness, the
  owner relays the exchange. Treat it as a back-and-forth: provide focused
  questions and a pinned change digest, validate the findings, and return fixes
  and measured evidence for another pass when useful. Ask again when the changes
  or remaining uncertainties warrant it; do not leave follow-up solely to the
  owner's memory. A quiet stretch of bot activity is not proof that hidden
  failures are absent. These audits supplement the required independent PR
  reviews and CI; preserve any explicit owner hold. See the
  [external audit procedure](docs/OPERATIONS.md#owner-relayed-external-audits).

- Owner decision, 2026-10-18: prompt tuning is frozen. Do not implement or roll
  out the reviewed shell/tool prompt recommendations until the owner reopens
  that work. When the owner reports a concrete behavior problem, consider the
  effective prompt alongside tool receipts and runtime code: distinguish model
  behavior, misleading guidance, new regressions and pre-existing bugs before
  proposing a change. Raise prompt tuning when evidence makes it relevant;
  discussing a possible cause does not lift the hold. The ongoing external shell
  audit remains separate. See [the deferred decision](docs/PLAN.md#2026-10-18--prompt-tuning-deferred).

## Read before continuing

Read `README.md` and `docs/OPERATIONS.md`. Consult `docs/PLAN.md` for the researched design, `docs/VERIFICATION.md` for actual test evidence, and `docs/API.md` / `docs/PLUGINS.md` for the control and extension contracts. Continue the existing implementation; do not replace it with a new scaffold or assume this is still a planning exercise.

Record new user preferences and operating decisions as part of the task, not just in chat. Update this file for standing agent rules, `docs/PLAN.md` for product decisions, `docs/OPERATIONS.md` for procedures and storage/backup locations, and `docs/VERIFICATION.md` for dated evidence. Distinguish implemented behavior, proposed follow-ups, mock tests and live checks. Never put secrets in these documents.

Do not create rolling session handover documents or continuation prompts: the user removed them because copied branch/runtime snapshots become obsolete and confuse future agents. Inspect Git, Screen, the API's startup-captured version and the backup directory's `.latest-requested` pointer for current state; keep durable instructions in the documents above.

## Security-policy decisions belong to the owner

Owner instruction, 2026-09-30: **“DO NOT MAKE DECISION FOR THE HUMAN IN TERMS OF SECURITY, ALWAYS ASK FIRST!”**

- Ask before introducing, expanding, removing or replacing security policy unless the user has already explicitly authorized that change in the session. This includes content filters, credential-pattern detection, redaction rules, publication refusals, path exclusions and access restrictions. Prepare a concrete proposal with evidence and operational impact so the owner can decide; do not silently decide what they may publish or execute. Continue work already authorized without repeatedly asking for the same permission.
- Inherited code, an earlier agent's recommendation, or a generic “security” label is not proof of owner approval. Flag inherited restrictions when a relevant review finds them; do not silently turn them into requirements or alter them. Unapproved security-policy changes are subject to rejection and reversion.
- Keep credentials outside model-accessible resources through the approved access boundaries. Pattern matching against generated prose is not a substitute for credential isolation. Environment-variable references and placeholders in API examples are not evidence of an exposed credential. Do not add a publishing tripwire, replacement heuristic, entropy check or allowlist workaround without explicit authorization.
- Path/privacy redaction and credential isolation are separate from content-based publication blocking; approval for one does not authorize the other. Preserve the existing credential-handling, SSH verification, ownership and filesystem contracts until a change is authorized. Recording this policy does not itself authorize a security audit, runtime changes or removal of existing protections.

The owner's supplied session records the concrete publisher failure in another project: see [DOCUMENTS.md](docs/DOCUMENTS.md#owner-decision-on-publication-filters--2026-09-30). Its commands and grants apply to that project, not this checkout.

## Shared runtime

- The user and agent share GNU Screen session `hortator`, window `dashboard`, under OS account `codexy`. Use it for foreground runtime commands so the user can observe and control the same terminal.
- Join with `screen -x hortator`; this permits concurrent attachment. Do not detach the user's display or terminate the Screen session. Ctrl-A then D detaches a viewer; Ctrl-C stops the foreground server but preserves the shell.
- Preserve this user's terminal configuration. If Screen is absent, create it using `/home/codexy/.screenrc` and `bash --login -i`; never bypass startup files with `-c /dev/null`, `--noprofile` or `--norc`. The user's Screen configuration sets 50,000 scrollback lines and `termcapinfo xterm* ti@:te@`; `.profile` loads `.bashrc` for aliases and PS1. Inspect with `screen -ls` and process/log state; avoid `screen -Q` on this host because a session disappeared during such a query (causation is unconfirmed).
- The shell working directory and Screen default directory must point into this repository. Persistent data belongs outside Git at `/home/codexy/.local/share/hortator`; the log is `/home/codexy/.local/share/hortator/logs/hortator.screen.log`. Never put live runtime data back under the checkout. Inspect Screen and foreground process state before sending input; do not interrupt an unrelated user command.
- Start with `/home/codexy/.local/bin/hortator serve --host 127.0.0.1 --port 8000 --color`. The explicit color flag implements the user's preference despite inherited `NO_COLOR`; do not remove the shell's environment setting globally. This launcher lives outside the repository, loads `/home/codexy/.config/hortator/runtime.env`, changes to this root, and runs the selected branch with the external `HORTATOR_DATA_DIR`. When using `uv run hortator` directly, source that environment file first, including on older branches (which may lack `--color`). One worker / one runtime only. Keep it running in Screen after the turn when it was running or the task requested startup. Do not create a second unsupervised API process.
- The production dashboard and API share port 8000. Do not use the legacy Vite process on port 5173 as evidence of the current build. Rebuild `web/dist` when UI source changes; new routes/static mounts may require a server restart.
- Commit and publish the completed task in an open PR, and pass appropriate local checks, before the final dashboard build and server restart. Local development deployment can proceed while remote CI/reviews run; report their pending state honestly. `!version`/`!ver` and authenticated `/api/version` identify the server snapshot captured at startup; the panel also shows its own build stamp. Never replace this with a live HEAD lookup that could misidentify already-loaded code after a checkout. Keep unknown metadata and uncommitted builds explicit. Rebuild after the final commit so dashboard/server identities match.
- Read-only diagnostics and repository edits may use ordinary tools. Runtime start/stop/restart commands belong in the shared Screen session. Keep the user informed of those changes.
- The user wants useful operational events in Screen, with ISO timestamps, colors, scope filters, verbosity and folded details. Keep successful HTTP polling at DEBUG, repeated incidents summarized, and the complete event ledger independent of console filters. The console must append to normal scrollback, never enter an alternate screen or clear the terminal. Its single-key controls only change the local view; preserve Ctrl-C, restore terminal settings on shutdown and never interpret input as shell commands. See OPERATIONS for the key map. Do not change Discord incident settings or provider/bot configuration as a side effect of console work. A richer dashboard log panel is a later task.

## Cross-cutting invariants

- Application concurrency must have explicit `asyncio.TaskGroup` ownership on Python 3.14. Use `Kernel.lifetime()` for background services/independent bot turns and scoped groups for related child I/O. Never reintroduce bare `asyncio.create_task`, abandoned task handles or ignored `gather(return_exceptions=True)` results. Independent failures must be recorded immediately with identity and credential-redacted traceback; related failures cancel and join siblings and preserve every exception. Propagate owner cancellation, finish process/delivery cleanup, and join tasks before closing SQLite/clients. Task groups do not make synchronous work nonblocking: keep bounded, detached CPU preparation off the event loop and SQLite/vault access on its owner thread. See [docs/CONCURRENCY.md](docs/CONCURRENCY.md).

- Current bot transcript/clock guidance and ordinary console/dashboard timestamps use `settings.timezone` (Europe/Madrid on this installation), with explicit UTC offsets. Model-facing tool metadata uses the same zone. Preserve epoch storage, canonical UTC build metadata, raw request evidence and historical note text; never rewrite historical instants or guess offsets for naive text. Seasonal offsets follow the configured IANA zone.

- Loki is the council's alpha tester for experimental capabilities and deliberately noisy workloads, not the harness or its universal configuration. Build features for any eligible bot with explicit grants/settings; never hardcode Loki-specific runtime behavior, copy his prompts/grants to stable members, or infer that the other bots need changes because they are not appearing in recent debugging. Experiments can later move to Hortator or another bot and be disabled for Loki.

- Every bot is a distinct Discord application. Provider transport, reusable model profiles, bot personality/capabilities, and scoped memory remain separate.

- Hortator accepts owner input only in its configured control channel, that channel's threads, and owner DMs. A mention elsewhere does not grant intake scope. The optional keyless owner-only `discord_send` action is the explicit exception for posting supplied text/files to another configured council room/channel or allowed observed thread. It never relocates Hortator's conversational turn, grants human priority, or replaces ordinary assistant answers. Preserve room/sender grants, strict directed reply targets, per-turn delivery-key idempotence, destination send locks, outbox uncertainty and receipt links. New installations leave the plugin disabled; enable only on operator request. See OPERATIONS/TOOLS for modes and examples.

- On 2026-09-07 the user confirmed the typing indicator works live, Hortator's new provider/Kimi K3 works, and context/concurrency configuration is resolved. The Qwen pool uses two concurrent requests for four units. Preserve current settings; do not reopen the earlier configuration audit's superseded corrections or add weighted concurrency for this resolved setup. Full council activation/acceptance is still for the user.

- Discord owner is the immutable snowflake `1482143139828596916` (`.normal.man.`, The Boss). Only that genuine human identity may command or converse with Hortator. Keep deterministic commands outside model execution; model inspection stays read-only. Credentials are dashboard-only.

- Preserve arbitrary vendor parameter JSON, independent cadence/cooldowns, intentional silence, bounded tools/compaction, durable trajectories and explicit delivery uncertainty. Do not publish provider reasoning fields to public Discord; the opt-in owner-only private `reasoning_viewer` is the sole Discord exception.

- Follow the existing test and build commands. Run checks appropriate to changes; never claim live Discord/provider validation from mocked tests. Do not expose secret values in tool output, documentation or commits.

A known failing required check blocks a readiness claim, even if it predates the
current change. Resolve the failure or document the unresolved blocker explicitly;
never report a focused pass as a full-suite pass. Tuning a constant must update
its assertions and current contract in the same change.

## Feature contracts

Before changing a feature, read its authoritative document below, including its
**Owner decisions** section. Detailed instructions moved there remain binding;
this index does not replace them. Keep feature defaults, limits and contracts in
that document; other current guides should link instead of restating them. Dated
design and verification records preserve what was decided or tested at the time,
including superseded behavior, and must not be treated as current configuration.

- Dashboard and explicit one-turn trigger: [DASHBOARD.md](docs/DASHBOARD.md).
- Console, provider requests, scheduling, footers and compaction: [OPERATIONS.md](docs/OPERATIONS.md).
- One-use recovery of incomplete model responses: [RESPONSE_RECOVERY.md](docs/RESPONSE_RECOVERY.md).
- Slash ingress and acknowledgement recovery: [SLASH_COMMANDS.md](docs/SLASH_COMMANDS.md).
- HTTP response evidence: [HTTP_EVIDENCE.md](docs/HTTP_EVIDENCE.md).
- Tools, ordinary answers, council inspection and private channel memory: [TOOLS.md](docs/TOOLS.md).
- Editable prompts, attribution and clean-slate controls: [PROMPTS.md](docs/PROMPTS.md).
- Opt-in Engram conversation state and history reduction: [ENGRAMS.md](docs/ENGRAMS.md).
- Owned background jobs and completion dispatch: [BACKGROUND_JOBS.md](docs/BACKGROUND_JOBS.md).
- Experimental researcher settings, capacity and task batches: [RESEARCH_ASSISTANT.md](docs/RESEARCH_ASSISTANT.md).
- Experimental secretary reminders, snoozes and periodic alarms: [SECRETARY.md](docs/SECRETARY.md).
- Opt-in owner-only reasoning viewer: [REASONING_VIEWER.md](docs/REASONING_VIEWER.md).
- Consistent snapshots and selective recovery: [SNAPSHOTS.md](docs/SNAPSHOTS.md).
- Private per-bot global memory: [GLOBAL_MEMORY.md](docs/GLOBAL_MEMORY.md).
- Web search and preserved engine evidence: [WEB_SEARCH.md](docs/WEB_SEARCH.md).
- Experimental Parallel Fast search: [DUMB_SEARCH.md](docs/DUMB_SEARCH.md).
- Opt-in DeepSeek search: [DEEPSEEK_SEARCH.md](docs/DEEPSEEK_SEARCH.md).
- Shared image intake and per-bot visibility: [VISION.md](docs/VISION.md).
- Opt-in native voice-note/audio input: [AUDIO_INPUT.md](docs/AUDIO_INPUT.md).
- Experimental speech cloning and native voice-note output: [FRENCHY_TTS.md](docs/FRENCHY_TTS.md).
- Speech generation and authenticated voice discovery: [TTS.md](docs/TTS.md).
- Profile pricing and usage provenance: [PRICING.md](docs/PRICING.md).
- Document ownership, publication and SSH identity: [DOCUMENTS.md](docs/DOCUMENTS.md).
- Remote hosting policy is operator-owned: never manage or reconcile remote
  `.htaccess` files. See the 2026-10-13 owner decision in DOCUMENTS; it supersedes
  earlier generated remote serving rules without changing site ownership or SSH trust.
- Scoped agentic tools and task budgets: [AGENTIC_TOOLS.md](docs/AGENTIC_TOOLS.md).
- Shell sandbox, limits and archival: [SHELL_RUNNER.md](docs/SHELL_RUNNER.md).
- Single per-bot filesystem and mount toggle: [FILESYSTEM.md](docs/FILESYSTEM.md).
- Optional Discord panels and automatic attachments: [DISCORD_PANELS.md](docs/DISCORD_PANELS.md).
- Plugin extension contracts: [PLUGINS.md](docs/PLUGINS.md).
- Plugin authoring workflow and tested starter: [council-plugin-author](skills/council-plugin-author/SKILL.md).
  Its source is versioned here; the local discovery link does not install a bot
  plugin or authorize enabling one. Coordinate parallel edits and publication
  ownership under the existing branch/review rules.
- Task ownership and cancellation: [CONCURRENCY.md](docs/CONCURRENCY.md).
