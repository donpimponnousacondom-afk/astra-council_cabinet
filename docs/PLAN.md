# Implementation and acceptance plan

This implementation now lives at `/home/codexy/codex/astra-council_cabinet`. The [session handover](SESSION_HANDOVER.md) records continuity, the shared GNU Screen runtime and the feature-branch-only workflow; the design below remains the accepted foundation.

The product is a single-host council, not a wrapper around one shared agent. Every bot is a distinct Discord application. Production runs one Uvicorn worker containing supervised discord.py clients, an asynchronous scheduler, provider HTTP clients, tool registry, control API and built React assets. SQLite WAL persists configuration, scoped memory, transcripts, requests, trajectories, metrics, credentials (encrypted), and an outbox. Multiple API workers are explicitly unsupported and prevented by a process lock.

## Grounding (researched 2026-09-07)

- Hermes Agent, commit `693641aa8b4359c602283bdbbc14041e03bc47bc`: inspected `plugins/platforms/discord/adapter.py`, `recovery.py`, and the Discord guide. Adopt thread/DM namespaces, disabled mass mentions, first-message replies, separate gateway health, and a supervised connection lifecycle. Implement our own small connector rather than vendor Hermes' full agent runtime. Unlike Hermes' usual personal-assistant routing, known council bots are valid conversation participants. Deduplicate identical messages observed by several clients.
- DeepSeek Harness, commit `d347e703908d0406b7a7ef80e3a0e594d86b2215`: inspected the trajectory inspection ledger, conversation-context assembly decisions, and snapshot builder. Adopt stable turn/request/tool/compaction IDs, exact effective request snapshots, causal event ordering, usage/timing inspection, and tail-first history paging. This is an original implementation inspired by those structures, not an assertion of wire compatibility with DSH.
- Discord's OAuth2 documentation: a user must create/authorize each application and obtain its token through the Developer Portal. Do not automate a user account or claim that the application can mint bot tokens. Generate least-privilege invitations and validate the token's application identity.
- OpenRouter's API documentation: retain arbitrary nested request options and provider usage fields. No mandatory vendor SDK translation layer.

Sources: [Hermes adapter](https://github.com/NousResearch/hermes-agent/blob/693641aa8b4359c602283bdbbc14041e03bc47bc/plugins/platforms/discord/adapter.py), [Hermes guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/discord), [DSH ledger](https://github.com/deepseek-ai/deepseek-harness/blob/d347e703908d0406b7a7ef80e3a0e594d86b2215/.agents/notes/implemented/feature/2026-07-27-trajectory-inspection-ledger.md), [DSH assembly](https://github.com/deepseek-ai/deepseek-harness/blob/d347e703908d0406b7a7ef80e3a0e594d86b2215/.agents/notes/implemented/architecture/2026-08-11-trajectory-conversation-context-assembly.md), [Discord OAuth2](https://docs.discord.com/developers/topics/oauth2), [OpenRouter API](https://openrouter.ai/docs/api/reference/overview).

## Work packages and acceptance

All six work packages below are implemented. Local acceptance results and the remaining external connection checks are recorded in [VERIFICATION.md](VERIFICATION.md).

1. **Storage and control:** typed configuration with references; reusable model profiles and cloning; encrypted secrets; audited owner controls; password/session/CSRF dashboard authentication. Validate duplicate application IDs, references, secret redaction and exact owner identity.
2. **Provider and plugins:** streamed/non-streamed OpenAI-compatible HTTP, arbitrary model JSON, timeouts, concurrency limits, usage/cost/TTFT, failure attribution/circuit recovery. Global + bot grants for fetch, search, image, TTS, scoped memory and Hortator inspection. Validate fragmented SSE, tool-call assembly, unsupported/missing usage and denied tools.
3. **Council runtime:** per-bot activation and delivery cooldowns, one turn per bot, room send serialization, deliberate silence/replies, durable outbox, immediate cancellation, persistent scoped sessions, bounded model-driven compaction and context reconstruction. Validate overlapping ticks, queued messages, provider stops, restart recovery and context boundaries.
4. **Discord and Hortator:** client per application, token/application identity check, configured guild/channel/thread routing, owner DMs, deterministic command parity with API, operational incident/recovery reporting, invitation wizard. Test owner spoofing and command routing without contacting Discord.
5. **Dashboard:** responsive operational overview, bot/profile/provider/prompt/plugin/room editors, raw advanced JSON, trajectory inspector and export, context/compaction inspector, statistics, alert/audit history, command reference, onboarding and secrets controls. Verify real API-backed browser workflows.
6. **Packaging and handoff:** reproducible lockfiles, Docker image/compose, local run commands, backup/restore and security/operations documentation. Run meaningful tests, lint, typecheck, production build and browser checks. Live Discord/provider acceptance needs user-entered credentials and guild/channel IDs; do not fabricate external verification.

## Invariants

- Only Discord user ID `1482143139828596916` is authorized to control or converse with Hortator. Display names, server ownership, role permissions, webhook authors and model-generated text grant no authority. Owner ID is a code constant, not a mutable setting.
- Models may inspect operational data through a bounded read-only tool; deterministic commands perform mutations. Every control entry point shares authorization and validation. Secrets may only be entered via the authenticated web API/dashboard, never Discord/model tools.
- Bot personality/tool grants survive profile changes. Credentials can be shared at provider level or overridden per bot; resource/memory ownership is always explicit.
- Each bot's activation timer is independent. A model may choose silence. One room delivery at a time, with a configurable room gap and a minimum per-bot send interval across rooms. No unbounded catch-up loop.
- Context is bot + Discord channel/thread/DM scoped. Every provider call records the actual redacted body, prompt layers, message IDs, model/profile revision, usage, timing and outcome. Summaries never erase historical records.
- Provider reasoning fields are excluded from Discord. Raw reasoning is not persisted; only reported reasoning-token counts and requested reasoning configuration are retained. Explicit reasoning delimiters in visible text are removed; semantic leakage inside ordinary prose cannot be mechanically guaranteed.
- Pausing cancels in-flight model/tool tasks and suppresses queued outputs. A Discord send already accepted remotely cannot be undone. Ambiguous delivery after a crash/network interruption is marked unknown and is never blindly resent.
- Usage is measured only when returned by the provider. Estimates and unknown values are labeled; missing cost or reasoning tokens never become invented measurements.
