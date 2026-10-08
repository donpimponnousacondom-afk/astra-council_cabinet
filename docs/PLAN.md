# Implementation and acceptance plan

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
  Follow the independent Luna review/CI/merge/restart workflow; broader process
  separation remains a measured follow-up, not part of this change. See
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
