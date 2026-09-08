# Private files, isolated Bash and complete web reading

Bots can import an observed attachment, inspect its measured size/dimensions, compress a separate copy with real local tools and attach the resulting owned artifact through their normal response. They can also traverse and search complete fetched text snapshots, keep working files and use a bounded extended task. Existing bot identities, allowed channels, owner authorization, provider budgets and uncertain-send handling continue to apply.

## Enablement

1. In **Plugins**, enable **Private workspaces** (`workspace`) for file operations. Enable **Isolated Bash** (`shell`) when execution is wanted. Both are disabled in a newly seeded configuration and require no API key. Existing installations receive these disabled entries without changing any bot grant or provider configuration.
2. In **Bots → Edit → Capabilities**, grant the desired tools to each bot. Bash requires both workspace and shell grants. **Web fetch** retains its existing ID/global setting and URL-only behavior; grant it for durable reading. Hortator still accepts only the genuine human owner `1482143139828596916`.
3. Inspect **Files, web reading and jobs** for actual runner readiness and selected utilities. A non-root Linux account, a compatible Bubblewrap, unprivileged namespaces, pidfds, libseccomp, Bash and the installed Python 3.14/Pillow/pip, uv, micromamba and documented utilities are needed. Readiness executes a real isolated probe. The runner fails closed when these requirements are unavailable. See [operator setup and execution boundary](SHELL_RUNNER.md).
4. Adjust the visible global working limits and per-bot **Extended file and reading tasks** fields. Advanced per-bot plugin JSON overrides the global configuration under the same validation. Provider keys, Discord credentials and document publication settings are independent.

Model commands may install Python/native packages into disposable sandbox storage; they cannot install host packages or change host policy. Installing/enabling the feature in another checkout requires the ordinary integration, backup, committed build and shared-Screen rollout procedure; the parallel implementation branch itself is not a deployment.

## Model workflow

Each available tool accepts `{}` for full usage without executing its handler. Invalid calls return all detectable argument errors and complete usage. Arguments are named JSON fields. See the permanent [tool contract](TOOLS.md).

```json
{"operation":"start","task":"compress-image"}
{"operation":"import_attachment","task":"compress-image","path":"original.png","message_id":"555555555555555555","attachment_id":"987654321"}
{"operation":"stat","task":"compress-image","path":"original.png"}
```

The three calls above use `workspace`. IDs must come from messages already observed in the authorized channel. Original image bytes up to **20 MiB** remain intact; cached pixels are reused. Expired signed URLs produce an accurate recovery message. A refreshed authenticated observation or a new upload can recover the source. Local transforms preserve the imported original and write another path.

Use `shell` for a real command, for example:

```json
{"operation":"run","task":"compress-image","command":"python3.14 -c \"from PIL import Image; im=Image.open('original.png'); print(im.size); im.convert('RGB').save('compressed.jpg',quality=60,optimize=True)\""}
```

Then call `workspace` with `{"operation":"export","task":"compress-image","path":"compressed.jpg"}` and pass its returned `artifact_id` to `council_speak.artifact_ids`. The export ceiling is independently **8,000,000 bytes**. Re-exporting an owned persistent output registers a new artifact for the current turn; cross-turn artifact references remain forbidden. Export queues no site publication and sends no Discord message itself.

For long reading, call `web_fetch` with `{"operation":"start"}`, then `{"url":"https://example.com/document"}`. Follow its exact `next` arguments to traverse the saved snapshot, or use `search` for stable excerpts and offsets. Download bytes remain capped at **1,000,000** by default and as an operator ceiling; pagination does not redownload or raise that limit. Python Unicode character offsets are distinct from file/log byte offsets and model tokens. [Web reading contract](WEB_READING.md)

## Task and active-context budgets

| Per-bot field | Default | Meaning |
| --- | --- | --- |
| `work_task_rounds` | 20 | Additional work rounds after the first successful workspace/web start; zero disables this extension |
| `work_task_calls_per_round` | 8 | Calls allowed in each subsequent extended round, executed sequentially |
| `work_task_seconds` | 900 | Elapsed extended work deadline, bounded to 30–3,600 seconds |
| `tool_working_set_tokens` | 6,000 | Maximum estimated active tool-exchange working set; reduced further to fit actual calibrated input headroom |

The existing `document_task_*` defaults and successful document-start behavior are retained. Only the **first successful task start** of any of these packs can extend a turn. Repeated starts, changing task IDs, starting shell jobs or switching between document/workspace/web tools cannot stack rounds or reset the clock. Zero additional rounds on that first start also prevents another pack from opening a later extension. Normal final-response opportunities, cancellation, provider concurrency, hourly activation and measured daily-cost checks remain in force. Exhausting the time deadline stops work and retains previously saved files; it does not promise a late Discord send.

Before every provider request, the runtime counts the assembled prompt, including schemas, fixed conversation, summary, current tool exchanges and trusted remaining-budget facts. Older large result bodies may be replaced by explicit references, then entire completed call/result pairs may leave the active prompt. Retained assistant continuation fields/signatures remain unchanged. The newest bounded result is retained whenever it fits. No helper model or unmetered summary request is used.

References explicitly state that omitted text is **not in the active prompt and has not been summarized**. They retain bounded recent progress, file/document/job handles and a unique `result_id`. Save durable notes through granted workspace or memory operations. To reread an original result, any granted workspace/web-fetch/shell pack supports `{"operation":"read_result","result_id":"result_…","offset":0,"length":2000}`. Its offsets count Unicode characters in the original serialized JSON. Fetch/file/job handles can instead reread their native content.

Complete redacted tool results are immutable in SQLite `tool_result_evidence`, alongside the original event and request trajectory. Final start-result evidence includes the budget metadata actually presented to the model. Reused provider call IDs receive distinct result IDs. Evidence reads enforce the original bot, channel and turn, and recheck all original source grants, including through chains of reread pages. Previously sent provider-request evidence is never rewritten. Evidence shares the existing trajectory retention; it does not expire when a fetched snapshot or job log expires.

## Inspection, storage and retention

Authenticated **Files, web reading and jobs** panels show bounded recent records, runner readiness, owned files, image/binary metadata, paged text and job output. Text appears as escaped source data, never executable HTML. Read-only inspection does not run model commands, publish a site or extend expiry. The dashboard remains the owner's administrative view; workspace APIs stay scoped. Granted shell networking shares host reachability, including local HTTP services; it supplies no dashboard credentials.

| Store | Location under external `HORTATOR_DATA_DIR` | Policy |
| --- | --- | --- |
| Workspaces | `workspaces/` + SQLite scope/import/turn metadata | Default 128 MiB/task, 512 MiB/bot, 32 tasks/bot, 1,000 entries/task; 30-day inactivity expiry, protected active references |
| Jobs | `jobs/` | Default 1 MiB combined output/job, 100 MiB output/bot, 200 saved job records/bot; inactive logs expire after 7 days; count exhaustion needs operator archival |
| Fetched text | `fetched_documents/` + SQLite snapshot/reference metadata | Default 50 MB/bot, 7-day fixed snapshot expiry, at most 1,000 ready snapshots and bounded expired metadata |
| Tool evidence | `council.sqlite3` | Immutable redacted trajectory evidence, with source permission dependencies |

Expiry cleanup happens on new work admission and protects active jobs/running-turn references; it never treats another store's files as disposable workspace scratch. Quota reductions do not evict existing data. Missing or expired snapshots have explicit errors and a separate refetch operation. Job restart recovery marks unfinished jobs interrupted and never replays commands. Monitor retained trajectory/storage and archive only after stopping the runtime and checking active references. [Workspace details](WORKSPACES.md), [job limits and resource scope](SHELL_RUNNER.md)

Backups/restores must include **workspaces, jobs and fetched_documents**, in addition to the existing database, matching key, artifacts, images and sites. The CLI includes these directories. Stop the runtime for a complete consistent archive; copying only SQLite cannot restore owned file bytes or logs. Restore into an empty external directory with 0700 directories/0600 files. [Backup procedure](OPERATIONS.md#backup-and-restore)

The shell has hard per-process address-space/process/file/output/time limits and finite tmpfs byte mounts. File counts are monitored during execution and strictly checked before copyback; this is not a hard aggregate cgroup resident/kernel-memory controller. Read [the exact boundary and measured limitations](SHELL_RUNNER.md#limits-and-their-scope) before changing those settings.

## Integration and acceptance boundary

Focused modules own workspace storage, the runner/worker, fetched snapshots and prompt working sets. Shared integration touches registry registration/authorization, bot fields and service validation, runtime/context assembly, authenticated API routes, CLI backup folders, console tool scope, editor/catalog hooks and isolated browser-fixture/port configuration. Site rendering, document blob writes, public sandbox/CSP, sync queues, remote transport, host/domain setup and production grants/configuration remain outside this feature.

For integration with concurrent document work, review these shared hooks together:

| Files | Integration behavior |
| --- | --- |
| `hortator/plugins.py` | Registers the packs, preserves the existing safe public HTTP helper, rechecks grants and adds immutable result IDs |
| `hortator/runtime.py`, `hortator/context.py` | Shares the once-per-turn extension with document tasks and bounds future prompt copies |
| `hortator/models.py`, `hortator/service.py` | Adds work-budget defaults, effective-limit validation and keyless presentation through existing configuration/cancellation paths |
| `hortator/app.py`, `hortator/cli.py` | Adds authenticated read-only inspection, runner shutdown and complete backup folders |
| `hortator/console.py`, `hortator/tool_feedback.py` | Adds concise tool scopes and discovery/reread guidance |
| `web/src/Editor.tsx`, `web/src/App.tsx` | Mounts the separate `AgentTools` controls and presents keyless plugins |
| `web/playwright.config.ts`, `scripts/seed_test_trajectory.py` | Isolates the browser port/data and adds synthetic private-inspection fixtures |

The document implementation and Documents UI are unchanged. Existing version-inspection tests account for the new tool-result evidence ID while checking that the underlying version/authorization response stays the same.

The automated workflow uses real local Bubblewrap/Bash/Pillow and real temporary storage, with simulated provider and Discord responses. It establishes neither live provider compatibility nor Discord visual/delivery acceptance. The owner coordinates integration and the final real council workflow. See [dated verification evidence](VERIFICATION.md).
