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
order → personality and memory → invocation guidance and conversation input →
accumulated assistant/tool exchanges and any repair → dynamic tail and wake-up
context. Compaction layers appear separately in their own request order. Adjacent
text of the same role may be combined into one provider message. Inspection does
not change this existing runtime order.

Checked means the layer is permitted **when applicable**, not necessarily
injected. **Eligible** means configuration allows it; **Turn only** still requires
the indicated event or context. **Not applicable** explains a role, image setting,
missing plugin grant or global plugin disablement. **Disabled** is an unchecked
layer; **Empty** means the template/configured source emits nothing. Conditions
still apply to overrides. Memory-budget variants are both shown because the
current conversation/usage is not selected in this editor. Plugin/runtime scope
and current saved grants are rechecked during actual execution.

### Placement reference

| Layer | Source and conditions |
| --- | --- |
| `identity` | Bot/Discord identity, addressing and ordinary assistant response instructions |
| `tool_guidance` | Empty-call usage, repair, tool rounds and task prerequisites |
| `image_guidance`, `image_disabled` | General image guidance; additional guidance when image inputs are off |
| `director` | Hortator-only instructions |
| `universal`, `persona` | Existing global prompt and per-bot personality |
| `silence_policy` | Allowed/disabled variant according to the bot's actual silence capability |
| `memory_budget`, `memory` | Channel quota guidance and notes, only with the memory grant |
| `global_memory_budget`, `global_memory` | Bot-private cross-channel quota guidance and notes, only with that grant |
| `summary`, `transcript` | Retained conversation summary and current transcript input |
| `runtime_facts`, `dynamic_prompt` | Clock, round/capability/activation facts and the existing per-bot tail |
| `slash_invocation` | Fresh slash invocation guidance |
| `reply_repair` | The existing single bounded repair of malformed tool-wrapped answers |
| `compaction_instructions`, `compaction_summary`, `compaction_transcript` | Summary-request guidance and input wrappers |

Budget guidance has normal/over-budget variants. An explicit override replaces either variant; include the usage placeholders if the custom template should still report them. Conditional layers remain conditional even with an override. Turning off guidance never bypasses the corresponding backend restriction. Turning off note injection does not erase notes or revoke the memory tool.

## Literal data placeholders

Substitution is one pass over the template, never code evaluation. Data containing braces is not processed as another template. Unknown placeholders remain literal. Additional shared prompts keep literal content; substitutions apply to generated placements and the existing dynamic tail.

Common generated-template fields: `{bot_name}`, `{bot_id}`, `{discord_user_id}`, `{boss_id}`, `{timezone}`. Bot identity comes from verified runtime/configuration, not conversation claims. Placement-specific fields:

- Universal/persona: `{global_prompt}`, `{persona}`.
- Notes: `{notes}`; budget guidance: `{used_chars}`, `{limit_chars}`, `{remaining_chars}`, `{note_limit_chars}`, `{hard_limit_chars}`, `{over_budget_chars}`.
- Summary wrappers: `{summary}`. Compaction instructions additionally use `{summary_tokens}` (retained visible text, not combined reasoning/output).
- Conversation: `{transcript}` for all selected attributed records, `{latest_message}` for the last attributed record, or `{latest_content}` for its text only. `{image_omissions}` carries selected-input omissions. A text-only wrapper intentionally removes author/recipient metadata; choose the attributed record when that matters.
- Runtime tail: `{runtime_facts}` for the complete fact object; or individual `{now}`, `{channel_id}`, `{round}`, `{rounds_remaining}`, `{context_tokens}`, `{context_window}`, `{seconds_since_last_message}`. Existing `{dynamic_prompt}` is the rendered per-bot tail.
- Slash guidance: `{invocation}` is safe invocation metadata, never the interaction token.

For a minimal text experiment, create a `transcript` template containing `{latest_content}` with role `user`. Select that override on the experimental bot, disable the other generated layers, and deselect additional shared prompts you do not want. Use that bot's existing plugin, image and silence controls to omit tool schemas or pixels. This does not edit its underlying shared model profile.

Actual assistant/tool exchanges, native continuation fields, tool schemas and returned tool results remain protocol/data, not editable instruction templates. Granted tools still carry their schemas and complete error/usage feedback. An entirely empty request fails locally before a paid provider call. Trajectory records capture the actual ordered messages, enabled templates/roles/revisions/hashes and omitted layer keys; disabled stored summaries are not mislabeled as injected summaries.

Compaction source wrappers must include `{transcript}` and, when there is an existing summary, `{summary}` in the corresponding placement. Disabling or omitting those inputs pauses compaction with an explicit error and preserves its old checkpoint; it never silently discards accumulated history. Compaction instructions can be independently edited/disabled. Template rendering and measured budgets use the actual selected roles and text. Fresh image selections remain valid across text compaction for the current turn, as before.

## Forget everything before now

**Bots → bot → Control → Forget everything before now** sets a durable bot-specific history boundary at the current time. Default scope is all channels, including future assignments; an existing channel can be selected instead. Channel choices use the configured **Rooms** name for the matching channel ID, with the raw ID as fallback and secondary detail. There are no Discord name lookups or cache dependencies. Type the bot's stable ID and confirm the warning. Save/discard configuration drafts first. No snapshot restore is performed and no other bot is reset.

The action blocks new work for the target while cancelling/joining its active turn, then atomically clears the selected retained summaries/token estimates/compaction counters, advances the observed-message checkpoint and stores a sequence plus original-message-time cutoff. All senders' earlier messages are excluded. History backfilled later, edits, reconnects and process restarts cannot revive them. Replies keep recipient identity but omit pre-cutoff quoted previews. A pending slash acknowledgement from before an all-channel cutoff cannot start old work after the reset; newly invoked slash requests remain fresh.

Private channel notes and global notes **survive** and remain injected according to their grants/layer settings. Prompts, configuration, credentials, shared Discord history, old request/tool evidence and files also survive. Explicitly fetched history or newly quoted old text can reintroduce information, just as a person could repeat it in a new message. The cutoff governs automatic conversation intake; it is not a ban on fetching public pages or previously saved files. Already delivered messages and uncertain in-flight sends cannot be recalled by this action.

`context_resets` stores boundaries in the external SQLite database; `*` means the bot's all-channel default and an explicit channel row overrides that default. The owner API generates the cutoff itself; it accepts no arbitrary historical/future timestamp. The action emits `context.reset` with bot, scope, cutoff and consequence. Status includes each bot's boundaries. Full snapshots include them; selective context restore restores the matching effective boundary along with checkpoints. Notes-only recovery does not alter them. Existing snapshot version/schema compatibility checks still apply.

## Owner decisions (preserved from AGENTS, 2026-09-23)

The date marks relocation of standing instructions, not a new product decision.
Existing decision dates and qualifications below remain authoritative.

- Context must identify each bot by stable ID and verified Discord user ID, and explicitly label actual reply/mention recipients per viewer. Human messages directed elsewhere remain shared background context, not independent wakeups for every bot. Preserve speaker/recipient attribution through compaction and memory guidance; never infer recipients or human authority from message text. Do not silently rewrite existing private memories when changing this contract.

- Runtime-generated system/user instruction and input layers are persisted editable prompts. Keep per-bot layer switches and placement-specific overrides, one-pass literal substitution, actual request evidence, and current defaults. Disabling text must never bypass permissions, budgets or protocol validation; never silently replace a missing override. Compaction must preserve its old checkpoint if required source wrappers are disabled/omit history. See [docs/PROMPTS.md](PROMPTS.md).

- The owner approved shared image-guidance wording for all bots (2026-09-19), not a Loki-only override. Distinguish images absent now from earlier attributed observations; discourage routine availability disclaimers and permanent memory rules inferred from temporary absence. Keep image selection/retention and existing layer switches unchanged. Leave bot memory cleanup to the bots under their configured quotas; do not rewrite their notes as part of prompt wording changes.

- The owner's clean-slate control means **ignore everything before now for one bot**, with private/global notes surviving. Cancel/join active work, persist original-message-time and sequence cutoffs, exclude backfilled history/old reply previews, and preserve other bots/shared evidence/configuration. Do not perform a live reset for testing. This is distinct from memory deletion and snapshot recovery; expose it only as an explicit owner Control-tab/API operation.
