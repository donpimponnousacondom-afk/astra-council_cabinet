# Interactive Discord panels

`discord_panel` is an optional, keyless model tool, independently enabled globally and per bot. It prepares a native Discord Components V2 card around the bot's next ordinary assistant answer. Buttons and a single-choice dropdown start actual tasks, with normal provider requests, tools, retries and trajectories. No HTML, JavaScript, executable callbacks or invented telemetry are accepted.

The default accent is gold (`#D6A447`). The bot can choose another six-digit accent and Discord's primary/secondary/success/danger button styles. Discord controls the layout; this is not an embedded webpage. Text, files and controls are explicitly represented as V2 components. Mentions remain suppressed. Ordinary messages without a prepared panel keep their existing formatting.

## Enable and use

Enable **Plugins → Interactive Discord panels**, then **Bots → bot → Capabilities → Interactive Discord panels**. No new secret or OAuth scope is needed for message components; the application must already be able to respond/send in the destination. `/prompt` and image generation remain separate grants. A modern-editor preview explains the behavior; it is not a live Discord control.

Ask, for example:

> Create a gold Loki panel with three real buttons: search the latest AI news, explain your last answer, and create an HTML briefing. Give each action a clear prompt. Use discord_panel to prepare it and answer normally.

The initial access policy is exact-owner only. The bot can prepare a panel during ordinary conversation or a slash task, but only the configured human owner can activate its controls. Public panels produce public responses; panels prepared in private interactions keep their subsequent responses private. Each click responds separately; it does not modify the original panel or resume its original model context. The bot may prepare another panel in that new answer.

## Model contract

An empty `{}` returns complete usage without acting. All detectable schema errors are returned together. Preparation is a tool action, never a substitute for the final assistant text answer.

```json
{
  "operation": "prepare",
  "title": "👑 Loki's desk",
  "description": "Choose a task. I will run it and reply here.",
  "accent_color": "#D6A447",
  "buttons": [
    {"label": "Search the news", "style": "primary", "prompt": "Search current AI news and give me a short sourced briefing."},
    {"label": "Create a briefing", "style": "success", "prompt": "Create a new HTML briefing using document_site and report its actual publication status."}
  ],
  "select_placeholder": "Choose an explanation…",
  "select_options": [
    {"label": "Explain the workflow", "prompt": "Explain how your tools produce and publish a document."}
  ],
  "expires_hours": 168
}
```

| Operation | Required parameters | Behavior |
| --- | --- | --- |
| `prepare` | `title`, `buttons` and/or `select_options` | Stage one panel for this turn's final answer; a subsequent preparation replaces the pending one. |
| `cancel` | none | Remove this turn's pending presentation; no Discord post is deleted. |
| `list` | none | Return the latest 100 owned definitions with title, status and destination. |
| `status` | `panel_id` | Inspect an owned panel. |
| `disable` | `panel_id` | Revoke its callbacks. The old message stays on Discord; later clicks receive a disabled receipt. |

Bounds: title 120 characters, description 500; up to ten buttons in two rows and twelve dropdown options. Each action has a label up to 80 characters and a prompt up to 1,500 characters. Only one dropdown selection is accepted. Expiry is 1–720 hours, default seven days; at most 100 unexpired active panels per bot. Models cannot supply custom IDs, URLs as callbacks, arbitrary component trees, other bots' identifiers or interaction tokens.

## Execution and persistence

1. Preparation stores a bot/application/turn/channel-owned definition in SQLite's `discord_panels` table. Its callback prompt stays local; Discord receives an opaque panel/action ID.
2. The existing outbox sends the completed assistant answer, optional prepared files and the component view. The panel becomes active only after a confirmed message ID. Outbox routing records `panel_id`.
3. The gateway receives a component interaction. It checks the owner, current grants, application, exact stored message and channel, action index and expiry. Hortator additionally retains its current owner/control-channel/DM scope. No message text or display name can establish identity.
4. The shared slash/interaction admission path acknowledges promptly, rechecks permission after the await, then reserves the same per-bot active slot and council/provider budget gates. Concurrent clicks receive a busy receipt; duplicate interaction IDs never repeat model work.
5. A fresh `panel:<bot>:<channel>` context contains the selected saved action and original panel answer. It has no ambient history or previous interaction transcript. Private channel notes/workspaces use this panel scope; global notes remain that bot's cross-channel notebook. The editable `panel_invocation` prompt layer explains this boundary.
6. The ordinary reasoning/tool loop runs. Provider retries retry only the failed request. Stop/configuration changes cancel and join it. The entire interaction has the existing 14-minute local deadline, including acknowledgement time. Nothing automatically resumes at expiry.
7. The bot edits its new interaction response with a normal answer or another prepared panel. Uncertain delivery is recorded without resending.

Persistent definitions need no in-memory view registration after restart: the gateway resolves custom IDs against SQLite. Interaction tokens stay in memory, never in definitions, events, requests or snapshots. Full application snapshots include the table; selective memory/context restoration does not roll back panels. Restoring a snapshot cannot undo existing Discord messages. A panel absent from the restored database receives a truthful rejection.

Inspection uses ordinary Trajectory/Events, including `panel_action` turn triggers and `discord.panel_prepared`, `discord.panel_active`, acknowledgement, rejection, disable and failure events. The shared invocation ledger retains owner/channel/request identity; its historical table name remains `slash_invocations`. Runtime/API ownership and ordinary bot scheduling are unchanged.

## Attachments and document exports

Attachments are not a plugin grant. Long answers still automatically include `full-response.txt`; this also works inside panels. `discord_attach` is offered once the current bot turn has a generated/exported artifact, prepares owned files for the final response, and does not itself post.

- **Workspace** is an optional plugin with an `export` operation; it is not necessary for document-site exports.
- **Documents & local sites** now supports `export` with `site`, `path` and optional immutable `revision`. The saved file is integrity-checked, copied into a bot/turn-owned artifact and returned for `discord_attach`. The existing 8 MB artifact limit applies. No publication or file edit is performed.
- An **artifact** is the internal handle for a generated/exported file, not an additional plugin. Image generation is one possible producer and remains independently optional.

```json
{"operation":"export","site":"briefing","path":"index.html","revision":3}
```

Then call `discord_attach` with the returned handle:

```json
{"artifact_ids":["art_returned_by_export"]}
```

Finish with ordinary text. An export is a single file, not a bundled site: relative CSS/image references still require those assets or the published URL. Existing publication ownership, immutable revisions and automatic SSH synchronization are unchanged.

## Boundaries and follow-ups

This version supports buttons and single-choice dropdowns, not modal forms, arbitrary web UI or editing an already-sent panel in place. Disabling a panel/grant rejects interactions server-side; it does not delete or repaint old Discord messages. No bot is granted workspace, shell or image generation merely to enable panels. New installations leave this plugin disabled.

References: [Discord component reference](https://docs.discord.com/developers/components/reference), [Components V2 usage](https://docs.discord.com/developers/components/using-message-components), [interaction lifecycle](https://docs.discord.com/developers/interactions/receiving-and-responding). The implementation requires discord.py 2.7+ within the existing major-version bound.
