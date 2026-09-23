# Slash command assistant

The optional `slash_commands` plugin lets an ordinary council bot such as Loki serve both its existing assigned-room conversations and owner-invoked `/prompt` requests. It is globally disabled on installation and requires an explicit per-bot capability grant. Hortator's director identity and intake scope are not changed by this plugin; create a separate council bot for the companion assistant.

This is an **ingress capability**, not a model-callable tool. It adds no tool schema to a provider request. The shared plugin inventory marks that distinction. Disabling the plugin rejects further invocations immediately and removes only its own registered command when the bot's gateway is connected. Discord may briefly display a cached command; that does not bypass the runtime grant.

## Set up Loki

1. Create Loki's separate Discord application. Keep its token in the dashboard's existing per-bot credential field. Configure its model, persona, ordinary room assignments and tool grants as for any council member.
2. In **Discord Developer Portal → Installation**, enable **Guild Install** and **User Install**. Both are required by this plugin's command registration.
3. Choose **Discord Provided Link**. Under **Default Install Settings**, use `applications.commands` for User Install; use `applications.commands` and `bot` with the ordinary required bot permissions for Guild Install.
4. Open the provided installation link and choose **Add to my apps** for personal invocation. Also install the bot into the server for ordinary assigned-room chat.
5. Leave **Interactions Endpoint URL** unset for this implementation: interactions arrive through the existing authenticated Discord gateway. The dashboard remains local; no public dashboard endpoint or OAuth redirect callback is required.
6. Enable **Plugins → Slash command assistant**, then grant it under **Bots → Loki → Capabilities**. Enable Loki when ready. Successful registration produces `discord.slash_registered` in the event ledger and console.

Discord separates application installation contexts from command invocation contexts. This plugin registers the global command for user/server installation and guild channels, bot DMs and other private/group conversations. Visibility and public response permissions still depend on Discord's settings; installation does not grant channel history access. See [Discord's user-installable app guide](https://docs.discord.com/developers/tutorials/developing-a-user-installable-app).

The command remains **owner-only**, checked against the genuine Discord user ID `1482143139828596916`, even if another user installs the app or can see the command. A prompt, display name or selected room cannot grant that authority.

The connected backend reconciles registration every 60 seconds. Unchanged commands require only a read, with no registration event. Discord may omit an optional parameter's `required: false` default; this is equivalent to the saved definition, not drift. `discord.slash_registered` means the backend created or updated its owned command, **not** that someone invoked it. Actual invocation emits `turn.started` with `trigger: slash_prompt`.

## Invocation

```text
/prompt text:Search for the latest release notes and summarize them
/prompt text:Create a small HTML checklist for this project private:true
```

| Parameter | Meaning |
| --- | --- |
| `text` | Required prompt, 1–6,000 characters. Surrounding channel messages are not implicitly read. |
| `private` | Optional boolean, default `false`. The answer is public in the invocation's channel. Set `true` for a response visible only to the invoking owner. |

The owner requested public answers by default on 2026-09-13 for sharing news and searches in their channels. No extra command or saved preference is needed; `private:true` overrides visibility for that invocation. This changes response visibility only, never who can invoke the assistant. Registration refreshes the option's help text after deployment; an already-running invocation retains its original visibility.

Discord permits at most 6,000 characters for a string command option. Options have strict validation; malformed/stale input receives all detected option errors together with usage. [Application command option limits](https://docs.discord.com/developers/interactions/application-commands#application-command-object-application-command-option-structure).

Every invocation starts a **fresh conversation** from the prompt, configured persona/shared prompts, and granted memory layers. It does not import earlier slash answers or messages from that Discord channel. The ordinary `memory` plugin uses a separate private `slash:<bot-id>:<channel-id>` scope, also shared by that channel's slash workspaces. This does not overlap with ordinary council-room notes. The optional global-memory plugin supplies the bot's own cross-channel notebook, including original source-channel attribution. Other bots' private/global notes remain inaccessible.

Granted search, fetching, document creation, sandbox workspaces and other ordinary tools run through the existing tool loop. Their schemas, complete argument feedback, per-bot budgets, provider configuration, reasoning capture, pricing and measured footer behavior are reused. The plugin grants no director capabilities, arbitrary channel-history reading, or unrestricted cross-channel sending. Existing attachment import tools cannot fetch unspecified surrounding messages through the private slash scope. This first `/prompt` accepts text; attachment command options and follow-up conversation sessions are not implemented.

The result is ordinary assistant text, not a council reply tool. Longer answers use the same Markdown preview/full-response attachment behavior as channel chat. Prepared artifacts can accompany the answer. Discord mentions are suppressed. Intentional silence is still controlled by the bot's existing capability and produces a concise explicit silence receipt for the invoking owner.

## Time, cancellation and diagnostics

The handler defers the interaction before starting model work. Discord requires an initial response within 3 seconds, and interaction tokens remain usable for 15 minutes. [Interaction response timing](https://docs.discord.com/developers/interactions/receiving-and-responding#responding-to-an-interaction).

### Acknowledgement retries and recovery

The local acknowledgement cutoff is **2.9 seconds from interaction creation**,
replacing the former one-shot 2.5-second wait. Earlier gateway/handler delay is
included; retries do not get another three seconds. Up to **thirty deferral calls**
are allowed for quick connection/timeout/server failures, with 90 ms between
attempts while time remains. Thirty is a ceiling, not a promise of thirty calls:
HTTP latency and the remaining shared deadline can stop retries earlier. The
owner confirmed this later merged tuning on 2026-09-23, superseding the earlier
five-attempt/25-ms policy. discord.py also manages its own HTTP retries and
rate limits inside this outer deadline; application attempt counts are not a
count of every wire request. Authentication/permission/expired-interaction and
local programming errors are not blindly retried.

A missing acknowledgement response does not establish that Discord rejected it.
After an uncertain timeout/connection failure, or Discord's **40060** already-
acknowledged response, the runtime makes up to **three read-only lookups** of the
original response (five seconds per lookup, 250 ms between retries). It continues
only if Discord returns the expected deferred/loading placeholder with matching
visibility and any supplied application/interaction identity. It does not post
another message, change private/public visibility, overwrite a completed answer,
or infer acceptance from a local exception. Receipt reads may follow the initial
three-second window because they only check an acknowledgement already accepted
by Discord. Cancellation and the existing 14-minute invocation deadline still
apply. If acceptance cannot be confirmed, no inference or tool work starts; the
owner must invoke `/prompt` again. Interaction claims prevent duplicate work.

These initial-response retries are separate from **provider retries**, which
already apply to ordinary turns, slash generation and compaction. A gateway
reconnect does not itself rerun or abandon a provider/tool loop. Uncertain final
answer delivery retains its existing outbox policy; this change does not replay
completed model work or automatically resend an uncertain final answer.

Console/dashboard events identify each stage: `discord.slash_received`,
`discord.slash_ack_retry`, `discord.slash_acknowledged`, `discord.slash_ack_checking`,
`discord.slash_ack_receipt_retry`, `discord.slash_ack_recovered` and final failure
or cancellation. Evidence includes bot/channel/interaction identity, remaining
acknowledgement time, actual HTTP status/Discord code and bounded exception causes
when supplied. Local timeouts are labelled `local_deadline`; expired initial
windows are `platform_deadline`. Gateway state/heartbeat are context, not proof
that a reconnect caused a REST failure. Interaction tokens remain excluded at
every depth. Final answer/status-notice failures also name their Discord REST
operation and available HTTP/error-code evidence.

### Task lifetime

These are separate clocks, not interchangeable retry delays:

| Setting | What it limits |
| --- | --- |
| `ACK_SECONDS = 2.9` | The entire initial acknowledgement window, including time already elapsed since creation; Discord requires acknowledgement within three seconds. |
| `ACK_RETRY_DELAY = 0.090` | 90 ms between quick transient acknowledgement failures, with at most thirty calls inside the same window. It does not cancel a pending call every 90 ms. |
| `ACK_RECEIPT_SECONDS = 5` | Each read-only lookup of a possibly accepted acknowledgement; separately limited to three lookups. |
| `asyncio.timeout(30)` in `SlashEngine.deliver` | Final delivery of the already-generated answer/files through `edit_original_response`, including uploads, HTTP waiting and library retries. It is not an inference timeout. |
| `MAX_SECONDS = 14 * 60` | The whole slash invocation, including acknowledgement, provider/tool work and answer delivery; reserves a minute before Discord's 15-minute token expiry. |
| `asyncio.timeout(10)` in `notice` | A final status/cancellation/failure notice. |

The shortest applicable deadline wins. Expiry of the final-delivery timeout leaves
the outbox uncertain because Discord may already have accepted the edit. Saved
work/evidence are retained; the runtime does not blindly send another answer.

The local slash deadline is **14 minutes from interaction creation**, reserving time for a final status notice. The bot's ordinary document/workspace settings may allow 7,200 seconds; those do **not** extend the slash platform deadline. A shorter configured provider deadline still applies. At expiry the runtime cancels provider/tool work, preserves saved notes/files and request evidence, and reports the local cause. It does not queue an invisible continuation or automatically restart the request. Long-lived background slash jobs are not implemented.

Slash and ordinary turns share one active slot per bot and the existing global concurrency/hourly/cost/circuit checks. A busy bot receives no second generation; the deferred response explains that the owner should retry when it is free. `!stop loki` through Hortator or the dashboard's normal stop control cancels its slash work as well. Snapshot maintenance refuses new ingress and joins active work before manipulating state.

The existing Trajectory view shows the `slash_prompt` turn, provider requests, original tools/results and private provider reasoning. A synthetic channel identifier distinguishes invocation state from ordinary Discord transcripts. Slash interaction IDs are durable idempotence keys; replayed gateway events cannot incur another model turn. Failed/cancelled invocations are never selected by the autonomous scheduler. Raw interaction tokens are kept only in memory and never enter database rows, request bodies, ordinary diagnostics or backups.

Failure receipts fit Discord's message limit even when a provider returns a long error. The receipt preserves its turn trace ID and points to Trajectory for the complete retained error; folding the notice does not discard diagnostic evidence.

A successful interaction edit records its Discord message ID and outbox receipt. If acceptance is uncertain, the outbox is `unknown`; the runtime does not send another message or claim delivery. After a process restart, incomplete invocation claims become `interrupted` because their transient response tokens cannot be recovered. Historical evidence remains inspectable.

## Isolation and removal

`hortator/slash_commands.py` owns registration, invocation records, isolated context construction and interaction delivery. Narrow hooks in `discord_gateway.py` dispatch through the application's `BackgroundTasks` owner. The implementation reuses `Engine.run_turn` through a specialized executor; it does not replace the ordinary scheduler or gateway message handler.

Registration uses the Discord API's individual command create/update/delete operations through the pinned discord.py HTTP client. It never bulk-overwrites the app's command tree. Existing unowned `/prompt` commands cause a polite registration refusal instead of being overwritten. The recorded bot/application/command IDs remain the authority for command deletion. Removing this stage's hooks and registry entry leaves ordinary bots, shared tools and retained history intact; keep the stage's local commit as the code rollback boundary.

The implementation and mocked tests do not establish that a newly configured Discord application has been installed or successfully invoked. Finish portal setup and use `/prompt` for live acceptance after deployment.

## Owner decisions (preserved from AGENTS, 2026-09-23)

The date marks relocation of standing instructions, not a new product decision.
Existing decision dates and qualifications below remain authoritative.

- Slash acknowledgement recovery uses at most thirty transient deferral calls with 90 ms gaps within one 2.9-second window from interaction creation, never fresh deadlines. Retry gaps do not shorten a pending request's timeout. Uncertain acceptance or Discord 40060 is reconciled by at most three read-only original-response lookups; require the expected loading placeholder and matching visibility/identity before starting model work. Do not replay a completed answer, manufacture acceptance or persist interaction tokens. Keep acknowledgement, gateway reconnect and inference retry diagnostics distinct; see SLASH_COMMANDS for eligibility and timing.

- The keyless `slash_commands` plugin is owner-only ingress for council companions, not a model tool. Preserve global/per-bot opt-in, ordinary room behavior, shared active-bot/concurrency/budget gates, exact registered command ownership and fresh isolated slash contexts. Personal installation does not grant ambient channel history or director tools. Defer promptly and cancel/join at the 14-minute local deadline; Discord tokens expire after 15 minutes and must stay in memory, never logs/database/provider evidence. The owner requested public slash replies by default (2026-09-13), with explicit `private:true` per invocation; preserve exact-owner access, mention suppression, explicit silence/failure receipts and uncertain delivery semantics. Disable/unregister only owned commands; never bulk overwrite another app command tree. See [docs/SLASH_COMMANDS.md](SLASH_COMMANDS.md).
