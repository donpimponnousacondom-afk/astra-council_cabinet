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
force_search: true, max_keyword: 3, limit: 5}]` by default. The operator can
choose 1–50 query expansions and 1–50 results per query. The dashboard calls these
**Maximum search queries** and **Results per search query
(maximum)**. A query may be a multiword phrase such as "capital of France". These
map unchanged to MiMo's `max_keyword` and `limit`; neither sets word count or
research/model rounds. They are ceilings, not guaranteed useful or distinct
sources. More queries can increase search charges; more returned content can
increase input tokens. Each job saves its effective limits when submitted.
Existing explicit values and per-bot overrides are preserved; a missing result
limit uses five for new jobs. Older job payloads retain their historical
one-result allowance, and restart never replays them. The adapter submits one
completion request with native search, rather than running a local iterative
research loop. The selected profile supplies exact reasoning JSON and
streaming mode. Each job explicitly sends `max_completion_tokens`, replacing
inherited output-cap fields on the wire copy only. It bounds reasoning plus
visible output; upstream search-context tokens are separately billable. There is
no inherited conversation summary or automatic compaction of research inputs.

Each researcher still performs **one research pass per job**: one model request
with native search, then its report. Provider retries repeat a failed request,
not a new research iteration. The calling bot evaluates those results and may
dispatch refined assignments to new researchers. **Research batches per task
(maximum)** configures `max_research_batches`: default **3**, including the
initial dispatch plus up to two refinements; minimum one, with no fixed upper
ceiling. A value of one disables follow-up dispatch. These controls belong to
the research plugin; ordinary bot tool-round/call controls remain unchanged.

All researchers accepted in one dispatch turn consume one batch together.
Partial sibling completion waves and later descendants share the original
task's durable allowance. Only results claimed by the current completion turn
can supply its ancestry; the model cannot select another task to refill its
budget. A fresh ordinary conversational turn can start a new research task.
The initial ceiling is retained: raising the setting does not refill existing
tasks, while lowering it applies to subsequent admissions. Rejected submissions
and idempotent recovery do not consume another batch. When the allowance is
exhausted, existing workers continue and the bot can read/report their results,
but another completion cannot launch more researchers for that task. Jobs from
before this feature have no recorded ancestry and remain report-only on wake-up.

On a completion wake-up, the parent bot decides whether evidence is sufficient
or narrower questions would help. Refined starts use the same capacity, per-call
cost, acknowledgement and sleep workflow as the initial dispatch. Progress
counts describe that dispatch batch; `research_cycle` status describes the
whole task's used/remaining allowance. The runtime does not automatically retry
weak reports or instruct worker researchers to delegate. Every acknowledgement
and later progress/result is a **new message**, never an edit to an old answer.

The plugin's `research_batches` SQLite ledger is included in full snapshots.
Budget entries remain while any retained researcher references their task,
including when its initial job has aged out; unreferenced tasks are pruned on
subsequent research admission. Restart does not replay paid requests or reset
saved allowances. Loki is an experimental user of this capability, not a special
runtime identity; any granted bot can use it, and other bots keep their settings.

Configuration defaults: no selected profile, 16,384 output-token ceiling,
600-second total deadline (including queue/retries, configurable 1–7,200), three
search queries, five results per query, four outstanding researchers per bot,
three dispatch batches per task,
automatic completion follow-up enabled. The system prompt is editable;
`{now}` uses the council timezone. Per-bot plugin JSON overrides use the usual
shallow merge and validation. Profile changes cancel affected work. Sharing a
provider with the main bot also shares its concurrency limit; use an independent
provider when research must not occupy the bot's only inference slot.

Default guidance asks the researcher to use the native search evidence supplied
for its request and distinguish insufficient results from explicit search
errors. It has no local tools to run repeated searches or open pages; ask for
findings rather than a prescribed sequence of tool calls. A model-written claim
that search is unavailable is not a structured provider error. Saved custom
system prompts are not overwritten on upgrade. Remove requirements for repeated
search rounds when tuning an existing prompt, preserving other operator text.

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

Official references checked 2026-09-23 (the API's Web search tool schema documents
both search bounds as 1–50; its separate defaults are five):
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
