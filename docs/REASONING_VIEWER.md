# Private Discord reasoning viewer

`reasoning_viewer` is a separate, keyless, opt-in presentation plugin. Enable
**Plugins → Reasoning viewer**, then **Bots → bot → Capabilities → Reasoning viewer**.
Both grants are required. Existing bots and new installations start with it off.
It does not require Interactive Discord panels, Slash command assistant,
workspace, image generation or an attachment grant.

## What appears

New delivered answers get a **REASONING** button if at least one of their
generation requests has saved, readable provider reasoning. This works with
ordinary answers, slash answers and existing component panels, including their
files and automatic long-answer attachment. Public buttons contain only opaque
identifiers. Ordinary answer text, canonical transcript and diagnostic footer
are unchanged. Old messages are not retroactively edited.

Only the configured genuine human owner can open the viewer. The response is
always ephemeral, even when the answer is public. Opening it reads the database:
no model call, generated explanation, tool execution, activation or tool budget
is involved. Reading can happen while the bot is generating another answer.

The final generating request is selected first. **Previous round / Next round**
select individual generation requests in the saved turn, including tool rounds
and failed attempts with partial captures. No reasoning is aggregated. The
header identifies final/tool/earlier requests, provider/model, local timestamp,
reported or marked estimated reasoning count, completion/partial status and page.
If only earlier requests contain reasoning, the button still appears; the final
request explicitly reports that no readable text was returned.

**Previous page / Next page** update the same private card. **Download text**
opens a separate private response containing the complete selected request's
reasoning as UTF-8, with credentials masked. The viewer preserves Unicode across
pages and protects code fences; downloads preserve original formatting. If the
complete file exceeds the attachment limit supplied by Discord, the response
states both byte counts and points to paging/dashboard inspection. It never
silently truncates the trace.

## Evidence and access

Reasoning already lives in `request_diagnostics` inside external `council.sqlite3`,
separate from ordinary answers. Capture remains enabled regardless of this
plugin. Native full text, supported textual details and inline reasoning are
alternative representations: full native text wins, avoiding duplication.
Encrypted payloads, short reasoning summaries, previous-request replay and
numeric token counts alone cannot establish a readable trace. Nothing is
invented when a provider withholds text or a historical capture is missing.

`reasoning_bindings` saves the owning bot/application, source channel, turn,
confirmed outbox/message, exact final request and ordered request IDs through
that final request. `reasoning_views` binds private reader messages to those
records and deduplicates opening interactions. Neither table copies reasoning
text or stores Discord interaction tokens. Full application snapshots include
them automatically. Missing records after a restore produce a refusal; local
restores cannot undo old Discord messages.

Every click verifies the human owner, current global/bot grants, application,
message author, channel, confirmed delivery and selected request ownership.
Private page/download controls additionally verify their saved ephemeral viewer
message. Grants and bindings are checked again after asynchronous work. Disabling
either grant immediately blocks further reads through old public/private controls;
it does not delete diagnostics or recall text already read. Credentials are
masked again using current vault values. Reasoning is never added to ordinary
events, model inspection, channel transcripts or bot memory.

Controls survive process restarts through central gateway dispatch. Opening and
downloading reuse the existing private interaction acknowledgement/recovery
path. Paging acknowledges a quiet update; uncertain acceptance can be recovered
only for the exact saved private viewer. Existing slash acknowledgement settings
and model execution behavior are unchanged. Uncertain answer delivery does not
create an active viewer binding or trigger a resend.

Navigation IDs distinguish button direction even when multiple disabled controls
point at the same destination. Earlier saved page handles remain readable. If
rendering fails after acknowledgement, the viewer attempts one plain private
failure notice with its controls/files cleared, replacing the loading placeholder.
If Discord also rejects that notice, a separate `discord.reasoning_notice_failed`
warning explains the remaining placeholder. Dismiss it and click the original
REASONING button again; saved traces are unchanged and no inference is repeated.

`discord.reasoning_*` events record acknowledgements, reads, refusals and failures
with identifiers only. They do not include reasoning text or download contents.
Long capture parsing, paging and encoding run on detached worker data with two
concurrent preparation slots and a 30-second preparation deadline. SQLite and
vault state remain on their owning thread. The plugin is `model_tool=False`, so
enabling it adds no model schema or prompt instructions.

## Owner decisions (preserved from AGENTS, 2026-09-23)

The date marks relocation of standing instructions, not a new product decision.
Existing decision dates and qualifications below remain authoritative.

- The owner approved `reasoning_viewer` as a narrow opt-in exception to keeping provider reasoning off Discord. It is keyless, global/per-bot, default off and never a model tool. Automatically attach REASONING only to new answers with readable saved captures; exact-owner clicks read a private paged/downloadable view, final request first with earlier generation requests separate. Verify durable bot/application/channel/message/outbox/request ownership and current grants on every read, including after awaits. No inference calls, public reasoning, transcript/memory injection, hidden-text reconstruction or footer changes. Disabling revokes old controls without deleting diagnostics. Existing private dashboard/console evidence remains unchanged. See [REASONING_VIEWER.md](REASONING_VIEWER.md).
