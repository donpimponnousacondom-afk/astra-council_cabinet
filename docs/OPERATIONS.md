# Operations

All commands in this guide run from `/home/codexy/codex/astra-council_cabinet`, the standalone repository root. Read [AGENTS.md](../AGENTS.md) for branch and documentation rules and [VERIFICATION.md](VERIFICATION.md) for dated evidence. Inspect actual Git/Screen/API state when resuming work. The user handles PRs with GitHub **Squash and merge**; after a merge, update main with a fast-forward pull and create a fresh task branch. Never push to main or push any branch without an explicit request.

## Process and storage

Use **one Uvicorn worker per data directory**. A file lock prevents two council runtimes from claiming the same database. Do not start a second bot runner, use `--reload` in production, or put multiple replicas in front of one SQLite file. Every Discord application has its own supervised client task within the one Python event loop. A failing client's connection does not take down the others.

This workspace sets `HORTATOR_DATA_DIR=/home/codexy/.local/share/hortator`, outside the Git checkout. The installed `/home/codexy/.local/bin/hortator` launcher loads `/home/codexy/.config/hortator/runtime.env`, changes to the application root, and runs `uv run hortator` with your arguments. Both files live outside Git and remain available when switching branches. The underlying CLI still falls back to `./data` if no directory is supplied, so use the launcher or source the external environment file before running the CLI directly. Storage includes:

- `council.sqlite3` plus WAL files: configuration, observed messages, context checkpoints, scoped memory, requests, turns, events, encrypted secrets, auth sessions and outbox.
- `master.key`: Fernet encryption key, unless supplied through `HORTATOR_MASTER_KEY`.
- `initial-password`: one-time bootstrap password, created only if no password is configured.
- `artifacts/`: bot/turn-owned generated media.
- `images/`: private original-image cache used by the shared multimodal pipeline.
- `sites/`: owned document drafts, immutable publication blobs and local revision data; queue metadata lives in SQLite.
- `site_history/`: separate private Git snapshots of complete sites selected for remote delivery, with job/revision metadata; no source-code submodule or credentials.
- `workspaces/`: private bot/channel/task files and generations.
- `jobs/`: isolated execution metadata and retained stdout/stderr logs.
- `fetched_documents/`: complete private text snapshots with stable reading offsets.
- `ssh/`: public SSH key exports and verified host pins in `known_hosts`. Private SSH keys stay encrypted inside the database, never as plaintext files here.
- `logs/`: private operational and rollout evidence, captured separately in complete operator archives.

The data directory is mode 0700; database/key/artifacts are owner-readable. **The master key is required to recover credentials.** Protect the key separately from the database and restrict host access. Conversations, prompt snapshots and provider response content are intentionally stored as readable observability data; secret encryption is not full-disk encryption. Never commit the data directory.

The application retains history instead of silently discarding observability. Monitor available disk space and back it up. Large contexts and many bots increase CPU, memory, database and provider usage; tune concurrency and cadence for the host. SQLite is appropriate for this single-host design; this implementation does not claim a horizontally distributed scheduler.

## Branch changes and persistent storage

Keep the database, encryption key, bootstrap password, artifacts, and logs in the external data directory. Some historical branches tracked `data/`; moving between those commits can replace or remove files inside the checkout even though the current branch ignores them. Never restore those historical files over the external live directory.

Before switching a running application's branch, stop the foreground server with Ctrl-C in Screen. Switch to the intended feature branch, rebuild `web/dist` for the selected commit (including after a squash merge even when UI code is identical), then run `hortator serve --host 127.0.0.1 --port 8000 --color`. The launcher selects the same external database on every branch. Code, dependencies, and generated UI remain in the checkout.

For branches that change database schemas, make an external backup first; separating storage from Git does not make schema changes reversible. Use an explicit `--data-dir` pointing to separate temporary storage for tests or experiments that should not use live configuration.

### Repeating the squash-merge workflow

From any normal shell prompt on this workspace, after the previous PR is merged:

```bash
hortator-next-feature
```

The user intentionally uses `feat/next_feature` as a local placeholder and renames it before pushing. This command checks the canonical repository and clean tree, fetches main, verifies that the current task and any existing placeholder are merged, and verifies the shared Screen foreground process before sending Ctrl-C. Squash merges are accepted only when GitHub CLI reports a merged PR whose exact head matches the local tip and whose merge commit is included in fetched main. Extra local commits, divergent main, another worktree owning main/the placeholder, failed merge verification, unrelated foreground commands and concurrent maintenance stop the operation. No automatic stash, reset, rebase, merge commit, branch deletion or push occurs.

The worker runs inside the existing Screen Bash and preserves attachments, startup/profile settings, scrollback and colors. It performs `git switch main`, `git pull --ff-only origin main`, prepares `feat/next_feature`, runs `uv sync --frozen`, `npm ci --prefix web`, `npm run build --prefix web`, then starts the existing external runtime launcher with `--color`. An existing placeholder at the selected main commit is reused. An older verified-merged placeholder is retained under a unique `archive/next_feature-<UTC>-<old-commit>` name before a fresh placeholder is created; named task branches remain untouched. The worker runs a temporary private copy outside the checkout so a branch switch cannot remove the running workflow.

The invoking terminal reports progress and verifies the public dashboard build stamp against the startup-captured `runtime.started` record and HTTP health without reading a dashboard password. A successful external invocation records non-secret deployment evidence in `$HORTATOR_DATA_DIR/logs/next-feature.json`. Build output and failures are visible in Screen. Refresh the browser with **Ctrl-Shift-R** after completion. The command does not attach/detach viewers automatically; `screen -x hortator` joins the existing display.

Two additional modes are available:

```bash
hortator-next-feature --check    # Fetch/check prerequisites; no stop, checkout or build
hortator-next-feature --refresh  # Build/restart the clean current task; no fetch or branch switch
```

`--refresh` is useful after committing a feature before its PR is merged. It also repairs a stale dashboard on the current task branch. Invoke from a shell prompt: text typed into the active runtime console is not a shell command. If invoked at the shared Screen's own idle Bash prompt, the workflow runs there directly and hands the terminal to the server. Missing Screen sessions are reported, not recreated automatically; follow the shared-terminal recovery procedure below if needed.

Preflight failures leave the runtime running. After shutdown, a failed dependency/build/startup step leaves an explicit failure in Screen and does not start a partially built release. Inspect the error, fix its cause and retry (use `--refresh` if the branch transition already completed). Runtime shutdown uses the existing cancellation rules. This command does not create a configuration backup or change bot/provider settings; take a complete backup before deployments that require one, especially schema changes.

The tracked shell entry point is `scripts/hortator-next-feature`, with its standard-library controller in `scripts/next_feature.py`. The workspace installation is a symlink in the user's existing PATH:

```bash
ln -s /home/codexy/codex/astra-council_cabinet/scripts/hortator-next-feature \
  /home/codexy/.local/bin/hortator-next-feature
```

Installation does not alter `.bashrc`, `.profile`, `.screenrc`, the runtime launcher or credentials. It requires the existing runtime environment/launcher plus Git, GNU Screen, Python 3.14, uv, npm and `ss`; GitHub CLI authentication is needed for squash-merge verification when ordinary ancestry cannot prove inclusion. This launcher is local operator automation, not a bot tool.

For the manual equivalent, attach with `screen -x hortator`, let active work finish, then press Ctrl-C and wait for the shared Bash prompt. Use a new task name when the placeholder already exists:

```bash
cd /home/codexy/codex/astra-council_cabinet
git status --short
test -z "$(git status --porcelain)" &&
git switch main &&
git pull --ff-only origin main &&
git switch -c feat/next_feature
```

The clean-tree guard stops if edits remain; commit them on their task branch or resolve them deliberately. `--ff-only` refuses divergent history instead of creating a merge commit. Do not work on main if the pull or new branch creation fails. A squash merge has a new commit ID, so do not merge the old feature branch back into main or rebase it automatically. Keep old local branches until their work is verified as merged; delete them only when requested. [Git pull documentation](https://git-scm.com/docs/git-pull).

After the branch is created, restore dependencies if needed, build the dashboard and start the foreground server:

```bash
uv sync --frozen &&
npm ci --prefix web &&
npm run build --prefix web &&
/home/codexy/.local/bin/hortator serve --host 127.0.0.1 --port 8000 --color
```

When the feature is finished and verified, make its local commit **before** the final build/restart. Builds embed commit metadata, so a build produced before that commit correctly identifies the older commit plus uncommitted changes. Keep all pushes and the PR squash merge under the user's control.

## Running code identity

Send **`!version`** or **`!ver`** to Hortator, or inspect the dashboard's **Running server** banner. Both show the commit, commit title and ISO 8601 commit date/time in UTC. The command also shows startup time, branch, source cleanliness and package version. These are deterministic commands and remain available while model execution is paused. The authenticated **`GET /api/version`** returns the same server metadata; `/api/status` includes it as `version`. Hortator can query `council_inspect` with `resource: "version"` when answering an owner question.

Server metadata is captured once at startup and is not recomputed on each request. A later Git checkout does not change the identity of loaded code. The dashboard embeds a separate source stamp and build timestamp during Vite's build; **Build details** shows both identities and highlights differing commits. Uncommitted builds remain explicitly labelled, even when commit IDs match; the commit alone cannot identify their local edits. The package's `0.1.0` remains compatibility metadata, not the displayed release identity. `/api/health` keeps its small existing package-version response; detailed metadata requires dashboard authentication.

Git-free archives report unavailable commit metadata unless supplied with an explicit build stamp. Docker carries the dashboard stamp into the Python package. To build a stamped image from a committed checkout:

```bash
docker compose build --build-arg HORTATOR_BUILD_INFO="$(uv run python -m hortator.version)"
```

This argument is non-secret source metadata (commit, commit title, timestamp, branch and dirty flag), not credentials. A packaged Python deployment can use the same `HORTATOR_BUILD_INFO` JSON or `hortator/_build_info.json`; Git metadata takes precedence in a real checkout. No version stamp contains a database path or runtime secrets. Build output/stamps remain ignored by Git.

## Shared GNU Screen terminal

The workspace runtime runs in the named GNU Screen session **`hortator`**, window **`dashboard`**, under the `codexy` account. Use this session for runtime commands so the operator and agent share the same terminal and output. The shell remains available after stopping the server. Screen survives terminal disconnection; it does not restart the machine or automatically restart a crashed application.

Attach, including when another terminal is already attached:

```bash
screen -x hortator
```

Press **Ctrl-A, then D** to detach and leave the dashboard running. Press **Ctrl-C** to stop the foreground server and return to the shared shell. From that shell, restart with:

```bash
hortator serve --host 127.0.0.1 --port 8000 --color
```

The server serves both the built dashboard and API at `http://127.0.0.1:8000`. To access it from a different computer, forward that port over SSH or use the configured reverse proxy. The shared shell and Screen's default directory are `/home/codexy/codex/astra-council_cabinet`.

Terminal output is saved to `/home/codexy/.local/share/hortator/logs/hortator.screen.log`, with a one-second flush interval. The user's `/home/codexy/.screenrc` sets 50,000 lines of Screen scrollback and `termcapinfo xterm* ti@:te@` for terminal scrollback. Keep that configuration. Screen's own history is available with **Ctrl-A, then [**, followed by arrows/Page Up/Page Down; **Esc** exits copy mode. Mouse-wheel/terminal history behavior also depends on the attaching terminal. Inspect runtime output without taking control (logs are private and must not be pasted without redaction):

```bash
tail -F /home/codexy/.local/share/hortator/logs/hortator.screen.log
screen -ls
```

`screen -ls` shows the current session identifier and socket directory. On this host the socket is `/run/screen/S-codexy/<pid>.hortator`; its PID changes when a new Screen session is created. The stable attach name is `hortator`.

After a reboot, if the session no longer exists, recreate it from the repository root:

```bash
cd /home/codexy/codex/astra-council_cabinet
source /home/codexy/.config/hortator/runtime.env
mkdir -p "$HORTATOR_DATA_DIR/logs"
chmod 700 "$HORTATOR_DATA_DIR/logs"
umask 077
screen -c /home/codexy/.screenrc -dmS hortator -t dashboard -L -Logfile "$HORTATOR_DATA_DIR/logs/hortator.screen.log" bash --login -i
screen -S hortator -p dashboard -X logfile flush 1
screen -S hortator -p dashboard -X stuff $'/home/codexy/.local/bin/hortator serve --host 127.0.0.1 --port 8000 --color\n'
```

An interactive login Bash reads this user's `.profile`, which sources `.bashrc` (the file is `.bashrc`, not `.bash_rc`). This loads their aliases and PS1. Do not use `--noprofile`, `--norc`, or an empty Screen configuration. The recovery on 2026-09-07 verified the login/interactive flags, nonempty prompt, and `ll` alias. Preserve an existing attachment when restarting the foreground server; recreating Screen is only necessary if the session is gone.

The agent can send commands through `screen -S hortator -p dashboard -X stuff` and inspect the same output log. Inspect the foreground process first, then use Ctrl-C to stop this server and wait for Bash to regain the terminal before entering a shell command. The CLI now closes dashboard SSE streams before draining HTTP connections, so an open dashboard does not hold shutdown indefinitely. Other HTTP requests have a ten-second drain backstop; runtime cleanup then cancels model/tool work using the existing delivery rules. Confirm process exit before startup or backup. Older branches lack this SSE fix and can require a second Ctrl-C after confirming no active work. Avoid `screen -Q` queries on this host: a session disappeared during a query and the cause is unconfirmed. Use `screen -ls`, `/proc` and the log. Screen may expand variables in `stuff`; source a local script for shell diagnostics involving `$PS1` or other shell variables. Do not start a second council runtime outside this session.

## Console inspection and filtering

The foreground `hortator serve` console receives the same redacted operational events persisted in SQLite, plus Python/Uvicorn warnings and errors. Lines include a local ISO 8601 timestamp with UTC offset, severity, scope, event kind/sequence and bot/provider identity when available. Request/turn details retain IDs for correlation with dashboard trajectories. The default is **INFO, all scopes enabled, details folded**. Healthy dashboard GETs are DEBUG; normal bot turns, provider requests, tools, compaction, deliveries and gateway changes remain visible. HTTP 4xx/5xx responses are warnings/errors. Console filters never change scheduling, grants, configuration, Discord notifications or event persistence.

In the attached Screen window, press a key without Enter:

Scope colors are consistent across events and help: system white, bots magenta, providers cyan, Discord blue, tools yellow, context green, dashboard teal. Shortcut keys stand out in bold yellow. Enabled/online states are green, disabled/error states red, and offline/idle values and long profile IDs muted. Bot/provider identities are highlighted; warning/error messages use their severity color. Expanded JSON also colors numbers, booleans and nulls. All text labels remain present in plain output and with `NO_COLOR`.

| Key | Effect |
| --- | --- |
| `+` / `-` | Increase/decrease verbosity: ERROR → WARNING → INFO → DEBUG |
| `f` | Fold/expand JSON and exception details; redisplay the last five matching entries |
| `T` / `P` | Independently cycle tools/providers through concise → event JSON → stored evidence, replaying the latest matching entry |
| `[` / `]` | Select an older/newer retained event in the selected tools/providers scope |
| `n` / `N` | Next/previous page of the selected stored evidence snapshot |
| `r` | Replay the last 20 matching entries with their original timestamps |
| `e` | Replay recent warnings/errors, using a separate 50-entry history so polling cannot displace them |
| `i` | Read-only snapshot: council enablement, bot enablement/gateway/profile/active turn, provider enablement/failure count/circuit delay |
| `d` | Toggle Discord, received-message and delivery events |
| `p` or `a` | Toggle providers and model requests |
| `w` | Toggle web/dashboard HTTP traffic; successful requests also require DEBUG (`+`) |
| `b` / `t` / `c` / `s` | Toggle bots/turns, tools, context/compaction, or system events |
| `0` | Restore INFO, all scopes, folded details |
| `?` or `h` | Show the current filters and key map |

Every filter change prints its state. Disabled scopes hide their warnings/errors too; `0` restores visibility. `i` is an explicit snapshot independent of filters. Keys do not stop or reconfigure bots. Ctrl-C still stops the foreground server; Ctrl-A then D still detaches a Screen viewer. Normal terminal scrollback remains available: there is no alternate screen, cursor repaint or screen clearing. `f` affects subsequent output and appends a short replay; it does not rewrite old scrollback.

For a terse `job.failed` line, press **`T` twice** from the default view, then use **`[` / `]`** to select the incident and **`n`** to read more. Stored evidence joins the original tool arguments and command, job state, exit code, stdout/stderr and retained tool result. A nonzero command exit with successful workspace copyback is labeled **`command_exit`**; runner/validation errors are identified separately. A command exit is still recorded as a failed job, but its output lets the operator decide whether that result was expected. Missing or expired evidence is stated explicitly, never reconstructed by rerunning the command.

Use **`P` twice** for a provider request's recorded request body, response, context, usage, timings and error. Select a `request.*` event when a provider health event has no request ID. This reads the existing trajectory, not HTTP wire logs. Tools/providers detail levels are independent of each other and of the lowercase scope switches. Document and publishing events belong to the tools scope. The ordinary concise view stays available by cycling again or pressing `0`.

Evidence pages contain at most 6,000 characters or 80 source lines. Navigation pins the selected snapshot; new live events cannot steal the reading cursor. A snapshot is bounded to 8,388,608 Unicode characters (individual job-log reads cap at 8 MiB), with explicit event/request/job/result IDs for further dashboard inspection if it reaches that limit. Full stored commands and output are available beyond the short event preview; credentials remain redacted at every detail level. The private provider evidence section includes returned reasoning captured since the reply-protocol hotfix; older discarded reasoning remains unavailable. Secret redaction is applied again before each page is displayed. The global `f` switch can also expand event JSON while the independent tools/providers depth remains selected.

The first occurrence of an incident is immediate. Identical errors/gateway transitions and repeated HTTP requests are grouped over 30 seconds, with a count and latest event reference. Different errors or bot/provider identities are separate groups. All operational occurrences remain in SQLite. The console keeps 200 recent entries plus 50 warnings/errors and loads bounded recent ledger history at startup for replay without automatic chatty backfill. Details are bounded and explicitly indicate truncation; use the event ledger/trajectory for the full stored record.

Console preferences last for this server process. Startup options also work with redirected output or noninteractive service/container logs:

```bash
hortator serve --host 127.0.0.1 --port 8000 \
  --console-level debug --console-scopes providers,bots,tools \
  --console-details --no-console-keys --no-color
```

Available scopes are `system,bots,providers,discord,tools,context,dashboard`, or `all`. Colors automatically turn off for redirected output, `TERM=dumb`, or `NO_COLOR`. Single-key input is only enabled on a foreground terminal with a terminal output stream; `--no-console-keys` leaves terminal input untouched. Terminal echo/canonical input are restored on orderly SIGINT/SIGTERM shutdown. Do not use SIGKILL for normal runtime control.

**This shared workspace inherits `NO_COLOR`.** The user explicitly wants colors in Screen, so append **`--color`** to its foreground serve command to override automatic detection and `NO_COLOR`. Do not unset the shell environment globally. `--color` and `--no-color` are mutually exclusive; without either flag, the normal automatic behavior above applies. Older branches may not support this new flag.

Known credentials, secret fields and authorization values are scrubbed. Ordinary event output still omits vendor reasoning; explicit private `P` evidence can show retained provider reasoning and partial failed streams. HTTP access logs omit query strings, headers, cookies and bodies. Explicit provider evidence inspection can display the redacted request/response bodies already retained in the trajectory. Untrusted terminal controls are escaped. Low-level Discord/HTTP transport wire-debug payloads remain disabled even at console DEBUG; safe runtime events provide the inspection data. The Screen logfile records what was displayed; it is not an unfiltered substitute for SQLite. Discord incident notifications retain their existing independent dashboard setting and throttling. A richer dashboard log panel is deferred.

## Scheduling and message semantics

**Bots → Edit bot → Capabilities → Allow intentional silence** controls the built-in `council_silence` tool independently for each bot, including Hortator. It defaults on for existing records and new bots and needs no key/global plugin switch. Turn it off for conversation/provider stress tests: the schema is removed from every generation round, the runtime asks for a text contribution, and unexpected silence calls receive explicit refusal/usage feedback within the existing budget. Provider failures, empty/reasoning-only completions and exhausted tool budgets remain visible failures. This switch does not accelerate the scheduler or bypass cooldowns, concurrency, scope or usage limits. Saving cancels an affected active turn, like other bot edits. The deterministic owner command also works: `!set bots dirac {"allow_silence":false}`; use `true` to restore it. Saved shared/persona prompts are preserved, with the current operator policy stated explicitly in runtime context.

A bot gets at most one active turn across its rooms. Its interval starts again when the turn settles. Idle evaluation is optional and begins only after there is conversation history. Pending new input is selected before idle contexts; among eligible contexts, the oldest evaluated channel runs first. Hortator requires a new owner question and never generates autonomous chatter.

Model requests for different bots may overlap, subject to global and provider concurrency limits. Discord deliveries in the same channel are serialized with the room's send gap. The bot's normal minimum send interval applies across all rooms and is measured from confirmed delivery. Its personal cooldown waits outside the room lock, so it cannot hold up another bot's delivery. There is no round-robin speaker sequence and no backlog of missed timer activations.

A **new human mention or Discord reply to a bot** bypasses that bot's activation interval and personal send cooldown. Pending directed input gets the next available scheduler slot before routine chatter. The event `activation.human_directed` records the triggering message, `human_mention`/`human_reply`, and the number of coalesced messages; the trajectory uses that trigger too. Ordinary content replies automatically reference the human message. Only authenticated live human input qualifies: other bots, webhooks, quoted mention text, repeated gateway observations and history backfill do not. A durable per-bot/message claim prevents repeated priority retries, including after restart. New input during a personal cooldown replaces the unsent draft with a fresh turn. New input during generation remains pending until that request finishes, after which a stale unsent answer is suppressed and regenerated; a Discord send already in flight is allowed to finish safely.

Priority does not bypass pause, scope, Hortator's exact-owner authorization, an active turn for the same bot, global/provider concurrency, usage limits, provider retry delays or the short shared room send gap. These limits, generation and tools can still delay an answer. Intentional silence remains available when that bot allows it. No bot/profile configuration change is needed to enable this human-priority behavior.

Prompts identify the model's own stable bot ID and verified Discord user ID. Each transcript message includes actual mention/reply targets and a viewer-specific audience: `you`, `other_participant`, `channel` or `unresolved_reply`. Known reply authors are resolved from the same-channel ledger even if the parent has been compacted out, with at most 600 characters of quoted preview; an uncached Discord reference gets a bounded same-channel lookup. Missing references remain explicitly unresolved. Human replies directed elsewhere stay visible as background, but do not independently trigger other bots; their regular autonomous evaluation and response to shared conversation remain available. Compaction and memory instructions retain speaker/recipient attribution. Existing mistaken private memories require operator review through the memory inspector; this fix does not rewrite them.

Every active model turn starts Discord typing in that bot's actual channel/thread/owner DM, before context preparation. Typing renews every five seconds through compaction, provider queueing, generation, tool calls and delivery waits. It stops renewing when the turn sends, chooses silence, fails, is cancelled, or leaves its allowed channel scope. Each bot uses its own Discord identity. Presence requests have a five-second timeout and run separately from model work; failures produce at most one redacted `discord.typing_failed` warning per turn and do not fail the turn. No partial response content is posted. Typing means a turn is active, including waiting for a provider, and does not promise a reply. Discord's last pulse may remain visible for up to ten seconds after cancellation or silence; the API provides expiry rather than an explicit stop request. See [Discord's typing endpoint](https://docs.discord.com/developers/resources/channel#trigger-typing-indicator).

Incoming messages are deduplicated by Discord message ID, because several clients can observe the same message. Context checkpoints and last-seen cursors are scoped by bot and actual channel/thread ID. Messages arriving during generation remain pending for a later activation; claiming a context never consumes future input. An edit or deletion is recorded, and future context uses the current message state; recorded old requests are not rewritten.

Changing a room's guild, channel or thread policy immediately removes its old channels from eligible work. The runtime checks current scope before sending as well. Deleted configuration identifiers are permanently retired so a new bot cannot accidentally inherit the old identity's memory, resources or statistics; use a new identifier when recreating one.

Retained contexts for old room assignments do not need deletion. Reconnect skips them before fetching history, recording `discord.history_skipped` at DEBUG without marking the bot faulty. Current assigned roots and permitted observed threads still backfill. Real `discord.history_failed` warnings include `channel_id`, `stage` (`fetch_channel`, `validate_scope` or `read_history`) and a channel-labelled error so permission failures, wrong current guild settings and history-read problems remain distinguishable. An unrelated `request.started` beside a history warning is not itself a failed provider request; use its request/turn ID to follow that outcome.

Known council bots are admitted as speakers. **Rooms → Include external bots** admits external bots and application-owned responses, including slash-command results carrying Discord's `application_id` and `webhook_id`. Ordinary incoming webhooks remain excluded. Application messages keep external/webhook attribution; their invoking human, display name or rendered text grants no owner authority or human-priority activation. Regular bots ignore command-prefixed ordinary input. Hortator routes commands before model execution, including commands supplied as owner-uploaded UTF-8 text/JSON attachments.

Intake combines ordinary content, visible embed fields and nested Components V2 text in the transcript. Each rich source is bounded to 16,000 characters plus an explicit truncation marker; components also have a 100-node/eight-level limit. Original sources are stored separately so an embed-only edit preserves ordinary text/components/attachments, while explicit empty fields clear that source. Known council answer echoes/unfurls preserve their canonical text without footers. Component buttons/custom IDs execute nothing; linked media is metadata only, while real image attachments retain the existing vision pipeline. Raw edits recheck current room admission and never resurrect deleted messages. Delayed duplicate creates do not revert newer edits. Reconnect overlaps the latest 100 stored-channel messages to recover recent skipped apps and offline edits; `discord.history_imported.overlap_observed` reports that read separately from newer backfill. Older skipped messages outside this window require reposting; no historical human priority is manufactured.

### Hortator control scope and cross-channel posts

**Council settings → Hortator control server/channel ID** defines both owner-chat intake and incident-report destination. Hortator accepts only the genuine owner's messages in that channel, its threads, or owner DMs. A mention in another channel does not bypass this scope. The former “reporting channel” label described only half of that setting's behavior. Reading other council context through the owner inspector does not open a conversation there.

The optional **Send to council channel** (`discord_send`) capability supplies an explicit outbound path. It is keyless, requires both global plugin enablement and Hortator's bot grant, and is unusable by regular bots even if mistakenly granted. New installations keep it disabled. The owner requested enabling it on this installation. Ask Hortator, for example, “Send `Testing the new route` to room `random` as a cross-post.” `random` is an example stable room ID; the tool's `targets` lists actual configured IDs. Either that ID or its channel snowflake is accepted. Observed threads are eligible only when their configured parent room includes threads; arbitrary channels, DMs and forum/category roots are not targets.

`mode: cross_post` (default) creates a labelled channel message. `mode: directed` requires `reply_to`, a real destination-channel message ID, and sends a strict native reply; deletion before sending fails rather than silently becoming an unrelated post. Both use Hortator's identity, preserve Markdown, suppress mentions and accept up to 4 artifacts owned by this bot/turn. Supplied message text is bounded to 12,000 characters; long text uses the existing full-response attachment/preview. Routing labels are `-#` subtext and remain separate from canonical context. The original model turn stays in the control channel/DM and can report the result there as normal assistant text.

Every send needs a `delivery_key` identifying the intended post within this turn. Reuse the same key and arguments to inspect/recover that attempt; a repeated call never resends it. Changed arguments with the same key are refused. `operation: status` with its returned `delivery_id` reads a retained Hortator receipt, including from a previous turn. Only `status: sent` supplies a confirmed message URL. `unknown` means Discord acceptance is uncertain: inspect the receipt/target; do not automatically retry with a new key. Private outbox `routing` and delivery events retain source/target/mode and fingerprint; target transcripts retain bot attribution and routing mode, without converting the post to owner input. Current grants, source scope, bot pause, target mapping and cooldown/room spacing are checked at dispatch; existing nonce reconciliation can resolve an uncertain receipt.

**`!help`** sends the command list inside a code block, without the repeated owner/credential preface. Longer lists are split at line boundaries into independently fenced messages within Discord's 2,000 UTF-16-unit budget. `!version` and `!ver` use the same formatting. Authorization still runs before either command.

All bots receive guidance to use Discord Markdown when helpful, and message delivery preserves it: emphasis, underline/strike, spoilers, headings/subtext, lists, quotes, links, inline code and fenced code. Ordinary prose stays outside code blocks. Long replies still use the existing full-response attachment; a truncated preview closes an open code fence before the attachment note. Mention suppression and hidden-reasoning filtering remain in force. [Discord formatting guide](https://support.discord.com/hc/en-us/articles/210298617-Markdown-Text-101-Chat-Formatting-Bold-Italic-Underline).

Long council output is sent as one message with a readable preview and `full-response.txt`. This prevents multi-chunk output from defeating cooldowns. Full generated content remains in the outbox/trajectory and is used as canonical council context. Generated media is attached to the same message; each attachment is limited to 8 MB. Forum channels must contain a thread/post; `!thread` or the Rooms panel creates it. Commands and incident reports use Hortator's control connection and are not delayed by the model's conversational cadence.

First connection imports the latest 100 messages for a configured text channel. Reconnection can backfill up to 5,000 messages after the most recent stored message; hitting that bound emits `discord.history_gap`. Discord only exposes history the application can access, so the ledger is an authoritative record of **observed** input and actual requests, not a claim to contain messages that predate installation or were never accessible. Private and archived threads may require explicit membership/permissions or a direct configured room. There is no unbounded historical scrape at installation.

## Discord message footers

Open **Bots → Edit bot → Message footer** to toggle **Show diagnostic footer**, edit its template and see an example preview. Save applies only to that bot. Hortator defaults to enabled; Ada, Socrates, Dirac, Curie and other council bots default to disabled. Existing configurations acquire these effective defaults without a database rewrite. An explicitly saved choice survives restarts and model-profile changes.

The default template is `TTFT: {{TTFT}} | TPS: {{TPS}}`. Discord receives a final subtext line such as:

```text
-# TTFT: 1858ms | TPS: 477.7
```

`-# ` is added automatically; a pasted leading prefix is accepted and normalized. Templates allow one line of 1–300 characters, with no code fences or control characters. Unknown/malformed placeholders are rejected. Substitution is literal and case-insensitive; templates cannot access credentials, request JSON or arbitrary expressions. Rendered metadata is redacted, flattened, Markdown-escaped and limited to 500 UTF-16 units. Longer metadata is visibly truncated. The example preview uses sample timings/usage, not a live measurement.

| Placeholder | Meaning |
| --- | --- |
| `{{TTFT}}` | Final generating request's time to first streamed token, rounded milliseconds including `ms` |
| `{{TPS}}` | Approximate streaming output tokens/second, one decimal place |
| `{{PROVIDER}}` | Configured provider display name |
| `{{CONTEXT}}` | Provider-reported input tokens / configured context window, e.g. `8192/262144` |
| `{{MODEL}}`, `{{MODEL SELECTED}}` | Selected model identifier for that request |
| `{{BOT}}` | Bot display name |

TTFT starts when the HTTP model request is sent, after the provider concurrency queue. The first non-empty reasoning/content/tool-argument delta counts, matching the existing trajectory metric. TPS is `output_tokens × 1000 / (duration_ms − ttft_ms)` when usage and a positive streaming interval are available. Reported completion tokens may include reasoning/tool arguments; this is an approximate generation rate, not a count of visible Discord words. Neither metric includes earlier tool/compaction requests or delivery cooldowns. Context uses measured input tokens, not a silently substituted estimate.

Missing measurements show **`—`**. Buffered responses have no measured TTFT or streaming TPS. Deterministic commands, incident notices and forum starter messages have no generating request, so their timing fields are unknown; their provider/model fields describe the selected configuration. No old request's measurements are reused. Silence does not send a footer-only message.

Owner commands work while models are paused and use the same validated, audited configuration service as the dashboard:

```text
!footer
!footer enable
!footer disable
!footer ada
!footer ada enable
!footer ada disable
!footer template TTFT: {{TTFT}} | TPS: {{TPS}}
!footer curie template {{BOT}} | {{MODEL SELECTED}} | {{PROVIDER}} | {{CONTEXT}} | {{TTFT}} | {{TPS}}
```

Omitting the bot ID targets Hortator. `enabled` and `disabled` are also accepted. Template edits preserve the enable switch and unrelated bot settings. These are ordinary bot configuration saves, so existing revision/validation/cancellation rules apply. The model's inspector remains read-only.

Footers appear outside help/version code blocks and on report attachments' accompanying messages. Model output reserves space within Discord's 2,000 UTF-16-unit budget; long answers retain their full-response attachment and a balanced preview. The footer is kept separately in `delivery.queued` evidence with its generating request ID. Canonical model content in the outbox, full-response attachment and conversation context excludes it. Known gateway echoes, history without nonces, and repeated-content embed edits preserve that canonical answer. Mention suppression and reasoning filtering remain intact.

The fields are `footer_enabled` and `footer_template` on a bot. No SQLite migration is required. After saving these fields, a branch predating them can run the existing configuration but its older schema may reject bot edits; return to compatible code or the matching backup instead of clearing the data directory.

## Context and compaction

The council's **Settings → Timezone** is the presentation clock for transcript records, model clock guidance, tool metadata, console timestamps and dashboard dates. Europe/Madrid uses +02:00 in summer and +01:00 in winter. Dates include an offset; browser/host timezone differences do not change this convention. Raw exports/request evidence keep their original values, SQLite instants stay epoch-based and build APIs retain UTC. Old memories are not rewritten. See [CONCURRENCY.md](CONCURRENCY.md#one-presentation-timezone).

The request combines runtime identity/The Boss, a shared system prompt, selected templates in order, persona, scoped persistent notes, prior summary, ordered message records, tool exchanges, and a final dynamic system message. Dynamic placeholders are literal substitutions, never executable templates. Every speaker has a trusted author ID and timestamp outside their untrusted message content.

The counter is `cl100k_base` with a 15% allowance and framing estimate, calibrated conservatively from the bot/profile's latest reported input usage. Other models may use different tokenizers; estimates are explicitly labeled. Set an honest context window and output reserve for the endpoint. Exact observed provider usage stays alongside estimates.

At the threshold, the bot summarizes the older input in bounded requests, retaining a recent tail when it fits. Very large backlogs are summarized in multiple batches. Only after every batch returns a complete, usable summary is the checkpoint advanced. A failed, truncated or oversized summary leaves the old context intact and fails the turn visibly. Individual messages or system prompts that cannot fit cause an explicit error; they are never silently dropped. Use a larger model/profile, smaller prompts or shorter memory when that occurs.

Full request snapshots preserve which summary and source messages were actually sent, including per-round tool additions. Compaction events retain before/after summaries and source boundaries. Scoped memory notes are separate, editable via the Context panel or `!memory`.

Open **Model profiles → Edit profile → Retained summary limit (tokens)** to set `summary_tokens`. It counts only the final text retained as memory using local **cl100k_base** tokenization, with no JSON/framing overhead or reasoning tokens. This is a deterministic local budget, not an exact count from every model's tokenizer and not billing usage. The former field label was **Compaction output limit**, and older releases sent it as a combined reasoning/output limit. Existing numeric values are preserved but now govern only retained text. The fixed 32,000 maximum is removed; the value and response reserve must still each remain below 60% of the configured context window. The complete rebuilt context must also fit before its checkpoint is committed.

Compaction sends **no application total-output cap**. `max_tokens`, `max_completion_tokens` and `max_output_tokens` are removed from the outgoing compaction body after merging model JSON and compaction overrides. Their saved values and normal generation behavior remain unchanged. Native reasoning effort/thinking configuration remains operator-owned. The prompt permits private reasoning, preserves attributed perspectives and asks for a complete summary comfortably below the retained-text limit. It does not ask the provider to cut off at that many reasoning-plus-output tokens.

This removes Hortator's cap, not the endpoint's defaults, model/context constraints, HTTP response-byte limit, provider deadline or bot turn deadline. An endpoint may impose a default when the cap is omitted, or require an explicit cap and reject the request; no hidden cap/retry is added. A `length` response still cannot be assumed complete. [DeepSeek's API](https://api-docs.deepseek.com/api/create-chat-completion/) documents combined input/generated context limits and its model-specific output defaults; proxy behavior must be checked from the actual request/response evidence.

Preparation and summary tokenization run off the event loop. `compaction.batch_prepared` reports preparation time, message count and tokenization probes in scope **c**. `compaction.summary_checked` records the retained-text count/limit, tokenizer, reported output/reasoning usage, finish reason and acceptance. Private reasoning remains in authenticated diagnostics rather than the retained summary. If the candidate exceeds the local text limit or is incomplete, its full returned content remains in the request record, the old checkpoint is retained, and the runtime reports the precise failure. It does not slice away the end of a memory or issue unbounded repair calls. Every batch must pass before any checkpoint advances. Use **P** or the private trajectory to inspect the actual request and candidate.

`runtime.loop_delayed` in scope **s** reports scheduling delays above one second, with active bots for correlation. Async waiting on a long tool/provider call is not proof of blocking. Unexpected background failures emit `runtime.task_failed` with identity and a bounded credential-redacted traceback; **f** expands event details and **e** recalls errors. Authenticated `/api/status.background_tasks` lists running tasks and up to 50 unexpected failures for this process. Ordinary provider failures retain their request/turn events. Independent failure does not cancel other bots; scoped child failures cancel/join related work. Shutdown joins owned work before closing storage. See [CONCURRENCY.md](CONCURRENCY.md).

Provider reasoning fields needed to continue a tool-call exchange remain in memory and in the separate private diagnostic store described below. They are scrubbed from model-readable request snapshots and Discord messages. Known inline `<think>`, `<thinking>`, `<analysis>` and `<reasoning>` blocks are removed from visible content. This cannot prove that a model never expresses reasoning inside ordinary untagged prose; the universal prompt instructs it to publish only its considered answer. The runtime never publishes vendor reasoning fields.

## Provider headers and discovery errors

Open **Providers → Edit provider → User-Agent**. The field is visible beside the API base URL and edits `headers["User-Agent"]`, the same entry exposed in **Advanced · non-secret HTTP headers**. A value applies to `GET /models` and every `POST /chat/completions`, including summaries. Header names are case-insensitive; saving normalizes this header without changing unrelated headers. Clearing it restores the HTTP client's default. Values must be one line of printable ASCII, at most 1,024 characters. Authentication remains in the encrypted credential store. There is no automatic browser impersonation, fallback rotation or assumption that changing this header will satisfy a provider's access policy. [HTTPX custom headers](https://www.python-httpx.org/quickstart/#custom-headers).

An upstream provider **401** and local `POST /api/control` **502** describe different HTTP exchanges. Hortator returns 502 when model discovery fails at the upstream boundary; a local 401 is reserved for dashboard authentication. The displayed error now identifies the provider, upstream status and local API status together. It includes a bounded provider error message when one was supplied. Transport failures with no response have an explicitly unknown upstream status, rather than an invented provider HTTP code.

`provider.discovery_failed` stores the endpoint, elapsed time, effective User-Agent, local/upstream statuses, a credential-redacted response excerpt up to 2,000 characters, truncation flag and selected diagnostic response headers. Cookies and authentication headers are excluded. Open the event ledger or console **P** for this evidence; discovery does not create a model turn or completion request. Neither successful nor failed catalog discovery changes completion failure counters. Invalid JSON/catalogs and HTTP-200 error envelopes are discovery failures too. The ordinary access log still records the local API status, with the preceding provider event explaining the upstream cause.

Do not infer Cloudflare blocking or an invalid key from a bare 401. An error may come from the provider's own client/account policy. A `cf-mitigated: challenge` response header identifies a Cloudflare Challenge Page; selected headers are retained for diagnosis, and a successful catalog can still be public. Consult the provider's documented client/account requirements when its response rejects a client. [Cloudflare challenge-response detection](https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/detect-response/).

## Streaming and response diagnostics

Each **Model profiles** card has an **SSE streaming** switch; **Edit profile** exposes the same saved field. It applies to that model profile and its selected provider, including compaction. New profiles default on; deployment preserves existing settings. All bots sharing a profile share its mode. Clone the profile if you want to compare two modes independently.

Off requests one buffered JSON response. Discord waits for the final answer in both modes. Returned usage, prices, tools and private reasoning are retained, while TTFT and streaming TPS display as unknown. The saved stream-usage preference is retained but disabled in the editor until SSE is re-enabled. The outgoing buffered body removes `stream_options`, including values in model/compaction JSON; it does not rewrite the saved JSON. The footer requires provider-reported output tokens and measured streaming timing, never a tokenizer fallback. A provider returning JSON despite an SSE request also has unknown timing; a provider returning SSE despite a buffered request is parsed safely but still gets no streaming timing claims.

The bounded parser supports UTF-8/BOM, CR/LF/CRLF, comments, multiline data, named keepalives, null packets, metadata/usage-only events, nullable optional fields and fragmented native tool calls. Compatibility counters in private response evidence explain tolerated variants. EOF after a complete final choice is accepted; a final event without the usual blank separator is accepted as explicit compatibility. A stream ending before a completion boundary fails and withholds partial output. The existing total deadline and 8 MB response-body ceiling include keepalives (this is separate from the 20 MiB image-intake limit). There is no automatic reconnect/replay of a model POST.

Folded console failures now include `error_origin`, actual response format, frame index and field where available. Uppercase **P** still cycles JSON/evidence depth; `n`/`N` page and `[`/`]` select requests. The authenticated trajectory has the same private evidence. Interpret origins as follows:

| Origin | Meaning |
| --- | --- |
| `response_format` | The received payload has invalid JSON/encoding, an unsupported shape or an invalid field. Expected/received types and a bounded redacted frame excerpt are private evidence. This does not count as provider downtime. |
| `local_client` | Python adapter/client failure. Private diagnostics show exception type and function/line locations, without locals. This does not count as provider downtime. |
| `upstream_error` | The provider explicitly sent an error, including inside HTTP 200 or a named SSE error event. Existing error-code attribution determines circuit impact. |
| `upstream_http` | An upstream HTTP failure. Actual HTTP status remains separate from the control API status. |
| `transport` | Timeout, connection/protocol failure or incomplete stream. Partial reasoning is retained; usage may be incomplete. |
| `request_configuration` / `cancelled` | Local request setup or deliberate interruption. No provider outage is inferred. |

Malformed frames are never silently skipped to manufacture a successful answer. Format errors can still be caused by an incompatible upstream response; the classification distinguishes observed evidence instead of attributing every Python exception to provider availability. Raw frames and reasoning do not enter ordinary events or Discord.

## Reasoning controls

For execution diagnostics, open **Trajectory → select a turn → requests → Provider reasoning & diagnostics**. Authenticated trajectory exports contain the same private section. The console's uppercase `P` cycles concise → JSON → stored evidence; use `n`/`N` for pages and `[`/`]` for requests. Private evidence captures native `reasoning_content`/`reasoning`/`thinking`, `reasoning_details`, tagged inline reasoning, continuation fields indexed by request message, finish reason, the actual outgoing output cap and structured provider errors. Active streams checkpoint diagnostics at most once every two seconds when packets arrive; completion, failure and cancellation save the final available partial trace. Each private capture identifies the request, provider, model and start time. It reports reasoning as present, pending, not returned, or not received before failure; reported reasoning-token usage without exposed text is stated explicitly. A missing historical capture means whether the provider returned reasoning is unknown. If a new request declared capture enabled but its record is missing, the diagnostic instead identifies a capture/storage problem. No generic claim about discarded reasoning replaces the selected request’s evidence. A provider that returns no reasoning cannot have it reconstructed by Hortator.

The private store is `request_diagnostics` inside the existing backed-up SQLite database. It is excluded from model `council_inspect`, Discord `!trajectory`, canonical answers and ordinary event payloads; credentials remain redacted. Private `P` pages can enter the existing private Screen logfile. The same provider's required native continuation stays intact in memory for subsequent tool rounds. No diagnostic is an instruction to the bots.

An HTTP 200 stream can contain an error envelope. `request.failed` records the real HTTP transport status separately from an inferred error status and whether it counts against shared provider health. Invalid request/model/context arguments do not trip other bots' provider circuit. Capacity/server failures still do. `finish_reason: length` is a completed upstream request with incomplete output: inspect the actual `max_tokens`/`max_completion_tokens` in its body. The context response reserve is a budgeting estimate, not an override of that native output limit. Provider-reported output usage may include reasoning.

Open **Model profiles → Edit profile → Reasoning**. Providers own transport and credentials; reasoning belongs to the model profile's `request_json`. There is no provider-level reasoning default. Choose the native request field supported by your endpoint/model, then set its effort, thinking mode or token budget. The field selector alone changes no JSON. **Unset** removes that field and empty parents; **Off** preserves an explicit `false`. Other fields remain intact. If a parent or field has a custom object/array structure, use **Model parameters** to change its structure deliberately. Incomplete raw JSON disables the quick controls until it is valid.

The controls cover `reasoning_effort`, `reasoning.effort`/`enabled`/`max_tokens`, common `chat_template_kwargs` thinking toggles/budgets, and `thinking` or its `type`/`budget_tokens`. They send native JSON without translating between providers. The list of effort levels is a choice of values, not a claim that every model accepts them. Arbitrary other vendor fields remain editable in **Advanced · exact request JSON**. The card and **Reasoning fields sent** preview include nested reasoning/thinking fields and disabled values. “No explicit reasoning override” means the service decides its behavior when those fields are absent; it is not a saved selection.

**Advanced · compaction parameter overrides** starts with Model parameters and replaces the top-level keys supplied in `compaction_request_json`. For example, a compaction `reasoning: {"effort":"low"}` replaces the entire generation `reasoning` object. The editor previews the resulting reasoning fields. Compaction omits combined output-cap fields even when explicitly present here; **Retained summary limit** instead checks the completed text locally, excluding reasoning. A request trajectory records the effective request under the existing redaction rules.

**Response token reserve** is space reserved by context planning. It does not set a generation cap. The card's **Generation output cap** shows any explicit `max_tokens`/`max_completion_tokens` in Model parameters; configure the supported field if you want that limit sent for normal replies. The separate **Compaction total-output cap** row shows that no cap is sent for summarization. Saved generation JSON is unchanged.

## Pause, failures and recovery

`!stop all` or **Pause council** persists the pause, cancels active provider/tool tasks, and suppresses queued output. Stopping a provider cancels its dependent bots' current turns. Editing a shared model/prompt/plugin/room cancels affected work so it cannot later post stale output. A paused bot's model stays paused after restart. Hortator's deterministic command connection remains available even while its model is disabled.

A send already accepted by Discord cannot be recalled by cancelling Python. If delivery is interrupted after dispatch and acceptance cannot be established, its outbox state is **unknown**. Unknown sends are not automatically resent. A gateway echo with a matching bot identity, channel and unique nonce can reconcile it to sent. On process startup, pending outputs become suppressed, in-progress sends become unknown, and unfinished requests/turns become interrupted. Inspect the timeline before deciding what to do next.

Provider authentication, rate-limit, network and server failures affect the provider's failure streak/circuit. Errors from an individual bot’s overridden credential do not open the shared provider circuit. Model-specific HTTP 400/404/422 configuration errors remain attributed to the request/profile/bot and do not falsely open a provider-wide circuit. After the recovery delay, one completion probe is admitted; success closes the circuit. Every attempt is recorded, with no hidden automatic request retry loop. Rate-limit `Retry-After` delays are honored when numeric. Discord's library separately handles its REST rate limits.

Gateway status/heartbeat latency is separate from provider health. Three unhealthy gateway checks (15-second checks; finite latency below 30 seconds) cause a supervised reconnect. Invalid credentials or missing Message Content Intent show a specific failed state. Fix the Portal setting and use `!restart <bot-id>` (or `restart` via the API) to retry without replacing configuration. A successful `/models` discovery probe never erases completion-failure counters.

Incident notifications are sent through Hortator to its configured reporting channel, grouped by incident kind and bot/provider with a five-minute repeat throttle. Discord reconnects have a **30-second grace period**: brief reconnect/resume pairs stay in the dashboard ledger without two chat messages. A sustained interruption reports elapsed time and the supplied cause/close code, or explicitly says Discord did not supply a reason. Recovery reports the interruption's duration; its throttle is separate from initial connection notices. Intentional client shutdown does not report a network failure. The ledger includes callback/watchdog source and previous state where available; missing metadata does not establish which side caused the interruption.

Circuit openings and recovery remain distinct events, and provider/turn failures bypass the reconnect grace period. Every recorded occurrence remains in the dashboard even when chat repeats are suppressed. If Hortator itself cannot connect, the dashboard remains the source for that failure. Failure details are scrubbed against the credential vault before persistence/reporting.

**Discover models** only requests the provider's model catalog. Some catalogs are public, so HTTP 200 does not verify a credential, generation, account limits or tool support. The API returns `authentication_verified: false` and a note explaining this limit. Successful discovery never clears completion-failure counters. Inspect a real user-requested turn for generation evidence.

## Usage and budgets

Input/output/cache/reasoning tokens and costs are recorded only if reported. Missing values remain null. Reasoning tokens are often a subset of completion tokens. Optional per-million profile prices provide **estimated** cost when reported cost is absent. Do not interpret a sum of known costs as a complete invoice if coverage is partial.

Metrics include failed/interrupted requests when usage was received, but cancellation or a broken stream can prevent the provider from reporting billable usage. Buffered responses have no measurable TTFT; streaming TTFT includes the first non-empty reasoning/content/tool-argument delta, and first visible content time is separate.

Hourly activation limits stop excessive model wakeups. The optional bot daily model-cost limit uses recorded UTC-day request costs; if a completed request has unknown cost, a cost-limited bot stops instead of assuming it was free. The threshold is checked between model calls, including tool rounds and compaction batches. A request already in flight can exceed a threshold before its cost is known. Search/image/TTS charges are separate and are not included in the chat-model cost total. The system is an operational cost monitor, not a substitute for the provider's billing ledger or hard account budget.

## Authentication and deployment

Discord owner ID is the constant `1482143139828596916`. Both command handling and shared control-service methods check authority. Hortator ignores all non-owner chat, including bots and webhooks. Its administrative inspection capability is bounded and read-only; prompt injection cannot transform it into configuration writes. Separately granted document, workspace and shell tools retain their own ownership and execution boundaries. No bot plugin has an administration API token or credential-reading capability.

The web panel requires its local administrator password. It is intended for The Boss: do not share that password. Login is rate-limited; sessions are HttpOnly/SameSite Strict, mutations require the session CSRF token, and cross-site origins are rejected. No credentials appear in GET responses or exports. API credentials can only be written through the authenticated dashboard API, not a Discord command or model tool.

For remote access, terminate TLS at a reverse proxy, preserve the original `Host` and use `HORTATOR_SECURE_COOKIES=1`. The proxy can forward the cleartext backend connection over loopback or a private container network. Do not forward authentication headers or publish bootstrap password files. Keep the API and UI on one origin; explicitly set `HORTATOR_ALLOWED_ORIGINS` only if a controlled alternate origin is necessary. The app does not trust forwarded client-IP headers by default.

Environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `HORTATOR_DATA_DIR` | Workspace: `/home/codexy/.local/share/hortator`; bare CLI fallback: `./data` | Persistent storage; use the external workspace environment on every branch |
| `HORTATOR_ADMIN_PASSWORD` | Generated file | Password override, 12+ characters; changes revoke sessions on startup |
| `HORTATOR_MASTER_KEY` | `$HORTATOR_DATA_DIR/master.key` | Fernet key; wrong keys fail closed |
| `HORTATOR_SECURE_COOKIES` | `0` | Set `1` for HTTPS deployments |
| `HORTATOR_ALLOWED_ORIGINS` | Same Host only | Comma-separated extra trusted origins |
| `HORTATOR_WEB_DIR` | `web/dist` | Built UI assets |
| `HORTATOR_BUILD_INFO` | Git metadata, otherwise unknown | Non-secret JSON source stamp for Git-free release/container builds |
| `TIKTOKEN_CACHE_DIR` | Library default | Optional tokenizer asset cache; baked into Docker image |

## Publishing SSH identity

The local operator can prepare a portable identity before configuring a server. Stop the foreground runtime in shared Screen first: the CLI takes its lock so the server's cached vault cannot miss a new credential. Use the installed launcher (or explicitly set the external data directory on another installation):

```bash
hortator ssh-key create publishing
```

This generates Ed25519 once and stores its OpenSSH private key **encrypted** at vault scope `ssh/publishing/private_key` in `council.sqlite3`. The matching `master.key` unlocks it without a separate SSH passphrase. It never creates a plaintext private key file or modifies personal `~/.ssh`. Existing identities are reused; invalid stored keys fail without rotation. The command requires an existing database/key and does not seed bots or call providers/Discord. Standard output is the single public `ssh-ed25519 ... hortator-publishing` line; standard error contains its public SHA256 fingerprint. Only that public line belongs in the remote account's `~/.ssh/authorized_keys`. See the [OpenSSH key documentation](https://man.openbsd.org/ssh-keygen#FILES).

To export the public line while the runtime is stopped:

```bash
umask 077
mkdir -p "$HORTATOR_DATA_DIR/ssh"
chmod 700 "$HORTATOR_DATA_DIR/ssh"
hortator ssh-key public publishing > "$HORTATOR_DATA_DIR/ssh/publishing.pub"
ssh-keygen -lf "$HORTATOR_DATA_DIR/ssh/publishing.pub"
```

Then restart the server normally. The public file can be read/copied while it runs. Both identity commands are local offline administration, with no Discord/model/API private-key export or bot grant. Identity names are bounded lowercase names, defaulting to `publishing`. There is deliberately no implicit overwrite/rotation command.

Normal backups include the encrypted identity in SQLite and public exports in `ssh/`. Move the complete backup and matching master key to the new installation, preserve owner-only permissions, and use its own `HORTATOR_DATA_DIR`; no source-host path is encoded in the identity. `ssh-key public publishing` can reconstruct a lost public export. The database and master key together can unlock all backed-up credentials, so transport them through a secure channel.

Key creation does **not** start remote delivery. Verify the server's host-key fingerprint through a trusted source before storing its pin in `$HORTATOR_DATA_DIR/ssh/known_hosts`. The implemented worker decrypts the named identity into a short-lived Linux memory file descriptor and invokes OpenSSH with `BatchMode=yes`, `IdentitiesOnly=yes` and `StrictHostKeyChecking=yes`. It disables password/interactive fallback, agent use/forwarding, personal SSH configuration and automatic host-key updates. Missing or changed pins fail the connection; they never prompt or silently establish trust. [OpenSSH client settings](https://man.openbsd.org/ssh_config#BatchMode). This transport runs trusted publisher code only and grants models no remote command tool.

## Automatic remote publishing

Open **Plugins → Documents & local sites** to configure publication and delivery. Grant the plugin to each desired bot independently. **Automatically publish document changes** controls whether every save becomes a local public revision and queues remote sync. **Enable remote delivery** controls the independent SSH worker. Both default off for new installations. With automatic publication off, bots use `publish` explicitly; enabling remote delivery also processes already queued eligible work. Worker activity requires the council, bot and document plugin to remain enabled and the bot to retain its grant.

The operator-selected DreamHost destination uses the following non-secret global plugin configuration. Deployment and live HTTPS verification are recorded separately in [VERIFICATION.md](VERIFICATION.md); this example does not establish current running state.

```json
{
  "local_base_url": "http://127.0.0.1:8000",
  "public_base_url": "https://council.zombiedawn.net",
  "auto_publish": true,
  "remote_enabled": true,
  "remote": {
    "host": "council.zombiedawn.net",
    "port": 22,
    "username": "theredroom",
    "identity": "publishing",
    "web_root": "/home/theredroom/council",
    "state_root": "/home/theredroom/council-publishing",
    "debounce_seconds": 5
  }
}
```

`identity` names the encrypted vault key; it is not a path or private key value. Remote account, host, roots, public URL and automation are global operator settings. The only permitted per-bot document override is `local_base_url`. The public URL must use HTTPS when remote delivery is enabled. Public and publisher-state roots must be absolute, separate directories, neither containing the other. `web_root` must already exist and be the actual domain document root. The publisher never changes the existing domain-root index or adopts an unmanaged bot/site folder.

The local host needs OpenSSH, Git and Linux memory-file support. The remote account needs passwordless SSH with the verified pin, Python 3 and Git; cron is used for the optional periodic audit. On DreamHost the generated static-serving rules use Apache rewrite/header support and symlink traversal. Server setup must verify the HTTPS site and response headers after an actual test delivery; local readiness reports are configuration checks, not proof of remote login, TLS or browser behavior. Remote enablement can be configured through the fields above or the existing authenticated plugin-save API; no private key is sent through a model tool or placed in plugin JSON.

### Save, snapshot, transfer and acknowledge

The queue is driven by successful document operations. Arbitrary workspace, shell or host-file changes are not watched or published. A save fsyncs immutable bytes and commits its revision, local published pointer and queue row together. The worker checks pending work every two seconds and waits for the configured debounce interval (default five seconds, allowed 0–60). Newer revisions may supersede older jobs that have never been attempted, while every edit remains recoverable in SQLite and the blob store.

Before an actual transfer, the worker commits the complete selected site plus job metadata to `$HORTATOR_DATA_DIR/site_history/`, a private Git repository on its `history` branch without remotes. It sends only that immutable revision to a versioned receiver installed under the configured remote state directory. The receiver verifies names, sizes, hashes, namespace ownership and its existing deployment state, then records a second private Git snapshot before atomically selecting the new public release. Neither content repository tracks the runtime database, master key, SSH credential or application checkout.

The worker marks `delivered` only after a receipt matches the exact bot, site, revision, job and manifest hash and includes a remote snapshot commit. This is different from an upload merely starting. Queue rows retain `attempts`, `retry_at`, `last_error`, `delivered_at`, `snapshot_commit` and `remote_commit`. A failure retries with bounded exponential backoff, from five seconds up to five minutes. Pause, grant removal, destination changes and shutdown stop active transport; they cannot retract a release the server already activated. The same job is retried and its receipt reconciled after interruption. Repeating `publish` cannot reset a delivered entry.

A previously attempted older job is preserved even when a newer edit arrives. The newer job waits behind that site's unresolved attempt, including its retry backoff, because the receiver may have reserved the older job before an acknowledgement was lost. Other eligible sites can proceed. Do not delete or manually reset these rows to bypass recovery: that can strand a remote reservation or obscure which content is live.

The document panel and authenticated `GET /api/documents` show worker readiness and per-site state. `revision` is the latest local edit, `published_revision` selects the local public snapshot, and `synced_revision` is the last acknowledged remote revision. `public_url` refers to that last delivery; `planned_public_url` is destination metadata only. `delivery_current` tells whether the latest edits are included. A link to an older delivery remains useful while newer work is queued, but must not be described as already updated. See [DOCUMENTS.md](DOCUMENTS.md) for the model-facing commands, paged editing and public-source copies.

The worker also retains the remote current revision and timestamp from verified receipts. An old successful receipt after restoring local data does not prove those bytes are still current remotely. If the remote host is ahead, delivery remains failed with an explicit reconciliation error; `observed_remote_revision` exposes that newer state and `delivery_current` stays false. Compare the matching database/content history and remote records before recovery rather than rewinding a remote pointer or clearing its reservation. Legacy rows without this evidence are explicitly marked `remote_currentness_verified: false`.

### Remote filesystem and namespace ownership

Public sites have the fixed form `<web_root>/<stable-bot-id>/<site-slug>/`. Each site requires an explicit create in the local tool. The bot-root path returns 404 and never lists sites or serves a bot-level index. A site's default page may link separate relative HTML/CSS/JS/SVG/media assets. The receiver reserves each namespace with ownership metadata, refuses unmanaged/colliding paths, and never lets one bot—including Hortator—write another bot's folder. Retired bot IDs stay reserved. Public source copies create independent owned files; they do not transfer ownership.

The remote layout is:

| Path | Purpose and access |
| --- | --- |
| `<web_root>/<bot-id>/.htaccess` | Receiver-managed static rules; bot root and hidden files blocked, no listings or server-side script execution |
| `<web_root>/<bot-id>/<slug>` | Atomic symlink selecting that site's complete immutable release |
| `<state_root>/releases/` | Retained public static bytes; traversable directories 0755 and asset files 0644 so Apache can serve them |
| `<state_root>/metadata/` | Private 0700 ownership, job/receipt, pending-operation and audit metadata |
| `<state_root>/history/` | Private 0700 Git repository on branch `snapshots`, containing publication and drift evidence |
| `<state_root>/receiver-<source-hash>.py` | Versioned trusted application receiver; not model-generated code |

The state root and release ancestors are traversable for static serving; metadata, Git history and receiver files retain owner-only access and are outside the web root. Previous releases remain retained. The receiver refuses a deployment that drops existing site paths; file restoration creates another revision while preserving other assets. There is no model delete, rename or ownership-transfer operation. Static suffix checks also reject compound executable names such as `report.php.html`. Local preview and remote serving both use sandboxed browser content without same-origin privileges; scripts and public-asset CORS support ordinary modular sites, while administrative APIs, credentials and server-side execution remain outside their authority.

### Periodic remote audit and recovery

The operator installer `SSHDelivery.install_audit_schedule()` in `hortator/publishing.py` installs the current receiver and runs an initial audit, then manages one remote crontab line tagged `# hortator-publishing-audit`. It runs every 15 minutes and preserves all unrelated cron entries. Re-run that installation step during a receiver upgrade so the cron line selects the intended receiver version. Creating an SSH key or enabling model tools does not install cron, and the worker's ordinary upload path does not edit the crontab.

Audits inspect registered namespaces, public release pointers and checksums. Changed observations and safely readable changed assets are snapshotted in the private remote Git history; unchanged observations do not create repetitive commits. The latest result is `<state_root>/metadata/last-audit.json`, and scheduled output goes to `metadata/cron-audit.log`. Audit detects and records drift. It does not import remote edits into local documents, repair releases, adopt folders or overwrite another process's changes.

Use the recorded local/remote snapshot commits and job IDs to trace what changed. For a normal content rollback, use the document tool's `history`, pinned `read` and single-file `restore`: this creates a new revision and follows the same publication path without deleting newer assets or rewriting history. Unexpected remote drift or ownership errors require operator inspection of the recorded evidence; do not remove metadata to force adoption. Remote snapshots complement the complete Hortator backup below. They cannot recover local drafts, bot configuration or credentials without the matching database, key and other data stores.

## Backup and restore

From the repository root, or with `--data-dir` before the command:

```bash
hortator backup /absolute/path/to/new-backup-directory
```

This uses SQLite's backup API, copies `artifacts/`, `images/`, `sites/`, `ssh/`, `workspaces/`, `jobs/`, `fetched_documents/` and `site_history/` when present, and saves the matching encryption key separately inside the new backup directory. The SSH private identity is already encrypted in SQLite; `ssh/` contains public exports and verified host pins. The separate content history is backed up with its Git metadata, not added to the application repository. The command can read a running database, but filesystem changes during a live copy need not match its database snapshot. **Stop the foreground runtime in shared Screen for a complete point-in-time archive**; merely pausing model work does not stop every writer. Move the key to separate secure storage after verifying the backup.

This workspace uses timestamped snapshots under `/home/codexy/.local/share/hortator-backups`, outside both the code checkout and live data directory. Its **`.latest-requested`** file identifies the latest requested snapshot; inspect that path and the snapshot's `manifest.json` for current evidence rather than relying on a copied session note. [VERIFICATION.md](VERIFICATION.md) records dated checks. A Git repository around a live SQLite database does not provide a consistent snapshot and risks tracking the master key and bootstrap password. Use the CLI's SQLite backup, then capture host settings and logs while the server is stopped for a complete archive. Complete snapshots include a matching key, all eight directories above, redacted configuration export, bootstrap password if present, logs, external launcher/environment and `.screenrc`, with `manifest.json` hashes and `RESTORE.md`. Verify SQLite integrity, hashes and matching-key decryption before declaring a snapshot usable. Keep directories 0700 and files 0600. These local snapshots provide rollback; an independent secure disk copy is still needed for disk-loss recovery. Never overwrite an earlier snapshot, commit these files to the application repository, or confuse a redacted JSON export with a credentials backup.

To restore, stop Hortator, copy `council.sqlite3`, `master.key` and all eight backed-up directories above into an **empty** data directory, restore owner-only permissions, then start with that directory. Do not copy old `-wal`/`-shm` files over a restored database. If using an environment master key, supply the same key. The recovery logic marks unfinished work; it never blindly replays uncertain Discord sends or interrupted shell commands. Publishing jobs resume using their same immutable identities and verify remote receipts; retain their queue records, local content history and the matching remote publisher state when relocating an installation.

To reset the dashboard password, stop the runtime and run:

```bash
hortator password
```

This prompts privately, revokes sessions, and removes the bootstrap password file. If `HORTATOR_ADMIN_PASSWORD` is set at startup, that environment value remains authoritative. `hortator doctor` shows readiness without external calls; stop the runtime before using it.

## Image, pricing and publishing operations

See [VISION.md](VISION.md) for image capture limits and upstream failures, [PRICING.md](PRICING.md) for profile cache rates and historical pricing evidence, and [DOCUMENTS.md](DOCUMENTS.md) for local drafts, publication and the durable sync queue. The [TOOLS.md](TOOLS.md) usage/error contract and document-task budgets apply to all bots.

Image and document bytes and the separate `site_history/` repository belong under external runtime storage and are included by `hortator backup`. Stop the foreground runtime for a consistent snapshot including files, history and queue metadata. Older code may not understand new profile/bot configuration fields or document tables: preserve the matching backup and use compatible code rather than clearing configuration. Published local sites are intentionally readable without dashboard login; manual-mode drafts require authentication. The sandboxed local preview permits scripts but denies dashboard origin privileges, external resources, and cross-site asset access. Persistent browser storage is unavailable in this opaque origin. Remote transport is implemented with explicit global configuration; inspect the document panel for actual readiness and delivery evidence, as described [above](#automatic-remote-publishing).

Image intake currently accepts up to **20 MiB per image**, with **40 MiB combined original image bytes per request**, eight images and 20 megapixels per image. These are separate from text-fetch, document-import and outbound-artifact limits. See [VISION.md](VISION.md) for the one-time retry of historical size rejections and unchanged pixel handling. Optional bot filesystem/Bash tools are described in [AGENTIC_TOOLS.md](AGENTIC_TOOLS.md). They preserve this image ceiling and require explicit plugin grants.

## Private tools and isolated execution

### Web search credentials and engine selection

Create a Brave Search API key in the [Brave API dashboard](https://api-dashboard.search.brave.com/) after activating a Search plan (**API Keys → Add API Key**). In Hortator, open **Plugins → Edit Web search → API key → Save credential**. Enable the plugin globally and grant **Web search** under each desired bot's **Capabilities**. DuckDuckGo needs no key. The global Brave key is shared; **Per-bot plugin key**, with **Plugin credential → Web search**, is an optional override. Saving credentials never performs a paid probe.

The editor exposes **Default search engine**: Auto (Brave then DuckDuckGo after failure/empty results), Brave only, DuckDuckGo only, and Both. Select **Both** with **Search results per engine = 5** for up to five from each, deduplicated and attributed. Bots can override engine/count per call. Save non-secret settings with **Save changes**, separately from the credential button. Existing endpoint/count settings and bot grants are preserved. A saved key flag means stored, not verified by a live search.

Failures remain visible per engine and one engine's failure does not discard usable results from the other. DuckDuckGo can return rate limits/challenges; no bypass is attempted. See [WEB_SEARCH.md](WEB_SEARCH.md) for setup, JSON fields, bounded response behavior and error categories. `web_search.*` events use the tools console scope (`t`, with `T` evidence inspection).

For the separate text reader, a **1,000,000-byte download-limit** failure occurs before pagination; small returned pages do not lift that download ceiling. Before running Bash, the bot must call **workspace** with `{"operation":"start","task":"aa-data"}`, wait for success, then reuse `aa-data` in `shell.run`. Shell has no start action and a missing task means the command was not run. Private memory writes require the explicit `operation: "write"` field even when key/value are supplied. These hints preserve existing data and strict validation; no reset is part of search setup.

### Workspace and shell setup

Enable/grant private workspaces and isolated Bash separately in the dashboard; neither needs a key. Web fetch keeps its current ID and adds durable reading/search. The tool panel exposes runner readiness, selected utilities, working limits, bounded private inspection and per-bot extended file/reading budgets. Read [setup and resource scope](SHELL_RUNNER.md) before enabling execution. Bubblewrap, namespaces/pidfds, libseccomp and the Python/Pillow toolchain are operator-supplied; no readiness failure falls back to host Bash. The existing human Screen shell and production launcher stay unchanged.

Backups now include `workspaces/`, `jobs/` and `fetched_documents/`, with their SQLite scope/reference/evidence tables. Restore the matching directories and database/key into empty external storage; stopped-runtime snapshots are needed for file/DB consistency. Job recovery marks unfinished commands interrupted, never replayed. Default inactive retention is 30 days for workspaces and seven days for fetched snapshots/job logs; active jobs/running-turn references are protected. Cleanup occurs on new work admission, so time expiry is an eligibility/read-access limit, not a promise of immediate physical erasure. Saved-job count exhaustion requires operator archival of old inactive directories. Immutable tool-result evidence follows existing trajectory retention. See the [storage policies](AGENTIC_TOOLS.md#inspection-storage-and-retention) before archiving data.

`job.*`, `workspace.*` and `web_fetch.*` operational events belong to the console tools scope (`t`). Polling stays at DEBUG; model source text and binary payloads are not printed in normal concise events. Grant/configuration edits, bot/global pause and shutdown cancel active shell jobs and wait for namespace cleanup. Remote publication configuration is independent.

For an explicitly owner-authorized parallel linked worktree, keep dependency/build/test storage isolated and use an unused test port: `HORTATOR_TEST_PORT=18110 ./scripts/check.sh`. The browser server owns temporary `/tmp/hortator-e2e-*` data and refuses port 8000. Never point its fixture/server at production storage or reuse another session’s server.


## Python 3.14 and sandbox toolchain maintenance

The application now requires Python 3.14 (`.python-version` and `requires-python >=3.14,<3.15`). Provision it with `uv python install 3.14` or use an existing operator-managed Python 3.14; leave distribution Python symlinks alone. Use `uv sync --frozen --python 3.14`. Stop the shared Screen foreground runtime before replacing its `.venv`; validate in a separate `UV_PROJECT_ENVIRONMENT` while it is still running. The `hortator-next-feature` helper also invokes Python 3.14 explicitly. Historical verification records describe the interpreters actually used then, not today's baseline. External SSH receivers retain their independent system-Python contract.

Install the OS tools and pinned micromamba bootstrap described in [SHELL_RUNNER.md](SHELL_RUNNER.md#operator-requirements-and-readiness). Keep uv and micromamba discoverable in the service PATH, then refresh runner readiness. Missing tools or another application Python version fail closed. Never use readiness as evidence that every package fits the finite limits. A public package smoke check is opt-in: `HORTATOR_REQUIRE_SANDBOX=1 HORTATOR_TEST_PACKAGE_NETWORK=1 uv run pytest -q tests/test_shell_runner.py`. It downloads a small Python package and zstd inside disposable namespaces, not into host applications.

New package storage defaults are 2 GiB/50,000 entries per job; shell address space is 2 GiB per process, with 16 processes/threads. Existing explicit global or per-bot values remain authoritative and may need deliberate operator adjustment for a package workload. All installs/caches in `/packages` disappear at job completion. Backups continue to include persistent workspaces/jobs; package scratch needs no archive. Host networking includes loopback reachability; bot filesystem credentials and the application environment remain unmounted.
