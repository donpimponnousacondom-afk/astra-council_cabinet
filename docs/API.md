# Control API and Discord command parity

All endpoints except `/api/health` and login require the dashboard session. Log in at `POST /api/auth/login` with `{"password":"..."}`; retain the HttpOnly cookie and returned `csrf`. Send `X-CSRF-Token` for POST/PUT operations. There is no API key in a URL and no unauthenticated public control endpoint.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/auth/session`, `POST /api/auth/logout` | Session state/revocation |
| `GET /api/status` | Configuration, readiness, gateway state, provider health, active requests, outbox counts |
| `GET /api/version` | Startup-captured commit, ISO commit date/time, title, branch, dirty flag, provenance, package and server startup time |
| `GET /api/stats?hours=24` | Usage/cost/timing/failure groups, hourly history, tool and turn outcomes |
| `GET /api/config/{kind}` / `/{id}` | Read configuration, with credential-present flags only |
| `GET /api/config-schemas` | JSON schemas for every configuration resource |
| `POST /api/control` | Shared owner control service |
| `PUT /api/credentials/{kind}/{id}/{field}` | Write-only `{"value":"..."}`; empty value removes a credential |
| `GET /api/trajectory?bot_id=&status=&before=&limit=60` | Tail-first turn history; `before` is a Unix timestamp |
| `GET /api/trajectory/{turn-id}` / `/export` | Full requests, assembly metadata, tool events and deliveries |
| `GET /api/events?after=&before=&bot_id=&turn_id=&level=&limit=100` | Stable sequence cursor event ledger |
| `GET /api/events/stream?after=` | Authenticated SSE; reconnect via `Last-Event-ID` |
| `GET /api/context/{bot}/{channel}` | Summary, checkpoint, estimates, notes, first 500 uncompacted messages, compaction history |
| `GET /api/messages/{channel}?after=&through=&limit=100` | Original observed messages; page by sequence |
| `GET /api/artifacts/{id}` | Authenticated generated attachment download |
| `GET /api/export/config` | Configuration without credentials |
| `GET /api/commands` | Command help used by the dashboard |
| `GET /api/openapi.json` | Authenticated OpenAPI specification |

The configuration kinds are `providers`, `profiles`, `bots`, `prompts`, `rooms`, `plugins`, and `settings` (`global`). `id` is immutable. Optional `revision` guards concurrent edits. A patch merges top-level fields; nested JSON objects and lists are deliberately replaced, not magically combined.

Bots include `footer_enabled` (effective default: true for Hortator, false for council bots) and `footer_template` (default `TTFT: {{TTFT}} | TPS: {{TPS}}`). GET responses expose effective defaults for older records without rewriting them. Use `action: "save", kind: "bots", id: "ada", data: {"footer_enabled": true}` to toggle one bot, and patch `footer_template` to customize it. Discord parity is `!footer [bot-id] [enable|disable]` and `!footer [bot-id] template <text>`; no ID means Hortator, no action inspects settings, and `enabled`/`disabled` aliases are accepted. Templates are single-line literal substitutions with a 300-character limit; `-# ` is automatically added. See [footer operations](OPERATIONS.md#discord-message-footers) for supported placeholders, missing data and timing definitions. The command help endpoint includes these controls. `delivery.queued` records the rendered footer separately from canonical content and identifies the generating `request_id`.

`/api/status.version` and `/api/version` share the same snapshot: `commit` (full hash), `short_commit` (12 characters), `commit_title`, `committed_at` (ISO UTC), `branch`, `dirty`, `provenance` (`git`, `build` or `unknown`), `package_version` and `started_at` (ISO UTC). Missing source fields are null, not invented version numbers. Metadata stays fixed if the checkout changes after startup. The dashboard's build stamp is separately embedded in its JS bundle and emitted as `web/dist/build-info.json`; it also includes `built_at`. The public health endpoint retains its existing package-version field.

Control request shape:

```json
{
  "action": "save",
  "kind": "profiles",
  "id": "balanced",
  "data": { "request_json": { "temperature": 0.4, "reasoning_effort": "low" } }
}
```

Actions: `create`, `save`, `delete`, `start`, `stop`, `clone`, `probe`, `reset_circuit`, `restart`, `compact`, `memory`, `thread`. `start`/`stop` with `id: "all"` updates the global switch. `clone` defaults to profiles and accepts a new `id`/`name` in `data`. `compact` takes a bot ID and optional `data.channel_id`; `memory` takes `channel_id`, `key`, and `value`; `thread` takes a room ID and `data.name`. `probe` and `reset_circuit` take a provider ID. `restart` takes a bot ID.

`probe` is **model discovery** (`GET` to the provider's `/models`). Its successful result contains `provider_id`, `latency_ms`, `models`, `authentication_verified: false` and an explanatory `note`. A public catalog cannot establish credential validity, generation, account limits or tool support; discovery does not reset completion health.

The dashboard's Reasoning controls use the existing profile `request_json` field; there is no new API setting or provider default. Omitted values stay omitted, and compaction applies `compaction_request_json` as top-level replacements before runtime-owned fields/output limits. SSE streams finish cooperatively on CLI server shutdown; clients can reconnect with their cursor after restart.

Credential kinds/fields:

- `providers/{id}/api_key`
- `bots/{id}/token`, `bots/{id}/provider_key`, `bots/{id}/plugin:{plugin-id}`
- `plugins/{id}/api_key`

Bot token validation calls Discord to check both the application and bot identity before storage. The Bot token is not an OAuth client secret or a user account token. All other keys are stored without sending a paid probe; use provider discovery or a real configured activation to verify the endpoint.

## Discord examples

```text
!help
!version
!ver
!status
!stats 168
!stop all
!start all
!stop ada
!stop providers:openrouter
!start providers:openrouter
!restart ada
!models
!clone balanced DeepSeek low
!set profiles balanced {"model":"provider/model-id","context_window":272000,"compact_threshold":0.7,"request_json":{"temperature":0.6,"reasoning":{"effort":"low"}}}
!use ada balanced
!prompt ada You are Ada. Be analytical and inquisitive.
!interval socrates 180
!set bots ada {"enabled_plugins":["web_fetch","memory"],"max_tool_rounds":3}
!set settings global {"global_prompt":"Share interesting ideas. Recognize The Boss and let discussions develop."}
!context ada
!compact ada 123456789012345678
!memory ada {"channel_id":"123456789012345678","key":"topic","value":"Discuss the article The Boss shared."}
!trajectory
!trajectory turn_...
!events ada
!check openrouter
!reset-circuit openrouter
!thread council Today's discussion
!dm !status
!dm What failed in the last day, and which model profiles were involved?
```

`!get`, `!set`, `!create`, `!delete`, and `!clone` provide configuration parity instead of inventing a different command for every vendor parameter. Credentials are the deliberate exception: enter them in the dashboard. A command error never falls through to the model. A normal owner message is queued for Hortator's model, whose `council_inspect` tool can query version/status/statistics/configuration/events/contexts/trajectories but cannot mutate them.

`!help` returns the complete command list in fenced code blocks with no repeated owner/credential preface. `!version` and `!ver` are aliases for the same deterministic, read-only version report, also fenced. Formatting does not change exact-owner authorization, introduce model calls or enable mutations through the inspector. Native Discord Markdown in generated messages is preserved under the existing reasoning/mention rules.

## Local documents and publication

- Authenticated `GET /api/documents` returns `{sites: [...], remote_status: "disabled"}` with draft/published revisions, files, URLs and queue state.
- Authenticated `GET /api/documents/{bot_id}/{slug}/files/{filename}` downloads the current draft file as an attachment.
- Public `GET`/`HEAD /sites/{bot_id}/{slug}/` resolves the published `index.html`; `/sites/{bot_id}/{slug}/{filename}` resolves only that published immutable manifest. Unpublished/missing files are not served. This is local publication, not remote sync confirmation.

Published content receives a sandbox CSP without same-origin privileges, a site-specific resource allowlist and public asset CORS without credentials. Draft and administrative routes retain authentication and ordinary dashboard security headers. No remote transport endpoint is provided.

Bots expose `document_task_rounds` (default20,0–100 additional rounds), `document_task_calls_per_round` (default8,1–20) and `document_task_seconds` (default900,30–3600) through the existing bot-save API. Model profiles expose nullable nonnegative `cache_hit_input_price_per_million` and `cache_miss_input_price_per_million`. See [TOOLS.md](TOOLS.md), [DOCUMENTS.md](DOCUMENTS.md) and [PRICING.md](PRICING.md) for semantics.

## Private tool inspection

`GET /api/agentic-tools?bot_id=&limit=30` returns runner readiness, selected toolchain/limits, keyless configuration schemas/defaults and bounded recent workspace/fetched-document/job metadata. `limit` is 1–100. `GET /api/agentic-tools/inspect` takes `resource=files|file|document|job|output`, required `bot_id`/`channel_id`, and the relevant `task`/`path` or `id`. Optional `offset` is nonnegative; `length` is 1–18,000 (job pages cap at 8,000); `stream` is stdout/stderr. Text-file/log offsets count bytes, while fetched-document offsets count Python Unicode characters. Read responses expose continuation metadata; binary files return measured metadata without treating their bytes as text. Both endpoints require the normal authenticated dashboard session and expose no execution or publication mutation. GET inspection does not extend retention.

Plugin GETs include `keyless`; new workspace/shell packs seed disabled. Bot schemas include `work_task_rounds`, `work_task_calls_per_round`, `work_task_seconds` and `tool_working_set_tokens`, with effective defaults exposed for older records. Saves use the existing revision-aware configuration control path, validate global/per-bot effective working limits and cancel affected active work. See [the feature contract](AGENTIC_TOOLS.md).
