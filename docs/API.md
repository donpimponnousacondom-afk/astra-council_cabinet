# Control API and Discord command parity

All endpoints except `/api/health` and login require the dashboard session. Log in at `POST /api/auth/login` with `{"password":"..."}`; retain the HttpOnly cookie and returned `csrf`. Send `X-CSRF-Token` for POST/PUT operations. There is no API key in a URL and no unauthenticated public control endpoint.

`GET /api/status` includes `background_tasks: {running: [task_name, ...], failed: {task_name: redacted_error, ...}}`. The failure inventory retains up to 50 unexpected task exits for the current process; ordinary provider/turn failures remain in their existing ledgers. `runtime.task_failed` supplies the bounded redacted traceback. See [CONCURRENCY.md](CONCURRENCY.md).

Raw API timestamps keep their existing epoch or canonical UTC representation. Dashboard date labels and Discord `!version` render these instants in `settings.timezone` with an offset. Model-facing built-in tool metadata uses that same local ISO convention, including inspector version dates; raw embedded requests/responses, quoted content and old notes are excluded. No source identity or stored instant is rewritten.

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
| `GET /api/trajectory/{turn-id}` / `/export` | Full requests, private provider diagnostics, assembly metadata, tool events and deliveries |
| `GET /api/events?after=&before=&bot_id=&turn_id=&level=&limit=100` | Stable sequence cursor event ledger |
| `GET /api/events/stream?after=` | Authenticated SSE; reconnect via `Last-Event-ID` |
| `GET /api/context/{bot}/{channel}` | Summary, checkpoint, estimates, notes, first 500 uncompacted messages, compaction history |
| `GET /api/messages/{channel}?after=&through=&limit=100` | Original observed messages; page by sequence |
| `GET /api/artifacts/{id}` | Authenticated generated attachment download |
| `GET /api/export/config` | Configuration without credentials |
| `GET /api/commands` | Command help used by the dashboard |
| `GET /api/openapi.json` | Authenticated OpenAPI specification |

The configuration kinds are `providers`, `profiles`, `bots`, `prompts`, `rooms`, `plugins`, and `settings` (`global`). `id` is immutable. Optional `revision` guards concurrent edits. A patch merges top-level fields; nested JSON objects and lists are deliberately replaced, not magically combined.

`bots.allow_silence` is a boolean, default true (also exposed for older records without the field). False removes the engine's `council_silence` tool and rejects unexpected calls, while preserving ordinary replies, other plugin grants and all activation/usage limits. It is a per-bot built-in capability, not a plugin credential or provider/model parameter. Save through the usual owner control patch, for example `{"action":"save","kind":"bots","id":"dirac","data":{"allow_silence":false}}`, with the current revision when coordinating multiple editors. As with any bot edit, an affected active turn is cancelled.

The provider editor's **User-Agent** field is backed by `providers.headers["User-Agent"]`, not a second top-level setting. Existing header spelling is recognized case-insensitively; saving normalizes it, rejects duplicate spellings/control characters/non-ASCII/values longer than 1,024 characters, and removes blank values so the HTTP client default applies. API callers replacing `headers` must retain any other desired entries. The same header builder handles discovery, generation and compaction; authentication values remain vault-owned.

Provider errors from `/api/control` retain the local HTTP status (normally 502), and return `error`, `source: "provider"`, `api_status`, `upstream_status` (null if no response), and `details`. For model discovery, details include `provider_id`, `operation: "model_discovery"`, endpoint, duration, effective User-Agent, selected response headers and a bounded redacted response excerpt. `provider.discovery_failed` persists the same evidence and readable error. A provider's upstream 401 does not return a local 401 or expire the dashboard session. Discovery failures never reset or increment completion-health counters.

Authenticated trajectory requests include `requests[].diagnostics`: capture availability/status, returned native reasoning text/details, inline reasoning, message-indexed native request continuation, finish reason, actual output caps and structured provider errors. Running streams checkpoint arriving evidence approximately every two seconds; completed/failed/cancelled requests retain their final partial evidence. Credentials remain redacted. Earlier requests with no capture return `capture: "unavailable"` and an explicit note; this is different from a recorded request where the provider returned no reasoning. The default service/model inspector and Discord `!trajectory` omit this private section. There is no model-facing reasoning-read tool.

`profiles.stream` is a boolean, default true, exposed on the model card and editor. False sends buffered generation/compaction requests with `stream: false` and no `stream_options`; saved `include_usage` and vendor JSON remain intact. Reported usage, costs and reasoning still work, but TTFT/TPS stay null. `stream` in raw request JSON cannot override the profile switch. This is unrelated to the dashboard event subscription endpoint.

Private diagnostics include `request_id`, `provider_id`, `model`, ISO `request_started_at`, `reasoning_status` and a request-specific `note`. States are `present`, `pending`, `not_returned`, `none_received`, `not_recorded` (no historical capture), `capture_missing` (new request declared capture enabled but its record is absent), and `inspect_dashboard` (console size limit). Reported reasoning-token counts without text are identified explicitly. New captures include `capture_version: 2` and `response` evidence: requested mode, actual response format/content type, frame/byte counts, completion boundary and compatibility counters. Format failures retain a private, credential-redacted `offending_frame` excerpt capped at 4,000 characters; framing/encoding failures label the previous successfully decoded event `last_frame` instead.

`request.failed` exposes safe `error_origin`, `response_format`, `frame_index` and `field`, alongside actual transport HTTP status, inferred upstream error status and `provider_fault`. Origins distinguish `response_format`, `local_client`, `upstream_error`, `upstream_http`, `transport`, `request_configuration` and `cancelled`. Private `provider_error.details` adds expected/received field types or bounded local exception locations without locals. Format/local-client errors never increment provider-health counters. Raw frame text and reasoning remain outside ordinary events.

Message/context transcript rows expose `addressing` as a JSON object: `author_kind`, whether it was observed live, structured mention/reply `targets`, and any resolved `reply_target`. Legacy rows may have no captured addressing. Model request transcripts add per-viewer `audience` (`you`, `other_participant`, `channel`, `unresolved_reply`) and `addressed_to_you`, resolving stored same-channel reply authors where possible. This metadata comes from Discord identities, never message-text assertions. A reply preview is bounded to 600 characters and is untrusted quoted content.

Transcript rows also expose `discord_parts` with normalized `content`, `embeds` and `components` source strings; old uncaptured rows default to `{}`. The existing `content` field is their composed model-visible text. Partial Discord edits replace only supplied sources and produce an idempotent `message.edited` event with a `historical` flag. App/webhook addressing retains Discord-supplied `application_id`/`webhook_id` when present; these never establish human authority. `is_boss` excludes bot/webhook attribution even if an author ID were made to resemble the owner in a fixture.

The owner-only keyless `discord_send` plugin uses normal global/per-bot grants, has no extra credential field, and is off by default. Its targets/send/status schema is in the plugin catalog; no unauthenticated cross-channel send endpoint exists. Outbox rows include `routing` JSON (default `{}`), with private source/target/guild/room/mode and argument fingerprint. A per-turn delivery key derives one durable outbox ID. Confirmed receipts contain `status: sent`, `delivery_id`, target channel, mode, message ID and Discord URL; failed/uncertain receipts contain no claimed URL and are not retried. Routed target transcripts carry bot-authored `addressing.routing` mode/source-bot metadata. See [operations](OPERATIONS.md#hortator-control-scope-and-cross-channel-posts) and the [tool contract](TOOLS.md).

Directed human activations use trajectory triggers `human_reply` or `human_mention` and emit `activation.human_directed` with the triggering message ID and coalesced count. Their effective activation metadata is included in the recorded runtime prompt. `bot_runtime.retry_until` separates a provider/budget retry barrier from ordinary `next_at` cadence; human priority bypasses cadence and personal send cooldown, never the retry barrier. Persistent `human_attention_claims` are internal scheduling records, with no model/API command to manufacture a priority claim. Restart recovery marks partial running request diagnostics `interrupted` without discarding their last stored evidence.

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

The dashboard's Reasoning controls use the existing profile `request_json` field; there is no new API setting or provider default. Compaction applies `compaction_request_json` as top-level replacements, then omits `max_tokens`, `max_completion_tokens` and `max_output_tokens` from its outgoing copy. Stored JSON and ordinary generation caps are preserved. SSE streams finish cooperatively on CLI server shutdown; clients can reconnect with their cursor after restart.

`profiles.summary_tokens` now limits **retained summary text**, excluding private reasoning. It defaults to 1,024, has a minimum of 128 and no fixed 32,000 maximum; the existing below-60%-of-context validation still applies. Existing values are not migrated or reset. The finished text is counted with cl100k_base on a worker thread; its count is local retention accounting, not provider-reported billing usage. A completed oversized or incomplete candidate cannot advance the checkpoint. The entire returned candidate stays in `requests.response`, while private reasoning stays in `request_diagnostics`. No schema/table migration is required, but older releases interpret this same field as a combined provider cap.

Compaction request context includes `compaction_output_policy: "provider_default"`, `omitted_output_cap_fields`, `retained_summary_token_limit` and `summary_tokenizer`. `compaction.summary_checked` includes `summary_tokens`, the retained limit/tokenizer, reported output/reasoning counts, finish reason and `accepted`. Acceptance here is for one candidate; all batches and final context fitting must pass before `compaction.completed` commits the summary/checkpoint. Failed/incomplete candidates remain inspectable under the usual authentication/redaction rules.

Credential kinds/fields:

- `providers/{id}/api_key`
- `bots/{id}/token`, `bots/{id}/provider_key`, `bots/{id}/plugin:{plugin-id}`
- `plugins/{id}/api_key`

Bot token validation calls Discord to check both the application and bot identity before storage. The Bot token is not an OAuth client secret or a user account token. All other keys are stored without sending a paid probe; use provider discovery or a real configured activation to verify the endpoint.

For the `web_search` plugin, non-secret `config` fields are `engine` (`auto`, `brave`, `duckduckgo`, `both`), `count` (integer 1–10 per engine), and optional Brave `endpoint`. Defaults are Auto, 5 and the existing Brave web-search API. Equivalent bot overrides live in `plugin_config.web_search`; remember that control patches replace nested config objects. Validation occurs before persistence and preserves unrelated records. `plugins/web_search/api_key` stores the global Brave key; `bots/{id}/plugin:web_search` is its optional per-bot override. DuckDuckGo needs no key. GET returns only credential-presence flags, never the saved value, and does not prove the key works. See [search setup](WEB_SEARCH.md).

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

## Documents and automatic publication

- Authenticated `GET /api/documents` returns `{sites: [...], remote_status, publishing}`. `publishing` contains global worker `enabled`, `configured`, `status`, `last_error` and `public_base_url`; configured readiness checks local settings/key/pins, not live SSH/HTTPS acceptance. Each site reports current and local-published revisions, files, automatic publication mode and truthful queue/delivery state.
- Authenticated `GET /api/documents/{bot_id}/{slug}/files/{filename}` downloads the current draft file as an attachment.
- Public `GET`/`HEAD /sites/{bot_id}/{slug}/` resolves the published `index.html`; `/sites/{bot_id}/{slug}/{filename}` resolves only that published immutable manifest. Unpublished/missing files are not served. This is local publication, not remote sync confirmation.

Site responses include `synced_revision` (0 until a verified delivery), `delivery_current`, `public_url` for the last acknowledged revision, `planned_public_url` and the newest `sync` row. A newer pending revision may coexist with an older live URL. Queue rows retain status/attempts, retry/error timestamps and `snapshot_commit` / `remote_commit` for local/remote Git traceability. The global worker status describes enablement/readiness; a site's `remote_status` describes its queue state when configured. Neither a configured worker nor a planned URL establishes delivery.

Verified receipt observations appear as queue fields `remote_current_revision`, `remote_delivery_current` and `remote_observed_at`. The site's `observed_remote_revision` and `remote_observed_at` select the latest verified observation independently of job order or later retry timestamps. `remote_currentness_verified` is false with null observation metadata for legacy rows lacking this evidence. A verified remote revision ahead of restored local data prevents `delivery_current` from becoming true merely because the local database contains an old successful job.

Published local content receives a sandbox CSP without same-origin privileges, a site-specific resource allowlist and public asset CORS without credentials. Draft and administrative routes retain authentication and ordinary dashboard security headers. There is no bot-root page, deletion endpoint, model-selected destination owner or model remote-command endpoint.

The existing authenticated plugin-save API accepts global `document_site.config` fields `local_base_url`, `public_base_url`, `auto_publish`, `remote_enabled` and a `remote` object. Remote fields are `host`, `port` (1–65535, default22), `username`, `identity` (default `publishing`), `web_root`, `state_root` and `debounce_seconds` (0–60, default5). Both booleans default false. Enabled delivery requires HTTPS public URLs and complete, separate absolute remote roots. Bot-level document overrides may set **only `local_base_url`**. These fields never carry the private SSH key; the worker resolves its named identity from the existing vault and enforces verified application-owned host pins. Models receive document tools, not these configuration controls. See [OPERATIONS.md](OPERATIONS.md#automatic-remote-publishing) for setup.

The `document_site` tool contract requires explicit `create`; `start`/`edit` resume existing sites. Its paged reads/history, optimistic file edits/restoration and public-only replication use the registry's complete schema/error feedback. Automatic saves commit local publication and queue metadata together. Transfers run independently of HTTP requests and model turns, preserving immutable jobs through retries and receipt reconciliation. See [DOCUMENTS.md](DOCUMENTS.md) for complete operations and limits.

Bots expose `document_task_rounds` (default20,0–100 additional rounds), `document_task_calls_per_round` (default8,1–20) and `document_task_seconds` (default900,30–3600) through the existing bot-save API. Model profiles expose nullable nonnegative `cache_hit_input_price_per_million` and `cache_miss_input_price_per_million`. See [TOOLS.md](TOOLS.md), [DOCUMENTS.md](DOCUMENTS.md) and [PRICING.md](PRICING.md) for semantics.

## Private tool inspection

`GET /api/agentic-tools?bot_id=&limit=30` returns runner readiness, selected toolchain/limits, keyless configuration schemas/defaults and bounded recent workspace/fetched-document/job metadata. `limit` is 1–100. `GET /api/agentic-tools/inspect` takes `resource=files|file|document|job|output`, required `bot_id`/`channel_id`, and the relevant `task`/`path` or `id`. Optional `offset` is nonnegative; `length` is 1–18,000 (job pages cap at 8,000); `stream` is stdout/stderr. Text-file/log offsets count bytes, while fetched-document offsets count Python Unicode characters. Read responses expose continuation metadata; binary files return measured metadata without treating their bytes as text. Both endpoints require the normal authenticated dashboard session and expose no execution or publication mutation. GET inspection does not extend retention.

Plugin GETs include `keyless`; new workspace/shell packs seed disabled. Bot schemas include `work_task_rounds`, `work_task_calls_per_round`, `work_task_seconds` and `tool_working_set_tokens`, with effective defaults exposed for older records. Saves use the existing revision-aware configuration control path, validate global/per-bot effective working limits and cancel affected active work. See [the feature contract](AGENTIC_TOOLS.md).
