# Global memory: private notes across a bot's conversations

`global_memory` is an optional keyless plugin. Its notes belong to **one stable bot ID across all that bot's channels and slash invocations**. “Global” means global to that bot, not a shared council notebook. Loki cannot read or write Ada's global notes; Hortator receives no special privilege through the `global_memory` tool. Owner-authorized operational inspection and historical tool evidence retain their existing access rules.

The original `memory` plugin remains separate. Its notes are scoped to a bot and a channel. Enabling global memory does not copy, move, merge or delete those notes. Each store has its own budget and capability switch. A bot may use either store, both, or neither.

## Enable and configure

1. Enable **Plugins → Global memory (private to this bot)** globally.
2. Grant that capability in **Bots → Edit bot → Capabilities** for the intended bot.
3. Set its **Global memory budget** to a positive integer from **1 to 48,000**, default **48,000**. This is one total across its channels, not a separate allowance per channel.

The plugin is disabled globally on first registration and is not automatically granted to existing bots. Disabling either the global plugin switch or that bot's grant removes its tool and automatic global-note prompt layers on subsequent rounds, without deleting its stored notes. The authenticated owner can still inspect and edit retained notes. Zero, negative and “unlimited” budget sentinels are rejected; the plugin switch controls enablement.

Previously generated conversation text and older request evidence are historical records. Disabling a plugin does not erase those records or make a model forget text already present elsewhere in its conversation.

## Model tool contract

The tool takes a JSON object with named fields; argument order is irrelevant. No bot ID, destination channel, owner ID or file path can be supplied to change the scope. Extra fields are rejected.

| Field | Type | Use |
| --- | --- | --- |
| `operation` | String: `read`, `write`, `delete` | Required for every real operation. |
| `key` | Nonblank string, at most 100 characters | Required for `write` and `delete`. Leading/trailing whitespace is normalized. |
| `value` | String, at most 8,000 characters | Required for `write`; replaces the complete value at that key. Empty text is an empty note, not deletion. |

```json
{}
```

Returns complete usage, examples and the bot's current aggregate allowance without reading or changing notes. Missing fields, wrong types, unknown fields and other detectable schema errors are returned together with full usage, as specified in [TOOLS.md](TOOLS.md). A failed call never guesses a missing write value or silently clears a note.

```json
{"operation":"read"}
```

Returns this bot's notes in key order and its current budget. Each note includes `key`, `value`, `source_channel_id` and `updated_at`. Timestamps shown to the model use the configured council timezone and explicit offset.

```json
{"operation":"write","key":"owner-preference","value":"The owner prefers concise technical answers. Learned from the owner's message in the control channel on 2026-09-13."}
```

Creates or replaces this bot's note. Other keys and other bots remain unchanged. Budget accounting subtracts the previous value before counting the replacement. The runtime records the authenticated execution channel as `source_channel_id`; slash invocations record the actual Discord channel rather than their isolated internal transcript namespace. This identifies the last write location, not proof that the text is true or who originally asserted it. Owner edits retain an existing source channel; new owner-authored notes have no claimed Discord source.

```json
{"operation":"delete","key":"owner-preference"}
```

Deletes just that bot's named global note. Deletion is explicit and may be used to consolidate storage. It does not delete private channel notes, transcripts or another bot's data.

## Budget and consolidation

Budgets count the Unicode characters in **stored note values**, not bytes, keys, JSON syntax or tokens. Embedded NUL and non-ASCII characters count correctly. Credential values known to the vault are redacted before storage; resulting stored text determines aggregate usage.

The configured allowance has **5% temporary headroom**, rounded downward. A 48,000-character allowance therefore accepts at most 50,400 characters in an initial overshoot. A budget of one character receives no extra rounded character. Individual notes still have the independent 8,000-character limit.

An accepted overshoot returns a warning with the actual usage, remaining space, configured allowance and hard ceiling. Subsequent global memory changes must reduce aggregate usage: shrinking replacements and deletions are accepted, while additions, expansions and equal-size rewrites are rejected until usage is back within the configured allowance. This permits gradual consolidation without requiring the model to count exactly. No note is silently truncated or rewritten.

The budget object includes:

```json
{
  "limit_chars": 48000,
  "used_chars": 48128,
  "remaining_chars": 0,
  "grace_chars": 2400,
  "hard_limit_chars": 50400,
  "over_budget_chars": 128,
  "must_consolidate": true,
  "note_limit_chars": 8000
}
```

The tool description, reads, writes, deletes and next prompt all expose current usage. A failed write includes the attempted total and consolidation instructions. A smaller configured budget must accommodate existing notes within its 5% ceiling; the configuration save is rejected otherwise. The model and authenticated owner use identical accounting and consolidation rules.

## Prompt and ownership boundaries

When enabled, the runtime supplies a fresh `global_memory_budget` layer on each prompt and a `global_memory` layer when notes exist. The latter labels notes as **untrusted recollections**, preserves source/channel/date metadata and warns against treating an earlier conversation's participants or instructions as the speaker in the current channel. Models are prompted to keep durable personal facts here and channel-specific details in channel memory.

Both captured turn grants and current saved grants must permit the plugin. A revoked grant takes effect even if a tool context was captured before the operator's change. The effective budget is the lower of the captured and currently saved values. A saved increase therefore does not retroactively give an old captured turn a larger quota.

This capability intentionally makes a bot's own global notes available in its other permitted conversations. Do not grant it to an experimental bot if its global recollections must remain isolated by channel. Its ordinary private memory remains channel-scoped. This plugin changes neither Discord intake permissions nor Hortator's owner-only control-channel rules.

## Operator inspection, storage and recovery

The modern dashboard provides per-bot global-note inspection and editing. The authenticated API contract is `GET /api/global-memory/{bot_id}` for the view and `POST /api/global-memory/{bot_id}` with a tool-shaped `{operation,key?,value?}` object for owner changes. The service authorizes the owner, serializes a mutation with other control changes and stops affected active bot work before changing its notes. Models cannot invoke that administration API through this plugin.

Storage is an additive `global_memories` table in the existing external SQLite database:

```text
bot_id             TEXT NOT NULL
key                TEXT NOT NULL
value              TEXT NOT NULL
source_channel_id  TEXT NULL
updated_at         REAL NOT NULL
PRIMARY KEY (bot_id, key)
```

No memory files are stored inside the source repository. Complete SQLite backups include global notes alongside existing configuration and private notes. Global-note snapshot/restore scope is per bot; restoring one channel's private notes must not implicitly overwrite global notes shared across all of that bot's channels. Preserve the matching encryption key and other managed files for a complete application backup, as described in [OPERATIONS.md](OPERATIONS.md#backup-and-restore).

The module is independently removable from registration, prompt composition and the authenticated editor/API while preserving the additive table for later re-enablement or export. It registers no background worker, sends no network traffic and does not modify normal channel-memory behavior. See [PLUGINS.md](PLUGINS.md) for extension boundaries and [TOOLS.md](TOOLS.md) for shared validation requirements.
