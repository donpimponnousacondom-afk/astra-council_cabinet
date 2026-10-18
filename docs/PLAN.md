# Implementation and acceptance plan

## 2026-10-18 — explain temporary files in the late mount notice

The owner approved a small model-guidance correction after Sun and Zelda saved
downloads in `/tmp` and expected them in a later shell call. State that `/tmp`
is cleared after every call and direct reusable files to mounted `/workspace`.
Use the existing late filesystem notice; preserve tool schemas, early prompt
text, storage lifetimes and limits. See [FILESYSTEM](FILESYSTEM.md).

## 2026-10-15 — explain shell limits and saved package installation

The owner declined raising Sun's entry limit after his NumPy/chess installation
hit the existing 1,000-file/directory ceiling. Keep all live limits unchanged.
Expose the exceeded tree, entry ceiling, measured lower bound and discarded-save
semantics to the model and operator. Do not infer OOM from a bare SIGKILL.

Correct tool instructions left stale by PR #126: owner-execute already persists.
Explain `pip --target /workspace/pylibs` and subsequent `PYTHONPATH` for suitable
regular-file packages when persistent storage is mounted. This grants no extra
capacity and does not imply every package tree or native dependency is supported.
Keep these instructions static; live limits/counts belong in tool results, with
no prompt-layer reordering or changing counters in the stable prefix.

The owner separately authorized placing the supplied local CPU/Eigen lc0 binary
in Sun's saved filesystem. Use the existing atomic generation/quota machinery,
preserve all other files and settings, retain owner-execute permission, and verify
the executable in an isolated sandbox before installing it.

## 2026-10-15 — preserve executable files between shell calls

The owner asked whether the recurring lost executable bit was a regression and
authorized its repair for saved files. Canonical merged history shows the shell
pipeline has dropped it since PR #10 (`937c8b8`, 2026-09-08); the October 10
workspace probe explicitly documented the limitation, and Simple filesystem
inherited it in PR #100 (`09ff2ad`). PR #125 changed capacity limits without
altering this path. Snapshot backup copying and packaged shell wrappers already
preserved executable permission in separate paths; neither fixed shell persistence.

Carry only owner-executable intent through the shared shell/file pipeline,
preserving the existing private access boundary, grants and atomic save behavior.
Apply it to both Simple filesystem and Private workspaces, including direct
file edits and overwrite, explicit removal, and rollback. Do not infer executable
status for previously saved files or change the bot's binaries/libraries.
Current contracts: [FILESYSTEM](FILESYSTEM.md), [WORKSPACES](WORKSPACES.md),
and [SHELL_RUNNER](SHELL_RUNNER.md).

## 2026-10-15 — filesystem capacity determines file size

The owner requested that a saved binary can use half of its bot's configured
Simple filesystem capacity: 256 MiB allows a 128 MiB file, and 512 MiB allows
256 MiB. Derive the limit from effective capacity, including per-bot overrides,
and retire the independent fixed setting rather than leaving existing 32 MiB
values in force. The current contract and migration are in [FILESYSTEM](FILESYSTEM.md).
Keep total capacity, entry limits, original-file protection and atomic shell
save/rollback behavior. This does not authorize changing Private workspace or
disposable-shell limits, import/export limits, or filesystem access boundaries.

File-hash reuse is deferred: a local experiment found same-size content changes
with identical inode, modification-time and change-time fingerprints. Reusing
those hashes would return stale metadata. Existing fresh hashing and validated
shell snapshot copying remain in place; a future optimization needs reliable
content-version tracking rather than filesystem timestamps alone.

## 2026-10-14 — cache-stable memory epochs (owner scope)

The owner accepts strategy items 1 and 2: move changing usage counters to the
late prompt, keep advertised tool descriptions stable, and retain a durable
memory snapshot before conversation with complete keyed changes at the tail.
Successful compaction or context reset is the normal snapshot rebuild boundary;
no guessed idle TTL or arbitrary delta-size rotation. Size pressure uses existing
compaction. Canonical writes/deletes persist immediately. Template/grant changes
must remain effective and scoped; snapshots never expand access.

Implementation is split into sequential PRs. The first moves counters/status
only and preserves early live notes. The second implements durable snapshots,
net edits/deletions, and compaction/reset integration. Stable tool lists across
finalization (strategy item 3) remain deferred. No provider settings, bot grants,
Engram behavior, cache TTL assumptions or cost guarantees are part of this work.

PR #121 completed the counter/status increment. The memory-epoch increment keeps
the original note-template text and operator overrides, persisting their rendered
snapshots per bot/conversation. Canonical writes remain immediate; complete net
replacement/deletion entries follow conversation/tool exchanges in editable
`memory_delta` and `global_memory_delta` layers, before live budgets/runtime facts.
Both layers use the required `{{MEMORY_DELTA}}` data placeholder. Snapshot entries
remain until superseded by a matching key; embedded old counters are historical.
Empty snapshots and empty changes still receive delta guidance.

When the note grant and an enabled, nonempty wrapper containing `{{NOTES}}` apply,
missing/disabled/empty delta text or omission of its data placeholder fails before
inference. A disabled/empty note wrapper or one omitting notes retains its existing
output behavior without hidden fallback or delta injection. Relevant note-template,
identity, grant and configuration changes rebuild the affected snapshots. Existing
synthetic slash/panel conversation keys preserve their scope. Full snapshots restore
the prompt cache; selective context restoration clears affected pins; notes-only
restoration becomes a delta. Existing compaction handles size pressure without
truncation and can fail when no eligible history remains, including under Engram
history reduction. See [the prompt contract](PROMPTS.md#pinned-notes-and-saved-changes).

## Restore the pre-#119 prompt order — owner decision (2026-10-14)

The owner rejected PR #119's memory-after-conversation arrangement and requested
a complete revert, including its schema/status and dashboard changes. Conversation
changes more often than saved notes; putting the growing transcript before a large
unchanged memory block prevents reuse of that memory suffix. The earlier tests
proved history-prefix stability during writes, not improved overall cache reuse.
Restore the pre-#119 implementation and contracts; preserve saved memories,
contexts, prompts, grants and provider/bot settings. The owner will supply a new
design separately. Do not retain selected optimizations or introduce a replacement
ordering as part of this revert. The established trailing runtime/Engram layers
are restored unchanged; moving conversation to the literal end is not part of
this rollback.

## Stabilize progress-mode tool use — owner decision (2026-10-13)

Keep `end_cycle` experiments parked until the owner has stable models for a
controlled test. The current fixes target ordinary progress/narrate behavior;
they do not change saved bot modes, grants, model parameters or memory contents.
The owner authorized actionable argument errors and operation-specific help,
followed by a separate supersession change that stops starting stale work at
safe boundaries and preserves truthful receipts for completed actions.

The feedback change preserves validation and job ownership. Memory paging fields
are explicitly `read_result`-only; shell help distinguishes byte-based `read`
from character-based saved-result paging and explains cycle-scoped job IDs.
The supersession/receipt work is a separate follow-up, not implemented by the
feedback change. Model explanations are diagnostic leads; ledger receipts,
effective configuration and provider-reported identity are the evaluation evidence.

## GitHub PR workflow — owner decision (2026-10-12)

Completed task changes are automatically committed on their task branch, pushed
and opened as a PR to main under standing owner authorization. The agent also
performs GitHub squash merging after both independent reviews, resolution of
blocking findings and passing required checks. Do not stop at readiness or ask
again for an ordinary merge unless the owner explicitly pauses or reserves it.
Never make local implementation commits/merges on main or push directly to it.
This supersedes earlier per-push/PR/merge permission requirements, owner-only
merge wording and local-only review substitutes.

Every open PR receives two independent reviewers: `gpt-6-luna` at `max` reasoning
and `gpt-6.1-sol` at `high`. Latest verified newer successors in those model
families are allowed; never use an older Sol. Both inspect the actual published
PR, watch GitHub Actions and checks for its current head, and post their own PR
comments. Preserve independent initial findings and record the reviewed SHA,
verification limits and measured evidence. Older dated local-review records
remain historical evidence, not proof of this workflow.

The parent can assign CI repairs and confirmed finding fixes to available
reviewers without routine owner questions, coordinating edits in the shared
checkout. Local development rollout may proceed after the PR opens and local
checks pass while CI and reviews continue; readiness still requires the actual
current-head results. General sub-agent delegation is welcomed and unrestricted
by this PR protocol. See the
[operating procedure](OPERATIONS.md#independent-pr-review-and-check-monitoring)
and [standing agent instructions](../AGENTS.md).

## Narration is not an answer draft — owner correction (2026-10-13)

The owner approved item 1 of the external field report's §7 review of PR #112.
A response containing narration plus action calls without `end_cycle` must not
freeze that pre-result narration as the only publishable answer. Withhold the
narration, run the actions once, count the strike and remove action tools. If
the existing failure limit allows finalization, let the bot write its final
answer from those results together with `end_cycle {}`. No action replay or
new work is allowed. Engram state and answer provenance belong to that actual
final answer, not the earlier narration.

Real answer drafts remain immutable: retain text-only replies missing the
finish call and answer text accompanying recoverable failed `end_cycle`
batches. The existing reverse-order finish is also legal during no-draft
finalization and permits only its single tool-free first answer. Numeric strike
semantics stay unchanged: one stops after the first failure, while two allows
one finalization chance. New v4 default guidance distinguishes these paths;
owner edits and explicit older-template overrides remain preserved. No mode,
grant, policy or other field-report item changes are part of this correction.

## Bounded first answer after end_cycle — owner decision (2026-10-13)

The owner approved the reverse order when no answer has yet been produced:
a valid bare `end_cycle {}` with no frozen draft allows exactly one tool-free
model request for the **first** public answer. This is a legal finish, not a
strike, including when the configured failure limit is one. Permitted companion
writes run once before that request; a failed companion stops the cycle and
preserves successful writes and their receipts. The extra request accepts
ordinary public answer text only. Empty text, any tool call or incomplete
output stops without retry. Private reasoning never becomes answer text.

This is not draft regeneration. If an answer already exists, confirmation
publishes only the frozen original and cannot replace it with new prose. Both
final paths remove action tools and cannot return to work. The versioned v3
protocol and dashboard help describe this bounded alternative while preserving
owner prompt overrides. This supersedes only the no-draft stop in the initial
correction below; the immutable-draft, strike-ceiling and companion-failure
contracts remain.

## Immutable end_cycle recovery — owner correction (2026-10-12, late)

Live failures exposed that the earlier implementation discarded valid work and
asked models to regenerate or repeat malformed finishes. The owner rejected
that recovery model. A failed response with usable text is frozen as the
original draft. Action schemas are removed; the model can only confirm it with
`end_cycle {}` or choose silence if permitted. New prose cannot replace it.
The existing strike setting bounds failed responses themselves, including
confirmations: default two means one failure and one confirmation chance, and
one means stop at the first failure. No extra repair attempt is added at the
limit. Tool-only work keeps its normal budget and does not count as a strike.

No answer text means there is no draft to recover: preserve successfully
executed permitted writes and stop incomplete without asking for a new answer.
A finishing companion failure likewise stops without publishing a potentially
false success message, preserving completed writes and their receipts. Never
replay the entire batch. Engrams, footers and delivery evidence must remain
bound to the original draft source; confirmation is separate evidence, not a
replacement source. Normal cancellation/access and uncertain-delivery rules
continue to apply.

Retire the optional retained-draft switch with an explicit isolated migration;
preserve all bot modes, enabled states, grants and numeric strike values. A
versioned default protocol prompt reaches existing installations, while saved
custom overrides remain untouched. Update unedited shipped general guidance
only by exact prior-content match, including the exception that `end_cycle {}`
is an action rather than help discovery. This correction supersedes the
replaceable-draft and extra-finalization behavior in the dated decision below.

## end_cycle response protocol — owner decision (2026-10-12, later)

After the first increment merged (#109) the owner resolved the five deferred
questions and had the external auditor implement `end_cycle` locally, leaving
publication, PR review and merge to the project agent. Decisions: text is
published only together with a no-argument `end_cycle` call; narrated action
batches are withheld but executed, with two strikes by default (1–5) before a
single finalization round; a text-only response is retained for that one round
and a bare `end_cycle` publishes it by default; day-one companions are memory and
global-memory writes plus `discord_attach`, executed before publication; the
incomplete-cycle Discord notice is per bot, default off, one per activation and
paused after three consecutive incomplete activations in a channel; no
provider-capability measurement is required because the retained-draft path
lets a provider that cannot combine text and tool calls finish in two rounds.
The mode is a per-bot setting beside `narrate` and `progress`; bots that are not
opted in run their previous code paths unchanged, and `cycle.ended` events give
the comparison data. This supersedes the deferral in the next section.

The owner's subsequent dashboard feedback requested clearer help and correctly
placed toggles. Keep response behavior in its own group under Capabilities,
show only the selected mode's settings, and put longer explanations behind
keyboard-accessible disclosures. Preserve values when switching modes. This is
a presentation change, not a new protocol/default or automatic bot opt-in.

The external auditor closed round 2 after accepting the evidence-based
corrections and inspecting the PR #110 fixes. Their remaining wording correction
is applied with the dashboard cleanup: failed companions cannot be retried in
the terminal-only finalization round. The larger accepted engineering follow-ups
remain separate work: off-loop filesystem scans/copies/commit work (F1), slash/
panel mount facts (F3), storage-error classification (F4), provider console labels
(P9), and stale seeded guidance. Disposable validation (S1), unsafe-entry recovery
(F6), and audit §6 access/policy decisions still require the owner's decision
before restrictions are changed. Closure evidence is retained in the ignored
`audit/2026-10-12/fable-external-audit/CLOSURE.md` and its round-2 reply.

## Response protocols, first increment — owner decision (2026-10-12)

Following the external audit, the owner authorized per-bot `response_protocol`
controls, a cap on progress attempts, best-effort progress delivery, and a
read-only ledger report of actual text/tool response combinations. Existing bots
use the pre-#103 `narrate` behavior; the owner will opt bots into `progress`.
The cap defaults to one attempt per turn, accepts 0–20, survives task-budget
extensions, and never limits the final reply. Private withheld notices are
deduplicated, and optional progress must not delay tool execution through
cooldowns or fail the batch on ordinary send errors. Existing cancellation and
delivery-uncertainty contracts still apply.

`end_cycle` is a reserved, rejected configuration value only. The owner deferred
all five decisions: narration strikes, retained-draft fallback, terminal-compatible
tools, incomplete-cycle notices, and provider-capability rollout. Finish this
first increment without implementing its terminal tool or deciding those options.
The ledger measures returned normalized text/tool combinations independently of
whether progress was posted; zero observations remain unknown capability.

This supersedes the unconditional progress rollout described in the earlier
same-day decision below. The current contract is in
[TOOLS.md](TOOLS.md#normal-and-extended-turns), prompt selection in
[PROMPTS.md](PROMPTS.md#response-protocol-guidance), and operator reporting in
[OPERATIONS.md](OPERATIONS.md#observing-text-with-tool-calls). External-audit
policy questions remain outside this implementation; no merge, vault, ruleset,
shell-grant or path-redaction policy is changed here.

## Progress messages with action calls — owner decision (2026-10-12)

Permit completed model responses containing ordinary text and real action calls
to post progress first, execute the batch, then continue inference with results.
The owner explicitly chose this over treating mixed content as a final answer;
additional rounds are acceptable in exchange for earlier human feedback. Apply
the shared protocol to eligible action tools, without a memory-only restriction.
Text alone remains terminal and cannot imply or queue an absent tool call.
Keep private reasoning, final-only Engrams, prepared files/panels, cancellation,
delivery uncertainty and per-turn budgets intact. The new editable response
protocol layer clarifies this without overwriting saved prompt edits.
See [TOOLS.md](TOOLS.md#normal-and-extended-turns).

The same patch includes the console-readability follow-up requested on
2026-10-11: lead provider failures with the received error, label outgoing
reasoning settings as requested, and retain irrelevant image/size facts in
expanded evidence. The earlier text-only MiMo research HTTP 400 was attributed
by the owner to their CPA parameter-forwarding change; this work does not
translate parameters, identify an unreported rejected field or edit profiles.

## Simple filesystem — owner decisions (2026-10-10)

The owner authorized a separate opt-in `filesystem` plugin, preserving Private
workspaces for Hortator and other bots using named tasks. Each granted bot gets
one saved directory across its conversations, with 256 MiB by default, no
inactivity expiry, no model-selected task names and no automatic migration.
The owner will switch experimental bots' grants themselves.

The final ON/OFF decision supersedes the earlier read-only proposal: disabling
unmounts saved files completely and preserves them for re-enabling. No-task Bash
then uses disposable storage. Tell bots with saved files that their mount is OFF,
their files still exist but cannot be accessed, and they should ask the owner if
needed rather than inventing file contents or claiming an inspection.

Report used/total space and an integer percentage, such as `182/256 MiB (71%)`.
Remove the proposed tidiness instruction; the owner is testing their own prompting.
Keep optional `{}` schema/help, factual file limits and current mount/usage at the
runtime prompt tail. Compatible explicitly saved dependency files use the same
quota; this does not promise unchanged virtualenv or Node-install compatibility.
Preserve per-call process cleanup; no persistent background shell process.

Implementation and model-facing wording are in [FILESYSTEM.md](FILESYSTEM.md).
Measured checks and limitations are in [VERIFICATION.md](VERIFICATION.md). Package
persistence findings were first reproduced on the old task workspace and are not
proof that every package layout works in the new filesystem.

## Owner decision — independent disposable shell (2026-10-09)

Shell-only bots must be able to run one-off commands, curl/POST and inline Python using the existing sandbox toolchain without a persistent workspace. Omit `task` for empty disposable storage; an explicit task retains the existing workspace workflow and grant. Keep job evidence, cancellation, quotas, network boundaries and bot settings intact. The current contract is in [SHELL_RUNNER.md](SHELL_RUNNER.md).

## Recent owner decisions

On 2026-10-08, after investigating native tokenizers, the owner chose to keep
one fixed `cl100k_base` fallback for unreported token counts across models.
Do not implement the proposed per-model tokenizer mappings. Reported usage stays
authoritative and local estimates remain marked `~`. The owner confirmed that
the proxy was rewriting reasoning parameters and is patching it separately to
preserve them; evaluate subsequent reasoning observations after that repair.
The repair is owner-reported work in progress, not verified Council behavior.
See [token reporting](OPERATIONS.md#discord-message-footers).

On 2026-10-08 the owner requested private, factual feedback when Engram updates
are absent, with counts based on delivered replies and useful rejection reasons.
They clarified that retaining unchanged memory is a legitimate model choice.
Keep complete replacement semantics and public jokes before the private block;
do not force invented updates or add repair inference. See
[Engram feedback](ENGRAMS.md#feedback-when-memory-stays-unchanged).
The owner confirmed the reminder remains in scope while switching Curie between
models; assess memory quality after several days of that experiment. Keep history
reduction off and memory management with the model, rather than adding automatic
rewriting or a scheduled monitor.

On 2026-10-08 the owner stopped per-feature/hotfix backup creation because local
disk space is scarce. Backups and Storage Box offloads are on request; retain
one newest verified usable local recovery copy after requested cleanup. See
[backup operations](OPERATIONS.md#backup-and-restore) for verification and remote
storage. No scheduler or automatic deletion policy is introduced.

On 2026-10-08 the owner requested a separate experimental Frenchy TTS plugin:
preset discovery and choice, emotions/languages from provider metadata, saved
cloning, one-shot reference audio and native Discord voice notes with a separate
written answer. Copy this installation's TTS settings/key and switch Curie's
grant, preserving Marie Excited; new installations stay opt-in. See
[Frenchy TTS](FRENCHY_TTS.md) for the implemented contract and provider limits.

On 2026-10-07 the owner authorized a one-use user-role recovery note containing
that bot's retained failed-response draft and readable reasoning, including
content-filtered output. Append it after the existing prompt, let the bot use or
ignore the evidence, and keep it out of transcript/compaction/state unless the bot
deliberately saves useful facts. Preserve scope, budgets, cancellation and public
reasoning isolation. See [the recovery contract](RESPONSE_RECOVERY.md).

This is the current planning index. Detailed implemented contracts and owner
instructions live in the feature documents linked from [AGENTS.md](../AGENTS.md).
The [newest-first design index](history/README.md) links the
[dated design history](history/PLAN_HISTORY.md), which preserves the complete earlier
plan, including researched sources, old branch references, superseded decisions
and acceptance criteria. Historical statements describe their date, not the
current checkout or running configuration.

## Current work and acceptance

- On 2026-10-08 the owner chose Claude Code-style character-based tool-result
  pages and recoverable saved output as the direction for correcting Curie's
  repeated-read loop. Do not size those pages using an OpenAI tokenizer: the
  council uses multiple model families, and the owner prefers the conventions
  of Claude Code for these tools. Read-only investigation confirmed that the
  shared active tool-exchange limiter can omit a newly returned `web_fetch`
  body before its first model exposure; its scope includes ordinary results
  from other tools too. The owner authorized the shared fix, dual review and
  normal rollout. Character-based result pages and the scoped fallback reader
  implement that decision; live model acceptance remains separate. Distinguish
  character-based result paging from the separate overall provider-context
  budget. Preserve complete results before shortening model-facing copies,
  including the remaining generic registry truncation path. See the current
  [tool-result contract](AGENTIC_TOOLS.md#task-and-active-context-budgets).

- On 2026-10-06 the owner authorized lossless memory-result pagination after
  discussing Loki's keyed-read and paging suggestions. Preserve the full reply
  before bounding its prompt copy, expose a native reader on both memory tools,
  and allow exact-key reads. Reuse immutable evidence and existing scope/grant
  checks; retain note budgets and stored data. Do not change Engram, compaction,
  live bot settings or add dashboard controls. See [memory paging](GLOBAL_MEMORY.md).

- On 2026-10-06 the owner authorized correcting ambiguous recovery instructions
  for large tool results after Curie sent `read_result` to `global_memory`.
  Name an actually advertised reader and its arguments in each omitted-result
  reference, preserve evidence/scopes/grants, and report when no reader is available.
  No memory, Engram or compaction settings change. The owner is separately
  preparing Curie's compaction before the Engram trial; avoid interrupting active
  compaction during deployment. See [tool-result paging](AGENTIC_TOOLS.md#task-and-active-context-budgets).

- On 2026-10-06 the owner authorized a narrow punctuation-intake correction:
  repeated leading exclamations and a wrapped first word such as `!hello!`
  should reach ordinary conversation without breaking Hortator commands.
  Keep single-prefix command/error handling, owner/scope gates and the namespace
  for eventual council commands. No scheduler or configuration changes. See
  [intake semantics](OPERATIONS.md#scheduling-and-message-semantics).

- On 2026-10-05 the owner requested model self-identification through existing
  late runtime facts and dynamic tails, with the same `{{MODEL}}` placeholder
  as the public footer. The owner then chose uppercase double braces for all
  dynamic placeholders and shared parsing with footers; existing single-brace
  templates keep working. Keep the captured request slug authoritative, layer
  switches effective and earlier prompt text unchanged. No model/profile,
  memory, compaction or image-capability changes. See
  [literal prompt data](PROMPTS.md#literal-data-placeholders).

- On 2026-10-01 the owner authorized incremental dashboard/event-loop fixes:
  remove repeated prompt-JSON decoding from statistics/calibration, load full
  dashboard evidence on demand, and move reporting reads to a bounded worker.
  Retain one runtime and the existing model context/configuration semantics.
  Follow the current [PR workflow](OPERATIONS.md#independent-pr-review-and-check-monitoring);
  broader process separation remains a measured follow-up, not part of this
  change. See
  [reporting ownership](CONCURRENCY.md#reporting-reads) and
  [summary API](API.md#reporting-summaries).

- The owner requested two independent opt-in experiments (2026-09-26): readable
  attributed [conversation text](PROMPTS.md#experimental-conversation-text) for
  generation/compaction, and [Engram rolling memory](ENGRAMS.md). Existing bots
  keep structured input and no Engram grant. Engram history reduction is a second
  explicit opt-in, initially false even when the plugin is enabled. Start live
  memory trials on a disposable/empty-context bot; do not reduce Loki's existing
  context as part of implementation. Keep full operational metadata on disk.
  The independent context/compaction audit (2026-09-26), "Not losing things like
  tears in the rain", identified presentation/attribution fixes and found no
  checkpoint or coverage defect. The owner authorized remediation, PR review and
  merge after checks. Keep both experiments opt-in; live model acceptance remains
  separate. Engram trials use a new, empty conversation with other memory plugins
  off and its own recent-message/reduction controls, retaining all uncovered input.
  The ignored `audit/` index links the review and responses. No live bot opt-in is
  implied by the shipping authorization.

- The experimental [Secretary](SECRETARY.md) provides one persistent ledger per
  bot across contexts, with explicit delivery destinations, snoozes and recurring
  alarms through ordinary bot turns. It is off by default;
  live opt-in and reminder acceptance remain operator actions.
- The audit remediation implements bounded runtime/plugin/test-support changes,
  structured verification and preserved documentation contracts. Follow
  [VERIFICATION.md](VERIFICATION.md) for measured evidence and remaining acceptance.
  Larger turn-loop/frontend refactors remain incremental follow-ups, not completed
  architecture work. Publish the CI workflow and require its status check before
  treating it as a merge gate. Original audit reports remain unchanged.
- Preserve the owner-confirmed slash acknowledgement contract in
  [SLASH_COMMANDS.md](SLASH_COMMANDS.md); earlier retry limits in the history are
  superseded, not instructions to change current behavior.
- Researcher functionality is implemented but live fan-out/search-breadth acceptance
  remains an owner task. Use [RESEARCH_ASSISTANT.md](RESEARCH_ASSISTANT.md) and
  [BACKGROUND_JOBS.md](BACKGROUND_JOBS.md) for the current contract, and
  [VERIFICATION.md](VERIFICATION.md) for what has actually been exercised.
- Full council activation and daily-use dashboard acceptance remain with the owner.
  Keep the frozen fallback until acceptance under [DASHBOARD.md](DASHBOARD.md).

## Search coverage rollout · 2026-09-28

Owner authorized direct Ollama search through the existing web-search plugin,
using the stored account credential and Loki as the initial tester. Preserve
other bots' defaults, generation providers and the MiMo researcher. Other search
services remain exploratory. Current behavior and setup are owned by
[WEB_SEARCH.md](WEB_SEARCH.md); test/live rollout evidence belongs in VERIFICATION.

On 2026-10-01 the owner requested a backend guidance correction: show effective
per-bot search defaults and demonstrate calls that respect them. Keep all engines
and the existing dashboard interface; no priority or engine-disable controls.
See the [search contract](WEB_SEARCH.md) for current selection semantics.

## Deferred product work

The owner authorized a separate [Dumb Search experiment](DUMB_SEARCH.md) on
2026-10-02: inexpensive Parallel Fast retrieval with real API evaluation, standard
plugin credentials and explicit bot grants. Existing search stays available;
the owner will select the experimental plugin manually. Parallel monitoring,
Extract and research-agent products remain deferred. No vendor tooling install.

Separate-model/background compaction and a richer dashboard log panel remain
future work. Extra branch-hook automation is deferred. These are planning notes,
not authorization to change configuration, activate bots, remove the fallback,
or run paid/live acceptance. See the dated history for original scope and rationale.

## Deferred operational follow-ups

- Owner deferred the recurring Loki DM history-import warning on 2026-09-26
  (`discord.history_failed`, event `61308`, stage `validate_scope`, channel
  `1553280213004062780`). Review the manually configured DM-as-guild-room entry:
  startup history import rejects its guild/room association. Secretary's verified
  owner-DM intake and reminder routing are separate. The warning predates the
  global-ledger patch; no configuration correction or intake change was made.

## Where to continue

| Work | Authoritative documents |
| --- | --- |
| Procedures, storage, runtime identity and recovery | [OPERATIONS.md](OPERATIONS.md), [SNAPSHOTS.md](SNAPSHOTS.md) |
| Dashboard behavior and control contracts | [DASHBOARD.md](DASHBOARD.md), [API.md](API.md) |
| Prompts, context and clean-slate controls | [PROMPTS.md](PROMPTS.md), [OPERATIONS.md](OPERATIONS.md#context-and-compaction) |
| Background work and research | [BACKGROUND_JOBS.md](BACKGROUND_JOBS.md), [RESEARCH_ASSISTANT.md](RESEARCH_ASSISTANT.md) |
| Tools, plugins and concurrency | [TOOLS.md](TOOLS.md), [PLUGINS.md](PLUGINS.md), [CONCURRENCY.md](CONCURRENCY.md) |
| Discord interactions | [SLASH_COMMANDS.md](SLASH_COMMANDS.md), [DISCORD_PANELS.md](DISCORD_PANELS.md), [REASONING_VIEWER.md](REASONING_VIEWER.md) |
| Images, documents and isolated execution | [VISION.md](VISION.md), [DOCUMENTS.md](DOCUMENTS.md), [AGENTIC_TOOLS.md](AGENTIC_TOOLS.md) |
| Test evidence and outstanding live acceptance | [VERIFICATION.md](VERIFICATION.md) |

Record new product decisions in the relevant feature contract and update this
plan when they change current work, acceptance or deferred scope. Preserve dated
rationale in the historical record. New dated entries use newest-first order;
existing historical statements are not rewritten into claims of current state.

### 2026-10-06 — Manual Trigger destination

Owner selected the most recent eligible conversation with human activity for an
untargeted dashboard Trigger. Use stored message timestamps, authenticated human
author metadata and existing scope/reset rules; preserve directed reply targeting
separately from conversation selection. Explicit channel
selection remains authoritative; normal scheduler fairness is unchanged. Do not
add a destination picker or alter bot configuration. See the current contract in
[OPERATIONS](OPERATIONS.md#dashboard-workbench).

### 2026-10-06 — Readable conversation scopes

Owner requested names instead of unexplained snowflakes in Engram inspection and
related dashboard scopes. Cache observed Discord channel/thread names, display
parent/thread labels with IDs for disambiguation, and share the formatter across
Engram, clean-slate and context selectors. Keep IDs authoritative and avoid extra
Discord queries during polling. See [DASHBOARD](DASHBOARD.md) for the current contract.

## Per-human directed queue — owner decision, 2026-10-06

A new human's mention/reply must wait for the current human's answer rather than superseding it. Coalesce pending input only for the same human in the same channel, choose the oldest waiting human, and retain the selected human through asynchronous context preparation. Preserve same-human/same-channel supersession of unsent drafts, routine-turn human priority, and existing delivery/authorization/budget barriers. Merely including another person's message in context must not consume their pending activation. No dashboard controls or parallel turns per bot are introduced.

## Remove speculative Bearer redaction — owner decision, 2026-10-07

The shared regex replaced the next ordinary word after “bearer”, including “doors” in Loki’s answer. The owner explicitly requested deleting this heuristic entirely, without replacing it with another pattern or token-shape guess. Keep existing credential isolation, exact saved-secret replacement and structured secret-field redaction. This applies to the shared redactor across all bots and text storage/evidence paths and duplicate rules in the console and private reasoning viewer; no bot-specific workaround, new setting, or historical text rewrite. The owner also requested an inventory of other filters; see [OPERATIONS](OPERATIONS.md#text-filtering-inventory--2026-10-07). That inventory does not authorize changing the remaining rules.

## Native audio input — owner decision, 2026-10-07

Add an opt-in, non-tool audio capability for new Discord voice notes and audio
attachments through each bot’s current model/provider. The owner confirmed the
same CPA route already works in another harness and explicitly chose to leave
the new plugin disabled for manual enablement. Preserve existing wake rules,
per-bot handled boundaries, original bytes and text-only compaction; no separate
transcription model or voice-channel listener. See [AUDIO_INPUT](AUDIO_INPUT.md).

### TTS voice discovery and Mistral response support — 2026-10-08

The owner requested model-accessible voice discovery without exposing API keys.
Extend the existing TTS pack with read-only `operation: voices`, Mistral v2 cursor
pagination and an optional per-generation voice choice. Retain old text-only calls
and saved defaults. Decode Mistral JSON/base64 speech alongside existing binary
audio; expose bounded upstream error details through existing redaction. This is
provider-specific, with no new dashboard controls or voice-list promises for
other services. The current contract is [TTS](TTS.md).

## Operator-owned remote hosting — 2026-10-13

The owner changed and renamed remote `.htaccess` files while enabling browser
features and explicitly directed the publisher never to sync those files again.
Remote domain/bot-namespace rules are now outside receiver management: no
creation, comparison, restoration or deletion, and no delivery/status/audit
refusal because rules are absent or edited. Existing files stay untouched and
new namespaces get no generated rules. Local preview behavior, model document
paths, ownership/receipts and SSH host verification retain their existing
contracts. [DOCUMENTS](DOCUMENTS.md) owns the current feature policy.

SSH failure diagnostics must report structured receiver errors from stdout even
when a login banner exists on stderr. Include the exit code; use bounded,
labelled stream diagnostics when there is no receiver error. The operator can
make Bash banners interactive-only; the application does not edit `.bashrc`.

## 2026-10-13 — ordinary-cycle supersession and outcome continuity

Owner authorized the diagnosed fixes while keeping end_cycle experiments parked.
After the argument/help repair, ordinary narrate/progress work yields to eligible
newer directed input at safe boundaries. Already-started work settles; accepted
research jobs remain independently owned. A metadata-only journal carries truthful
completed/refused/uncertain action receipts to the next ordinary cycle. No action
replay, model/grant changes or actual memory cleanup is included. The current
contract is [TOOLS](TOOLS.md#supersession-and-interrupted-action-receipts).

## 2026-10-14 — resumable compaction catch-up

After Sun's ninth catch-up batch exhausted its output allowance, the owner
authorized saving successful chunks so retries do not reprocess completed work.
Keep provisional progress durable and separate from the active context; validate
its source and instructions before reuse, and preserve existing complete-summary
and final context-budget gates. Expose saved/resumed progress to the operator.
Do not reset or interrupt Sun's ongoing compaction for this change. Initial-history
controls, renamed retention settings and provider tuning remain separate proposals.
Current behavior lives in [OPERATIONS](OPERATIONS.md#resumable-compaction-chunks).

## Engram character-only budget — owner decision, 2026-10-14

Remove the separate Engram token ceiling and its model-facing budget guidance.
Keep the existing character limit as the single configurable MEM + FACTS size
budget: a ceiling, not a target to fill. Preserve stored memory, coverage, grants
and all unrelated settings. Retire saved token-limit fields with an auditable
startup migration; upgrade only the unedited shipped prompt and preserve custom
text. Token counts remain diagnostic. See [Engram](ENGRAMS.md) for the current
contract; this does not alter provider output limits or context/compaction budgets.


### Owner decision: shell task scratch and prompt stability — 2026-10-18

Implement a shared shell environment/install instruction block across storage
modes. Normal `pkg install` and `uv pip install` come first; persistent Python
package trees remain optional. `/tmp` and `/packages` share the existing configurable
package allowance in RAM-backed private tmpfs, retained across shell calls and
qualifying same-human/channel follow-ups until task completion. Command processes
remain per-call. Keep changing limits/usage and attachment state in the late
runtime-facts layer. Preserve explicit export → attachment preparation → final
answer delivery; progress is not a receipt. Do not carry old prepared attachments
into a replacement turn.

The owner accepts tmpfs resource risk and explicitly rejected RAM-pressure
mitigations/browser intervention; normal task ownership/cleanup still applies.
No idle TTL, pressure eviction, host browser changes or disk-backed package cache
is introduced. Review disk usage read-only, including existing backups, and
provide a task-specific external audit digest/prompt. The separate external
auditor will be invoked manually by the owner. Normal independent PR reviews
remain required.
