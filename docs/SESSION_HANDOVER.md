# Session handover: Astra Council Cabinet / Hortator

Updated 2026-09-07 during the move to the standalone repository. This document carries the project and operating context into a new agent session. Read [AGENTS.md](../AGENTS.md) first; it contains the user's repository boundary and branch rules. The application is still named **Hortator Council** in its package, CLI and dashboard; the repository directory is **astra-council_cabinet**. This move does not rename or redesign the product.

## Canonical workspace and Git

- **Only working root:** `/home/codexy/codex/astra-council_cabinet`. Application files (`hortator/`, `web/`, `data/`, `pyproject.toml`) are directly under this root.
- **Working branch:** `dev/initial_phase`. **NEVER PUSH TO MAIN.** No push of any branch is authorized by this handover. Do not merge or commit to main, bypass the guard, force-push, or rewrite the existing commits.
- The user copied/restored this project and its Git history into this destination. The three incoming commits are `81c4686 Initial`, `c4b5738 Second phase`, and `9447ba6 first release`. The migration adds one fourth commit on the same feature branch; `git log -1` gives its identifier. No remotes were configured when inspected.
- The former `/home/codexy/codex/t3-code` workspace is explicitly out of scope. The user will clean it separately. Do not run Git there, edit it, use it as a worktree, restore from it, or synchronize this repository back into it.
- `.githooks/pre-push` blocks every update or deletion targeting `refs/heads/main`. This repository has `core.hooksPath=.githooks`; on a fresh clone install that setting again. The guard was tested locally without contacting a remote.

### Copied local files and history

The incoming commits tracked the runtime database/WAL, bootstrap password, master encryption key, logs, installed Node dependencies, generated web build, test screenshots and Python caches. This handover commit removes 6,275 such files **from Git tracking only**. Their local copies are preserved. A root `.gitignore` prevents staging them again; lockfiles, source, tests and deployment files remain tracked. Local data directories/files have owner-only permissions.

**The earlier three commits still contain the bootstrap password and encryption key.** Removing files from the current tree does not remove historical copies. No history rewrite, credential rotation or remote publication was performed. Address that history in a separately scoped task before sharing the repository elsewhere. At migration inspection, no provider keys or Discord bot tokens were configured; the only stored secret was the dashboard password hash. Never print secret contents in reports or commit them again.

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

| Area | Files and purpose |
| --- | --- |
| Configuration and persistence | `hortator/models.py`, `store.py`: typed entities, SQLite WAL, references/revisions, identity tombstones, channel scope, contexts, memory, events, turns, requests, outbox and artifacts |
| Security and control | `security.py`, `service.py`, `app.py`: encrypted vault, exact-owner checks, password sessions/CSRF/origins, shared audited controls and authenticated API |
| Provider calls | `provider.py`: compatible HTTP/SSE, arbitrary JSON, concurrency/circuit recovery, credential overrides, token/cost/timing reporting and partial-output evidence |
| Agent execution | `context.py`, `runtime.py`: prompt assembly, conservative estimates/calibration, compaction, independent activation, tool rounds, cancellation, cooldowns and durable delivery |
| Discord | `discord_gateway.py`: client per application, reconnect/backfill, threads/DMs, token identity checks, commands, notifications, long-output attachments and nonce reconciliation |
| Plugins | `plugins.py`: global/per-bot registry, capability enforcement, public-network fetch boundary, media artifacts, memory and read-only council inspection |
| Dashboard | `web/src/`: overview, editors, OAuth setup, raw JSON, secrets entry, trajectory/context inspector, analytics, commands and responsive layout |
| Operations | `cli.py`, Dockerfile, compose, `scripts/check.sh`, docs: initialization, serve/password/doctor, backup/restore and verification |

See [README.md](../README.md) for setup, [API.md](API.md) for command/API parity, [PLUGINS.md](PLUGINS.md) for extension contracts and [OPERATIONS.md](OPERATIONS.md) for behavior and recovery semantics.

## Shared GNU Screen session

Keep the existing session; the user may already be attached.

| Item | Current value |
| --- | --- |
| OS account | `codexy` |
| Stable session name / window | `hortator` / `dashboard` |
| Session identifier at handover | `267858.hortator` |
| Current socket | `/run/screen/S-codexy/267858.hortator` |
| Shell and default working directory | `/home/codexy/codex/astra-council_cabinet` |
| Terminal log | `/home/codexy/codex/astra-council_cabinet/data/logs/hortator.screen.log` |
| Dashboard and API | `http://127.0.0.1:8000` |
| Persistent data | `/home/codexy/codex/astra-council_cabinet/data` |
| Password location | `data/initial-password` (read locally; do not print in the handover) |

The original ad-hoc API process did not survive the previous turn. The user explicitly requested GNU Screen so both parties can observe output and control the same process. The session was preserved during this move, including its concurrent attachment, and repointed to this repository and log. The server was already stopped when migration began; dependencies/build/tests were run visibly in that shared shell before restarting it here.

Attach with `screen -x hortator`. **Ctrl-A then D** detaches your viewer without stopping the server. **Ctrl-C** stops the foreground server and returns to the shell. Inspect `screen -ls`, foreground processes and the log before sending input; do not interrupt an unrelated user command. The stable session name is preferable to assuming the PID will never change.

From the shared shell, start/restart with:

```bash
cd /home/codexy/codex/astra-council_cabinet
uv run hortator serve --host 127.0.0.1 --port 8000
```

The agent can send shell input with `screen -S hortator -p dashboard -X stuff` once the foreground server has been deliberately stopped. Keep runtime commands in this session and report them to the user. Ordinary repository edits and read-only diagnostics can use normal tools. Do not detach the user's display, kill Screen, or start a second API worker. Screen survives terminal/turn detachment, not host reboot, and does not automatically restart a crashed application. Creation/recovery commands are in [OPERATIONS.md](OPERATIONS.md#shared-gnu-screen-terminal).

A separate legacy Vite process on port 5173 was observed outside Screen. It belongs to the retired workspace and was left alone under the user's cleanup instruction. **Use port 8000 for this project.** Do not confuse the old development server with the current production build.

## Verification and readiness

- In the new root: created a fresh Python 3.12 environment with the frozen lockfile, installed Node dependencies from the lockfile, and rebuilt the production dashboard. The build passed strict TypeScript checking.
- Re-ran **57 backend tests** and **5 Chromium browser tests** successfully in the shared Screen session. The browser suite starts an isolated temporary API on port 18000 and uses synthetic provider/Discord transports; it does not operate live bots.
- The previous implementation also passed its backend suite on Python 3.14, Ruff checks/formatting and Prettier. The migration did not change application behavior. [VERIFICATION.md](VERIFICATION.md) separates historical and current evidence.
- Main data is preserved. Hortator, Ada and Socrates remain disabled drafts; the default OpenRouter profile still needs a real model identifier, provider credentials, Discord application tokens and guild/channel IDs. No live messages or model requests were present at migration inspection. Do not invent connected bots or measured usage.
- Live Discord, OpenRouter/custom endpoints and optional media/search integration still require the user's configuration and external acceptance. Docker packaging is present; actual image build/container startup was not verified because the available environment denied Docker daemon socket access.
- Process locking, backup/restore, authorization, cancellation, scoped context, compaction, raw JSON, provider failures, write-only secrets and populated trajectory inspection have controlled test coverage. Such coverage is not a claim of compatibility with every vendor or a proven large-scale deployment limit.

## Resume without losing continuity

1. Confirm this repository root and feature branch, read AGENTS and this handover, and inspect local status without overwriting the user's changes.
2. Check the existing Screen session and port 8000. Confirm the running executable, working directory and log resolve to this root. Preserve the user's attachment and current configuration.
3. Give a short status confirmation, then continue the user's next requested work on this implementation. There is no new feature mandate hidden in the handover; the next session should not launch a redesign or enable unconfigured bots.
4. Update this handover when branch/runtime conventions or material project state change. Keep the continuation prompt useful, keep work off main, and do not push unless explicitly asked.

The copy/paste prompt for a fresh session is [CONTINUE_PROMPT.md](CONTINUE_PROMPT.md).
