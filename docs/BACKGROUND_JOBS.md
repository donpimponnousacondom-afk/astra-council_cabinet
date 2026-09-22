# Background jobs

The reusable `BackgroundJobs` service owns persisted plugin jobs independently
of conversational turns. A registered handler supplies execution and current
configuration checks. Kernel owns all worker tasks through its lifetime group.
The first consumer is the experimental Sub-agent researcher plugin.

Jobs are stored in the application SQLite database, included in consistent
snapshots. Their bot, channel, origin turn, assignment, deadline and submission
identity survive conversational completion. No interaction token is saved.
Two workers share a queue of at most eight jobs. A bot may have one active job
or pending completion; a turn may create at most two jobs. Result snapshots are
bounded to 2 MiB, expire after seven inactive days, and roll at 100 settled
records per bot. Pending notifications are protected until consumed/revoked.

Completion queues one ordinary turn in its originating admitted channel. Fresh
human-directed input takes priority. Timer-zero bots can receive this explicit
completion event; global/bot/provider pauses, recovery, concurrency, cost/turn
budgets, channel grants and delivery spacing still apply. The follow-up receives
the assignment and saved result handle through editable runtime facts. Its
notification is claimed before execution, never automatically replayed following
an inference or uncertain Discord delivery failure. Follow-ups cannot recursively
launch more background jobs. Normal answers and intentional silence still end
the creating turn and stop its typing indicator while workers continue.

Pause/configuration cancellation joins relevant workers and revokes pending
notifications. Clean-slate boundaries prevent old work from reentering context.
Restart marks leftover queued/running work interrupted and never reissues the
paid request. Researcher removal cannot delete this general service; uninstalled
handlers are unavailable and cannot trigger model work.

This feature does not automatically offload existing synchronous tools or change
the lifetime, tool limits or delivery behavior of ordinary turns.
