# Session handover: Astra Council Cabinet / Hortator

Updated 2026-09-08 for per-bot Discord diagnostic footers, after the user squash-merged logging in PR #7 and prepared `feat/debug_footer`. This document carries the project and operating context into a new agent session. Read [AGENTS.md](../AGENTS.md) first; it contains the user's repository boundary, branch and documentation rules. The application is still named **Hortator Council** in its package, CLI and dashboard; the repository directory is **astra-council_cabinet**. Continue this implementation and preserve the user's external configuration.

## Canonical workspace and Git

- **Only working repository:** `/home/codexy/codex/astra-council_cabinet`. Application files (`hortator/`, `web/`, `pyproject.toml`) are directly under this root. Persistent data and host launch settings live outside the checkout, at the user's explicit request; see the recovery section below.
- **Current task branch:** `feat/debug_footer`, already prepared by the user from main/origin-main `a350990` (squashed logging PR #7). Its starting tree exactly matched the prior local logging tip `1fbaae8`. The old local logging branch remains; no further branch deletion was requested. The agent did not repeat the user's transition or push anything. After a merged PR, the standing workflow is clean tree → fast-forward main → new `feat/`, `hotfix/` or `dev/` branch, with the runtime stopped before checkout. See [OPERATIONS.md](OPERATIONS.md#repeating-the-squash-merge-workflow) for copy/paste commands. Keep new commits linear; the user handles PRs and **Squash and merge**. **NEVER PUSH TO MAIN.** No push is authorized by this handover. Do not create local implementation/merge commits on main, bypass the guard, force-push, or rewrite existing commits. Additional hook work is deferred.
- Historical branch cleanup: typing PR #4 was merged as `2a20ca0`, and its obsolete local branch was removed after tree equality was checked. The original typing base was `dev/initial_phase` at `737c6bf`; that local development branch was already absent. Do not recreate those historical branches merely because they appear in older notes.
- The user copied/restored this project and its Git history into this destination. The three original incoming commits were `81c4686 Initial`, `c4b5738 Second phase`, and `9447ba6 first release`. The initial migration added a handover commit. No remotes existed at that original inspection; an `origin` remote and additional branches were present at the later Screen recovery. Inspect current Git state instead of assuming the original history is still the branch tip. No push is authorized by this handover.
- The former `/home/codexy/codex/t3-code` workspace is explicitly out of scope. The user will clean it separately. Do not run Git there, edit it, use it as a worktree, restore from it, or synchronize this repository back into it.
- `.githooks/pre-push` blocks every update or deletion targeting `refs/heads/main`. This repository has `core.hooksPath=.githooks`; on a fresh clone install that setting again. The guard was tested locally without contacting a remote.

### Copied local files and history

The incoming commits tracked the runtime database/WAL, bootstrap password, master encryption key, logs, installed Node dependencies, generated web build, test screenshots and Python caches. The original migration removed 6,275 such files **from Git tracking only**, preserving their local copies at that time. A root `.gitignore` prevents staging them again on the current branch; older branches still tracked them. Later, the database/key/build were found missing during Screen recovery. Do not treat the original preservation check as evidence that the old runtime files still exist. Lockfiles, source, tests and deployment files remain tracked.

**The earlier three commits still contain the bootstrap password and encryption key.** Removing files from the current tree does not remove historical copies. No history rewrite, credential rotation or remote publication was performed. Address that history in a separately scoped task before sharing the repository elsewhere. At migration inspection, no provider keys or Discord bot tokens were configured; the only stored secret was the dashboard password hash. Never print secret contents in reports or commit them again.

## Runtime recovery and external storage

The user reported that Screen crashed, then explained that they had been moving between Git branches and requested that runtime data be independent of the checkout. At recovery inspection, the old Screen session and API were gone, and the checkout had no `council.sqlite3`, `master.key`, or production dashboard build. The exact cause of the Screen crash was not established. No current database/key backup was located in the inspected workspace and known external Hortator locations; the retired workspace was left untouched.

Persistent storage is now `/home/codexy/.local/share/hortator`, including its database, encryption key, bootstrap password, artifacts, and logs. The remaining local artifact directory and recovery log were moved there. A fresh disabled-draft setup was initialized at that recovery; previous runtime configuration was **not recovered**, and no historical credentials were restored from Git. **The user subsequently configured five real bots and Featherless through the dashboard. That current setup exists and is backed up; do not initialize, clear, or replace it with drafts.**

The executable `/home/codexy/.local/bin/hortator` loads `/home/codexy/.config/hortator/runtime.env`, changes to this repository, and runs `uv run hortator` with its arguments. Both host files live outside Git. The environment file exports the absolute `HORTATOR_DATA_DIR`, so older branches with a `./data` fallback use the same external storage. The Screen shell also sources that file, and Screen exports the same directory to future windows. Data/config directories are mode 0700, the environment file is 0600, and the launcher is 0700.

Use the external launcher for serve, backup, doctor, and password commands. Stop the server before switching branches, rebuild the dashboard when UI source changes, and restart through the launcher. Back up before trying schema-changing branches; external storage protects against checkout/cleanup operations, not application migrations. See [OPERATIONS.md](OPERATIONS.md#branch-changes-and-persistent-storage).

### Configured state and snapshots

**The user considers configuration complete.** They confirmed that Hortator's new provider and Kimi K3 are working, the context issue is resolved, and Qwen uses two concurrent requests for the plan's four units. They also confirmed the Discord typing indicator works live. Do not reopen the earlier provider-plan/credential/model-ID corrections or change settings based on the historical [configuration audit](CONFIGURATION_STATUS.md). No weighted concurrency change is required for this resolved Qwen pool.

Ada, Socrates, Dirac and Curie were still disabled in the new pre-refinement snapshot; Hortator was enabled. Preserve newer dashboard changes and leave full council activation/acceptance to the user. No live settings, credentials, tool grants or activation flags were changed for the refinements. The original audit's five distinct Discord identities and effective permissions passed read-only checks; optional media/search capabilities remain subject to whichever grants/endpoints the user chooses.

Verified snapshots live outside Git under `/home/codexy/.local/share/hortator-backups`:

- `20260907T113338Z-configured-before-typing`: complete stopped-runtime archive before implementation; at that moment all five model activation flags were false.
- `20260907T115803Z-configured-before-deploy`: complete stopped-runtime archive before deploying typing; Hortator's model is enabled, the four council models are disabled. The timeout was still 120 seconds at this snapshot.
- `20260907T120654Z-configured-final`: consistent live SQLite snapshot at 14:06 Europe/Madrid, including the user's 360-second Featherless timeout, new Ollama provider/credential/profile and Hortator assignment. All configuration bodies and credential scopes matched the live database immediately after capture.
- `20260907T121700Z-configured-final`: consistent live SQLite snapshot at **14:17 Europe/Madrid**, preserving the user's then-disabled Ollama provider. Both live snapshots passed standalone database integrity, all nine file hashes and decryption of **13** encrypted entries. Logs/artifacts were copied separately during live capture, not as an atomic filesystem snapshot. Validation used immutable reads and excluded temporary WAL/SHM sidecars.
- `20260907T124253Z-before-refinements`: stopped-runtime snapshot before refinement edits. SQLite integrity, all nine file hashes, owner-only permissions and decryption of **13** encrypted records passed. Retained as an older rollback point.
- **Latest: `20260908T001457Z-before-debug-footer`**: stopped-runtime snapshot at 02:14:57 Europe/Madrid before footer deployment, with no active turns/requests or pending/sending deliveries. SQLite integrity, **11** file hashes, owner-only permissions and decryption of **14** encrypted records passed. All entity bodies/revisions and encrypted entries exactly matched the stopped live database. Hortator enabled, Ada/Socrates/Dirac/Curie disabled. Host settings, artifacts and logs are included. `.latest-requested` now points here; this captures the user's newer configuration that the September 7 archives did not cover.

Each contains SQLite, the matching master key, artifacts, a redacted configuration export, bootstrap password if present, logs and the external launcher/environment/Screen configuration. The first two archives passed SQLite integrity, SHA-256 checks and decryption of their 12 encrypted entries; the final snapshot includes the additional Ollama secret. Directories are 0700, files 0600; `RESTORE.md` in each snapshot gives restoration steps. The `.latest-requested` path file in the backup root identifies the most recent snapshot. Use timestamped SQLite snapshots rather than Git around a live database/key. No backup remote or automatic schedule was configured. A separate disk copy is still needed for disk-loss recovery.

## What the user is building

The user requested a complete, usable Discord council, grounded in Hermes Agent's Discord connector and DeepSeek Harness's Trajectory inspection. This is already an implementation, not an unexecuted proposal. Preserve these decisions:

1. Every bot is a separate Discord OAuth application with its own token and visible identity. One supervised Python process hosts the clients, scheduler, providers, plugins and API; the React/Vite dashboard is served as built static assets by that same process.
2. Providers own transport settings and shared credentials; reusable model profiles own model identity, context limits and arbitrary vendor parameter JSON; bots own personalities, instructions, capabilities, cadence and optional credential overrides. Swapping a profile must preserve the bot's personality and memory. Profiles can be cloned for different reasoning/sampling/context variants.
3. Global and per-bot plugin grants control web fetch/search, image generation, TTS, scoped memory and Hortator inspection. Standard OpenAI-compatible tool calls support bounded multi-round execution. The user deliberately wants an advanced raw JSON editor, not a growing vendor-parameter translation layer.
4. Council bots evaluate independently on their timers and may speak, reply or remain silent. Per-bot send cooldowns and room serialization prevent bursts; incoming messages remain queued while a turn runs. Bots can converse with other council members and permitted humans.
5. Context is isolated by bot and actual Discord channel/thread/DM. Global instructions, reusable prompts, persona, memory, prior summary, timed transcript and a final dynamic prompt form each request. Bounded automatic/manual compaction records before/after summaries and source boundaries without erasing history.
6. Trajectories must explain actual requests and context assembly, tools, usage, timing, compaction and messages sent or withheld. Preserve explicit unknown/missing measurements and uncertain delivery states. Provider failures, model/configuration failures and gateway failures have separate attribution.
7. **The Boss:** `.normal.man.`, immutable Discord snowflake **`1482143139828596916`**. Only that genuine human identity can command or converse with Hortator. No display name, role, webhook or model text grants authority. The universal prompt identifies The Boss, but authorization is enforced outside the model.
8. Hortator reports in a separate channel and supports owner DMs. Deterministic commands such as `!stop`, `!status`, profile/prompt/provider changes bypass the model. Owner questions can invoke a model with bounded, read-only inspection tools. API keys/tokens are entered only in the authenticated dashboard. Never publish vendor reasoning fields to Discord.

The original source investigation, pinned Hermes/DSH references and accepted work packages are in [PLAN.md](PLAN.md). The user values a finished implementation, fine observability, direct action and visible shared terminal control. Do not begin another architecture proposal or recreate the scaffold when resuming.

## Current implementation map

The current console feature adds `hortator/console.py` and bridges redacted persisted events from `Store.emit` into CLI logging. INFO shows useful operational events; successful dashboard requests require DEBUG. Local keys: `+/-` verbosity, `f` folded/expanded JSON/errors, `d` Discord, `p/a` providers, `w` web/dashboard, `b/t/c/s` other scopes, `r` recent events, `e` recent errors, `i` read-only bot/provider state, `0` defaults and `?` help. Repeated incidents are summarized over 30 seconds. The append-only view preserves Screen scrollback and terminal restoration. Console controls never alter configuration or Discord incident policy. See OPERATIONS for flags, replay bounds and full behavior. A richer dashboard logging panel is a later user task.

The preceding explanatory review made no code/configuration changes: tool limits count sequential batches, Council inspector/memory/web fetch need no keys, and private memory is visible in each bot card's Context capacity panel. The proposed clearer tool-limit labels and credential form treatment remain UI follow-ups, not part of this logging feature. At that read-only check, no persistent notes existed; this is a dated observation, not a claim about newer user activity.

On the user's 2026-09-07 console color follow-up, the same logging branch adds distinct scope colors, highlighted help keys and identities, green/red state values, muted idle/profile metadata and JSON scalar colors. This is presentation only; key bindings, filtering, redaction and terminal behavior are preserved.

Live inspection found that the server inherits `NO_COLOR`, disabling automatic ANSI output. The current Screen command therefore appends `--color`, an explicit per-process override requested by the user. Preserve that flag on this branch's restarts; leave the shell environment unchanged. Automatic/no-color behavior remains available for other deployments.

| Area | Files and purpose |
| --- | --- |
| Configuration and persistence | `hortator/models.py`, `store.py`: typed entities, SQLite WAL, references/revisions, identity tombstones, channel scope, contexts, memory, events, turns, requests, outbox and artifacts |
| Security and control | `security.py`, `service.py`, `app.py`: encrypted vault, exact-owner checks, password sessions/CSRF/origins, shared audited controls and authenticated API |
| Provider calls | `provider.py`: compatible HTTP/SSE, arbitrary JSON, concurrency/circuit recovery, credential overrides, token/cost/timing reporting and partial-output evidence |
| Agent execution | `context.py`, `runtime.py`: prompt assembly, conservative estimates/calibration, compaction, independent activation, tool rounds, cancellation, cooldowns and durable delivery |
| Discord | `discord_gateway.py`: client per application, reconnect/backfill, threads/DMs, token identity checks, commands, notifications, long-output attachments and nonce reconciliation |
| Plugins | `plugins.py`: global/per-bot registry, capability enforcement, public-network fetch boundary, media artifacts, memory and read-only council inspection |
| Dashboard | `web/src/`: overview, editors, OAuth setup, raw JSON, secrets entry, trajectory/context inspector, analytics, commands and responsive layout |
| Operations | `cli.py`, `server.py`, Dockerfile, compose, `scripts/check.sh`, docs: initialization, cooperative SSE shutdown, serve/password/doctor, backup/restore and verification |
| Running version and text | `version.py`, `discord_text.py`, `web/build-info.ts`, `web/src/Version.tsx`: startup/build identities, `!version`/`!ver`, fenced help and native Discord Markdown |

See [README.md](../README.md) for setup, [API.md](API.md) for command/API parity, [PLUGINS.md](PLUGINS.md) for extension contracts and [OPERATIONS.md](OPERATIONS.md) for behavior and recovery semantics.

## Shared GNU Screen session

The footer task keeps the same attached session and external storage. It stopped only foreground Hortator PID `593173` after verifying idle work, took the fresh backup above, and deploys from the final committed footer branch after rebuilding the dashboard. Rediscover the current PID; `/api/version` and `web/dist/build-info.json` identify the actual server/build. The sanitized post-restart report belongs at `/home/codexy/.local/share/hortator/logs/footer-verification.json`, including observed source identity, configuration comparison and local health/readiness checks.

Keep the existing session; the user may already be attached.

| Item | Current value |
| --- | --- |
| OS account | `codexy` |
| Stable session name / window | `hortator` / `dashboard` |
| Session identifier at handover | `387556.hortator` |
| Current socket | `/run/screen/S-codexy/387556.hortator` |
| Shell and default working directory | `/home/codexy/codex/astra-council_cabinet` |
| Terminal log | `/home/codexy/.local/share/hortator/logs/hortator.screen.log` |
| Dashboard and API | `http://127.0.0.1:8000` |
| Persistent data | `/home/codexy/.local/share/hortator` |
| Password location | `/home/codexy/.local/share/hortator/initial-password` (read locally; do not print in the handover) |
| Launcher / environment | `/home/codexy/.local/bin/hortator` / `/home/codexy/.config/hortator/runtime.env` |

The user explicitly requested GNU Screen so both parties can observe output and control the same process. During this task the prior `343348.hortator` session disappeared while a `screen -Q windows` query was being issued; the precise crash cause is unconfirmed. Avoid `-Q` on this host. The new `387556.hortator` was created from this repository using **the user's `.screenrc` and `bash --login -i`**. Their 50,000-line scrollback and `termcapinfo xterm* ti@:te@` setting are preserved, and `.profile` sources `.bashrc` for aliases and PS1. Interactive/login status, the prompt and the `ll` alias were verified. Earlier recovery commands bypassed these files; do not repeat that workaround. Other Screen sessions were left alone.

Runtime deployment restarts only the foreground server in this attached Screen session. Rediscover PIDs before control; the API must listen on loopback port 8000 with cwd in this repository and database/lock/log paths outside Git. The old server needed a second Ctrl-C because a dashboard SSE connection held Uvicorn's connection drain; no active turns or deliveries existed at that stop. `server.py` now signals SSE streams before connection drain, and real subprocess tests confirm orderly single-SIGINT and SIGTERM shutdown. This is separate from the earlier Screen crash and gateway reconnect events.

The prior version deployment was PID `562825` in attached Screen `387556.hortator`, and was still serving the old startup-captured version at logging task start even though the user had advanced the checkout. Do not reuse a historical PID. Rediscover the current foreground process before control, and use `!version`/`!ver`, the dashboard banner or authenticated `/api/version` for the loaded commit. The current feature uses a startup snapshot for the server and an embedded build snapshot for the dashboard. Commit before final build/restart; otherwise the uncommitted-build label is intentional. Console startup announces the available controls; `i` inspects live state without HTTP polling. The configured external data and existing verified rollback locations remain; the older backup is not evidence that later user configuration changes were captured.

Attach with `screen -x hortator`. **Ctrl-A then D** detaches your viewer without stopping the server. **Ctrl-C** stops the foreground server and returns to the shell. Inspect `screen -ls`, foreground processes and the log before sending input; do not interrupt an unrelated user command. The stable session name is preferable to assuming the PID will never change.

From the shared shell, start/restart with:

```bash
cd /home/codexy/codex/astra-council_cabinet
/home/codexy/.local/bin/hortator serve --host 127.0.0.1 --port 8000 --color
```

The agent can send shell input with `screen -S hortator -p dashboard -X stuff` once the foreground server has been deliberately stopped. Keep runtime commands in this session and report them to the user. Ordinary repository edits and read-only diagnostics can use normal tools. Do not detach the user's display, kill Screen, or start a second API worker. Screen survives terminal/turn detachment, not host reboot, and does not automatically restart a crashed application. Creation/recovery commands are in [OPERATIONS.md](OPERATIONS.md#shared-gnu-screen-terminal).

A separate legacy Vite process on port 5173 was observed outside Screen. It belongs to the retired workspace and was left alone under the user's cleanup instruction. **Use port 8000 for this project.** Do not confuse the old development server with the current production build.

## Verification and readiness

- Operational console work passed **116 backend tests**, including 13 console cases and actual pseudo-terminal key/SIGINT/SIGTERM restoration checks, plus Ruff lint/format and diff checks. No UI source changed; the final Vite build refreshes the commit stamp before restarting the foreground server. Existing Discord notification behavior remains independently controlled by the dashboard. No manual Discord posts, paid probes, schema changes or configuration edits were performed for this logging task. See VERIFICATION for exact scope and rollout checks.
- Current version/formatting work passed **103 backend tests** and **11 Playwright tests** (nine browser flows and two Node build-metadata checks), plus Ruff, Prettier, TypeScript and the production build. Tests use temporary Git repositories, isolated API storage and fake Discord/provider transports. They establish snapshot accuracy, Git-free fallback, owner enforcement, formatted help/aliases and Markdown preservation, including all five identities. Desktop/mobile build screenshots were inspected. Live Discord rendering of the new commands remains for the user; no manual posts or paid probes were made.
- Current refinements passed **80 backend tests** and **7 Chromium browser tests**, Ruff lint/format, Prettier, TypeScript and the production build. The new cases cover visible reasoning controls/JSON preservation, reconnect grace/cause/recovery, honest catalog discovery and real subprocess SSE shutdown. User-confirmed live typing and working Hortator configuration supersede earlier pending acceptance/corrections; the full normal council still needs user activation. See [VERIFICATION.md](VERIFICATION.md) for scope and evidence.
- At the initial migration: created a fresh Python 3.12 environment with the frozen lockfile, installed Node dependencies from the lockfile, and rebuilt the production dashboard. During later recovery, `uv sync --frozen`, `npm ci --prefix web`, and the production build passed again in the replacement Screen session. Strict TypeScript checking passed with the build.
- The initial migration re-ran **57 backend tests** and **5 Chromium browser tests** successfully in the shared Screen session. These suites were not rerun for the host environment/documentation-only recovery. The browser suite starts an isolated temporary API on port 18000 and uses synthetic provider/Discord transports; it does not operate live bots.
- The previous implementation also passed its backend suite on Python 3.14, Ruff checks/formatting and Prettier. The migration did not change application behavior. [VERIFICATION.md](VERIFICATION.md) separates historical and current evidence.
- The typing feature passed **71 backend tests on Python 3.12**, including 14 new presence tests, plus Ruff lint/format checks. No UI source changed, so the existing built dashboard was retained. Health, dashboard HTML, authenticated bot readiness, login/logout revocation and external runtime file paths passed after deployment. The user remains attached to Screen.
- The user-configured five applications passed live read-only token/application/intent/guild/channel-permission checks. Later the user confirmed Hortator's new provider/Kimi K3, corrected context/concurrency settings and live Discord typing. Treat those as operator acceptance; no new paid probes/manual Discord posts were sent and no council bots were enabled by the agent. Full normal-council completion/tool/delivery acceptance remains for the user.
- Optional search/image/TTS integration has not received external acceptance in this session; do not change the user's chosen grants or credentials. Docker packaging is present; actual image build/container startup was not verified because the available environment denied Docker daemon socket access.
- Process locking, backup/restore, authorization, cancellation, scoped context, compaction, raw JSON, provider failures, write-only secrets and populated trajectory inspection have controlled test coverage. Such coverage is not a claim of compatibility with every vendor or a proven large-scale deployment limit.

## Diagnostic footer implementation (2026-09-08)

Hortator defaults to a `-# TTFT: … | TPS: …` line on outgoing messages; other bots default off. Every bot has **Message footer** controls in its editor, with a toggle, literal template, insertion buttons, example preview and metric definitions. Owner commands: `!footer [bot-id] [enable|disable]` and `!footer [bot-id] template <text>`. No ID targets Hortator; `enabled`/`disabled` aliases work. `{{TTFT}}`, `{{TPS}}`, `{{PROVIDER}}`, `{{CONTEXT}}`, `{{MODEL}}` / `{{MODEL SELECTED}}`, and `{{BOT}}` are supported. See OPERATIONS for exact meaning and limits.

`footer.py` handles effective defaults, validation, redaction and rendering; model/service fields expose settings without bulk rewriting old records. Runtime delivery uses the final `Completion.request_id`, records footer evidence separately, and excludes it from canonical answers/attachments/context. Discord delivery reserves its message budget and keeps subtext outside code fences; echo/history/embed-edit handling preserves canonical answers. Commands, incidents and forum starters use unknown timing, never stale model metrics. Intentional silence produces no footer-only output.

No live bot/provider configuration, credentials, activation flags or SQLite schema were changed by deployment. Read-only pre-deployment inspection found TTFT and reported output usage on the three latest completed Hortator generation requests. Backend tests use synthetic providers/Discord; browser tests use isolated data. New footer rendering in the real Discord client remains for the user's next message, along with full council activation. Earlier console tool-label/plugin-key UI follow-ups and a richer dashboard log panel remain separate work.

## Resume without losing continuity

1. Confirm this repository root and feature branch, read AGENTS and this handover, and inspect local status without overwriting the user's changes.
2. Check the existing Screen session and port 8000. Confirm the running executable and working directory resolve to this root, while the database and log resolve to `/home/codexy/.local/share/hortator`. Preserve the user's attachment and current configuration. Do not use any `data/` directory recreated by an old branch as live storage.
3. Give a short status confirmation, then continue the user's next requested work on this implementation. There is no new feature mandate hidden in the handover; the next session should not launch a redesign or enable unconfigured bots.
4. Update this handover when branch/runtime conventions or material project state change. Keep the continuation prompt useful, keep work off main, and do not push unless explicitly asked.

The copy/paste prompt for a fresh session is [CONTINUE_PROMPT.md](CONTINUE_PROMPT.md).
