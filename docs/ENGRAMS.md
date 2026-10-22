# Engram conversation memory · experimental

Engram is an optional non-tool plugin that lets a bot write bounded factual
conversation state alongside its ordinary final answer. It is globally disabled
by default and needs a per-bot grant. No existing bot is enabled automatically.
The separate [conversation text format](PROMPTS.md#experimental-conversation-text)
can be used without Engram.

## Setup and first trials

Enable **Plugins → Engram** and grant it under the chosen bot's **Capabilities**.
Plugin configuration supplies defaults, with optional per-bot overrides:

| Setting | Default | Allowed range / meaning |
| --- | --- | --- |
| State character limit | 8,000 | 1–128,000, combined MEM and FACTS |
| Recent messages | 12 | 1–1,000 acknowledged messages, plus all uncovered input |
| Reduce history | false | Explicit opt-in to replacing covered history with state |

The character limit is the only Engram memory-size ceiling. Count the decoded
MEM and FACTS string values together, including spaces and newlines; JSON keys,
escaping and the public answer do not count toward it. The prompt describes a
maximum, not a target to fill. Over-limit replacements are rejected intact;
previous memory and uncovered history remain available. There is no automatic
retry or truncation. Local `state_tokens` counts remain inspection diagnostics,
not an acceptance limit; provider output and overall context limits still apply.

On upgrade, the retired `state_token_limit` field is removed from saved global
Engram configuration and per-bot overrides, with an audit receipt. Character
budgets, other settings, memory and coverage are preserved. Older API clients
that submit the field must remove it; it is no longer advertised or accepted.
The unedited built-in prompt is upgraded by exact fingerprints of both shipped versions. Custom
prompts remain unchanged; their legacy `{state_token_limit}` placeholder resolves
to `unlimited`, but is no longer offered in the prompt catalog. Old missed-update
feedback labels a token rejection as a former limit that has now been removed.

Start with history reduction off, on an empty-context experimental bot. The bot
receives its state and writes updates, but ordinary transcript/compaction behavior
continues. Inspect results before enabling reduction. Both state output and the
visible answer consume the existing model profile's completion allowance; reserve
enough output for both. There is no second inference or background memory worker.
This experiment can increase cost on short conversations because each accepted update
rewrites the full state. If nothing useful has changed, the model may omit the
block and retain existing memory. It does not guarantee recall, speed or savings
for a particular model.

For the owner's future isolated memory trial, create a new empty-context bot in a
separate test conversation, with no global or private channel-memory grants or
imported summary. After checking state capture, explicitly enable **Reduce history**
and choose a small **Recent messages** value. That setting counts acknowledged
messages, not conversational rounds: all uncovered messages are still included,
so it is not a strict last-round-only cut. Keep **Conversation input**, **Engram
memory instructions** and **Engram conversation memory** enabled, with their
required placeholders intact. Reducing history through the plugin is the supported
way to isolate memory; disabling transcript or state layers prevents safe coverage
tracking. This trial is separate from conversation-format acceptance and does not
change Loki or any existing council bot.

Initial scope is ordinary room/thread/owner-DM turns. Slash and interactive-panel
invocations keep their fresh-context behavior and do not read/update Engram state.
Each bot/conversation has an independent state. This plugin does not make memory
global across rooms or grant access to another bot's data.

## State and prompts

**MEM** is the current situation, useful preferences, commitments and open work.
**FACTS** is durable attributed information, corrections and uncertainty. Both are
strings, replaced together by the model's complete next state. This is explicit
factual memory, not private chain-of-thought or vendor reasoning. Remembered text
is treated as fallible recollection, never as permission or verified authority.

The editable **Engram memory instructions** and **Engram conversation memory**
templates live in **Prompt library**, with ordinary per-bot overrides. They are
injected at the end of ordinary turn context. The state template must retain
`{engram_state}`, and both layers must remain enabled/nonempty while the plugin
is active; invalid configuration fails before a provider request. Fixed structural
protocol guidance follows those editable layers. It cannot be disabled by editing
the prose while retaining the feature. That fixed guidance requires durable
attribution by participant names and user IDs in MEM/FACTS, never temporary
transcript `P` labels: those labels can refer to different people on the next
request. This is a model instruction, not a semantic identity validator; inspect
state for misattribution during the trial.

Engram also requires the **complete selected transcript and retained summary** to
be represented in the actual request. A disabled conversation layer or a
`{latest_content}` / `{latest_message}`-only override that omits other selected
messages rejects the turn before inference, even with history reduction off.
Use the plugin's recent-message/history-reduction settings for its bounded-input
experiment. Latest-only prompt experiments remain available with Engram disabled.
This input-configuration rejection is distinct from an invalid returned state:
the latter may still deliver its safe ordinary answer without advancing memory.

When updating memory, the final completion consists of ordinary visible answer text followed by a
nonce-bound terminal block:

```text
Your ordinary answer.

[^ENGRAM:the-runtime-supplied-nonce]
{"MEM":"Complete next working memory","FACTS":"Complete next factual memory"}
[^END:the-runtime-supplied-nonce]
```

The model must use the nonce supplied in its actual request, not the example.
For a terminal `discord_attach` reply, the same answer and private block may instead
be supplied in its `message` argument. This is a final completion in every response
mode; it needs no separate assistant answer or `end_cycle`. The private block is
excluded from public tool evidence and validation errors, and memory advances only
after confirmed delivery, just as for an ordinary answer.

The private block is removed before Discord delivery. It is not a message-footer
feature and does not alter the existing diagnostic footer. Intermediate tool-call
responses, provider retries, compaction and intentional silence do not advance
state. Quoted examples are not executable memory writes. Missing, ambiguous,
malformed, oversized or truncated state cannot advance its coverage checkpoint.
An unambiguous visible answer prefix may still be delivered when an update is
missing or rejected; `engram.update_skipped` records the reason. No extra repair
inference is started for a missing memory update. Private suffix data remains
withheld, and uncovered input stays eligible for later turns.

### Feedback when memory stays unchanged

After a confirmed ordinary reply with a missing or invalid state block, the next
ordinary turn receives a private **user-role** reminder after the Engram protocol
at the end of the assembled context. It reports the count of tracked, successfully
delivered replies without an accepted replacement and the latest format/limit
result. The stable prompt prefix is unchanged; there is no dynamic timestamp,
new model call, tool or dashboard control. The generated layer and count are
inspectable in the owner's stored request evidence. They are not transcript,
compaction input or saved MEM/FACTS, and are never sent directly to Discord.

Missing state can be deliberate when there is nothing useful to preserve. The
reminder asks the model to review whether an update is needed; it does not demand
invented changes or classify omission as disobedience. There is no separate
"keep existing memory" acknowledgement. Omitting the block retains old memory
and leaves the reminder active; a valid complete replacement (even unchanged)
clears it after commit. Invalid JSON, markers and budget failures include an
actionable reason. Any public joke, sign-off or model-written footer belongs
before the private block; nothing may follow its END marker.

Counts are scoped to bot, conversation, memory epoch and state revision. They
start with replies tracked by this implementation; old event logs are not
backfilled or prompt snapshots rescanned. A small indexed `engram_misses` table
ties each rejected final to its request and intended ordinary outbox receipt.
Only a matching confirmed send counts, including written replies whose later
voice companion was cancelled. Tool rounds, provider retries/failures, filtered
or length-truncated output, silence, routed tool sends, failed/uncertain sends
and slash/panel invocations do not count. Durable receipts make restart counting
idempotent. An accepted state replacement or applicable memory reset ends the
previous count; late old-epoch receipts cannot resurrect it. Disabling Engram
suppresses its prompt layers, including this reminder, while keeping stored data.

## Retention and delivery

With reduction enabled, keep the configured recent tail of acknowledged input
plus every message newer than the state coverage boundary. Thus a busy room may
supply more than 12 messages. A hard last-12 slice could lose messages the model
never saw; this experiment deliberately does not do that. Normal request capacity
checks remain in force and fail visibly when uncovered input cannot fit.

A saved compaction summary remains necessary until an accepted Engram has seen
and covered that summary. Engram coverage never advances the ordinary compaction
checkpoint. Turning off reduction or the plugin restores the normal context path;
original message records, compaction history and request evidence remain on disk.

Candidate state is tied to the input actually supplied, starting state revision,
turn/request and delivery outcome. Only valid state with a confirmed normal answer
delivery becomes committed state. Cancellation, stale revisions, revoked grants,
failed sends and uncertain Discord delivery do not silently advance memory.
Public progress messages accompanying tools do not commit state or count as
delivered final replies for reminders. Their private suffix, if any, is stripped;
the model must supply any state update with its final answer. In opt-in
`end_cycle` mode this is the ordinary text accompanying a validated `end_cycle`
call, after any permitted companions succeed. There is also the approved
finish-first order: if a valid `end_cycle {}` arrived without an answer or draft,
the harness permits one tool-free request for the first public answer. Append
any Engram update to that answer without another tool call. Staging verifies the
accepted preceding finish request/call, the `cycle.answer_pending` linkage and
the completed public-answer request; `cycle.answer_published` records the
publication linkage. The answer request is the state and coverage source, not
the earlier empty finish. Neither private reasoning nor incomplete output can
supply an answer or state update.

Text with action calls but no `end_cycle` is intermediate narration, not an
answer draft. Its text and any private suffix are never frozen for later
publication or state commit. The actions run once; bounded finalization can
then produce a final answer from their results. That final answer request is
the Engram and coverage source. A valid bare finish during this no-draft phase
may instead use the same bounded first-answer path described above.

A text-only answer missing `end_cycle`, or answer text accompanying a recoverable
failed `end_cycle` batch, freezes the answer and its parsed Engram together.
A later valid `end_cycle {}` confirms this exact draft; it cannot replace the
answer or its Engram. Staging verifies both completed requests, the recorded
draft hash and the confirmation call. State and context coverage come from the
original answer request, including when it had permitted finish companions.
The confirmation request supplies only permission to publish. A failed companion
or a response with no usable public answer cannot enter this confirmation path.
Both `stop` and `tool_calls` complete terminal boundaries are supported, while
intermediate tools, unrecovered rejected terminal batches, failed companions and
partial provider output cannot commit state. The same confirmed-delivery and miss-counting
rules apply; an `end_cycle` call never goes inside the private state block.
Restart recovery may promote an already confirmed candidate; it does not resend
an uncertain answer. State and coverage move together on the SQLite owner thread.

## Owner controls and privacy

The bot's **Control** tab provides saved Engram inspection and an explicit reset.
Reset requires the bot ID and invalidates in-flight state; the bot's broader
**Forget everything before now** control clears this conversation-state layer too.
Existing private/global notes remain governed by their own plugins.

Full snapshots include Engram state and candidates in SQLite. A selective restore
that includes conversation context invalidates Engrams for the affected bot/channel
instead of combining a restored summary with a different memory checkpoint.
Notes-only selective restores leave Engrams intact. This also handles snapshots
created before the plugin existed, without assuming they contain its tables.

Owner API routes:

- `GET /api/engrams/{bot_id}` lists state; optional `channel_id` selects a conversation.
- `POST /api/engrams/{bot_id}/reset` accepts `confirm_bot_id` and an optional `channel_id`.

State is withheld from public Discord and other bots' model-readable inspection.
The configured provider and authenticated owner can still see the state: it is
part of that bot's prompt. Private diagnostics retain response evidence. This
does not guarantee the model never mentions a remembered fact in its visible
answer; prompts and conversation scope still matter.

## Research basis and limits

The practical precedent is writable working memory and bounded conversation
context, as explored in [MemGPT](https://arxiv.org/abs/2310.08560),
[Letta memory blocks](https://docs.letta.com/v1-sdk/memory/memory-blocks), and
[MemoryBank](https://arxiv.org/abs/2305.10250). This experiment is a smaller,
same-response state-replacement design, not an implementation of those systems.
DeepSeek's [Engram conditional-memory architecture](https://arxiv.org/abs/2601.07372)
is a different model-internal lookup mechanism; this plugin does not modify model
weights or implement that architecture.

## Owner decisions (2026-09-26)

- Implement readable transcripts and Engram as independent opt-in experiments.
  Preserve complete harness metadata on disk and the normal plain-text reply path.
- Keep Engram disabled on existing bots. Test on empty context before trusting
  history reduction; do not use implementation as permission to reduce Loki's context.
- The owner will supply/refine their own Engram prompt later. Keep its factual
  memory guidance editable, with strict state/delivery validation in code.
- Evaluate Engram separately on a fresh bot without other memory grants, using
  explicit history reduction and a small recent-message tail while preserving all
  uncovered input. Do not disable the required input/state layers to mimic this.
- Loki is an alpha tester, not a special runtime case. Every eligible bot can use
  the same plugin with explicit grants; stable council bots retain their settings.

### Private update feedback (2026-10-08)

- Add a factual reminder after missed updates, counting delivered ordinary
  replies rather than provider/tool rounds. Report current evidence, not guessed
  causes or unrelated historical provider failures.
- The owner permits deliberately keeping memory unchanged when there is nothing
  useful to preserve. Reminders request a review, not a mandatory rewrite. Memory
  remains complete replacement, never automatic accumulation or append.
- Preserve public jokes/footers before the private terminal block. Do not alter
  the owner's editable personalities, add automatic repair inference or enable
  history reduction as part of this feedback change.

### Pending human requests and history reduction (2026-10-06)

Coverage records which messages were observed while forming Engram state; it does not mean every human in that context was answered. Reduction preserves all messages beyond the bot’s handled cursor, including queued humans’ original questions, even if a previous turn’s Engram state covers them. The ordinary recent-message tail still applies to covered, handled messages. See [human priority](OPERATIONS.md) for the per-human queue.

### Character-only memory budget (2026-10-14)

The owner retired Engram's token ceiling and token-budget instructions. Models
share these prompts but cannot reliably count a chosen tokenizer's tokens while
writing. Keep the existing character budget as the sole state-size setting,
without converting it to words or changing saved capacities. Token measurement
may remain diagnostic. This does not change provider completion allowances,
context planning, compaction, grants, or the owner's customized prompt text.
