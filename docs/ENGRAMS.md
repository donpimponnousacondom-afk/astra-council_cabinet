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
| State token limit | 2,048 | 1–32,000, local cl100k_base accounting |
| Recent messages | 12 | 1–1,000 acknowledged messages, plus all uncovered input |
| Reduce history | false | Explicit opt-in to replacing covered history with state |

Start with history reduction off, on an empty-context experimental bot. The bot
receives its state and writes updates, but ordinary transcript/compaction behavior
continues. Inspect results before enabling reduction. Both state output and the
visible answer consume the existing model profile's completion allowance; reserve
enough output for both. There is no second inference or background memory worker.
This experiment can increase cost on short conversations because each final answer
rewrites state. It does not guarantee recall, speed or savings for a particular model.

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
the prose while retaining the feature.

The final completion consists of ordinary visible answer text followed by a
nonce-bound terminal block:

```text
Your ordinary answer.

[^ENGRAM:the-runtime-supplied-nonce]
{"MEM":"Complete next working memory","FACTS":"Complete next factual memory"}
[^END:the-runtime-supplied-nonce]
```

The model must use the nonce supplied in its actual request, not the example.
The private block is removed before Discord delivery. It is not a message-footer
feature and does not alter the existing diagnostic footer. Intermediate tool-call
responses, provider retries, compaction and intentional silence do not advance
state. Quoted examples are not executable memory writes. Missing, ambiguous,
malformed, oversized or truncated state cannot advance its coverage checkpoint.
An unambiguous visible answer prefix may still be delivered when an update is
missing or rejected; `engram.update_skipped` records the reason. No extra repair
inference is started for a missing memory update. Private suffix data remains
withheld, and uncovered input stays eligible for later turns.

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
- Loki is an alpha tester, not a special runtime case. Every eligible bot can use
  the same plugin with explicit grants; stable council bots retain their settings.
