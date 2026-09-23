# Background jobs

The reusable `BackgroundJobs` service owns persisted plugin jobs independently
of conversational turns. A registered handler supplies execution and current
configuration checks. Kernel owns all worker tasks through its lifetime group.
The first consumer is the experimental Sub-agent researcher plugin.

Jobs are stored in the application SQLite database, included in consistent
snapshots. Their bot, channel, origin turn, assignment, deadline and submission
identity survive conversational completion. No interaction token is saved.
Admission uses each registered handler's positive per-bot allowance across all
channels. A handler can supply a `limit(bot)` callback; otherwise its allowance
is one outstanding job. Queued/running jobs and pending completion notifications
occupy slots. The research plugin defaults to four, with an operator-configured
global default and per-bot override. There is no separate two-worker semaphore,
eight-job global ceiling or two-submissions-per-turn restriction. Admitted jobs
have independent owned workers; the provider pool still enforces actual request
concurrency. Each submission remains one tool call within the bot's ordinary
round/call budget.

Status includes current capacity, active jobs, pending completions and available
slots. Admission checks the current allowance; any over-limit saved work stays
inspectable. Saving configuration through the control service retains its normal
cancel/join behavior for affected work. Result snapshots are bounded to 2 MiB,
expire after seven inactive days,
and roll at 100 settled records per bot. Pending notifications are protected
until consumed/revoked. The owner inventory includes all outstanding jobs plus
the latest 50 settled records, so large configured allowances remain inspectable.

With notifications enabled, the first accepted dispatch batch ends tool use for
its originating turn. Every call already present in that batch is handled, then
one text-only inference produces the ordinary acknowledgement. Typing stops at
handoff; malformed acknowledgement tool calls execute nothing. Failed or
superseded acknowledgement does not discard accepted workers or retry that
answer. Idempotent recovery of an older job does not trigger a new handoff.
Explicit notification opt-out retains the existing bounded polling workflow.

Workers in a dispatch batch wait on an owned in-memory gate before acquiring
provider slots. Ending the origin turn releases them, including failed/cancelled
turn finalization; operator cancellation still cancels and joins the workers.
This prevents a one-slot provider from running children ahead of the parent's
acknowledgement. The original job deadline includes this wait; expiry makes no
paid request. A restart still interrupts rather than resubmits saved jobs.
An entirely notification-disabled batch releases its gate after the tool batch,
so explicit polling remains usable. A mixed batch keeps all its new workers
behind the acknowledgement. Previously running work retains its provider slots.

Completion queues an ordinary turn in its originating admitted channel. Fresh
human-directed input takes priority. Timer-zero bots can receive this explicit
completion event; global/bot/provider pauses, recovery, concurrency, cost/turn
budgets, channel grants and delivery spacing still apply. The follow-up receives
compact assignments and saved result handles through editable runtime facts.
Settled siblings with the same bot, channel, plugin and origin turn are claimed
atomically with creation of the continuation turn as one wave, with cumulative settled/total, active, completed, failed
and cancelled counts. Notification opt-outs do not contribute to those progress
counts. Only newly claimed IDs enter the wave; workers finishing later wake a
later turn. The metadata is bounded to 12,000 characters of receipts; overflow
stays pending for another wave, without an additional admission limit. Full
assignments/results remain in scoped tool reads. Receipts appear once in their
dedicated prompt layer rather than being copied into runtime facts and progress
guidance again. Counts of earlier claimed notifications identify prior waves;
the scoped list and durable continuation turn IDs retain their individual state.
Batch siblings are protected
from retention while any worker, notification or reporting turn is outstanding.

Each notification is claimed before execution, never automatically replayed following
an inference or uncertain Discord delivery failure. Follow-up submissions are
blocked unless a trusted plugin supplies explicit admission policy. The research
plugin permits refined batches within a durable task-wide allowance; workers
themselves cannot delegate. Normal answers and intentional silence still end
the follow-up. Progress and results use **new ordinary outbox messages**; the
workflow never edits the dispatch acknowledgement or an older answer. It does
not manufacture findings or bypass configured silence/delivery gates. A follow-up
may read results and use its ordinary tool budget, but cannot wait on remaining
researchers; subsequent waves report their outcomes. The research plugin may
admit a refined batch while earlier workers continue, within both available
slots and the shared task budget. The normal acknowledgement handoff then ends
that completion turn too.

Pause/configuration cancellation joins relevant workers and revokes pending
notifications. Clean-slate boundaries prevent old work from reentering context.
Restart marks leftover queued/running work interrupted and never reissues the
paid request. Researcher removal cannot delete this general service; uninstalled
handlers are unavailable and cannot trigger model work.

This feature does not automatically offload existing synchronous tools or change
the lifetime, tool limits or delivery behavior of ordinary turns.

A worker may fail validation while retaining a saved result for inspection.
`JobResultError` passes that result through the usual grant check, credential
redaction and 2 MiB bound, then records a failed job and its reason. Status,
dashboard inspection and the single-consumption completion notification expose
the failure while scoped result reads retain the evidence. This does not replay
the worker or classify valid upstream transport as provider downtime.

The completion instruction and new **Background dispatch & progress** instruction
are editable Prompt library templates, with the usual per-bot
`background_completion` and `background_updates` switches/overrides. Existing
saved templates are not overwritten. Text switches do not disable runtime
handoff, bounds or permission enforcement. Normal generations do not receive
these conditional instructions. Job payloads/results are credential-redacted; provider private
reasoning remains in its existing diagnostics store. No worker sends Discord
messages directly or holds a typing indicator after its parent turn ends.

The additive SQLite table is created by Kernel before services start. Full
application snapshots include it; a restored runtime does not replay active paid
requests. Clean shutdown cancels/joins workers and revokes pending notifications;
a crash can leave an interrupted receipt for a single failure follow-up. Saved
reports remain available until ordinary retention cleanup. Code rollback leaves
the extra table unused; full snapshot compatibility still requires matching
source/schema as documented in SNAPSHOTS.
