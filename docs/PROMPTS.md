# Editable context and clean-slate controls

The modern dashboard owns these controls. The legacy UI remains frozen. Prompt text controls do not grant permissions or change provider JSON, plugin grants, memory quotas, owner authorization or delivery rules.

## Prompt library

Generated instructions are ordinary persisted prompt records, with a **Message role** (`system` or `user`) and **Prompt placement**. The application seeds missing `runtime-*` defaults at startup, once per identifier; subsequent startups never overwrite edits. Existing bots default to all applicable layers enabled and no overrides. Existing personas, selected shared prompts and dynamic prompt tails remain their current sources through editable wrapper templates.

Edit a default to change its inheriting bots. To specialize one bot, create/clone a template, select the same placement, then choose it under **Bots → bot → Prompts → Injected prompt layers**. Uncheck a layer to omit it. The bulk switches affect generated layers; additional shared prompts retain their separate selection/order. Empty text also omits a layer. A missing or incorrectly placed override fails explicitly instead of silently injecting a fallback.

### Inspecting a bot's prompt composition

In **Bots → bot → Prompts**, click a generated layer's name to expand its
read-only inspector. It shows the selected saved template (including operator
edits), role, ID/revision, default variants or per-bot override, conditions, and
referenced personality/global/dynamic source text. Personality and dynamic sources
reflect the current unsaved draft. Inspection never saves a draft or loads live
conversation/private-note content; **Trajectory** remains the evidence of the
exact request sent to a provider.

The compact list follows assembly placement, not the alphabetical library order:
shared generated instructions → selected additional shared prompts in their saved
order → personality → invocation guidance and conversation input → accumulated
assistant/tool exchanges and any repair → channel/global memory notes and budgets
→ dynamic tail and wake-up context. Compaction layers appear separately in their
own request order. Adjacent text of the same role may be combined into one provider
message. Memory and runtime tails each begin a new message so changing content is
not folded into earlier input or exchanges. Inspection follows this runtime order.

Owner decision, 2026-10-14: memory writes must not change the prefix before a long
conversation. Notes and budgets remain fresh each round, with the same roles,
scope, attribution and editable templates, but are placed after tool history.
Advertised memory/research descriptions omit live usage and capacity counters;
memory-budget layers and runtime facts supply those counters at the tail, and
`{}`/error help remains current. Research facts are included only while its tool
is available in that round; finalization does not advertise removed tools through
stale status. No provider cache lifetime or hit rate is guaranteed by this order.

Checked means the layer is permitted **when applicable**, not necessarily
injected. **Eligible** means configuration allows it; **Turn only** still requires
the indicated event or context. **Not applicable** explains a role, image setting,
missing plugin grant or global plugin disablement. **Disabled** is an unchecked
layer; **Empty** means the template/configured source emits nothing. Conditions
still apply to overrides. Memory-budget variants are both shown because the
current conversation/usage is not selected in this editor. Plugin/runtime scope
and current saved grants are rechecked during actual execution.

### Response protocol guidance

**Bots → bot → Capabilities → Response protocol** chooses `narrate` (default,
including older records), opt-in `progress`, or opt-in `end_cycle`. This runtime
setting controls publication; editing or disabling prompt text does not override
it. The current delivery contract and limits are in
[TOOLS.md](TOOLS.md#normal-and-extended-turns).

The `response_protocol` layer automatically selects
`runtime-response-protocol-narrate`, `runtime-response-protocol-progress` or
`runtime-response-protocol-end_cycle-v4`. The versioned end-cycle default renders
`narration_strikes` as the maximum failed responses. Text with action calls but
no finish is narration: run the actions once, count a strike and finalize from
their results with action tools removed. That narration is never an answer
draft. Text-only replies and recoverable failed finish batches retain their
original answer, to be confirmed rather than regenerated. A valid finish with no answer
or draft permits one tool-free request for the first public answer and does not
count as a strike. That request accepts only public answer text: empty text,
tool calls or incomplete output stop without retry. Failed companions also stop
the cycle. `end_cycle {}` is explicitly an executing decision, not tool discovery.

The new identifier seeds once without replacing saved legacy protocol records,
including the previous v2 and v3 contracts.
Explicit bot overrides and disabled layers remain authoritative for prompt text;
runtime gates still apply. Unedited shipped identity/tool-guidance text is
upgraded only on an exact previous-content fingerprint, including the interim
draft-only guidance; custom edits are preserved. Operators using overrides should compare their wording with the
current protocol. The removed `publish_retained_draft` setting remains only as
a deprecated custom-template placeholder with the constant value `True`, to
avoid unresolved placeholders in saved owner text. It cannot disable retention.

The modern inspector follows the unsaved mode and shows the resolved cap in the
response-protocol text. `{{RESPONSE_PROTOCOL}}` and
`{{MAX_PROGRESS_MESSAGES_PER_TURN}}` are literal configuration substitutions;
the runtime-facts object also reports their effective values. They describe the
configured cap, not remaining attempts. Private withheld/delivery feedback is
added after tool results and deduplicated by reason within the turn; it is not
an editable system instruction or a public progress message.

### Experimental conversation text

**Bots → bot → Prompts → Conversation format** selects either the existing
**Structured records** (default) or **Conversation text · experimental**. This is
a presentation choice for generation and compaction, independent of the
[Engram experiment](ENGRAMS.md). Fresh slash/panel invocations also use the chosen
format for their supplied message; this does not add channel history or Engram
state to those routes. Existing bot settings are not migrated.

Conversation text uses a participant legend with verified user IDs, compact
speaker/recipient labels, one message reference, dates grouped by day/UTC offset,
and times displayed to seconds. Message bodies are quoted as untrusted conversation
content. Replies retain the target ID and an earlier excerpt when the target is
outside the supplied batch; an already visible parent's body is not repeated.
Role-directed mentions and unresolved recipients remain explicit. Actual image
parts, attachment links and image availability/resize notices remain intact.
Capture failures include their reason. External bots/webhooks remain identified,
the viewing bot is marked "you", and routed posts retain their mode/source bot.
Temporary participant labels must not be carried into replies, summaries or
Engram state; use names and user IDs where names are ambiguous. This is model
guidance, not proof that a particular model will preserve attribution correctly.
Line separators are normalized before quoting and nonprinting control bytes are
removed from displayed text; original message content remains stored unchanged.
Repeated sequence/age fields, null/boolean flags and cached image hashes are not
printed in the conversation text. Stored Discord records and historical requests
retain the full metadata and original timestamp precision.

The format selects editable `runtime-transcript-conversation` and
`runtime-compaction-instructions-conversation` default variants. Explicit per-bot
template overrides still win. `{transcript}` expands to the selected presentation;
`{latest_message}` remains the structured latest record, while `{latest_content}`
remains just its text. The new compaction guidance asks for a concise continuity
note with relevant facts, attribution, corrections and open work, and retains
dates/references when useful instead of copying routine metadata. Existing edited
templates are not overwritten. Token planning measures the selected text, including
compaction batch fitting; switching back restores structured generation immediately.

If an experimental template was already seeded before a wording update, compare
and edit its saved text in Prompt library; startup never overwrites owner edits.
Signed attachment URLs remain intact so granted tools can use their original
references. Removing those URLs and format-specific token calibration are separate
follow-ups, not part of this presentation change.

This changes no history boundary or summary by itself. Subsequent compactions can
gradually replace existing summaries; it does not reset context or rewrite old
notes. Compaction still receives no image pixels, and fresh image inputs still
survive compaction within their current generation turn. See [VISION](VISION.md).

### Placement reference

| Layer | Source and conditions |
| --- | --- |
| `identity` | Bot/Discord identity, addressing and ordinary assistant response instructions |
| `tool_guidance` | Empty-call usage, repair, tool rounds and task prerequisites |
| `response_protocol` | Mode-specific narrate/progress/end_cycle default; configured cap or strike settings and final-answer guidance; explicit overrides remain authoritative for text only |
| `image_guidance`, `image_disabled` | General image guidance; additional guidance when image inputs are off |
| `director` | Hortator-only instructions |
| `universal`, `persona` | Existing global prompt and per-bot personality |
| `silence_policy` | Allowed/disabled variant according to the bot's actual silence capability |
| `slash_invocation`, `panel_invocation` | Fresh invocation guidance, only for its corresponding entry route |
| `summary`, `transcript` | Retained conversation summary and current transcript input |
| Assistant/tool exchanges and `reply_repair` | Existing tool history and bounded repair of malformed tool-wrapped answers |
| `memory_budget`, `memory` | Fresh channel quota guidance and notes after exchanges, only with the memory grant |
| `global_memory_budget`, `global_memory` | Fresh bot-private cross-channel quota guidance and notes after exchanges, only with that grant |
| `runtime_facts`, `dynamic_prompt` | Clock, round/capability/activation facts and the existing per-bot tail |
| `scheduled_alarm`, `background_completion`, `background_updates` | Applicable wake-up and job context |
| `engram_instructions`, `engram_state` | Eligible ordinary conversations only; followed by the fixed Engram protocol and any feedback |
| `compaction_instructions`, `compaction_summary`, `compaction_transcript` | Separate summary request: guidance, retained summary, then transcript |

Budget guidance has normal/over-budget variants. An explicit override replaces either variant; include the usage placeholders if the custom template should still report them. Conditional layers remain conditional even with an override. Turning off guidance never bypasses the corresponding backend restriction. Turning off note injection does not erase notes or revoke the memory tool.

## Literal data placeholders

The canonical spelling is uppercase with double braces, matching Discord footers:
`{{NOW}}`, `{{BOT_NAME}}`, `{{MODEL}}`, and so on. The backend shares its double-brace
matcher and case normalization with the footer. Whitespace/case variants such as
`{{ now }}` are accepted. Existing single-brace fields such as `{now}` keep their
original behavior; saved prompts and built-in defaults are not rewritten.
Footer and prompt values remain separate: TPS/TTFT only exist after a response.

Substitution is one pass over the template, never code evaluation. Data containing braces is not processed as another template. Unknown placeholders remain literal. Additional shared prompts keep literal content; substitutions apply to generated placements and the existing dynamic tail.

Common generated-template fields: `{{BOT_NAME}}`, `{{BOT_ID}}`, `{{DISCORD_USER_ID}}`, `{{BOSS_ID}}`, `{{TIMEZONE}}`. Bot identity comes from verified runtime/configuration, not conversation claims. Placement-specific fields:

- Universal/persona: `{{GLOBAL_PROMPT}}`, `{{PERSONA}}`.
- Response controls: `{{RESPONSE_PROTOCOL}}`, `{{MAX_PROGRESS_MESSAGES_PER_TURN}}`; also included in runtime facts.
- Notes: `{{NOTES}}`; budget guidance: `{{USED_CHARS}}`, `{{LIMIT_CHARS}}`, `{{REMAINING_CHARS}}`, `{{NOTE_LIMIT_CHARS}}`, `{{HARD_LIMIT_CHARS}}`, `{{OVER_BUDGET_CHARS}}`.
- Summary wrappers: `{{SUMMARY}}`. Compaction instructions additionally use `{{SUMMARY_TOKENS}}` (retained visible text, not combined reasoning/output).
- Conversation: `{{TRANSCRIPT}}` for all selected attributed records, `{{LATEST_MESSAGE}}` for the last attributed record, or `{{LATEST_CONTENT}}` for its text only. `{{IMAGE_OMISSIONS}}` carries selected-input omissions. A text-only wrapper intentionally removes author/recipient metadata; choose the attributed record when that matters.
- Runtime tail: `{{RUNTIME_FACTS}}` for the complete fact object; or individual `{{MODEL}}`, `{{NOW}}`, `{{CHANNEL_ID}}`, `{{ROUND}}`, `{{ROUNDS_REMAINING}}`, `{{CONTEXT_TOKENS}}`, `{{CONTEXT_WINDOW}}`, `{{SECONDS_SINCE_LAST_MESSAGE}}`. Existing `{{DYNAMIC_PROMPT}}` is the rendered per-bot tail.
- Slash guidance: `{{INVOCATION}}` is safe invocation metadata, never the interaction token.

`{{MODEL}}` uses the same spelling and requested-model source as the Discord
footer. It is available in the per-bot dynamic tail and the `runtime_facts` /
`dynamic_prompt` templates. Previously unknown `{model}` / `{MODEL}` remain literal; use `{{MODEL}}`. Runtime facts
also include `model` automatically when that layer and its fact object are
included. The value is the exact model slug from the request's captured profile,
not a fresh lookup of the bot's assignment or an inferred upstream architecture.
It does not establish vision support or disclose a proxy's private routing.

Example bot tail:

> You are currently using {{MODEL}} for this response. Boss may change models
> between turns, and provider routing or fail-over may use a different back-end.
> Earlier messages may therefore mention another model; that does not change
> who you are or whose memories these are. Treat this as the configured model,
> and don't guess a back-end the provider hasn't disclosed.

These facts stay after conversation input and tool exchanges. Changing the slug
does not edit the earlier personality/shared prompt prefix; actual provider cache
reuse remains provider-dependent, especially across different models. Ordinary,
slash and panel turns share this rendering. Layer switches/overrides still apply;
no hidden identity layer is added when these are disabled. Stored compaction
templates/memories and public-footer behavior are unchanged; generated templates
also accept the new spelling, with the same source-coverage checks.

For a minimal text experiment, create a `transcript` template containing `{{LATEST_CONTENT}}` with role `user`. Select that override on the experimental bot, disable the other generated layers, and deselect additional shared prompts you do not want. Use that bot's existing plugin, image and silence controls to omit tool schemas or pixels. This does not edit its underlying shared model profile.

Run that latest-only experiment with [Engram](ENGRAMS.md) disabled. Engram requires
all selected messages and the retained summary in the actual request so its
coverage cannot acknowledge omitted input; use its own retention controls instead.

Actual assistant/tool exchanges, native continuation fields, tool schemas and returned tool results remain protocol/data, not editable instruction templates. Granted tools still carry their schemas and complete error/usage feedback. An entirely empty request fails locally before a paid provider call. Trajectory records capture the actual ordered messages, enabled templates/roles/revisions/hashes and omitted layer keys; disabled stored summaries are not mislabeled as injected summaries.

Compaction source wrappers must include `{{TRANSCRIPT}}` and, when there is an existing summary, `{{SUMMARY}}` in the corresponding placement (existing single-brace spellings also work). Disabling or omitting those inputs pauses compaction with an explicit error and preserves its old checkpoint; it never silently discards accumulated history. Compaction instructions can be independently edited/disabled. Template rendering and measured budgets use the actual selected roles and text. Fresh image selections remain valid across text compaction for the current turn, as before.

The [one-use failed-response notice](RESPONSE_RECOVERY.md) is diagnostic protocol
data appended after all assembled layers, always as a separate user-role message.
It is not a new persisted prompt, speaker record or memory layer. Its fixed label
distinguishes quoted incomplete evidence from human instructions. It is supplied
only on an eligible first generation round and kept out of compaction inputs.

## Forget everything before now

**Bots → bot → Control → Forget everything before now** sets a durable bot-specific history boundary at the current time. Default scope is all channels, including future assignments; an existing channel can be selected instead. Channel choices share the [dashboard conversation labels](DASHBOARD.md): configured room names or cached observed Discord names, parent/thread labels, and the immutable ID for disambiguation. Missing names remain explicitly unknown; no Discord lookup is performed while displaying or selecting a scope. Type the bot's stable ID and confirm the warning. Save/discard configuration drafts first. No snapshot restore is performed and no other bot is reset.

The action blocks new work for the target while cancelling/joining its active turn, then atomically clears the selected retained summaries/token estimates/compaction counters, advances the observed-message checkpoint and stores a sequence plus original-message-time cutoff. All senders' earlier messages are excluded. History backfilled later, edits, reconnects and process restarts cannot revive them. Replies keep recipient identity but omit pre-cutoff quoted previews. A pending slash acknowledgement from before an all-channel cutoff cannot start old work after the reset; newly invoked slash requests remain fresh.

Private channel notes and global notes **survive** and remain injected according to their grants/layer settings. Prompts, configuration, credentials, shared Discord history, old request/tool evidence and files also survive. Explicitly fetched history or newly quoted old text can reintroduce information, just as a person could repeat it in a new message. The cutoff governs automatic conversation intake; it is not a ban on fetching public pages or previously saved files. Already delivered messages and uncertain in-flight sends cannot be recalled by this action.

`context_resets` stores boundaries in the external SQLite database; `*` means the bot's all-channel default and an explicit channel row overrides that default. The owner API generates the cutoff itself; it accepts no arbitrary historical/future timestamp. The action emits `context.reset` with bot, scope, cutoff and consequence. Status includes each bot's boundaries. Full snapshots include them; selective context restore restores the matching effective boundary along with checkpoints. Notes-only recovery does not alter them. Existing snapshot version/schema compatibility checks still apply.

## Owner decisions (preserved from AGENTS, 2026-09-23)

The date marks relocation of standing instructions, not a new product decision.
Existing decision dates and qualifications below remain authoritative.

- On 2026-10-05 the owner requested the configured request model in late runtime
  facts and the editable dynamic tail, using the footer's exact `{{MODEL}}`
  spelling, then requested uppercase double braces for every dynamic placeholder
  and reuse of footer parsing where possible, while preserving saved single-brace
  templates. Describe the current engine without redefining the bot's identity,
  guessing undisclosed routing/capabilities or changing its personality prefix.

- Context must identify each bot by stable ID and verified Discord user ID, and explicitly label actual reply/mention recipients per viewer. Human messages directed elsewhere remain shared background context, not independent wakeups for every bot. Preserve speaker/recipient attribution through compaction and memory guidance; never infer recipients or human authority from message text. Do not silently rewrite existing private memories when changing this contract.

- Runtime-generated system/user instruction and input layers are persisted editable prompts. Keep per-bot layer switches and placement-specific overrides, one-pass literal substitution, actual request evidence, and current defaults. Disabling text must never bypass permissions, budgets or protocol validation; never silently replace a missing override. Compaction must preserve its old checkpoint if required source wrappers are disabled/omit history. See [docs/PROMPTS.md](PROMPTS.md).

- The owner approved shared image-guidance wording for all bots (2026-09-19), not a Loki-only override. Distinguish images absent now from earlier attributed observations; discourage routine availability disclaimers and permanent memory rules inferred from temporary absence. Keep image selection/retention and existing layer switches unchanged. Leave bot memory cleanup to the bots under their configured quotas; do not rewrite their notes as part of prompt wording changes.

- The owner's clean-slate control means **ignore everything before now for one bot**, with private/global notes surviving. Cancel/join active work, persist original-message-time and sequence cutoffs, exclude backfilled history/old reply previews, and preserve other bots/shared evidence/configuration. Do not perform a live reset for testing. This is distinct from memory deletion and snapshot recovery; expose it only as an explicit owner Control-tab/API operation.
