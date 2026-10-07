# One-use failed-response recovery

Ordinary council conversations receive one temporary diagnostic note after an
incomplete generation. It is the final **user-role** message in the next eligible
generation request, after dynamic facts and any Engram protocol. It identifies
itself as an automatic harness notice, not a human message. Earlier prompt
messages are unchanged; actual cache reuse remains provider-dependent.

The notice supplies the received draft, readable provider reasoning, partial
tool-call text, source IDs/model/provider, local request timestamp and recorded
failure/finish reason. It asks the bot to use relevant work for the current request
or ignore it, distinguish facts from unfinished speculation, keep private reasoning
out of public replies, and check actual receipts before repeating actions. Old
Engram text is an uncommitted draft with an obsolete nonce, never accepted state.

## Eligibility and lifetime

- A failed/cancelled/interrupted ordinary turn queues its last generation request
  when that request failed or was interrupted, or completed with `length` or
  `content_filter`. An HTTP-200 `content_filter` response is eligible even though
  the request ledger calls its transport exchange completed.
- If the final retry returned no text, the reader can recover the latest useful
  capture from an earlier failed attempt of that same logical completion. Final
  error and capture request IDs remain distinct. Successfully recovered retries
  do not create a note; earlier successful tool rounds are not replayed.
- There is at most one pending reference per bot/conversation. A newer qualifying
  failure replaces it. There is no new wake-up, automatic resend or model call.
  Existing scheduling, activation, grants and cost controls still apply.
  Slash/panel fresh invocations, compaction and research workers neither queue
  nor consume these conversation notes.
- The reference is consumed immediately before starting the HTTP attempt.
  Transport retries reuse the same body and note. It is absent from subsequent
  tool rounds and turns, even if the receiving request fails. A new failure can
  supply new evidence; previous recovery input is not recursively copied.
- Waiting for a provider slot, compaction, preparation cancellation or a local
  failure before HTTP does not consume the reference. If the reader is busy or
  even the minimal notice cannot fit, ordinary generation can proceed without it
  and the reference remains available for a later first round.

The runtime does not append this material to shared history, compaction input,
channel/global notes or accepted Engram state. The bot may deliberately save useful
factual conclusions through its existing grants; this feature cannot promise that
a model never repeats or remembers text it has seen.

## Evidence, limits and isolation

`response_recovery_pending` stores IDs only in external SQLite, not duplicate
response bodies. It references existing requests and private captures. An indexed
request lookup stages a failure; the existing bounded reporting worker opens its
own read connection for parsing and token fitting. Writes and current vault
redaction remain on the runtime owner thread. A queue-writing failure is logged
without replacing the original turn failure or cancellation.

The reader decodes at most **16 MiB per response/private capture**, and fits at most
**1,000,000 source characters** into the remaining calibrated input budget after
the response reserve. Oversized captures are explicitly marked omitted; originals
remain intact. Budget excerpts retain beginnings/endings, omitted-character counts
and original section sizes. The runtime measures the assembled request again before
inference. Missing text and encrypted-only reasoning are not invented.

Only the same bot and conversation can receive the evidence. Changing models does
not change bot ownership; the note identifies the earlier requested model. Current
known credentials are redacted again before inference. Model-readable request
records contain a placeholder in the note's slot; the exact supplied note is kept
under private diagnostics `request_recovery`. This preserves operator inspection
without spreading private reasoning through public trajectory tools, events or
transcript replay. Events `response_recovery.queued`, `.offered`, and read-failure
`.deferred` contain metadata, not captured text. No general model-facing reasoning
reader is added.

Pending references survive restart. Startup queues only turns it just recovered
as interrupted, without backfilling old finished failures. Full snapshots include
pending references with their source evidence. Clean-slate reset and selective
restore that includes context clear affected references; notes-only restores leave
them alone. History cutoffs also exclude earlier captures at read time.

## Owner decision · 2026-10-07

The owner requested visibility into work lost when Curie's response was cut off,
including `content_filter`. They explicitly authorized returning a bot's own
retained reasoning and incomplete answer as a one-use user-role tail, clearly
automatic and separate from persistent context. This is a narrow exception to
operator-only diagnostic access, not permission to publish reasoning, execute
partial calls, commit partial Engrams, alter provider filtering or change credential
isolation. It applies to eligible ordinary conversations for every council member.
