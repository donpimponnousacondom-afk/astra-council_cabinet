# Operations

## Process and storage

Use **one Uvicorn worker per data directory**. A file lock prevents two council runtimes from claiming the same database. Do not start a second bot runner, use `--reload` in production, or put multiple replicas in front of one SQLite file. Every Discord application has its own supervised client task within the one Python event loop. A failing client's connection does not take down the others.

`HORTATOR_DATA_DIR` defaults to `./data`. Storage includes:

- `council.sqlite3` plus WAL files: configuration, observed messages, context checkpoints, scoped memory, requests, turns, events, encrypted secrets, auth sessions and outbox.
- `master.key`: Fernet encryption key, unless supplied through `HORTATOR_MASTER_KEY`.
- `initial-password`: one-time bootstrap password, created only if no password is configured.
- `artifacts/`: bot/turn-owned generated media.

The data directory is mode 0700; database/key/artifacts are owner-readable. **The master key is required to recover credentials.** Protect the key separately from the database and restrict host access. Conversations, prompt snapshots and provider response content are intentionally stored as readable observability data; secret encryption is not full-disk encryption. Never commit the data directory.

The application retains history instead of silently discarding observability. Monitor available disk space and back it up. Large contexts and many bots increase CPU, memory, database and provider usage; tune concurrency and cadence for the host. SQLite is appropriate for this single-host design; this implementation does not claim a horizontally distributed scheduler.

## Shared GNU Screen terminal

The workspace runtime runs in the named GNU Screen session **`hortator`**, window **`dashboard`**, under the `codexy` account. Use this session for runtime commands so the operator and agent share the same terminal and output. The shell remains available after stopping the server. Screen survives terminal disconnection; it does not restart the machine or automatically restart a crashed application.

Attach, including when another terminal is already attached:

```bash
screen -x hortator
```

Press **Ctrl-A, then D** to detach and leave the dashboard running. Press **Ctrl-C** to stop the foreground server and return to the shared shell. From that shell, restart with:

```bash
uv run hortator serve --host 127.0.0.1 --port 8000
```

The server serves both the built dashboard and API at `http://127.0.0.1:8000`. To access it from a different computer, forward that port over SSH or use the configured reverse proxy. The session starts in `council/`.

Terminal output is saved to `data/logs/hortator.screen.log`, with a one-second flush interval and 20,000 lines of Screen scrollback. Inspect it without taking control:

```bash
tail -F data/logs/hortator.screen.log
screen -ls
```

`screen -ls` shows the current session identifier and socket directory. On this host the socket is `/run/screen/S-codexy/<pid>.hortator`; its PID changes when a new Screen session is created. The stable attach name is `hortator`.

After a reboot, if the session no longer exists, recreate it from `council/`:

```bash
mkdir -p data/logs
chmod 700 data/logs
screen -dmS hortator -t dashboard -L -Logfile "$PWD/data/logs/hortator.screen.log" bash --noprofile --norc -i
screen -S hortator -p dashboard -X logfile flush 1
screen -S hortator -p dashboard -X scrollback 20000
screen -S hortator -p dashboard -X stuff $'uv run hortator serve --host 127.0.0.1 --port 8000\n'
```

The agent can send commands through `screen -S hortator -p dashboard -X stuff` and inspect the same output log. Use Ctrl-C before entering a shell command while the server owns the foreground terminal. Do not start a second council runtime outside this session.

## Scheduling and message semantics

A bot gets at most one active turn across its rooms. Its interval starts again when the turn settles. Idle evaluation is optional and begins only after there is conversation history. Pending new input is selected before idle contexts; among eligible contexts, the oldest evaluated channel runs first. Hortator requires a new owner question and never generates autonomous chatter.

Model requests for different bots may overlap, subject to global and provider concurrency limits. Discord deliveries in the same channel are serialized with the room's send gap. The bot's minimum send interval applies across all rooms and is measured from confirmed delivery. There is no round-robin speaker sequence and no backlog of missed timer activations.

Incoming messages are deduplicated by Discord message ID, because several clients can observe the same message. Context checkpoints and last-seen cursors are scoped by bot and actual channel/thread ID. Messages arriving during generation remain pending for a later activation; claiming a context never consumes future input. An edit or deletion is recorded, and future context uses the current message state; recorded old requests are not rewritten.

Changing a room's guild, channel or thread policy immediately removes its old channels from eligible work. The runtime checks current scope before sending as well. Deleted configuration identifiers are permanently retired so a new bot cannot accidentally inherit the old identity's memory, resources or statistics; use a new identifier when recreating one.

Known council bots are admitted as speakers. External bots require room opt-in; webhooks are excluded. Regular bots ignore command-prefixed input. Hortator routes commands before model execution, including commands supplied as owner-uploaded UTF-8 text/JSON attachments.

Long council output is sent as one message with a readable preview and `full-response.txt`. This prevents multi-chunk output from defeating cooldowns. Full generated content remains in the outbox/trajectory and is used as canonical council context. Generated media is attached to the same message; each attachment is limited to 8 MB. Forum channels must contain a thread/post; `!thread` or the Rooms panel creates it. Commands and incident reports use Hortator's control connection and are not delayed by the model's conversational cadence.

First connection imports the latest 100 messages for a configured text channel. Reconnection can backfill up to 5,000 messages after the most recent stored message; hitting that bound emits `discord.history_gap`. Discord only exposes history the application can access, so the ledger is an authoritative record of **observed** input and actual requests, not a claim to contain messages that predate installation or were never accessible. Private and archived threads may require explicit membership/permissions or a direct configured room. There is no unbounded historical scrape at installation.

## Context and compaction

The request combines runtime identity/The Boss, a shared system prompt, selected templates in order, persona, scoped persistent notes, prior summary, ordered message records, tool exchanges, and a final dynamic system message. Dynamic placeholders are literal substitutions, never executable templates. Every speaker has a trusted author ID and timestamp outside their untrusted message content.

The counter is `cl100k_base` with a 15% allowance and framing estimate, calibrated conservatively from the bot/profile's latest reported input usage. Other models may use different tokenizers; estimates are explicitly labeled. Set an honest context window and output reserve for the endpoint. Exact observed provider usage stays alongside estimates.

At the threshold, the bot summarizes the older input in bounded requests, retaining a recent tail when it fits. Very large backlogs are summarized in multiple batches. Only after every batch returns a complete, usable summary is the checkpoint advanced. A failed, truncated or oversized summary leaves the old context intact and fails the turn visibly. Individual messages or system prompts that cannot fit cause an explicit error; they are never silently dropped. Use a larger model/profile, smaller prompts or shorter memory when that occurs.

Full request snapshots preserve which summary and source messages were actually sent, including per-round tool additions. Compaction events retain before/after summaries and source boundaries. Scoped memory notes are separate, editable via the Context panel or `!memory`.

Provider reasoning fields are carried in memory only when needed to continue a tool-call exchange, and scrubbed from stored request snapshots and Discord messages. Known inline `<think>`, `<thinking>`, `<analysis>` and `<reasoning>` blocks are removed from visible content. This cannot prove that a model never expresses reasoning inside ordinary untagged prose; the universal prompt instructs it to publish only its considered answer. The runtime never publishes vendor reasoning fields.

## Pause, failures and recovery

`!stop all` or **Pause council** persists the pause, cancels active provider/tool tasks, and suppresses queued output. Stopping a provider cancels its dependent bots' current turns. Editing a shared model/prompt/plugin/room cancels affected work so it cannot later post stale output. A paused bot's model stays paused after restart. Hortator's deterministic command connection remains available even while its model is disabled.

A send already accepted by Discord cannot be recalled by cancelling Python. If delivery is interrupted after dispatch and acceptance cannot be established, its outbox state is **unknown**. Unknown sends are not automatically resent. A gateway echo with a matching bot identity, channel and unique nonce can reconcile it to sent. On process startup, pending outputs become suppressed, in-progress sends become unknown, and unfinished requests/turns become interrupted. Inspect the timeline before deciding what to do next.

Provider authentication, rate-limit, network and server failures affect the provider's failure streak/circuit. Errors from an individual bot’s overridden credential do not open the shared provider circuit. Model-specific HTTP 400/404/422 configuration errors remain attributed to the request/profile/bot and do not falsely open a provider-wide circuit. After the recovery delay, one completion probe is admitted; success closes the circuit. Every attempt is recorded, with no hidden automatic request retry loop. Rate-limit `Retry-After` delays are honored when numeric. Discord's library separately handles its REST rate limits.

Gateway status/heartbeat latency is separate from provider health. Three unhealthy gateway checks (15-second checks; finite latency below 30 seconds) cause a supervised reconnect. Invalid credentials or missing Message Content Intent show a specific failed state. Fix the Portal setting and use `!restart <bot-id>` (or `restart` via the API) to retry without replacing configuration. A successful `/models` discovery probe never erases completion-failure counters.

Incident notifications are sent through Hortator to its configured reporting channel, grouped by incident kind and bot/provider with a five-minute repeat throttle. Circuit openings and recovery are distinct events. Every occurrence remains in the dashboard ledger even when repeated Discord notifications are suppressed. If Hortator itself cannot connect, the dashboard remains the source for that failure. Failure details are scrubbed against the credential vault before persistence/reporting.

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
| `HORTATOR_DATA_DIR` | `./data` | Persistent storage |
| `HORTATOR_ADMIN_PASSWORD` | Generated file | Password override, 12+ characters; changes revoke sessions on startup |
| `HORTATOR_MASTER_KEY` | `data/master.key` | Fernet key; wrong keys fail closed |
| `HORTATOR_SECURE_COOKIES` | `0` | Set `1` for HTTPS deployments |
| `HORTATOR_ALLOWED_ORIGINS` | Same Host only | Comma-separated extra trusted origins |
| `HORTATOR_WEB_DIR` | `web/dist` | Built UI assets |
| `TIKTOKEN_CACHE_DIR` | Library default | Optional tokenizer asset cache; baked into Docker image |

## Backup and restore

From `council/`, or with `--data-dir` before the command:

```bash
uv run hortator backup /absolute/path/to/new-backup-directory
```

This uses SQLite's backup API, copies artifacts, and saves the encryption key separately inside the new backup directory. It can read a running database. Move that key to separate secure storage after verifying the backup. Keep the artifacts and database from the same operational period; new artifacts created during a live backup may not be in the snapshot. For a complete point-in-time archive, pause the council first.

To restore, stop Hortator, copy `council.sqlite3`, `master.key`, and `artifacts/` into an **empty** data directory, restore owner-only permissions, then start with that directory. Do not copy old `-wal`/`-shm` files over a restored database. If using an environment master key, supply the same key. The recovery logic marks unfinished work; it never blindly replays uncertain sends.

To reset the dashboard password, stop the runtime and run:

```bash
uv run hortator password
```

This prompts privately, revokes sessions, and removes the bootstrap password file. If `HORTATOR_ADMIN_PASSWORD` is set at startup, that environment value remains authoritative. `uv run hortator doctor` shows readiness without external calls.
