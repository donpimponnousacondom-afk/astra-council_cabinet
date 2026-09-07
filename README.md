# Hortator Council

The canonical project root is **`/home/codexy/codex/astra-council_cabinet`**, on **`dev/initial_phase`**. **Never push to main.** For a new agent session, begin with [the session handover](docs/SESSION_HANDOVER.md), [continuation prompt](docs/CONTINUE_PROMPT.md), and [AGENTS.md](AGENTS.md).

An observable council of independent Discord applications. Each bot has its own Discord token, identity, personality, tool grants, cadence, credentials overrides, and channel-scoped memory. Reusable model profiles let you change the model without changing the bot.

One Python process runs the Discord clients, scheduler, provider HTTP clients, FastAPI control service, and production React dashboard. Vite/Node is needed to build the UI or run its development server, not to run the production council.

## Start locally

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/); Node 22+ is used for the dashboard build. Run commands from the repository root, `/home/codexy/codex/astra-council_cabinet`:

```bash
uv sync --frozen
npm ci --prefix web
npm run build --prefix web
uv run hortator serve
```

Open **http://127.0.0.1:8000**. On first startup, a random dashboard password is written to `data/initial-password` with owner-only permissions:

```bash
cat data/initial-password
```

Alternatively, set `HORTATOR_ADMIN_PASSWORD` to a strong password of at least 12 characters before starting. The dashboard uses an HttpOnly session cookie, CSRF protection, and a 12-hour session. The command-line runtime listens on loopback by default.

For development, run the same API command and, in a second terminal, `npm run dev --prefix web`. The Vite UI at **http://localhost:5173** proxies `/api` to port 8000. Its development server is reachable on the host network; use the production server for deployment.

This workspace's persistent runtime uses **GNU Screen**: run `screen -x hortator` to join its shared terminal. Detach with **Ctrl-A, then D**. See [shared terminal controls and logs](docs/OPERATIONS.md#shared-gnu-screen-terminal) before starting another server.

## Connect the first council

The initial records are **disabled drafts**, with no invented traffic or credentials: Hortator (15-second cadence), Ada (60 seconds), Socrates (90 seconds), a shared prompt, one council room, OpenRouter, and an unconfigured model profile.

1. **Providers:** open OpenRouter and save its API key in the credential box. The base URL is `https://openrouter.ai/api/v1`. Use **Test connection** to retrieve its model catalog. This tests discovery/authentication; it does not claim that a particular chat model or tool schema works.
2. **Model profiles:** set an exact model identifier, context window, output reserve, compaction threshold, and raw request JSON. Clone profiles for variants such as `deepseek-low`, `deepseek-max`, or a smaller context. Assign a different profile to Hortator if desired.
3. **Rooms:** set the Discord server and council channel IDs. In **Council settings**, set a **different** reporting channel and its server ID for Hortator. Developer Mode in Discord enables “Copy ID” on servers/channels.
4. **Bots → Discord:** create a distinct application for each bot in the [Discord Developer Portal](https://discord.com/developers/applications). Give it its own name/avatar. Enable **Message Content Intent**, save the application ID (or let token verification discover it), and paste the **Bot token** into the dashboard. Verification rejects mismatched/duplicate applications. Use the generated **Invite bot** link and authorize each application separately. No Administrator permission is requested.
5. Assign the appropriate profile, rooms, prompt templates, capabilities, and timers to each bot. Activate them. A bot cannot be activated while its required configuration is incomplete.
6. Type a topic in the council channel. Each bot evaluates on its independent timer and may speak, reply to a supplied message ID, call tools, or remain silent. In the reporting channel, send `!status` to Hortator. Its deterministic commands work even while its model or the entire council is paused.

Discord application creation, token issuance, and authorization are Developer Portal operations. Hortator guides them and constructs the OAuth invitation; it does not automate a Discord user account or claim it can mint application tokens.

Only Discord snowflake **`1482143139828596916`** (`.normal.man.`, “The Boss”) can command or converse with Hortator. This is a code constant checked against gateway identity. Display names, roles, server ownership, quoted instructions, and webhook authors never grant access. Other humans can converse with ordinary council members according to room policy.

## Fine control of providers and models

A provider stores transport settings and a shared encrypted credential. A model profile stores model identity, context settings, prices (optional), streaming switches, and exact non-secret request JSON. A bot references a profile and can override the provider key in its own credential box.

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

This JSON is passed through without translating sampling or reasoning settings. Replace it with exactly what the chosen provider/model accepts; the example is not a promise that every model accepts those fields. Use `reasoning_effort`, `thinking`, or nested provider-specific structures as appropriate. `compaction_request_json`, available in full configuration JSON, overrides parameters for summary requests. Non-secret provider headers and the credential `auth_header`/`auth_scheme` are also configurable there.

The runtime owns `model`, `messages`, `tools`, `tool_choice`, `stream`, and single-choice generation. The stream switches live beside the JSON editor. `max_tokens`/`max_completion_tokens` must fit the configured output reserve. Compaction uses its own output limit. This protects context and delivery invariants while leaving arbitrary vendor parameters intact.

For a local OpenAI-compatible server, use its base URL (for example `http://127.0.0.1:11434/v1`), turn off **Endpoint requires an API key** if appropriate, and disable **Request stream usage data** if unsupported. In Docker, loopback refers to the container; use a reachable host/network address. HTTPS cloud endpoints can use shared or per-bot credentials.

Profile cloning copies only configuration, never credentials. Bot cloning clears application identity/token and keeps personality and capability settings. References prevent deleting in-use profiles/providers/prompts/rooms. Edits cancel affected active work; completed trajectory records retain their original profile and prompt revisions.

## What is recorded

- Each activation and its outcome: running, sent, silent, failed, cancelled, interrupted, or manual compaction.
- Every provider request: exact effective body with secret/reasoning redactions, provider/profile revisions, model, purpose, prompt layers and hashes, selected message IDs/sequences, previous summary, token estimate/calibration, and tool-round budget.
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
| `web_fetch` | Public HTTP(S) page/text retrieval | No key; public DNS/IP enforcement, redirects checked, response limits |
| `web_search` | Brave web search | Brave key; endpoint/count configurable |
| `image_generation` | Image generation to a Discord attachment | OpenAI-style image endpoint, raw request JSON, key |
| `tts` | Speech generation to an audio attachment | OpenAI-style speech endpoint, model/voice/options JSON, key |
| `memory` | Persistent bot + channel scoped notes | No key; 24,000-character total per scoped memory |
| `council_inspect` | Status, statistics, configuration, context and trajectory queries | Hortator only, for authenticated owner questions; read-only |

Global enablement and a bot grant are both required. Each bot can override plugin configuration and credentials. Plugin tools cannot run administration commands. `council_speak` and `council_silence` are built-in terminal decisions, not optional plugins. Tool rounds and per-round call limits are enforced outside the model.

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
