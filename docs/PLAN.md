# Implementation and acceptance plan

This is the current planning index. Detailed implemented contracts and owner
instructions live in the feature documents linked from [AGENTS.md](../AGENTS.md).
The [newest-first design index](history/README.md) links the
[dated design history](history/PLAN_HISTORY.md), which preserves the complete earlier
plan, including researched sources, old branch references, superseded decisions
and acceptance criteria. Historical statements describe their date, not the
current checkout or running configuration.

## Current work and acceptance

- The owner requested two independent opt-in experiments (2026-09-26): readable
  attributed [conversation text](PROMPTS.md#experimental-conversation-text) for
  generation/compaction, and [Engram rolling memory](ENGRAMS.md). Existing bots
  keep structured input and no Engram grant. Engram history reduction is a second
  explicit opt-in, initially false even when the plugin is enabled. Start live
  memory trials on a disposable/empty-context bot; do not reduce Loki's existing
  context as part of implementation. Keep full operational metadata on disk.
  Before rollout, the owner requires a second independent context/compaction
  audit (2026-09-26), titled "Not losing things like tears in the rain". Hold
  context/compaction implementation changes and runtime deployment pending that
  review; Engram local validation may finish separately. The ignored `audit/`
  index links its brief and evidence. No live opt-in is implied by a test pass.

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

## Deferred product work

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
