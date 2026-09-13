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

## Invocation

```text
/prompt text:Search for the latest release notes and summarize them
/prompt text:Create a small HTML checklist for this project private:false
```

| Parameter | Meaning |
| --- | --- |
| `text` | Required prompt, 1–6,000 characters. Surrounding channel messages are not implicitly read. |
| `private` | Optional boolean, default `true`. The interaction response is visible only to the invoking owner. `false` requests a public response in that interaction's channel. |

Discord permits at most 6,000 characters for a string command option. Options have strict validation; malformed/stale input receives all detected option errors together with usage. [Application command option limits](https://docs.discord.com/developers/interactions/application-commands#application-command-object-application-command-option-structure).

Every invocation starts a **fresh conversation** from the prompt, configured persona/shared prompts, and granted memory layers. It does not import earlier slash answers or messages from that Discord channel. The ordinary `memory` plugin uses a separate private `slash:<bot-id>:<channel-id>` scope, also shared by that channel's slash workspaces. This does not overlap with ordinary council-room notes. The optional global-memory plugin supplies the bot's own cross-channel notebook, including original source-channel attribution. Other bots' private/global notes remain inaccessible.

Granted search, fetching, document creation, sandbox workspaces and other ordinary tools run through the existing tool loop. Their schemas, complete argument feedback, per-bot budgets, provider configuration, reasoning capture, pricing and measured footer behavior are reused. The plugin grants no director capabilities, arbitrary channel-history reading, or unrestricted cross-channel sending. Existing attachment import tools cannot fetch unspecified surrounding messages through the private slash scope. This first `/prompt` accepts text; attachment command options and follow-up conversation sessions are not implemented.

The result is ordinary assistant text, not a council reply tool. Longer answers use the same Markdown preview/full-response attachment behavior as channel chat. Prepared artifacts can accompany the answer. Discord mentions are suppressed. Intentional silence is still controlled by the bot's existing capability and produces a concise explicit silence receipt for the invoking owner.

## Time, cancellation and diagnostics

The handler defers the interaction before starting model work. Discord requires an initial response within 3 seconds, and interaction tokens remain usable for 15 minutes. [Interaction response timing](https://docs.discord.com/developers/interactions/receiving-and-responding#responding-to-an-interaction).

The local slash deadline is **14 minutes from interaction creation**, reserving time for a final status notice. The bot's ordinary document/workspace settings may allow 7,200 seconds; those do **not** extend the slash platform deadline. A shorter configured provider deadline still applies. At expiry the runtime cancels provider/tool work, preserves saved notes/files and request evidence, and reports the local cause. It does not queue an invisible continuation or automatically restart the request. Long-lived background slash jobs are not implemented.

Slash and ordinary turns share one active slot per bot and the existing global concurrency/hourly/cost/circuit checks. A busy bot receives no second generation; the deferred response explains that the owner should retry when it is free. `!stop loki` through Hortator or the dashboard's normal stop control cancels its slash work as well. Snapshot maintenance refuses new ingress and joins active work before manipulating state.

The existing Trajectory view shows the `slash_prompt` turn, provider requests, original tools/results and private provider reasoning. A synthetic channel identifier distinguishes invocation state from ordinary Discord transcripts. Slash interaction IDs are durable idempotence keys; replayed gateway events cannot incur another model turn. Failed/cancelled invocations are never selected by the autonomous scheduler. Raw interaction tokens are kept only in memory and never enter database rows, request bodies, ordinary diagnostics or backups.

Failure receipts fit Discord's message limit even when a provider returns a long error. The receipt preserves its turn trace ID and points to Trajectory for the complete retained error; folding the notice does not discard diagnostic evidence.

A successful interaction edit records its Discord message ID and outbox receipt. If acceptance is uncertain, the outbox is `unknown`; the runtime does not send another message or claim delivery. After a process restart, incomplete invocation claims become `interrupted` because their transient response tokens cannot be recovered. Historical evidence remains inspectable.

## Isolation and removal

`hortator/slash_commands.py` owns registration, invocation records, isolated context construction and interaction delivery. Narrow hooks in `discord_gateway.py` dispatch through the application's `BackgroundTasks` owner. The implementation reuses `Engine.run_turn` through a specialized executor; it does not replace the ordinary scheduler or gateway message handler.

Registration uses the Discord API's individual command create/update/delete operations through the pinned discord.py HTTP client. It never bulk-overwrites the app's command tree. Existing unowned `/prompt` commands cause a polite registration refusal instead of being overwritten. The recorded bot/application/command IDs remain the authority for command deletion. Removing this stage's hooks and registry entry leaves ordinary bots, shared tools and retained history intact; keep the stage's local commit as the code rollback boundary.

The implementation and mocked tests do not establish that a newly configured Discord application has been installed or successfully invoked. Finish portal setup and use `/prompt` for live acceptance after deployment.
