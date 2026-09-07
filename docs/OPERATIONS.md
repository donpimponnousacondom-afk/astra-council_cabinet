# Operations

All commands in this guide run from `/home/codexy/codex/astra-council_cabinet`, the standalone repository root. Read [SESSION_HANDOVER.md](SESSION_HANDOVER.md) for continuity and [AGENTS.md](../AGENTS.md) for branch and documentation rules. The user handles PRs with GitHub **Squash and merge**; after a merge, update main with a fast-forward pull and create a fresh task branch. Never push to main or push any branch without an explicit request.

## Process and storage

Use **one Uvicorn worker per data directory**. A file lock prevents two council runtimes from claiming the same database. Do not start a second bot runner, use `--reload` in production, or put multiple replicas in front of one SQLite file. Every Discord application has its own supervised client task within the one Python event loop. A failing client's connection does not take down the others.

This workspace sets `HORTATOR_DATA_DIR=/home/codexy/.local/share/hortator`, outside the Git checkout. The installed `/home/codexy/.local/bin/hortator` launcher loads `/home/codexy/.config/hortator/runtime.env`, changes to the application root, and runs `uv run hortator` with your arguments. Both files live outside Git and remain available when switching branches. The underlying CLI still falls back to `./data` if no directory is supplied, so use the launcher or source the external environment file before running the CLI directly. Storage includes:

- `council.sqlite3` plus WAL files: configuration, observed messages, context checkpoints, scoped memory, requests, turns, events, encrypted secrets, auth sessions and outbox.
- `master.key`: Fernet encryption key, unless supplied through `HORTATOR_MASTER_KEY`.
- `initial-password`: one-time bootstrap password, created only if no password is configured.
- `artifacts/`: bot/turn-owned generated media.

The data directory is mode 0700; database/key/artifacts are owner-readable. **The master key is required to recover credentials.** Protect the key separately from the database and restrict host access. Conversations, prompt snapshots and provider response content are intentionally stored as readable observability data; secret encryption is not full-disk encryption. Never commit the data directory.

The application retains history instead of silently discarding observability. Monitor available disk space and back it up. Large contexts and many bots increase CPU, memory, database and provider usage; tune concurrency and cadence for the host. SQLite is appropriate for this single-host design; this implementation does not claim a horizontally distributed scheduler.

## Branch changes and persistent storage

Keep the database, encryption key, bootstrap password, artifacts, and logs in the external data directory. Some historical branches tracked `data/`; moving between those commits can replace or remove files inside the checkout even though the current branch ignores them. Never restore those historical files over the external live directory.

Before switching a running application's branch, stop the foreground server with Ctrl-C in Screen. Switch to the intended feature branch, rebuild `web/dist` if UI source changed (run `npm ci --prefix web` first if its dependencies changed), then run `hortator serve --host 127.0.0.1 --port 8000`. The launcher selects the same external database on every branch. Code, dependencies, and generated UI remain in the checkout.

For branches that change database schemas, make an external backup first; separating storage from Git does not make schema changes reversible. Use an explicit `--data-dir` pointing to separate temporary storage for tests or experiments that should not use live configuration.

### Repeating the squash-merge workflow

After the previous PR is merged, attach with `screen -x hortator`, let active work finish, then press Ctrl-C and wait for the shared Bash prompt. In that shell, replace `feat/next_feature` with the new task name:

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
/home/codexy/.local/bin/hortator serve --host 127.0.0.1 --port 8000
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
hortator serve --host 127.0.0.1 --port 8000
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
screen -S hortator -p dashboard -X stuff $'/home/codexy/.local/bin/hortator serve --host 127.0.0.1 --port 8000\n'
```

An interactive login Bash reads this user's `.profile`, which sources `.bashrc` (the file is `.bashrc`, not `.bash_rc`). This loads their aliases and PS1. Do not use `--noprofile`, `--norc`, or an empty Screen configuration. The recovery on 2026-09-07 verified the login/interactive flags, nonempty prompt, and `ll` alias. Preserve an existing attachment when restarting the foreground server; recreating Screen is only necessary if the session is gone.

The agent can send commands through `screen -S hortator -p dashboard -X stuff` and inspect the same output log. Inspect the foreground process first, then use Ctrl-C to stop this server and wait for Bash to regain the terminal before entering a shell command. The CLI now closes dashboard SSE streams before draining HTTP connections, so an open dashboard does not hold shutdown indefinitely. Other HTTP requests have a ten-second drain backstop; runtime cleanup then cancels model/tool work using the existing delivery rules. Confirm process exit before startup or backup. Older branches lack this SSE fix and can require a second Ctrl-C after confirming no active work. Avoid `screen -Q` queries on this host: a session disappeared during a query and the cause is unconfirmed. Use `screen -ls`, `/proc` and the log. Screen may expand variables in `stuff`; source a local script for shell diagnostics involving `$PS1` or other shell variables. Do not start a second council runtime outside this session.

## Console inspection and filtering

The foreground `hortator serve` console receives the same redacted operational events persisted in SQLite, plus Python/Uvicorn warnings and errors. Lines include a local ISO 8601 timestamp with UTC offset, severity, scope, event kind/sequence and bot/provider identity when available. Request/turn details retain IDs for correlation with dashboard trajectories. The default is **INFO, all scopes enabled, details folded**. Healthy dashboard GETs are DEBUG; normal bot turns, provider requests, tools, compaction, deliveries and gateway changes remain visible. HTTP 4xx/5xx responses are warnings/errors. Console filters never change scheduling, grants, configuration, Discord notifications or event persistence.

In the attached Screen window, press a key without Enter:

| Key | Effect |
| --- | --- |
| `+` / `-` | Increase/decrease verbosity: ERROR → WARNING → INFO → DEBUG |
| `f` | Fold/expand JSON and exception details; redisplay the last five matching entries |
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

The first occurrence of an incident is immediate. Identical errors/gateway transitions and repeated HTTP requests are grouped over 30 seconds, with a count and latest event reference. Different errors or bot/provider identities are separate groups. All operational occurrences remain in SQLite. The console keeps 200 recent entries plus 50 warnings/errors and loads bounded recent ledger history at startup for replay without automatic chatty backfill. Details are bounded and explicitly indicate truncation; use the event ledger/trajectory for the full stored record.

Console preferences last for this server process. Startup options also work with redirected output or noninteractive service/container logs:

```bash
hortator serve --host 127.0.0.1 --port 8000 \
  --console-level debug --console-scopes providers,bots,tools \
  --console-details --no-console-keys --no-color
```

Available scopes are `system,bots,providers,discord,tools,context,dashboard`, or `all`. Colors automatically turn off for redirected output, `TERM=dumb`, or `NO_COLOR`. Single-key input is only enabled on a foreground terminal with a terminal output stream; `--no-console-keys` leaves terminal input untouched. Terminal echo/canonical input are restored on orderly SIGINT/SIGTERM shutdown. Do not use SIGKILL for normal runtime control.

Known credentials, secret fields, authorization values and vendor reasoning content are scrubbed. HTTP query strings, headers, cookies and bodies are not logged. Untrusted terminal controls are escaped. Low-level Discord/HTTP transport wire-debug payloads remain disabled even at console DEBUG; safe runtime events provide the inspection data. The Screen logfile records what was displayed; it is not an unfiltered substitute for SQLite. Discord incident notifications retain their existing independent dashboard setting and throttling. A richer dashboard log panel is deferred.

## Scheduling and message semantics

A bot gets at most one active turn across its rooms. Its interval starts again when the turn settles. Idle evaluation is optional and begins only after there is conversation history. Pending new input is selected before idle contexts; among eligible contexts, the oldest evaluated channel runs first. Hortator requires a new owner question and never generates autonomous chatter.

Model requests for different bots may overlap, subject to global and provider concurrency limits. Discord deliveries in the same channel are serialized with the room's send gap. The bot's minimum send interval applies across all rooms and is measured from confirmed delivery. There is no round-robin speaker sequence and no backlog of missed timer activations.

Every active model turn starts Discord typing in that bot's actual channel/thread/owner DM, before context preparation. Typing renews every five seconds through compaction, provider queueing, generation, tool calls and delivery waits. It stops renewing when the turn sends, chooses silence, fails, is cancelled, or leaves its allowed channel scope. Each bot uses its own Discord identity. Presence requests have a five-second timeout and run separately from model work; failures produce at most one redacted `discord.typing_failed` warning per turn and do not fail the turn. No partial response content is posted. Typing means a turn is active, including waiting for a provider, and does not promise a reply. Discord's last pulse may remain visible for up to ten seconds after cancellation or silence; the API provides expiry rather than an explicit stop request. See [Discord's typing endpoint](https://docs.discord.com/developers/resources/channel#trigger-typing-indicator).

Incoming messages are deduplicated by Discord message ID, because several clients can observe the same message. Context checkpoints and last-seen cursors are scoped by bot and actual channel/thread ID. Messages arriving during generation remain pending for a later activation; claiming a context never consumes future input. An edit or deletion is recorded, and future context uses the current message state; recorded old requests are not rewritten.

Changing a room's guild, channel or thread policy immediately removes its old channels from eligible work. The runtime checks current scope before sending as well. Deleted configuration identifiers are permanently retired so a new bot cannot accidentally inherit the old identity's memory, resources or statistics; use a new identifier when recreating one.

Known council bots are admitted as speakers. External bots require room opt-in; webhooks are excluded. Regular bots ignore command-prefixed input. Hortator routes commands before model execution, including commands supplied as owner-uploaded UTF-8 text/JSON attachments.

**`!help`** sends the command list inside a code block, without the repeated owner/credential preface. Longer lists are split at line boundaries into independently fenced messages within Discord's 2,000 UTF-16-unit budget. `!version` and `!ver` use the same formatting. Authorization still runs before either command.

All bots receive guidance to use Discord Markdown when helpful, and message delivery preserves it: emphasis, underline/strike, spoilers, headings/subtext, lists, quotes, links, inline code and fenced code. Ordinary prose stays outside code blocks. Long replies still use the existing full-response attachment; a truncated preview closes an open code fence before the attachment note. Mention suppression and hidden-reasoning filtering remain in force. [Discord formatting guide](https://support.discord.com/hc/en-us/articles/210298617-Markdown-Text-101-Chat-Formatting-Bold-Italic-Underline).

Long council output is sent as one message with a readable preview and `full-response.txt`. This prevents multi-chunk output from defeating cooldowns. Full generated content remains in the outbox/trajectory and is used as canonical council context. Generated media is attached to the same message; each attachment is limited to 8 MB. Forum channels must contain a thread/post; `!thread` or the Rooms panel creates it. Commands and incident reports use Hortator's control connection and are not delayed by the model's conversational cadence.

First connection imports the latest 100 messages for a configured text channel. Reconnection can backfill up to 5,000 messages after the most recent stored message; hitting that bound emits `discord.history_gap`. Discord only exposes history the application can access, so the ledger is an authoritative record of **observed** input and actual requests, not a claim to contain messages that predate installation or were never accessible. Private and archived threads may require explicit membership/permissions or a direct configured room. There is no unbounded historical scrape at installation.

## Context and compaction

The request combines runtime identity/The Boss, a shared system prompt, selected templates in order, persona, scoped persistent notes, prior summary, ordered message records, tool exchanges, and a final dynamic system message. Dynamic placeholders are literal substitutions, never executable templates. Every speaker has a trusted author ID and timestamp outside their untrusted message content.

The counter is `cl100k_base` with a 15% allowance and framing estimate, calibrated conservatively from the bot/profile's latest reported input usage. Other models may use different tokenizers; estimates are explicitly labeled. Set an honest context window and output reserve for the endpoint. Exact observed provider usage stays alongside estimates.

At the threshold, the bot summarizes the older input in bounded requests, retaining a recent tail when it fits. Very large backlogs are summarized in multiple batches. Only after every batch returns a complete, usable summary is the checkpoint advanced. A failed, truncated or oversized summary leaves the old context intact and fails the turn visibly. Individual messages or system prompts that cannot fit cause an explicit error; they are never silently dropped. Use a larger model/profile, smaller prompts or shorter memory when that occurs.

Full request snapshots preserve which summary and source messages were actually sent, including per-round tool additions. Compaction events retain before/after summaries and source boundaries. Scoped memory notes are separate, editable via the Context panel or `!memory`.

Provider reasoning fields are carried in memory only when needed to continue a tool-call exchange, and scrubbed from stored request snapshots and Discord messages. Known inline `<think>`, `<thinking>`, `<analysis>` and `<reasoning>` blocks are removed from visible content. This cannot prove that a model never expresses reasoning inside ordinary untagged prose; the universal prompt instructs it to publish only its considered answer. The runtime never publishes vendor reasoning fields.

## Reasoning controls

Open **Model profiles → Edit profile → Reasoning**. Providers own transport and credentials; reasoning belongs to the model profile's `request_json`. There is no provider-level reasoning default. Choose the native request field supported by your endpoint/model, then set its effort, thinking mode or token budget. The field selector alone changes no JSON. **Unset** removes that field and empty parents; **Off** preserves an explicit `false`. Other fields remain intact. If a parent or field has a custom object/array structure, use **Model parameters** to change its structure deliberately. Incomplete raw JSON disables the quick controls until it is valid.

The controls cover `reasoning_effort`, `reasoning.effort`/`enabled`/`max_tokens`, common `chat_template_kwargs` thinking toggles/budgets, and `thinking` or its `type`/`budget_tokens`. They send native JSON without translating between providers. The list of effort levels is a choice of values, not a claim that every model accepts them. Arbitrary other vendor fields remain editable in **Advanced · exact request JSON**. The card and **Reasoning fields sent** preview include nested reasoning/thinking fields and disabled values. “No explicit reasoning override” means the service decides its behavior when those fields are absent; it is not a saved selection.

**Advanced · compaction parameter overrides** starts with Model parameters and replaces the top-level keys supplied in `compaction_request_json`. For example, a compaction `reasoning: {"effort":"low"}` replaces the entire generation `reasoning` object. The editor previews the resulting reasoning fields, and the configured summary output limit still applies. A request trajectory records the effective request under the existing redaction rules.

**Response token reserve** is space reserved by context planning. It does not set a generation cap. The card's **Output limit sent** shows any explicit `max_tokens`/`max_completion_tokens` in Model parameters; configure the supported field if you want that limit sent. The agent does not change existing model JSON merely by adding these controls.

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

Discord owner ID is the constant `1482143139828596916`. Both command handling and shared control-service methods check authority. Hortator ignores all non-owner chat, including bots and webhooks. Its model has only a bounded read-only inspector; prompt injection cannot transform that tool into configuration writes. Regular bot plugins have no administration API token or credential-reading capability.

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

## Backup and restore

From the repository root, or with `--data-dir` before the command:

```bash
hortator backup /absolute/path/to/new-backup-directory
```

This uses SQLite's backup API, copies artifacts, and saves the encryption key separately inside the new backup directory. It can read a running database. Move that key to separate secure storage after verifying the backup. Keep the artifacts and database from the same operational period; new artifacts created during a live backup may not be in the snapshot. For a complete point-in-time archive, pause the council first.

This workspace uses timestamped snapshots under `/home/codexy/.local/share/hortator-backups`, outside both the code checkout and live data directory. A Git repository around a live SQLite database does not provide a consistent snapshot and risks tracking the master key and bootstrap password. Use the CLI's SQLite backup, then capture host settings and logs while the server is stopped for a complete archive. The snapshots listed in [SESSION_HANDOVER.md](SESSION_HANDOVER.md) include a matching key, artifacts, redacted configuration export, bootstrap password if present, logs, external launcher/environment and `.screenrc`, with `manifest.json` hashes and `RESTORE.md`. They passed SQLite integrity and decryption checks. Keep directories 0700 and files 0600. These local snapshots provide rollback; an independent secure disk copy is still needed for disk-loss recovery. Never overwrite an earlier snapshot, commit these files, or confuse a redacted JSON export with a credentials backup.

To restore, stop Hortator, copy `council.sqlite3`, `master.key`, and `artifacts/` into an **empty** data directory, restore owner-only permissions, then start with that directory. Do not copy old `-wal`/`-shm` files over a restored database. If using an environment master key, supply the same key. The recovery logic marks unfinished work; it never blindly replays uncertain sends.

To reset the dashboard password, stop the runtime and run:

```bash
hortator password
```

This prompts privately, revokes sessions, and removes the bootstrap password file. If `HORTATOR_ADMIN_PASSWORD` is set at startup, that environment value remains authoritative. `hortator doctor` shows readiness without external calls; stop the runtime before using it.
