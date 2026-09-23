# Sub-agent researcher (experimental)

`research_assistant` delegates one self-contained assignment to an independently
selected MiMo model using its native web-search tool. The main bot can use any
provider/model. The researcher receives only its configured system prompt and
the supplied assignment, never an automatic copy of conversation, memories or
other tool grants. It cannot call local tools or launch further agents.

Enable **Plugins → Sub-agent researcher · experimental** and grant it under
**Bots → Capabilities**. Select a separate research model profile whose provider
contains the MiMo endpoint and credential. The main bot's provider-key override
is deliberately not sent to the research provider. Provider request
retries, concurrency, health, timeout, pricing and private diagnostics use their
existing contracts. The plugin has no separate secret box. Nothing enables or
changes an existing bot/profile on installation.

The native request uses `/chat/completions`, `tools: [{type: "web_search",
force_search: true, max_keyword: 1, limit: 1}]`. The operator can choose one to
three query expansions per native search round. The dashboard calls this
**Search queries per search round (maximum)**: a query may be a multiword phrase
such as "capital of France". This sets neither word count nor research/model
rounds. It maps unchanged to MiMo's `max_keyword`; the adapter still submits one
completion request with native search, rather than running a local iterative
research loop. The selected profile supplies exact reasoning JSON and
streaming mode. Each job explicitly sends `max_completion_tokens`, replacing
inherited output-cap fields on the wire copy only. It bounds reasoning plus
visible output; upstream search-context tokens are separately billable. There is
no inherited conversation summary or automatic compaction of research inputs.

Configuration defaults: no selected profile, 16,384 output-token ceiling,
600-second total deadline (including queue/retries, configurable 1–7,200), one
keyword, four outstanding researchers per bot, automatic completion follow-up
enabled. The system prompt is editable;
`{now}` uses the council timezone. Per-bot plugin JSON overrides use the usual
shallow merge and validation. Profile changes cancel affected work. Sharing a
provider with the main bot also shares its concurrency limit; use an independent
provider when research must not occupy the bot's only inference slot.

**Outstanding researchers per bot (default)** sets `max_parallel_jobs` globally.
**Bots → Capabilities → Outstanding researchers for this bot** optionally overrides
it; blank restores inheritance. The default and minimum are four, with no fixed
upper ceiling. Active jobs and pending completion notifications share this
allowance across the bot's channels. Completed results whose follow-up has been
read, claimed or revoked no longer occupy a slot. Status/list/start responses
include current capacity and available slots; the tool description also states
the current allowance. Model-facing lists include all outstanding jobs in their
admitted channel plus up to 30 recent settled jobs.

Each `start` creates exactly one researcher and consumes one tool call. For
example, eight independent assignments require eight starts, an allowance of at
least eight available slots and a bot call budget permitting eight calls. Starts
return promptly. With completion notifications enabled, put independent starts
in one tool-call batch: after that batch the runtime closes tools and asks for a
brief text acknowledgement. Queued workers start when that turn ends, then run
independently subject to the selected provider's concurrency limit. This avoids
children occupying the parent's only provider slot before it can acknowledge.
There is no batch-start shortcut or extra tool
budget. Reading/claiming a finished job can free its slot, and each job retains
its own deadline, output allowance, saved evidence and notification.

```json
{"operation":"start","submission_id":"official-prices-sept","task":"Compare official prices. Cite URLs and dates; flag contradictions.","max_output_tokens":4096}
{"operation":"status","job_id":"bg_RETURNED_ID"}
{"operation":"wait","job_id":"bg_RETURNED_ID","seconds":10}
{"operation":"read_result","job_id":"bg_RETURNED_ID","offset":0,"length":6000}
{"operation":"list"}
{"operation":"cancel","job_id":"bg_RETURNED_ID"}
```

Empty arguments return full usage without starting work. `start` returns at once;
the runtime stops typing at notified dispatch and requests the acknowledgement.
Worker completion or failure queues contextual follow-ups, grouping settled
siblings and supplying batch progress counts, with human-directed messages
taking priority. Reading a finished job before its notification is claimed
consumes that notification, avoiding an unnecessary extra turn. `wait` is bounded
to 20 seconds outside dispatch/completion follow-ups, counts as a tool call, and
cancellation of the wait does not cancel the worker. During those follow-ups it
returns current status immediately: publish useful progress/findings in a **new
message**, then let the remaining workers trigger later updates. No old Discord
messages are edited. Notification opt-out retains explicit polling rather than
automatic handoff/wakeups. A repeated submission ID with the same assignment recovers its job;
different arguments require a new ID. This is local deduplication, not a promise
that an upstream retry cannot bill another search.

This first version starts jobs only in assigned conversational channels or
Hortator's existing owner scope. Slash/panel invocations and paused one-shot
turns are explicitly rejected; expiring interaction tokens are never persisted
or reused for later unsolicited follow-ups. No Discord sends originate directly
from a worker. See [background jobs](BACKGROUND_JOBS.md) for lifecycle, retention,
snapshots, completion claiming and cancellation.

`read_result` pages the complete saved JSON by Unicode-character offsets. The
report is labelled a researcher synthesis, with preserved native source
annotations, search errors, usage, finish reason and `complete` flag. A partial
or filtered completion is not advertised as complete. Absence of citation/usage
metadata cannot prove a search succeeded. Provider reasoning is retained only
in authenticated diagnostics, never returned through this tool.

Report validation is separate from a successfully completed HTTP/model request.
An empty report, unhandled native tool calls, or a literal tool-call envelope in
assistant content makes the job **failed**, with `complete: false`, an explicit
`validation_error` and `kind: "invalid_researcher_output"`. The saved output,
sources, usage and metrics remain readable through the same scoped result pages;
private reasoning stays private. Envelopes following a prose preface are also
rejected; plain discussion of tools and fenced/inline/quoted examples are
allowed. Content is never executed as a tool or automatically sent for a
paid repair/retry. A normal partial report stopped by an output limit retains
the existing incomplete-result behavior. Owner/timeout cancellation is handled
independently; old request/job evidence is not reclassified or rewritten.

Metrics include per-request TTFT/TPS (unknown for buffered responses), elapsed
job time, token counts with provenance, inference cost when known and native
search-use counts. Search cost remains unknown rather than silently zero. Native
search and model tokens may both be charged; do not assume the Token Plan or
MiMo Code promotional search allowance covers an arbitrary bot integration.

Official references checked 2026-09-22:
- https://mimo.mi.com/docs/en-US/api/chat/openai-api
- https://mimo.mi.com/docs/en-US/quick-start/usage-guide/text-generation/tool-calling/web-search
- https://mimo.mi.com/docs/en-US/price/pay-as-you-go

Disabling the plugin removes its model tool and revokes/cancels work while keeping
saved evidence. Its adapter, configuration UI and registration can be removed
without removing the reusable background service. Existing search/fetch tools,
main model profiles, memory and Discord footers retain their existing behavior.

Inspect saved work in **Bots → bot → Control → Background jobs**, or in the
researcher plugin editor. The owner can page reports/metrics and cancel an active
job or its pending follow-up. Dashboard inspection does not consume the bot's
notification. Research configuration and existing bot settings remain separate.
