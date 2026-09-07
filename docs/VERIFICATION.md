# Verification record

Verified in this workspace on 2026-09-07. Automated provider/Discord tests use controlled transports and synthetic credentials. Later read-only live checks used the user's configured credentials in memory without exposing them or making paid completion probes; see the typing/configuration verification below.

## Council refinements and merged PR verification

On `feat/council_refinements`, created from the checked-out main `2a20ca0` on 2026-09-07:

- [PR #4](https://github.com/donpimponnousacondom-afk/astra-council_cabinet/pull/4) was already merged into main at 12:28:47 UTC. The original local typing tip `e8c75d9` and main `2a20ca0` have identical trees; the PR's changed files contain the expected typing implementation/tests/docs and no new runtime or credential files. GitHub reported no CI checks or reviews. The obsolete local typing branch was deleted at the user's request after that comparison. Main and remote refs were left unchanged, with no push.
- **80 backend tests passed** on Python 3.12. Nine new cases cover brief reconnects in the same/separate notification polls, sustained outage/recovery despite startup throttling, reason updates preserving the original duration, unrelated incident reporting and secret redaction, disabled notifications/offline cleanup, intentional client close, public model discovery without authentication/health claims, and actual CLI shutdown under SIGINT and SIGTERM with an authenticated SSE connection held open. The shutdown cases use isolated child processes and verify persisted runtime cleanup and no forced-drain traceback.
- **7 Chromium browser tests passed** against a real isolated temporary API and production build on port 18000. New reasoning cases cover native effort/toggle/budget edits, removal versus false, sibling/vendor JSON preservation, profile persistence/reopening, nested/custom summaries, compaction's top-level replacement preview, invalid-JSON protection, custom structure protection, custom negative budgets and mobile layout. Numeric controls impose no universal vendor minimum. React preview assertions wait for the rendered update. The test profile data and credentials are synthetic; no live dashboard configuration was changed by these tests.
- Ruff lint/format, Prettier, strict TypeScript checking and Vite production build passed. The mobile reasoning screenshot was visually inspected; generated screenshots/traces remain ignored under `web/test-results/`.
- Deployed through the existing attached Screen `387556.hortator`, window `dashboard`. The old loaded server needed its known second Ctrl-C to finish SSE drain, after verifying no active turns/requests or pending deliveries. The tested server is now PID `422205` on loopback port 8000; rediscover before later control. Health, dashboard HTML and matching new JS/CSS assets returned HTTP 200. Authenticated status lists all five configured bots with empty local readiness lists, Hortator online/enabled and the four normal bots disabled. Login/logout revocation passed, the diagnostic session was revoked, and cwd/database/lock/log paths remain in the intended repository/external storage. Screen attachment was preserved.
- `20260907T124253Z-before-refinements` is a complete stopped-runtime rollback snapshot of the user-confirmed configuration. SQLite integrity, nine file hashes, owner-only permissions and decryption of all 13 encrypted entries passed. After deployment, all live configuration bodies/revisions and decrypted credential values still matched this snapshot exactly. No schema change, credential edit or saved model/provider setting change was required.
- **Operator acceptance:** the user confirmed live Discord typing and Hortator's new provider/Kimi K3, and considers context/concurrency configuration resolved. Qwen is set to two concurrent requests for four units. Earlier audit corrections below are historical and superseded; do not repeat them as current blockers. Full normal-council activation/tool/delivery acceptance remains for the user. This feature session made no paid probes, sent no manual Discord messages and enabled no normal bots.

## Earlier migration and recovery

During the standalone-repository migration on the same date, dependencies were restored and the production build, 57 backend tests on Python 3.12 and all 5 browser tests were rerun successfully from `/home/codexy/codex/astra-council_cabinet` inside the shared Screen session. Ruff, Prettier, documentation links and the migrated production API/assets/login/logout checks also passed. Python 3.14 results below refer to the earlier implementation check. The local pre-push guard was checked against feature-branch updates, pushes to main and deletion of main without contacting a remote. See [SESSION_HANDOVER.md](SESSION_HANDOVER.md) for repository/runtime continuity.

Later on 2026-09-07, the missing Screen session was recreated as `343348.hortator` and runtime storage was moved outside Git to `/home/codexy/.local/share/hortator`. A launcher and environment file installed outside the checkout select that directory on all branches. The old database/key were absent before recovery, so that recovery created a fresh disabled-draft setup, not a restoration of prior configuration. The frozen Python dependency sync, locked Node installation, strict TypeScript check, and production build passed. Launcher/environment shell syntax checks passed. The running API's file descriptors point to the external database and lock; Screen's log also points outside the checkout. Data directories are mode 0700 and the database/key/password/log files are mode 0600. Health, dashboard HTML, and matching JavaScript/CSS assets returned HTTP 200. Login and logout passed, the authenticated bot list showed three disabled bots with no tokens, and a post-logout status request returned HTTP 401. The backend/browser suites were not rerun for this host configuration and documentation change; their results below remain the earlier migration evidence. Live Discord/provider integration was unverified at that recovery.

## Earlier typing feature and configuration audit

On `feat/add_typing_indicator`, based on `dev/initial_phase` at `737c6bf`. These observations precede the operator's later configuration fixes and live typing confirmation above:

- `uv run pytest -q`: **71 passed** on Python 3.12, including **14 new typing tests**. The new cases cover Ada, Socrates, Dirac, Curie and Hortator; activity before context preparation and during generation/delivery; silence, provider failure, cancellation and compaction failure; independent simultaneous identities; renewal and scope removal; redacted/throttled nonfatal failures; slow presence requests and paused bots. These are controlled transports, not visual Discord acceptance.
- `uv run ruff check hortator tests` and `uv run ruff format --check hortator tests`: passed. No UI source changed; the existing production build was retained, and frontend/browser checks were not repeated for this backend-only feature.
- Recreated the absent Screen using the user's `.screenrc` and `bash --login -i`. Verified interactive/login Bash, configured PS1 and `ll` alias. The existing configuration supplies 50,000 lines of scrollback and the xterm alternate-screen override. Host startup files were not edited. Mouse-wheel behavior in the user's terminal still requires their observation.
- Restarted the server with the tested code in attached `387556.hortator`, window `dashboard`. Health and dashboard returned HTTP 200; authenticated readiness had no missing-field issues for all five bots, Hortator reconnected online, logout revoked the diagnostic session (subsequent status HTTP 401). API cwd resolves to this repository and database/lock/log paths resolve outside Git. One API process listens on loopback port 8000.
- Both stopped-runtime archives in the [handover](SESSION_HANDOVER.md#configured-state-and-snapshots) passed SQLite integrity, SHA-256 file checks, and decryption of all 12 encrypted entries using their matching keys. They include configuration, history, credentials, artifacts and host launch settings with owner-only permissions. Live snapshots at 14:06 and **14:17 Europe/Madrid** capture the user's timeout and new Ollama settings; the latest is `20260907T121700Z-configured-final`, including the disabled Ollama provider. Both passed the same checks for all **13** encrypted entries. Validation used immutable SQLite reads and verified all nine file hashes with WAL/SHM excluded. Logs/artifacts were copied separately rather than atomically with SQLite.
- All five live Discord tokens matched their distinct configured applications and stored user IDs; Message Content Intent, guild membership and all seven effective channel permissions passed, without Administrator. Featherless model discovery and plan lookup returned HTTP 200. The original selected models are available and advertise tools. The user's new Ollama endpoint returned a public model catalog with `kimi-k3`, while the saved profile uses `kimi-k3:cloud`. Its credential field contains the endpoint URL and needs a real cloud API key; the public catalog response does not establish authentication. These issues and Featherless's Qwen concurrency limit prevent claiming the entire setup is ready for simultaneous activation; see [CONFIGURATION_STATUS.md](CONFIGURATION_STATUS.md).
- Historical live ledger evidence includes successful Hortator Kimi requests/deliveries through Featherless, three brief Hortator reconnect pairs, one 120-second provider timeout and a cancelled Dirac request. No normal bots were enabled by the agent, no Discord messages or typing pulses were manually sent, and no paid completion probes were made. The new Ollama profile, normal-bot end-to-end tool/delivery behavior and visual typing remain for operator acceptance.

## Earlier baseline checks

| Check | Result |
| --- | --- |
| Backend suite, Python 3.12 | 57 passed |
| Backend suite, Python 3.14 | 57 passed |
| Ruff lint and formatting | Passed |
| TypeScript strict checking and Vite production build | Passed |
| Prettier frontend formatting | Passed |
| Chromium browser suite against a real temporary API and production UI | 5 passed |
| Local production startup, static assets, authenticated API and session revocation | Passed on port 8000; three disabled drafts, no external credentials |
| Compose YAML parsing and deployment settings | Passed |
| Docker image build / container startup | Unverified: Docker daemon socket access is denied in this environment |
| Live Discord gateways, OAuth invitations, OpenRouter/model and media endpoints | Unverified: operator credentials and Discord channel IDs are required |

The Python tests cover these behaviors:

- Exact owner identity, impersonation/webhooks, unauthorized model tools, command routing without model calls, protected credentials, encrypted storage, secret redaction, token/application mismatch, preservation of public application IDs and invite links, session cookies, CSRF and origin checks.
- Fragmented streaming responses and tool arguments, raw provider parameters, missing usage, hidden reasoning exclusion, provider metadata and partial output on failure, model-specific versus provider-wide errors, isolated credential overrides, circuit recovery and per-request cost thresholds.
- Independent timer decisions, deliberate silence, duplicate input, arrivals during generation, cancellation, cooldowns, ambiguous delivery, restart recovery, scoped memory, model swaps, tool-round budgets, compaction boundaries and failure rollback. Current guild/channel/thread configuration is checked again before scheduling and sending; retired IDs cannot inherit another bot's historical memory.
- Plugin permission enforcement, public-network fetch boundaries and DNS rebinding checks, Brave search key selection, image/audio attachment ownership, and read-only Hortator inspection.
- Exclusion of a second process from the same runtime directory, and a live SQLite backup restored with matching credentials, memory, event history and artifacts. Existing backups cannot be overwritten.

The browser suite covers login, pause/resume, command help, provider creation and write-only key entry, incomplete-setup errors, profile cloning, advanced parameter JSON, personality-preserving model swaps, all operational pages, and desktop/mobile layouts. Its seeded trajectory is produced by the actual engine using synthetic provider responses: a memory tool call, reply, delivery and compaction. Request bodies, prompt construction, tool results, delivery evidence and JSON export are inspected in the UI. This fixture only runs in an isolated temporary test directory; production begins with disabled drafts and no traffic.

Dependency deprecation warnings remain in the test client (and Python 3.12's Discord audio dependency); they do not fail the checks. The tests establish application behavior with controlled endpoints, not compatibility with every provider's interpretation of the OpenAI protocol or a large-scale production load limit.

## Repeat the checks

From `/home/codexy/codex/astra-council_cabinet`:

```bash
uv sync --frozen
npm ci --prefix web
(cd web && npx playwright install chromium)
./scripts/check.sh
```

Browser failure screenshots/traces and successful overview/trajectory screenshots are written under `web/test-results/` and ignored by Git.

## First live acceptance

After completing the dashboard's setup steps:

1. Authorize distinct Discord applications for Hortator and at least two council members, with Message Content Intent enabled. Set the council and separate reporting channel IDs. Verify each gateway reports online.
2. Choose real provider/model profiles, enter credentials, then activate the bots. Post a topic as The Boss. Inspect each activation's request, output/silence and delivery; confirm independent cooldowns and thread-specific history.
3. Ask Hortator for status and a model-assisted incident summary. Confirm `!status`, `!stop all`, `!start all`, prompt/model changes, and `!dm` operate under the owner's identity. Attempt a command from another human account and verify it is ignored with no configuration change or model call.
4. Force compaction through the Context panel, then inspect the before/after summaries and source boundaries. Swap a bot's model profile and confirm its personality, memory and tool grants remain intact.
5. Exercise a controlled failure with a temporary profile or credential, inspect its attribution and notification, then restore it and verify recovery. The stop control cannot recall a message already accepted by Discord; uncertain acceptance must remain visible in the outbox.
6. Enable the optional search/image/TTS plugins with their own credentials and endpoint options, then verify their provider-specific responses and Discord attachments. Keep their external usage separate from chat-model costs.

These live steps are provided for external acceptance; they have not been represented as completed tests.
