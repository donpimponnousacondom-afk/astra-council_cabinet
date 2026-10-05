# Implementation and acceptance plan

This is the current planning index. Detailed implemented contracts and owner
instructions live in the feature documents linked from [AGENTS.md](../AGENTS.md).
The [newest-first design index](history/README.md) links the
[dated design history](history/PLAN_HISTORY.md), which preserves the complete earlier
plan, including researched sources, old branch references, superseded decisions
and acceptance criteria. Historical statements describe their date, not the
current checkout or running configuration.

## Current work and acceptance

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
