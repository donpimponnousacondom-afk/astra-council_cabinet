# Task ownership and council time

The owner requires structured concurrency, observable failures and a responsive Discord loop even while another bot executes a long task. Python 3.14 is the application baseline. Preserve this contract when adding services or plugins.

## Ownership and failure boundaries

`Kernel.lifetime()` owns an application-lifetime `asyncio.TaskGroup` through `BackgroundTasks`. ASGI startup and asynchronous fixtures open this lifetime before starting services or launching turns. Shutdown stops components, cancels remaining children and joins finalizers before closing provider clients and SQLite. Offline CLI operations that schedule no background work may construct and close a kernel without starting it.

| Work | Owner and failure policy |
| --- | --- |
| Scheduler, Discord supervisor, publishing worker, event-loop monitor | Application group. Existing loops record ordinary iteration failures and retain their retry policy. An unexpected escaping exception is recorded as `runtime.task_failed`; that task stops and appears in the failure inventory. It is not silently restarted or allowed to cancel unrelated services. |
| Bot turn, Discord client runner, reconnect backfill | Application group with bot/task identity. Normal provider/delivery failures retain their state machine. Unexpected escaping failures produce a redacted traceback and explicit `TaskFailure` outcome. A still-running turn is marked failed. Other bots continue. |
| Typing presence | Active turn's scoped group. Existing best-effort handling records presence failures without failing an answer. Every exit cancels and joins presence. |
| SSH input/output/error collectors and process wait; current remote delivery | Scoped groups within the operation's deadline. Failure cancels related children and joins them before transport cleanup. Receipt recovery and uncertain-delivery rules remain intact. |
| Shell diagnostic pipe | Scoped group within the execution/export deadline. Process-tree cleanup and workspace release finish before the job is done. Command exit codes remain distinct from transport/runner failures. |
| Combined Brave/DuckDuckGo search | Scoped group. Expected engine failures become per-engine results, preserving a successful peer. Unexpected implementation failures propagate after related work is joined. |

`task_group()` uses native TaskGroup semantics. It unwraps a single exception to preserve domain-error/timeout contracts; multiple failures remain an `ExceptionGroup`. `error_text()` includes every leaf. `cancel_and_wait()` cancels and joins owned children; `join_tasks()` joins without cancelling ongoing cleanup. Neither discards non-cancellation errors. Owner cancellation propagates; do not swallow `CancelledError` in service loops or turn bodies.

The application group deliberately isolates independent workloads after recording their failure. TaskGroup itself normally cancels siblings when a child fails; isolation is our explicit supervision policy. If failure recording itself fails, that exception escapes the group and ends the application lifetime. Libraries such as Discord/Uvicorn own their internal tasks; this contract governs application-created work.

Bare `create_task` retains an exception, which propagates when awaited/retrieved. The problem is abandoning the handle or ignoring collected errors. Do not introduce bare task creation, unobserved futures, or discarded `gather(return_exceptions=True)` results. Tests enforce this source rule and exercise child failures, sibling cleanup, cancellation, bot isolation and shutdown ordering. [Python TaskGroup semantics](https://docs.python.org/3.14/library/asyncio-task.html#task-groups).

## Responsive preparation

TaskGroup cannot prevent synchronous code from blocking every coroutine. Context tokenization and tool working-set estimation operate on detached values in worker threads. SQLite, vault access, grant checks and viewer attribution stay on the event-loop thread. Transcript assembly yields between bounded chunks. Context preparation admits two simultaneous tokenization operations; operator bot/provider concurrency and budgets remain separate.

Compaction previously rebuilt and tokenized every growing transcript prefix, repeating most text N times for N messages. Preparation now checks the complete candidate first, then bisects only if it exceeds token/image limits. Every selected batch is checked; no checkpoint advances until all summary requests succeed. Cancellation during pure preparation cannot commit a checkpoint or send a late request. Cancellation does not forcibly stop native worker computation: keep it bounded and free of state mutations. [Python guidance on blocking work](https://docs.python.org/3.14/library/asyncio-dev.html#running-blocking-code).

`compaction.batch_prepared` records duration, message count, estimate and tokenization probes. Incomplete summaries report finish reason, visible character count, reported output/reasoning usage and the summary cap. A model can spend its entire output allowance on reasoning and return no summary; this differs from local loop blocking. Changing compaction limits/native reasoning overrides is an operator decision. Old context remains retained on failure.

The monotonic monitor emits `runtime.loop_delayed` for scheduling delays above one second. Active bot names give correlation, not proof of the blocking task. `runtime.task_failed` includes a bounded redacted traceback; `/api/status.background_tasks` reports running names and up to 50 unexpected failures for the current process. These diagnostics do not change grants, settings or activation budgets.

## One presentation timezone

Use `settings.timezone` for transcript timestamps, dynamic model clock, model-facing tool timestamp metadata, console timestamps and dashboard date labels. Europe/Madrid presents `2026-09-08T23:46:00Z` as `2026-09-09T01:46:00+02:00`. Keep the explicit offset and date change. The IANA zone uses +01:00 in winter; browser/host timezone differences must not change council presentation.

Models receive guidance to use the trusted clock and normalize older UTC references before comparing instants or writing new memories. Compaction uses the same convention. Tool formatting changes known metadata only, never quoted content, commands, schemas, arguments, old notes or raw request/response evidence.

SQLite keeps epoch instants. Version/build APIs retain canonical UTC startup/build stamps; normal dashboard and Discord `!version` presentation converts their dates. Raw evidence and exports retain originals. Existing notes/summaries are not rewritten. Durations, cooldowns and UTC-day cost accounting remain unchanged.
