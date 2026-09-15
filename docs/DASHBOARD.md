# Dashboard workbench and legacy boundary

The active dashboard is a desktop workbench for operating many bots and providers at once. Its inventory presentation is compact lists only, with aligned values, restrained decoration, monospace identifiers and a VS Code Dark+ inspired palette. Mobile and touch layouts are not product requirements. Information density must come from using the available space, not shrinking text, hiding controls or requiring browser zoom.

The previous dashboard is a frozen comparison and fallback during operator acceptance. It receives no new features. The owner will decide when the replacement matches their daily workflow and the fallback can be removed. This is a frontend transition, not a new council runtime or a configuration migration.

## Ownership and removal boundary

| Surface | Entry and source | Responsibility |
| --- | --- | --- |
| Active workbench | `/`, `web/index.html`, active modules in `web/src/` | All new dashboard implementation and UX work |
| Frozen dashboard | `/legacy/`, `web/legacy/index.html`, `web/src/legacy/` | Temporary fallback and original behavior reference |
| Control service | Existing `/api/*` HTTP endpoints | Authentication, CSRF, revision checks, validation, authorization and domain actions |
| Published bot sites | Existing isolated site-serving routes | Site content and security, independent of either dashboard |

`npm run build --prefix web` builds both HTML entries into `web/dist`. The existing Python static-file host serves the resulting files; Python does not import React components, CSS, frontend state or a legacy version flag. This transition changes no backend/API contract. Both frontends use the same authenticated HTTP API and external runtime data. Running either dashboard must not start another scheduler, Discord gateway or provider client.

The active frontend must never import a module or stylesheet from `web/src/legacy/`. The frozen copy owns its complete component/API-helper/style tree; relative imports remain inside that tree. Proven functionality may be copied into the active frontend and adapted there, but that must not become an ongoing shared UI dependency. Trajectory and Analytics are deliberately carried forward because the owner values those workflows.

The legacy HTML wrapper may identify the fallback and link to the workbench. That wrapper is outside the frozen application. Legacy retains the same security assumptions and authentication requirements as the active dashboard; its existence does not create a bypass or a second credential system. Its build identity must still describe the artifact being served.

Once the owner accepts removal:

1. Remove `web/src/legacy/` and `web/legacy/`.
2. Remove the legacy Vite build input, its `legacy/index.html` formatting-command inputs in `web/package.json`, and fallback links in the active frontend.
3. Remove `web/tests/legacy/` and any legacy-only test configuration. Keep active workbench/API/security tests.
4. Build and smoke-test the active root, authentication, API routes and published site isolation.

No database migration, backend feature removal, credential change, source submodule or copied data directory is involved. Historical Git commits preserve the old dashboard; it does not need to remain deployable indefinitely.

## Layout contract

Implemented navigation includes **Ctrl-K** for page/record search, **Ctrl-B** for the sidebar, **Ctrl-S** for the active configuration editor and **/** for the inventory filter. Editors are docked and can maximize. Unsaved drafts are protected when switching records/pages or closing an editor. The same configuration/control/inspection functions remain available through the current API; operator acceptance is separate from implementation and automated evidence.

The workbench uses a small navigation rail, compact command/header rows, a main workspace and a narrow status/build area. Configuration inventories use rows rather than tall cards. Six bots or six providers should fit in the initial list viewport on an ordinary desktop without scrolling past branding, onboarding copy or repeated labels first. Larger inventories scroll in their content area with headers remaining useful.

Provider rows should make identity, endpoint, state, credential status, profile count, recent failures, concurrency, timeout and actions easy to compare. `Credential: configured` and `Profiles: 3` do not each need their own padded section. Bot rows should make gateway/turn state, selected profile/provider, context usage, cadence, capabilities and actions visible together. Long IDs and errors may wrap or expand deliberately; a clipped value must remain available through an accessible inspection path.

Editing should use the available workspace width and height. Keep the entity identity/revision, section navigation and save/cancel controls visible while its fields scroll. Related short fields belong beside each other; JSON, prompt text and diagnostics need wider reading surfaces. Do not nest an unrelated inspection workflow deep inside a long form when it can be reached directly. A new visual layout must retain the exact save/revision/credential semantics.

Persistent warnings and failed saves must remain visible and attributable. Successful background polling must not steal focus, replace a draft, collapse an evidence panel or move the operator away from the selected record. Unknown values remain explicitly unknown, including token measurements and cost coverage.

Each asynchronous save or credential completion belongs to the editor instance that started it. If the operator has switched to another record, an old completion must not close or replace the new editor. Dirty-state protection includes invalid raw JSON, pending credentials and private-note drafts, not just the last successfully parsed configuration object. Changing a note's channel or a credential's target must not silently carry unsaved content into a different destination.

Use local, reusable CSS variables for the theme. Dark+ includes the base Visual Studio dark theme; relevant anchors are editor background `#1e1e1e`, editor text `#d4d4d4`, menu surface `#252526` and the familiar blue `#007acc`. These are visual references, not a dependency on the VS Code application. [Official base theme](https://raw.githubusercontent.com/microsoft/vscode/main/extensions/theme-defaults/themes/dark_vs.json).

Dark+ token accents such as `#9cdcfe`, `#4ec9b0`, `#b5cea8`, `#ce9178` and `#c586c0` can distinguish identifiers, values and evidence. Preserve text labels and severity meanings so color is not the only signal. [Official Dark+ theme](https://raw.githubusercontent.com/microsoft/vscode/main/extensions/theme-defaults/themes/dark_plus.json).

## Functional parity inventory

The following inventory was audited against the frozen application and existing browser tests. It defines behavior that must remain available; it is not a claim that every row has received a live Discord/provider test. Record actual execution evidence in [VERIFICATION.md](VERIFICATION.md), including whether checks use isolated fixtures or the live read-only runtime.

### Application, authentication and identity

- Session discovery, password login, logout and expired-session handling retain the HttpOnly cookie and CSRF contract. Credentials do not enter browser storage, URLs or exports.
- A failed upstream discovery request is presented separately from a failed dashboard session. An upstream HTTP 401 wrapped by the control API must not log the operator out.
- Initial loading, recoverable connection failures, manual refresh, live connection state, reconnect and SSE cleanup remain supported. Navigation and browser history retain the selected screen.
- Runtime status, statistics, recent events and configuration schemas are loaded through the existing API. New client state must not invent provider health or bot readiness.
- The server startup identity and dashboard build identity remain distinct. Commit, title, ISO date/time, branch, cleanliness, missing metadata, startup time and mismatch warnings remain inspectable. Ordinary displayed dates use the council timezone; source metadata preserves its original UTC instant.
- Council start/stop and the consequences for active work remain explicit owner actions. Background refreshing never changes enablement or configuration.
- JSON/code disclosure and copy controls remain available. Errors, empty states, disabled actions, meaningful labels, focus visibility and keyboard access remain functional at desktop density.

### Overview and bot inventory

- Overview retains active/total bots, input/output/reasoning usage, measured TTFT, p95, known cost coverage, activity history, provider state and recent operational activity.
- Readiness failures remain discoverable without implying a configured or paused bot is online. Setup actions lead to the relevant configuration.
- Every bot remains selectable for editing, context/memory inspection and start/stop. Gateway state, provider pause/circuit, active turn, next activation, readiness and runtime errors remain distinguishable.
- Profile/model/provider, interval, tool grants, role, identity color and context estimate/threshold remain inspectable. A model change must preserve personality, grants, credentials and memory.
- Bot creation uses a new stable identifier. Existing identifiers remain immutable. Deletion is explicit, validates existing domain dependencies and retains historical records.

### Bot editor

| Section | Required behavior |
| --- | --- |
| Identity | Display name, stable ID, role, identity color, room grants and activation setting |
| Model and rhythm | Selected model profile, activation interval, personal send cooldown, evaluate-with-no-new-messages toggle, hourly activation limit and optional daily cost stop |
| Prompts | Personality, ordered shared prompt assignments, dynamic tail and inherited global prompt preview |
| Capabilities | Plugin grants, global-off indicators, intentional-silence toggle, ordinary tool rounds/calls, per-bot non-secret plugin JSON and key overrides only for plugins that use keys |
| Extended work | Document task rounds/calls/time, permitted local preview URL override, file/reading task budget and active tool-context budget; no extra budget renewal implied |
| Footer | Per-bot enablement, role defaults, bounded literal template, supported placeholders, validation and preview; no executable template expressions |
| Discord | Application ID, separate write-only verified Bot token, reconnect, gateway/scheduling evidence, invite URL/copy, setup blockers and per-bot provider-key override |

Hortator control-channel/thread/owner-DM restrictions and the separate granted `discord_send` capability must remain clear. A mention in another room does not silently grant intake scope. Token verification may discover the application ID; an invalid token must not be saved as a verified identity.

### Providers

- Search/list/create/edit/delete remain available with stable identity and revision protection.
- Preserve provider kind, base URL, enabled/requires-key settings, total request timeout, request concurrency, failure threshold and circuit recovery delay.
- Preserve the explicit User-Agent field and all other non-secret headers. User-Agent round-trips through the case-insensitive existing header entry; clearing it restores the client default.
- Credentials are separate write-only save/replace/remove operations. Creating a provider does not imply a credential is present. Empty credential input must never overwrite an existing secret merely because an unrelated field was saved.
- Discovery retains latency, catalog, explanatory note and upstream/control status evidence. It is not a paid completion test and does not establish healthy generation.
- Pause/enable and reset-open-circuit remain available. Display recent failure/attempt counts separately from consecutive failures and the last error.
- Diagnostics distinguish a local total deadline, local connection-pool wait, parser/client problem, real transport failure and upstream error where the API records that distinction.

### Model profiles

- Search/list/create/edit/delete/clone remain available. Cloning starts a separate profile; changing a bot assignment does not rewrite its other configuration.
- Preserve provider, exact model ID, context window, compact threshold, response reserve, retained-summary text limit and recent-message tail settings.
- Preserve the immediate per-profile SSE toggle as well as its editor setting. Buffered mode retains reported usage/reasoning and explicitly has no measured streaming TTFT/TPS; the saved include-usage setting is not silently destroyed when disabled.
- Native reasoning controls must preserve exact generation JSON, unrelated vendor values, explicit false/zero/custom types, nested structures and the distinction between unset and off.
- Compaction overrides and effective reasoning remain inspectable. The retained summary limit excludes private reasoning; no combined output cap is sent for compaction. Generation caps and saved JSON are preserved. No fixed 32K maximum is reintroduced.
- Invalid JSON remains visible in the editor with actionable feedback. Changing a structured control must not replace an invalid or unsupported custom structure behind the operator's back.
- Preserve flat input/output prices, optional cache-hit/cache-miss rates and unknown/partial cost explanations. No tariff is inferred or overwritten by the UI.

### Prompt library, rooms and settings

- Prompt list/search/create/edit/delete, content preview, assignment counts and revision state remain available. All non-secret configuration is still reachable through the full JSON editor.
- Rooms retain name/ID, guild/channel, thread participation, minimum send gap, human and external-app intake switches, member list and explicit thread creation with returned thread ID.
- External application intake text distinguishes authenticated app responses from ordinary incoming webhooks and never grants app text human authority.
- Council settings retain immutable owner display, control guild/channel, global prompt, timezone, maximum simultaneous turns and Discord incident-notification setting.
- Configuration export remains credential-free and distinct from the complete local database/master-key backup.
- Discord command reference stays sourced from `/api/commands`, with syntax, descriptions, examples and relevant control/help guidance. Do not maintain a second manually divergent command catalog.

### Plugins and operational tools

- The plugin registry retains search, global enablement, grants count, schema inspection, generic non-secret JSON and applicable write-only credentials. Keyless plugins must not acquire empty secret forms for visual uniformity.
- Web search retains Auto/Brave/DuckDuckGo/Both selection, results-per-engine bounds, partial-result semantics and the Brave API-key setup location. DuckDuckGo needs no credential.
- Document settings retain automatic publication, remote delivery, local/public base URLs, global SSH destination/history/identity configuration and debounce. Per-bot editing must not gain control of the global remote endpoint.
- Sites retain owner/slug, draft/local/remote revisions, file count/size, queue state, attempts, retry/delivery times, errors and local/remote snapshot commits. A planned URL or queued revision is never represented as confirmed remote delivery.
- Existing published-site links and private draft file downloads retain their security behavior. Site content must not execute with dashboard-cookie/DOM/API access.
- Workspace/shell/web-fetch settings retain schema-provided limits and defaults, runner readiness/details and no-key explanation.
- Workspaces, fetched documents and jobs remain inspectable globally and per bot. Bounded file/document/output reads, stdout/stderr selection and Next chunk retain the exact original evidence; inspection never reruns a job. The frozen UI has no job-cancel button, so adding one is a new operation, not a parity obligation.

### Context and private memory

- Bot/channel/thread selection retains context separation. Inspect estimated context, compaction count, checkpoint, uncompacted-message count, current summary and history.
- Force compact remains an explicit action with its turn ID and error feedback. Opening the view must not compact, clear, consolidate or rewrite anything automatically.
- Notes show key, value and update timestamp. Editing writes the complete replacement value to that key. Saving an empty value deletes that key under the current control API contract; make that consequence visible.
- Uncompacted message-source evidence remains inspectable and bounded. The UI must distinguish persistent notes from the conversation summary and must not suggest ordinary compaction frees the memory quota.

### Trajectory and analytics

- Trajectory retains Turns/Event ledger views, bot/outcome/severity filters, search explicitly scoped to loaded records, older-record pagination, refresh and stable selection.
- Turn detail retains overview/timing waterfall, request count, decision, turn event timeline, exact requests, canonical responses, private provider reasoning/diagnostics, request context, tools/results, compaction details, delivery/outbox evidence and full JSON/export.
- Provider reasoning remains authenticated/private evidence. Missing capture and an actual empty reasoning response remain distinguishable. No frontend export or model-facing action may bypass existing redaction/authorization.
- Unknown/interrupted/failed/cancelled/suppressed delivery states remain distinct. Inspection cannot imply retry, success or guaranteed non-delivery.
- Analytics retains supported hour ranges, usage chart, totals, cached/reasoning tokens, measured mean/p95 latencies, error rate, known/estimated cost coverage, activation outcomes and tool execution.
- Grouping remains available by bot, profile, provider, model ID, profile revision and purpose. Export preserves the selected statistics. Missing measurements stay unknown; compact notation must not silently change exact data.

## Acceptance checklist

These are release checks, not a rolling session status. Their results belong in the dated verification record.

- [ ] Build both HTML entries from the intended commit; direct navigation and refresh work for root and legacy URLs.
- [ ] Verify active modules never import frozen legacy modules; deleting the legacy directories/build input does not prevent the active frontend from compiling.
- [ ] Test authentication, session expiry, CSRF-protected mutations and a provider discovery failure that does not log the user out.
- [ ] Exercise six or more bots/providers in a desktop viewport and verify useful rows/actions fit without the former card-height scrolling.
- [ ] Exercise every registry/editor category, including creation, revision conflict, validation error, clone/delete where available and credential replacement/removal using isolated dummy data.
- [ ] Verify dense checkbox/toggle visibility, focus labels, keyboard operation, long IDs/errors and invalid JSON; test at ordinary desktop zoom.
- [ ] Verify no polling refresh overwrites an open draft or steals inspection focus. Closing a changed draft must have an explicit, predictable outcome.
- [ ] Exercise model SSE/reasoning/compaction/pricing and per-bot footer/silence fields with unrelated JSON preserved.
- [ ] Exercise context/note inspection, paged tool evidence, document queue/current-delivered distinction and site isolation in fixtures.
- [ ] Preserve populated trajectory/private reasoning/export and analytics workflows, including unknown values and council-timezone timestamps.
- [ ] Verify live runtime/build identity and perform only read-only smoke checks against live configuration unless a separate live mutation is authorized.
- [ ] Keep the fallback available until the owner accepts the new daily workflow. Removal requires only the frontend steps above.

## Audit findings and intentionally separate follow-ups

The original UI's largest avoidable cost was layout: tall duplicated cards, a large version banner on every page, repeated headings/descriptions, explanatory paragraphs inside every configuration card, and long forms constrained to a narrow modal. Those are the redesign's direct targets. Inventory tables, compact status rows and wide editor sections address them without changing model behavior.

Useful modern-only improvements are search across inventories, sortable columns, direct links from bot to model/provider, open-record navigation, discoverable keyboard actions, unsaved-draft protection and direct access to document/tool inspection. Each improvement needs real behavior and a test; a visual affordance must not promise an unavailable action.

Some diagnostics remain bounded by the current API: trajectory text search searches loaded rows, agentic inspection lists recent records, and site inspection reports existing queue state. A frontend refresh cannot turn these into whole-ledger search, a job runner, arbitrary remote editing, private-memory revision history or background compaction. Those require separately scoped API/domain work. Do not rewrite the backend to make the visual migration seem more complete.

The desktop requirement removes mobile layout obligations for the active workbench. It does not remove keyboard navigation, readable focus, semantic labels, error visibility or usable controls. Existing legacy mobile tests live under `web/tests/legacy/` during acceptance but must not force touch-sized spacing back into the workbench.

## Optional experiment and companion controls (2026-09-13)

The active workbench adds **Snapshots** for named application captures, compatibility inspection and guarded full/selected-bot restoration with explicit runtime resume. **Bots → Global notes** inspects and edits that bot's cross-channel notebook without requiring an existing conversation; unsaved notes survive tab navigation and pending actions remain bound to their originating bot. Capabilities exposes its independent strict memory quota and optional slash registration/setup guidance. None of these features are added to the frozen legacy build or require legacy modules.
