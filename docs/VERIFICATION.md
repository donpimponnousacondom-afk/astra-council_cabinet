# Verification record

Verified in this workspace on 2026-09-07. Provider and Discord tests use controlled transports; no Discord tokens, provider keys, or paid requests were supplied.

During the standalone-repository migration on the same date, dependencies were restored and the production build, 57 backend tests on Python 3.12 and all 5 browser tests were rerun successfully from `/home/codexy/codex/astra-council_cabinet` inside the shared Screen session. Ruff, Prettier, documentation links and the migrated production API/assets/login/logout checks also passed. Python 3.14 results below refer to the earlier implementation check. The local pre-push guard was checked against feature-branch updates, pushes to main and deletion of main without contacting a remote. See [SESSION_HANDOVER.md](SESSION_HANDOVER.md) for repository/runtime continuity.

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
