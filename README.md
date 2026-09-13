# Hortator Council

The canonical project root is **`/home/codexy/codex/astra-council_cabinet`**. Inspect actual Git state and continue the user's active task branch. **Never push to main.** Begin with [AGENTS.md](AGENTS.md) for standing instructions and [operations](docs/OPERATIONS.md) for runtime/storage procedures. The [design](docs/PLAN.md) and [dated verification evidence](docs/VERIFICATION.md) record product decisions and checks.

An observable council of independent Discord applications. Each bot has its own Discord token, identity, personality, tool grants, cadence, credentials overrides, and channel-scoped memory. Reusable model profiles let you change the model without changing the bot.

One Python process runs the Discord clients, scheduler, provider HTTP clients, FastAPI control service, and production React dashboard. Vite/Node is needed to build the UI or run its development server, not to run the production council.

The root dashboard is a dense desktop workbench with Dark+ styling, sortable compact lists and docked/maximizable editors. **Ctrl-K** opens pages or records, **Ctrl-B** toggles navigation, **Ctrl-S** saves the active configuration editor, and **/** focuses the inventory filter. The previous dashboard remains at **`/legacy/`** as a frozen fallback while the owner evaluates workflow parity. Both use the same API and authentication; no settings or credentials are migrated. New features belong only to the workbench. See the [dashboard boundary and parity contract](docs/DASHBOARD.md) and [dashboard controls](docs/OPERATIONS.md#dashboard-workbench).

## Start locally

Requires **Python 3.14** and [uv](https://docs.astral.sh/uv/); Node 22+ is used for the dashboard build. Run commands from the repository root, `/home/codexy/codex/astra-council_cabinet`:

```bash
export HORTATOR_DATA_DIR="$HOME/.local/share/hortator"
uv sync --python 3.14 --frozen
npm ci --prefix web
npm run build --prefix web
uv run hortator serve
```

Persistent data stays outside the checkout, so changing branches or cleaning generated files does not remove the database, credentials, memory, or artifacts. On this workspace, the installed `~/.local/bin/hortator` launcher loads `~/.config/hortator/runtime.env` and sets the same data directory on every branch. Use `hortator serve --host 127.0.0.1 --port 8000 --color` in the shared Screen shell; the explicit flag overrides its inherited `NO_COLOR`. The launcher also supports commands such as `hortator backup /absolute/path/to/new-backup-directory`.

Open **http://127.0.0.1:8000**. On first startup, a random dashboard password is written to `$HORTATOR_DATA_DIR/initial-password` with owner-only permissions:

```bash
cat "$HORTATOR_DATA_DIR/initial-password"
```

Alternatively, set `HORTATOR_ADMIN_PASSWORD` to a strong password of at least 12 characters before starting. The dashboard uses an HttpOnly session cookie, CSRF protection, and a 12-hour session. The command-line runtime listens on loopback by default.

For development, run the same API command and, in a second terminal, `npm run dev --prefix web`. The Vite UI at **http://localhost:5173** proxies `/api` to port 8000. Its development server is reachable on the host network; use the production server for deployment.

This workspace's persistent runtime uses **GNU Screen**: run `screen -x hortator` to join its shared terminal. Detach with **Ctrl-A, then D**. See [shared terminal controls and logs](docs/OPERATIONS.md#shared-gnu-screen-terminal) before starting another server.

The console shows timestamped, colored bot/provider/Discord activity. Successful dashboard polling is hidden at normal verbosity. Press **`?`** for controls: **`+`/`-`** verbosity, **`f`** JSON/error details, **`r`** recent events, **`e`** recent errors and **`i`** current bot/provider state. **`T` / `P`** independently expand tool/provider details to stored commands, output and request evidence; **`[` / `]`** select incidents and **`n` / `N`** page through evidence. Scope keys include **`d`** Discord, **`p`/`a`** providers and **`w`** web/dashboard. Controls preserve ordinary Screen scrollback and change only the console view. See [console inspection](docs/OPERATIONS.md#console-inspection-and-filtering) for all keys and startup options.

The five-bot runtime uses real configuration in external storage; do not reinitialize it. Consult [backup procedures](docs/OPERATIONS.md#backup-and-restore) and [dated acceptance evidence](docs/VERIFICATION.md), and inspect the dashboard for current state. The [configuration audit](docs/CONFIGURATION_STATUS.md) retains historical findings; the user subsequently resolved the provider/context/concurrency setup and confirmed live typing. The first-run instructions below describe a new installation.

Prepare the publishing server's public SSH key with the offline operator command **`hortator ssh-key create publishing`**. Its private key stays in the existing encrypted vault and travels with normal backups. Configure verified host pins and the global destination before enabling the automatic delivery worker. See [SSH identity and portability](docs/OPERATIONS.md#publishing-ssh-identity) and [remote publishing setup](docs/OPERATIONS.md#automatic-remote-publishing).

Stop the foreground server before switching branches, rebuild the dashboard when its source changes, and restart through the installed launcher. Existing branches still have a `./data` CLI fallback; the external launcher and environment setting keep those branches on the same persistent data. See [branch changes and persistent storage](docs/OPERATIONS.md#branch-changes-and-persistent-storage).

After a PR is squash-merged, run **`hortator-next-feature`** from a shell prompt. It checks for unfinished work, controls the existing Screen server, updates main with a fast-forward pull, prepares the local `feat/next_feature` placeholder, rebuilds and verifies matching dashboard/server commits. Rename the placeholder before publishing the next PR. Use **`hortator-next-feature --refresh`** to rebuild/restart the clean current task without switching branches. The [repeatable workflow](docs/OPERATIONS.md#repeating-the-squash-merge-workflow) includes installation, failure handling and manual commands. No new work or pushes belong on main.

Use **`!version` / `!ver`** or the panel's **Running server** banner to see the loaded commit, ISO UTC date/time and commit title. **Build details** also shows server start time and the dashboard's separately embedded build identity. Unknown metadata and uncommitted builds are explicit. Commit before the final build/restart so the release identifies the final task commit.

Hortator's replies include a small Discord diagnostic footer with **TTFT / TPS** by default. Other bots start with it off. Configure each at **Bots → Edit bot → Message footer**, or use `!footer enable`, `!footer disable`, `!footer ada enable` and `!footer ada template {{TTFT}} | {{TPS}} | {{MODEL}}`. Provider, context and bot-name placeholders are also available; missing measurements remain `—`. See [message footer controls and metric definitions](docs/OPERATIONS.md#discord-message-footers).

Rooms that include external bots also ingest application responses such as slash-command results, including visible embeds and Components V2 text. Hortator keeps its owner/control-channel/DM intake; its optional **Send to council channel** capability can explicitly post to another configured room on the owner's request, with confirmed delivery receipts. See [intake and cross-channel controls](docs/OPERATIONS.md#hortator-control-scope-and-cross-channel-posts).

For provider/conversation stress tests, turn off **Bots → Edit bot → Capabilities → Allow intentional silence** on the bots you want answering. It removes their silence tool and asks for a text contribution while preserving normal activation, concurrency, cooldown and usage limits. It defaults on and requires no key. See [scheduling and silence controls](docs/OPERATIONS.md#scheduling-and-message-semantics).

## Connect the first council

The initial records are **disabled drafts**, with no invented traffic or credentials: Hortator (15-second cadence), Ada (60 seconds), Socrates (90 seconds), a shared prompt, one council room, OpenRouter, and an unconfigured model profile.

1. **Providers:** open OpenRouter and save its API key in the credential box. The base URL is `https://openrouter.ai/api/v1`. Use **Test connection** to retrieve its model catalog. This tests discovery/authentication; it does not claim that a particular chat model or tool schema works.
2. **Model profiles:** set an exact model identifier, context window, output reserve, compaction threshold, and raw request JSON. Clone profiles for variants such as `deepseek-low`, `deepseek-max`, or a smaller context. Assign a different profile to Hortator if desired.
3. **Rooms:** set the Discord server and council channel IDs. In **Council settings**, set a **different** control channel and its server ID for Hortator. This is its owner conversation and reporting channel; owner DMs and its threads also work. Developer Mode in Discord enables “Copy ID” on servers/channels.
4. **Bots → Discord:** create a distinct application for each bot in the [Discord Developer Portal](https://discord.com/developers/applications). Give it its own name/avatar. Enable **Message Content Intent**, save the application ID (or let token verification discover it), and paste the **Bot token** into the dashboard. Verification rejects mismatched/duplicate applications. Use the generated **Invite bot** link and authorize each application separately. No Administrator permission is requested.
5. Assign the appropriate profile, rooms, prompt templates, capabilities, and timers to each bot. Activate them. A bot cannot be activated while its required configuration is incomplete.
6. Type a topic in the council channel. Each bot evaluates on its independent timer and may answer, call tools, or remain silent. A human mention or Discord reply gives the addressed bot priority without its activation/send cooldown, and its ordinary answer replies to that human message. Shared concurrency, provider and usage limits still apply. Other bots see the exchange with explicit recipient labels. In the reporting channel, send `!status` to Hortator. Its deterministic commands work even while its model or the entire council is paused.

Discord application creation, token issuance, and authorization are Developer Portal operations. Hortator guides them and constructs the OAuth invitation; it does not automate a Discord user account or claim it can mint application tokens.

Only Discord snowflake **`1482143139828596916`** (`.normal.man.`, “The Boss”) can command or converse with Hortator. This is a code constant checked against gateway identity. Display names, roles, server ownership, quoted instructions, and webhook authors never grant access. Other humans can converse with ordinary council members according to room policy.

Set **Bots → Edit bot → Capabilities → Private memory budget (characters per channel)** to an integer from **1 to 48,000**, default **48,000**. A 5% temporary allowance avoids rejecting small overshoots; the bot then receives a warning and must shrink/delete notes before adding more. Disable **Private memory** to stop its tools and automatic note injection while retaining stored notes. Other tool grants remain independent. [Memory operations](docs/OPERATIONS.md#private-memory-budgets).

## Fine control of providers and models

Set **Providers → Edit provider → User-Agent** to customize the outgoing client identifier. The field and Advanced HTTP headers edit the same value; it applies to discovery, generation and compaction. Blank uses the HTTP client's default. [Provider transport and discovery diagnostics](docs/OPERATIONS.md#provider-headers-and-discovery-errors) distinguish the provider's HTTP response from the dashboard API response.

A provider stores transport settings and a shared encrypted credential. A model profile stores model identity, context settings, prices (optional), streaming switches, and exact non-secret request JSON. A bot references a profile and can override the provider key in its own credential box.

Use **Model profiles → SSE streaming** directly on the model card, or the same switch in **Edit profile**, to choose streaming or one complete JSON response for that model/provider profile. Existing modes are preserved; new profiles default to streaming. Buffered responses still retain returned reasoning, token usage and costs, but cannot measure TTFT or streaming TPS. Missing token counts are never replaced by tokenizer estimates. See [streaming and response diagnostics](docs/OPERATIONS.md#streaming-and-response-diagnostics).

Edit reasoning at **Model profiles → Edit profile → Reasoning**. Choose the native request field, then an effort level, thinking toggle or token budget. These controls update **Advanced · exact request JSON → Model parameters** directly, preserving other vendor fields. **Unset** omits only that field; **Off** sends `false`. With no override, the model service chooses its behavior; there is no provider-level reasoning setting. The card displays explicit reasoning fields, including nested values and `false`. Options depend on the endpoint/model; the UI does not establish remote support. See [reasoning operations](docs/OPERATIONS.md#reasoning-controls).

For example, a profile's **Model parameters** can be:

```json
{
  "temperature": 0.75,
  "top_p": 0.9,
  "max_tokens": 2048,
  "reasoning": { "effort": "low", "exclude": true },
  "provider": { "allow_fallbacks": false },
  "your_vendor_option": { "anything": "supported by that endpoint" }
}
```

This JSON is passed through without translating sampling or reasoning settings. Replace it with exactly what the chosen provider/model accepts; the example is not a promise that every model accepts those fields. Use `reasoning_effort`, `thinking`, or nested provider-specific structures as appropriate. **Advanced · compaction parameter overrides** edits `compaction_request_json` and previews effective reasoning fields for summaries. Non-secret provider headers and the credential `auth_header`/`auth_scheme` are configurable in full configuration JSON.

The runtime owns `model`, `messages`, `tools`, `tool_choice`, `stream`, and single-choice generation. The stream switches live beside the JSON editor. Generation `max_tokens`/`max_completion_tokens` must fit the configured response reserve. **Retained summary limit (tokens)** controls only the finished summary kept after compaction, excluding private reasoning. Compaction omits combined output caps from its outgoing request; the provider's defaults and context limits apply. Oversized or incomplete candidates leave previous context intact. See [compaction budgets](docs/OPERATIONS.md#context-and-compaction).

For a local OpenAI-compatible server, use its base URL (for example `http://127.0.0.1:11434/v1`), turn off **Endpoint requires an API key** if appropriate, and disable **Request stream usage data** if unsupported. In Docker, loopback refers to the container; use a reachable host/network address. HTTPS cloud endpoints can use shared or per-bot credentials.

Profile cloning copies only configuration, never credentials. Bot cloning clears application identity/token and keeps personality and capability settings. References prevent deleting in-use profiles/providers/prompts/rooms. Edits cancel affected active work; completed trajectory records retain their original profile and prompt revisions.

## What is recorded

- Each activation and its outcome: running, sent, silent, failed, cancelled, interrupted, or manual compaction.
- Every provider request: effective body with credentials redacted and native reasoning replay separated into private diagnostics, provider/profile revisions, model, purpose, prompt layers and hashes, selected message IDs/sequences, previous summary, token estimate/calibration, and tool-round budget.
- Provider-reported input/output/reasoning/cached tokens, raw usage JSON, reported or explicitly estimated costs, TTFT, first visible token time, queue time, completion duration, HTTP status and sanitized errors.
- Tool calls: arguments, results, timing, permission failures, artifacts and cancellation.
- Compaction: original and new summaries, compacted/retained message IDs, checkpoints, and before/after context estimates. Original transcripts are retained.
- Outbox states: pending, sending, sent, suppressed, failed, or unknown. Generated text is retained even when delivery fails. A matching authenticated Discord nonce can reconcile an uncertain delivery.
- Discord connection/heartbeat state, provider failure streaks, circuit opening/recovery, incident reporting, and audited configuration changes.

The dashboard includes per-bot/provider/profile/model/purpose aggregates, request timelines, context and memory inspection, an event ledger with pagination, full trajectory JSON downloads, and configuration/metric exports. Unknown measurements display `—`, not fabricated zeroes. Reasoning tokens are reported separately but may already be included in the provider's completion total; never add them again to calculate billable output.

See [operations and behavior](docs/OPERATIONS.md) for scheduling, recovery, security boundaries, costs and backups; [the API/command reference](docs/API.md); [plugin development](docs/PLUGINS.md); and [research and implementation plan](docs/PLAN.md).

## Built-in plugins

| Registry ID | Capability | Configuration |
| --- | --- | --- |
| `web_fetch` | Complete public text snapshots, chunking and search | No key; 1 MB download cap, owned stable offsets, storage/retention limits |
| `workspace` | Private task files, observed attachments and current-turn exports | No key; bot/channel isolation, quotas and bounded retention |
| `shell` | Real isolated Bash/Python 3.14/Pillow jobs | No key; workspace grant plus ready Linux/Bubblewrap boundary; host networking and disposable packages |
| `document_site` | Owned static documents, paged editing, revision recovery and automatic publication | Keyless bot tools; global SSH delivery settings, immutable revisions and private snapshot history |
| `web_search` | Brave + DuckDuckGo search, fallback or combined | Brave key; DuckDuckGo keyless; engine/endpoint/count configurable |
| `image_generation` | Image generation to a Discord attachment | OpenAI-style image endpoint, raw request JSON, key |
| `tts` | Speech generation to an audio attachment | OpenAI-style speech endpoint, model/voice/options JSON, key |
| `memory` | Persistent bot + channel scoped notes | No key; per-bot budget (default 48,000/channel), 5% temporary headroom, 8,000/note |
| `council_inspect` | Running version, status, statistics, configuration, context and trajectory queries | Hortator only, for authenticated owner questions; read-only |

Global enablement and a bot grant are both required. Each bot can override plugin configuration and credentials. Plugin tools cannot run administration commands. Models answer directly through ordinary assistant content. `council_silence` explicitly ends a turn without posting; there is no `council_speak` reply tool. When the turn has generated/exported artifacts, optional `discord_attach` prepares owned files (and an optional reply target) for the next ordinary answer. Preparation does not send or finish the turn. Tool rounds and per-round call limits are enforced outside the model.

Configure **Plugins → Edit Web search → API key → Save credential** for Brave, then select Auto fallback or Both (five results per engine by default). DuckDuckGo needs no key. Grant Web search to each desired bot. See [search setup and tool examples](docs/WEB_SEARCH.md) for the Brave account steps, per-bot overrides and partial-failure diagnostics.

## Docker

```bash
docker compose up --build -d
# Read the generated password once:
docker compose exec hortator cat /var/lib/hortator/initial-password
```

Open **http://127.0.0.1:8000**. A named volume holds the database, encryption key, and artifacts. One container and one process are sufficient. The image runs as an unprivileged user and the compose port is loopback-only. For remote use, put HTTPS in front of the server and set `HORTATOR_SECURE_COOKIES=1`; see the operations guide.

## Verify

```bash
uv run pytest -q
uv run ruff check hortator tests
npm run build --prefix web
(cd web && npx playwright install chromium)
npm test --prefix web
```

Backend tests use mocked compatible HTTP responses and Discord transports. Browser tests start a separate real API/server with temporary storage; they exercise login, owner controls, configuration, raw parameter JSON, encrypted credential entry, readiness errors, mobile layout, and trajectory inspection. They do not contact live Discord or spend provider credit. See [verification notes](docs/VERIFICATION.md) for the checks performed and external acceptance steps.

## Images, cache prices and local publications

Discord image attachments reach every bot as actual multimodal pixels for its current handled turn, with a durable private image cache and explicit omitted/unavailable-image feedback. Later turns and text compaction retain metadata and written observations without replaying old pixels. See [vision inputs](docs/VISION.md). Profile pricing supports separate cache-hit and cache-miss input rates alongside output prices, with request-time pricing evidence: see [pricing](docs/PRICING.md).

The optional **Documents & local sites** plugin creates sites under each bot's stable ID and an explicitly created slug. It supports separate HTML/CSS/JS/SVG assets, paged reads, small edits, file restoration and copies of other bots' published work. Bots cannot delete files, edit another bot's namespace or implicitly create a missing site while editing. Enable the plugin globally and grant it per bot; no per-bot API key is needed.

Global **Automatically publish document changes** and **Enable remote delivery** settings control the durable sync worker. Both default off for new installations. With them configured and enabled, each save queues an immutable revision; the worker snapshots it in private local/remote Git history and publishes it over verified passwordless SSH. The dashboard distinguishes current edits, the last delivered revision, retry errors and snapshot commits. A remote audit can record drift every 15 minutes without importing or overwriting it. See [document publishing](docs/DOCUMENTS.md), [server setup and recovery](docs/OPERATIONS.md#automatic-remote-publishing), and the permanent [tool usage, repair and task-budget contract](docs/TOOLS.md).

Optional [private files, isolated Bash and complete web reading](docs/AGENTIC_TOOLS.md) support attachment compression and long reading tasks. Configure grants, readiness, quotas, extended task budgets and private inspection in the dashboard. These tools never grant site publication or remote sync.

Experiment recovery is available in the modern dashboard's **Snapshots** page; see [snapshot guarantees and restore scopes](docs/SNAPSHOTS.md). The optional [global-memory plugin](docs/GLOBAL_MEMORY.md) gives each bot its own notebook across channels, independently of its private channel notes. Both preserve existing configuration unless explicitly used.

A separate [slash-command assistant plugin](docs/SLASH_COMMANDS.md) lets a council companion such as Loki answer owner `/prompt` invocations through personal/server Discord installation. It is off by default and leaves Hortator's existing intake unchanged.
