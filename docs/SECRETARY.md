# Secretary · experimental

The optional keyless `secretary` plugin keeps a durable reminder ledger. It is
off by default: enable it globally under **Plugins → Secretary**, then grant it
to each bot under **Capabilities**. Any eligible bot can use it; its normal
conversational profile answers alarms. No model/provider is prescribed.

## Alarms and delivery

Ask the bot to remind you at a time, snooze a task, or repeat an agreed reminder.
It saves the alarm, acknowledges the returned time and finishes its turn.
Waiting creates no worker, provider request or typing indicator. Due alarms
enter the existing scheduler and produce a **new ordinary bot turn** in their
original conversation. They never edit an older message or post directly from
a timer. Normal Discord mention policy applies; no arbitrary destination or
mass-mention permission is granted.

An alarm bypasses routine cadence and personal send cooldown, including a
zero activation interval. Council/bot/provider pauses, channel grants, gateway
readiness, recovery waits, circuits, hourly/daily budgets, concurrency and room
send gaps still apply. Directed human input and background completions take
priority. A paused bot waits until resumed; overdue reminders can be late.

Occurrence and turn are claimed in one SQLite transaction before inference.
Consumed occurrences are not replayed after restart, inference/delivery failure,
cancellation or intentional silence. The ledger exposes the last turn outcome;
Snooze rearms a fired reminder. This is not guaranteed delivery.

Recurring alarms use fixed **seconds**, not calendar rules. Their next due time
becomes claim time plus the interval. Missed repeats coalesce into one wake
after downtime. A 24-hour interval can shift local wall-clock hour at daylight
saving changes. Snooze moves the next due time and preserves the interval unless
changed. Cancel stops future occurrences; a turn already started may still post.

## Tool operations

`{}` returns usage only. Normal tool budgets, validation and durable evidence
apply. Model access is scoped to its bot and current channel. Scheduling needs
an observed context within current grants; Hortator retains owner-only intake.

| Operation | Fields |
| --- | --- |
| `schedule` | `key`, `message`, exactly one of `at` / `after_seconds`; optional `repeat_seconds` |
| `list` | This conversation's ledger; optional `offset` and `limit` (default 20, maximum 50) |
| `status` | `reminder_id` |
| `snooze` | `reminder_id`, exactly one of `at` / `after_seconds`; optional `repeat_seconds` |
| `update` | `reminder_id`, `message` and/or `repeat_seconds`; does not rearm inactive reminders |
| `cancel` | `reminder_id` |

`at` must be a future ISO 8601 timestamp with explicit UTC offset. The bot uses
the council runtime clock/timezone; naive timestamps are rejected.
`after_seconds` accepts 1–315,360,000. Messages allow 1–4,000 characters.
`repeat_seconds: 0` means one-off (default); positive values must meet the
configured minimum and cannot exceed 31,536,000 seconds.

`key` is a caller-selected identifier unique per bot/channel. Repeating a
schedule key with the same message/interval recovers the saved alarm and its
actual date without duplicating or rearming it, even if another date is supplied.
Use Snooze to move it. Different content under an existing key is rejected.
Use the `reminder_id` from receipts/list for later actions; never guess IDs.
List pages contain bounded message previews; `status` returns full reminder text.

```json
{"operation":"schedule","key":"usage-review","message":"Remind root to review usage","after_seconds":3600}
```

For an explicitly requested hourly reminder, add `"repeat_seconds":3600`.
Postpone with `{"operation":"snooze","reminder_id":"<saved ID>","after_seconds":7200}`;
finish with `{"operation":"cancel","reminder_id":"<saved ID>"}`.

## Operator controls, persistence and limits

**Plugins → Secretary** exposes global settings and the ledger. **Bots →
Control → Secretary reminders** shows a granted bot's ledger. Refresh shows
current state and last turn outcome. **Edit reminder** changes the message and
repeat interval (zero makes it one-off); **Save reminder** applies that edit
without moving the due time or rearming inactive entries. **Snooze** moves the
time/rearms an entry, and **Cancel reminder** stops future occurrences. These
owner actions need no bot approval and remain available in the global ledger
when the plugin or a bot's grant is disabled. Cancelled entries stay visible
until retention cleanup; cancellation does not delete historical evidence.

Changes use revision protection: if the bot or scheduler changes the reminder
first, the save fails visibly and keeps your draft. Discard it and refresh to
inspect the current version. Save/discard configuration drafts before changing
reminders; reminder drafts have their own save button and navigation protection.
Channel labels use internal room names where available. Per-bot non-secret plugin
JSON may override limits.

Defaults: `max_active_reminders: 100` per bot across channels (1–10,000),
`min_repeat_seconds: 300` (60–31,536,000). Inactive entries free active capacity
immediately and expire after 30 days during plugin access/eligible scheduling.
Active reminders never expire; historical trajectories retain their own policy.

Owner API: `GET /api/secretary[?bot_id=...]`; `POST /api/secretary/{id}` accepts
`revision` plus `cancel`, `snooze` or `update` fields above, without `reminder_id`.
Session/origin/CSRF protections apply; stale/missing revisions return 409.
Owner cancellation remains available with the plugin disabled.

The editable `scheduled_alarm` prompt placement uses `{alarm}` for its receipt.
Saved messages are task data, not new human authority. Disabling that guidance
does not bypass admission or quotas. Alarms live in SQLite `secretary_reminders`;
full snapshots include them automatically. Selective notes/context restoration
does not restore alarms. **Clean slate cancels scheduled alarms in its selected
scope**, and pre-cutoff alarms cannot be snoozed. Bot deletion also cancels its
alarms. Other bots and memories remain untouched. Disabling grants defers alarms
until restored; cancel them explicitly to retire them.

`secretary.changed` records operation, ID, channel, time, interval and actor.
`scheduled_alarm.claimed` records the committed wake receipt without message
text and associates its turn. Ordinary request/delivery/turn events supply the
outcome. No existing log field ordering changes.

## Owner decisions (2026-09-25)

Implement an experimental secretary for any granted bot, initially for testing
with Loki: reminders, snoozes, reprogramming and periodic alarms. Use normal bot
replies when due, without waiting inference/typing or model-specific behavior.
Preserve current workflows/grants. Live enablement and natural-language
acceptance remain operator actions.

The operator must be able to inspect, edit, snooze and cancel saved reminders
directly from the dashboard, independently of the bot's willingness to do so.
