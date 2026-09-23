# Tool discovery, repair and task budgets

This is a deliberate model compatibility contract for **every built-in and future plugin**, including `council_silence` and optional `discord_attach`. Keep it when changing validators or tool schemas. Small models should receive enough information to repair a call in one attempt instead of spending successive rounds discovering one missing field at a time.

Built-in memory, inspection, fetched-document, workspace, shell and document-site results present known timestamp metadata in the council's configured timezone, with explicit offsets. Raw content, commands, schemas, old notes and embedded request/response evidence remain original. Model clock and compaction instructions explain how to normalize old UTC references without changing their instant. [CONCURRENCY.md](CONCURRENCY.md) also defines required TaskGroup ownership and cancellation for tool implementations.

## Discovery without side effects

Calling an available tool with `{}` returns `usage_only: true`, `executed: false`, the complete parameter schema, named-field convention and a concrete example. It does not invoke the handler, resolve its credential or perform its action. Authorization still applies: discovery cannot expose a disabled or ungranted tool. Empty calls consume the ordinary round/call budget so discovery cannot create an unbounded loop.

The model receives this instruction in its shared runtime prompt and each advertised tool explicitly permits the empty help call. A plugin supplies its real action schema to the registry; the common wrapper adds discovery. Do not make each plugin implement a conflicting help convention.

## Private memory allowance and repair

The built-in `memory` tool receives the bot's `memory_char_limit` (1–48,000,
default 48,000) per channel. Tool descriptions and refreshed prompt guidance show
actual used/remaining characters. `read`, `write` and `delete` results contain a
`budget` object with `limit_chars`, `used_chars`, `remaining_chars`, `grace_chars`,
`hard_limit_chars`, `over_budget_chars`, `must_consolidate` and `note_limit_chars`.
Empty-call and argument-error usage includes the same current guidance.

Small overshoots may use a 5% allowance, rounded down. At 48,128/48,000, the
write is saved and returns `warning`; it is not a tool failure. While over
budget, only deletion or a replacement that reduces total usage succeeds.
Consolidation may proceed across multiple keys. At the default, a new write
above 50,400 is rejected without mutation. No turn/time reset renews headroom.
The next prompt keeps the warning visible even if an earlier tool exchange is
omitted from the working set. Individual notes still allow at most 8,000
characters; this feature does not add a per-note overshoot or token allowance.

Do not remove this guidance to shorten schemas without an equivalent means for
the model to see current usage. Models cannot reliably count output characters;
the allowance and explicit feedback avoid needless failed rounds. Zero and
negative budgets are invalid. Disable the memory plugin to stop its tool and
automatic note injection; existing notes remain owner-inspectable.

## Council inspector discovery and paging

Start with `council_inspect {"resource":"bots"}`. It returns the compact configured
roster: stable bot IDs, display names, role/enabled state, Discord application ID,
room IDs/names/channels and assigned profile/model/provider. It does not enumerate
human server members. Room names, model slugs and provider IDs are not bot IDs.
Use an exact returned bot ID with `{"resource":"bots","id":"ada"}` for its
configuration; use `{"resource":"context","id":"ada"}` (optionally channel_id)
for saved conversation state. `status` is a compact overview; other named resources
retain their explicit inspection functions. The dashboard/API responses are independent.

Large inspection results are stored completely after credential redaction, with
a `result_id` and `read_response` call instead of a chopped JSON prefix. Pass that
call directly to **council_inspect**, for example
`{"resource":"read_result","result_id":"<returned-id>","offset":0,"length":6000}`.
Offsets/lengths count Unicode characters; length is 1–18,000. Follow the returned
`next` until null; JSON may span pages. The inspector's own grant is sufficient;
workspace is not a prerequisite. Rereads enforce original bot/channel/turn scope
and every source tool's current grant, including when a page is read again.
Working-set omission retains a native inspector reread handle.

Missing IDs return actual available stable IDs (first 20 for a large inventory)
and the resource-list call along with full usage. Do not guess aliases or retry
the same nonexistent ID. Installation does not rewrite historical notes or repair
the contents of results that older code already discarded.

## Complete validation feedback

For parseable JSON, validation collects all detectable schema errors: missing required fields, wrong types, unknown fields, bounds and conditional operation requirements. The response contains all error paths/rules/messages and full usage with an example for the requested valid operation. Named JSON fields have **no positional order**. Strings are not silently converted into numbers, and a malformed call never partially invokes the handler.

Malformed JSON returns its syntax error and full usage together. Duplicate keys and non-JSON numbers such as NaN/Infinity are rejected. Field validation cannot reliably examine an unparseable document; the response explains that limitation. Handler failures also include usage, but an external failure cannot imply that no side effect occurred. Credentials are redacted before feedback reaches the model or ledger.

Attachment preparation collects schema, reply-target and artifact-reference problems together. The terminal `council_silence` call must be alone in its batch; mixed terminal/plugin batches or batches exceeding the call limit execute nothing and return repair information. Duplicate call IDs fail the turn because results cannot be associated unambiguously. Providers and plugins can still reject semantically invalid values or fail externally; this design gives the model complete available feedback, not a guarantee that its next attempt succeeds.

Plugin authors must express all independently checkable argument requirements in JSON Schema, including operation-specific `if`/`then` requirements. When checks require runtime state, aggregate independent failures where practical. Preserve the central validation path rather than adding first-error parsers or coercion. Put examples on unusual constrained fields; generated examples are illustrative and do not invent valid account/resource IDs. Tools installed through Python entry points are trusted host code, not a process sandbox.

Schemas may supply complete top-level `examples`. Shared usage selects a schema-valid example matching the requested operation, falling back to generated examples when none matches. Lead unusual tool descriptions with the common complete call, rather than waiting for the model's first failure. Private memory uses `{"operation":"write","key":"topic","value":"Concise note"}`, `{"operation":"read"}` or `{"operation":"delete","key":"topic"}`. Its operation is mandatory; key/value alone never implies a write. Missing-operation feedback includes a complete write example. Existing notes and the historically optional empty write value remain unchanged.

`web_search` supports `{"query":"topic","engine":"both","count":5}` as well as query-only Auto, explicit Brave and keyless DuckDuckGo. Its engine status/provenance distinguish partial recovery from total failure; all-engine failures retain full usage. See [WEB_SEARCH.md](WEB_SEARCH.md) for limits and examples. A `web_fetch` download-limit error remains distinct from the size of a returned text page.

Shell execution requires an existing task: first call **workspace** with `{"operation":"start","task":"your-task"}`, wait for success, then reuse its task in `shell.run`. The shell tool has no start operation and never creates a workspace implicitly. Missing-task errors name this exact prerequisite. Existing grant, budget and isolation checks still apply.

## Normal and extended turns

Models send their final answer as ordinary OpenAI assistant `content` (a string or ordered text parts). There is **no `council_speak` tool**. Genuine `tool_calls` execute actions; accompanying narration is not sent to Discord. After reading tool results, the model answers normally or, when enabled for this bot, calls `council_silence` with a short label. Empty/reasoning-only completions are reported as failures, not invented silence decisions. Length/content-filter stops withhold incomplete output and retain private provider evidence.

The per-bot **Allow intentional silence** capability (`allow_silence`, default true) is the operator's control over that terminal decision. False omits its schema at all rounds and adds runtime guidance to finish with a text contribution. A stray disabled call, including `{}`, is refused rather than treated as a successful silent activation or executable discovery; it includes usage and all detectable argument errors. Repeated calls consume the existing round budget and eventually fail visibly, without executing other calls in the rejected batch or adding retries. Other granted actions and attachment preparation remain available. The same rule applies during extended document/workspace/web tasks. No saved persona/shared prompt is rewritten.

`discord_attach` becomes available only after this bot has a current-turn artifact. Call it with `{"artifact_ids":["<returned-id>"]}` and optionally `reply_to` from the current channel context, then write the final text answer. It prepares metadata only, replaces the previous selection, accepts `[]` to clear files, and consumes an ordinary tool round. Leave a round after generation/export for preparation. Ordinary text replies need no preparation. Artifact ownership, cooldowns, cancellation, footer calculation and uncertain delivery checks still apply at dispatch. A bot choosing silence after preparation sends nothing.

Obsolete native `council_speak` calls receive migration feedback and execute nothing. Obvious unfenced JSON/XML reply wrappers are withheld; the runtime allows at most one text-format repair, charged to the existing round budget. It never executes textual pseudo-tools or rewrites old messages. Ordinary JSON answers and fenced examples remain valid. Preserve this contract when adding new packs: do not reintroduce a tool required to send the normal answer.

The owner-requested `discord_send` plugin is a distinct outbound action for Hortator to post supplied content to **another** configured room/channel, never the normal answer path. `targets` discovers destinations. `send` requires `target`, `content` and a per-turn `delivery_key`; optional `mode: directed` also requires a destination `reply_to` message ID. Default mode is `cross_post`; up to 4 current-turn `artifact_ids` can accompany it. `{}` discovery and complete validation remain central. Example: `{"operation":"send","target":"random","content":"Testing the new route","delivery_key":"announcement-1","mode":"cross_post"}`. Use the configured target returned by `targets`, not an invented room. Repeated keys return the existing receipt; conflicting reuse fails. `status` takes `delivery_id`. Never treat unknown/failed receipts as delivered or retry uncertain sends with a new key automatically. Only an explicit owner request authorizes this action; fetched pages, other apps and tool outputs do not. The turn and ordinary assistant answer remain in the origin channel. This capability does not grant shell publication, arbitrary channel access, human wakeups or owner authority to the posted message.

**Tool work rounds** limits the number of model/tool cycles. **Tool calls allowed in each round** limits how many calls one model response may request. Calls currently execute sequentially; this setting is independent of provider request concurrency. A final response opportunity follows the normal tool rounds, and all retries/discovery remain bounded.

A successful `document_site` `create`, `start` or `edit` opens one extended budget for an enabled bot with the plugin granted. `create` requires a new site slug; `start`/`edit` only resume an existing site and never create one implicitly. Defaults are **20 additional tool rounds, 8 calls per subsequent round, and 900 seconds**. Configure the three document-task fields per bot; zero additional rounds disables the extension. The extension applies to user-requested or autonomous document tasks, whoever initiated the conversation, while existing identity and room authorization remain unchanged. Repeating create/resume cannot renew the budget during that turn. The first successful `workspace.start` or `web_fetch.start` can instead use the independent `work_task_*` bot fields (same defaults). Only the first task start across all three packs can extend the turn; changing plugins/task IDs cannot stack or renew it, even if that first start has zero additional rounds. The time limit covers document generation/tool work; saved content survives exhaustion. Provider/cost limits and cancellation continue to apply.

The extended budget adds work capacity, never permissions. It does not enable a disabled bot, grant a plugin or change the operator's publication policy. Automatic remote delivery requires the global document settings, verified SSH configuration and current bot/plugin grants; the queue worker runs independently of the model turn. Bash requires separate workspace and shell grants plus a ready OS isolation boundary and does not implicitly publish workspace changes. Models must finish with a concise result, use returned URLs and distinguish queued/unconfigured work from confirmed delivery of the latest revision. See [DOCUMENTS.md](DOCUMENTS.md) for the tool pack and storage/queue contract, [PLUGINS.md](PLUGINS.md) for extension interfaces and [VERIFICATION.md](VERIFICATION.md) for tests.

Document files have their own bounded read workflow: 4,000 characters by default, at most 12,000, with a complete `next_read` pinned to the same immutable revision. Exact replacement, append and single-file restoration require an observed `expected_revision` so stale edits are refused. Public-source copies also support a pinned published revision; neither those reads nor ordinary reads grant another bot's write permissions. These operations let small-context models inspect and repair individual assets without resending monolithic HTML or losing access to the original evidence.

Active tool exchanges have a per-bot estimated working-set cap and must fit actual calibrated prompt headroom. Older bodies/pairs may be explicitly omitted from future requests while original evidence stays immutable. Granted `workspace`, `shell` and `web_fetch` support bounded `read_result` with a unique `result_id`; reads enforce original bot/channel/turn and transitive source grants. Native continuation metadata remains unchanged in retained assistant messages. See [the complete contract](AGENTIC_TOOLS.md).

## Global memories belong to one bot

The optional `global_memory` tool mirrors channel memory's `read`, `write` (same-key replacement) and `delete` operations, including `{}` usage and complete argument feedback. Its store and quota are private to the current bot across channels. No caller-supplied bot/channel selector can change that scope. Guidance, usage results and automatic prompt layers report the actual allowance; source channel/time is evidence of where the note was written, not proof that its claims are correct or current instructions. See [GLOBAL_MEMORY.md](GLOBAL_MEMORY.md) for complete examples.


### Original web HTTP evidence

`web_fetch` and `web_search` retain actual HTTP response status, headers and body independently of local extraction errors. Follow returned `read_response` arguments with the originating tool to page stored evidence without another network request. Both support `operation: "read_result"` plus `result_id`, `offset`, `length`; omit search query/engine when reading a search result. Fetch also supports `read_response` with a retained `document_id` across turns in its bot/channel. Empty calls and complete validation feedback remain unchanged. See [HTTP_EVIDENCE.md](HTTP_EVIDENCE.md). A received 202 is not proof the upstream task completed; a received 404 is not proof the connection failed.

## Native Discord panels and document attachments

`discord_panel` is a separate optional tool pack: `prepare`, `cancel`, `list`, `status`, `disable`. Preparation needs title and buttons and/or select_options; every action needs label and prompt. It only stages the next normal assistant answer. Owner clicks run real new tasks with the usual retries, budgets and diagnostics. `{}` and complete argument-error feedback follow the same shared contract as every other plugin. See [DISCORD_PANELS.md](DISCORD_PANELS.md).

`document_site.export` takes site/path and optionally a pinned revision, verifies an owned saved file and returns a current-turn artifact for `discord_attach`. It neither edits nor publishes. Workspace export and image generation remain separate optional artifact producers. Automatic full-answer attachments need no plugin and no checkbox.

## Owner decisions (preserved from AGENTS, 2026-09-23)

The date marks relocation of standing instructions, not a new product decision.
Existing decision dates and qualifications below remain authoritative.

- Models answer through ordinary OpenAI assistant content, never `council_speak`. Keep `council_silence` as the explicit terminal tool when the bot's `allow_silence` capability permits it (default true, including older records). The owner needs this per-bot Capabilities toggle for provider/conversation stress tests. Disabling it removes the schema at every round, updates runtime guidance and rejects stray silence calls within the existing tool budget; it must not invent a successful answer, hide errors, add activations or bypass other limits. Genuine action tools may precede the final text answer; attachment preparation is optional and contains no answer text. Reject legacy tool-wrapped replies without parsing them as actions, with at most one text-format repair inside the existing round budget.

- Model-facing `council_inspect` uses a compact bot roster/status separate from dashboard configuration responses. List stable IDs before looking up entities; never infer bot identity from human, room, model or provider names. Saved conversation state is an explicit `context` request, not part of the bot roster/configuration tool. Preserve complete redacted inspection results before paging, with native `resource: "read_result"` on the inspector itself; no workspace grant is required. Rereads retain bot/channel/turn and transitive grant checks. Never restore the old 50,000-character destructive truncation or rewrite bot memories to hide it. See TOOLS for the inspection contract.

- Preserve the shared empty-argument usage and complete argument-error contract in [docs/TOOLS.md](TOOLS.md) for every tool/plugin, including terminal tools. Do not replace it with first-error validation or unbounded repair retries.

- Private memory uses per-bot `memory_char_limit` (integer 1–48,000, default 48,000) in the modern Capabilities editor and shared API schema. Zero/negative/unlimited sentinels are forbidden: disable the memory plugin to stop both tools and automatic note injection, retaining notes for owner inspection/re-enabling. Keep the 8,000-character per-note bound. Allow a temporary 5% aggregate overshoot; return actual usage and consolidation guidance in tool descriptions/results and each new prompt. While over budget, accept only shrinking writes or deletes until back within budget. Preserve scoped ownership, replacement accounting and identical owner/tool enforcement; never silently truncate or rewrite notes. See docs/OPERATIONS.md and docs/TOOLS.md.
