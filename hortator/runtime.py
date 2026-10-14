from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import defaultdict
from dataclasses import dataclass

from aiohttp import ClientError

from .action_receipts import ActionReceipts
from .answer_validation import wrapped_reply
from .concurrency import cancel_and_wait, join_tasks, task_group, error_text
from .context import ContextBuilder
from .addressing import human_directed_elsewhere
from .footer import render_footer
from .diagnostics import reasoning_settings
from .models import ControlError, OWNER_ID
from .plugins import ATTACH, END_CYCLE, SILENCE, TERMINAL_COMPATIBLE_WRITES, ToolContext
from .provider import strip_reasoning
from .response_recovery import queue_failed_response, read_note
from .store import dumps, uid
from .tool_feedback import feedback, parse_arguments, syntax_feedback, usage
from .working_set import DEFAULT_WORKING_SET_CHARS, bound_exchanges, prompt_exchanges


REPLY_REPAIR = (
    "Your previous answer was withheld because it used a tool wrapper instead of an answer. "
    "council_speak does not exist. Write only the final Discord message as ordinary assistant "
    "content, without JSON/XML/function wrappers or a description of your tool decision. "
    "Use actual tool_calls only for available actions or terminal decisions."
)


@dataclass
class DeliveryError(Exception):
    message: str
    uncertain: bool = False

    def __str__(self):
        return self.message


class TurnSuperseded(ControlError):
    pass


class ProgressUnavailable(DeliveryError):
    """An optional message has no immediate delivery slot; keep doing the work."""


class CycleIncomplete(ControlError):
    """An end_cycle activation stopped without publishing an answer."""


PROGRESS_SEND_TIMEOUT = 5
# Consecutive incomplete cycles in one channel after which public notices pause
# until that bot publishes or chooses silence there again.
INCOMPLETE_NOTICE_STREAK = 3


class Engine:
    def __init__(self, store, vault, pool, registry, transport=None, *, reporting=None):
        self.store, self.vault, self.pool, self.registry = store, vault, pool, registry
        self.reporting = reporting
        self.action_receipts = ActionReceipts(store, vault.redact)
        self.contexts = ContextBuilder(
            store,
            pool,
            registry.global_memory,
            registry.application_emojis,
            registry.engrams,
            registry.filesystems,
        )
        self.transport = transport
        self.tasks: dict[str, asyncio.Task] = {}
        self.room_locks = defaultdict(asyncio.Lock)
        self.room_last = defaultdict(float)
        self.task = None
        self.closed = False
        self.delivery_waiting = {}
        self.human_superseded = set()
        self.incomplete_streaks = defaultdict(int)
        self.resetting = set()
        self.single_shots = {}
        self.jobs = None

    def start(self):
        self.task = self.background.spawn(self.loop(), name="council-scheduler")

    def configuration(self, bot):
        profile = self.store.get("profiles", bot["model_profile_id"])
        provider = self.store.get("providers", profile["provider_id"]) if profile else None
        return profile, provider

    def available(self, bot, *, manual=False):
        settings = self.store.get("settings", "global")
        profile, provider = self.configuration(bot)
        return bool(
            settings["enabled"]
            and bot["id"] not in self.resetting
            and (bot["enabled"] or manual)
            and profile
            and provider
            and provider["enabled"]
        )

    def valid(self, bot, profile, provider, *, manual=False):
        current = self.store.get("bots", bot["id"])
        return bool(
            current
            and self.available(current, manual=manual or self.is_single_shot(bot))
            and current["revision"] == bot["revision"]
            and self.store.get("profiles", profile["id"])["revision"] == profile["revision"]
            and self.store.get("providers", provider["id"])["revision"] == provider["revision"]
        )

    def room_for(self, bot, channel_id):
        channel = self.store.one("SELECT * FROM channels WHERE id=?", (channel_id,))
        if not channel:
            return None
        return next(
            (
                room
                for room in self.store.list("rooms")
                if room["id"] in bot["room_ids"]
                and room["guild_id"] == channel["guild_id"]
                and (
                    room["channel_id"] == channel_id
                    or room["include_threads"]
                    and room["channel_id"] == channel["parent_id"]
                )
            ),
            None,
        )

    def channel_allowed(self, bot, channel_id):
        if not self.store.one(
            "SELECT 1 FROM contexts WHERE bot_id=? AND channel_id=?", (bot["id"], channel_id)
        ):
            return False
        if self.store.is_owner_dm(bot, channel_id) and self.registry.owner_dm_enabled(bot):
            return True
        if bot["role"] != "hortator":
            return self.room_for(bot, channel_id) is not None
        row = self.store.one(
            "SELECT room_id FROM messages WHERE channel_id=? ORDER BY seq DESC LIMIT 1", (channel_id,)
        )
        channel = self.store.one("SELECT * FROM channels WHERE id=?", (channel_id,))
        if not row or row["room_id"] != f"owner:{bot['id']}" or not channel:
            return False
        if not channel["guild_id"]:
            return True  # Owner DMs are admitted only by the connector's exact-identity check.
        settings = self.store.get("settings", "global")
        return channel["guild_id"] == settings["control_guild_id"] and (
            channel_id == settings["control_channel_id"]
            or channel["parent_id"] == settings["control_channel_id"]
        )

    def is_single_shot(self, bot):
        return bool(
            bot.get("_single_shot_turn") and self.single_shots.get(bot["id"]) == bot["_single_shot_turn"]
        )

    def pick_channel(self, bot, *, force=False):
        contexts = self.store.rows(
            "SELECT * FROM contexts WHERE bot_id=? ORDER BY updated_at ASC", (bot["id"],)
        )
        candidates = []
        for context in contexts:
            channel_id = context["channel_id"]
            if not self.channel_allowed(bot, channel_id):
                continue
            latest = (
                self.store.one("SELECT max(seq) AS seq FROM messages WHERE channel_id=?", (channel_id,))[
                    "seq"
                ]
                or 0
            )
            unseen = any(
                self.store.after_context_reset(bot["id"], row)
                and not human_directed_elsewhere(self.store, row, bot["id"])
                for row in self.store.rows(
                    "SELECT * FROM messages WHERE channel_id=? AND seq>? AND deleted=0 AND (bot_id IS NULL OR bot_id<>?)",
                    (channel_id, context["last_seen"], bot["id"]),
                )
            )
            if bot["role"] == "hortator":
                unseen = any(
                    self.store.after_context_reset(bot["id"], row)
                    for row in self.store.rows(
                        "SELECT * FROM messages WHERE channel_id=? AND seq>? AND author_id=?",
                        (channel_id, context["last_seen"], OWNER_ID),
                    )
                )
            if (
                not force
                and not unseen
                and (
                    not bot["evaluate_when_idle"]
                    or bot["role"] == "hortator"
                    or self.store.is_owner_dm(bot, channel_id)
                )
            ):
                continue
            boundary = self.store.context_boundary(bot["id"], channel_id)
            if boundary and not self.store.one(
                "SELECT 1 FROM messages WHERE channel_id=? AND seq>? AND at>? LIMIT 1",
                (channel_id, boundary["after_seq"], boundary["after_at"]),
            ):
                continue
            if latest:
                candidates.append((not unseen, context["updated_at"], channel_id))
        return min(candidates)[2] if candidates else None

    def manual_channel(self, bot):
        """Newest eligible human conversation, independent of scheduler fairness."""
        candidates = []
        for context in self.store.rows("SELECT channel_id FROM contexts WHERE bot_id=?", (bot["id"],)):
            channel_id = context["channel_id"]
            if not self.channel_allowed(bot, channel_id):
                continue
            boundary = self.store.context_boundary(bot["id"], channel_id)
            # Read metadata only; late backfill must not look newer than live conversation.
            row = self.store.one(
                "SELECT seq,at FROM messages "
                "WHERE channel_id=? AND deleted=0 AND json_extract(addressing,'$.author_kind')='human' "
                "AND seq>? AND at>? AND (?=0 OR author_id=?) ORDER BY at DESC,seq DESC LIMIT 1",
                (
                    channel_id,
                    boundary["after_seq"] if boundary else 0,
                    boundary["after_at"] if boundary else 0,
                    bot["role"] == "hortator",
                    OWNER_ID,
                ),
            )
            if row:
                candidates.append((row["at"], row["seq"], channel_id))
        return max(candidates)[2] if candidates else None

    def attention(self, bot, channel_id=None, through=None):
        rows = self.store.rows(
            "SELECT m.* FROM messages m JOIN contexts c ON c.channel_id=m.channel_id AND c.bot_id=? "
            "WHERE m.seq>c.last_seen AND m.deleted=0 AND json_extract(m.addressing,'$.author_kind')='human' "
            "AND json_extract(m.addressing,'$.live')=1 "
            "AND EXISTS (SELECT 1 FROM json_each(m.addressing,'$.targets') t WHERE json_extract(t.value,'$.bot_id')=?) "
            "AND NOT EXISTS (SELECT 1 FROM human_attention_claims h WHERE h.bot_id=? AND h.message_id=m.discord_id) "
            "AND (? IS NULL OR m.channel_id=?) AND (? IS NULL OR m.seq<=?) ORDER BY m.seq DESC",
            (bot["id"], bot["id"], bot["id"], channel_id, channel_id, through, through),
        )
        return [
            row
            for row in rows
            if self.channel_allowed(bot, row["channel_id"])
            and self.store.after_context_reset(bot["id"], row)
            and (bot["role"] != "hortator" or row["author_id"] == OWNER_ID)
        ]

    def claim_attention(self, bot, channel_id, turn_id, *, through=None):
        pending = self.attention(bot, channel_id, through)
        # Keep the human selected at launch through asynchronous preparation.
        # Otherwise take the oldest waiting human, coalescing only their input.
        activation = bot.get("activation")
        author_id = activation["author_id"] if activation else pending[-1]["author_id"] if pending else None
        pending = [row for row in pending if row["author_id"] == author_id]
        for row in pending:
            self.store.execute(
                "INSERT OR IGNORE INTO human_attention_claims VALUES(?,?,?,?)",
                (bot["id"], row["discord_id"], turn_id, time.time()),
            )
        if not pending:
            return None
        row = pending[0]
        target = next(t for t in json.loads(row["addressing"])["targets"] if t.get("bot_id") == bot["id"])
        activation = {
            "kind": "human_reply"
            if "reply" in target["via"]
            else "owner_message"
            if "dm" in target["via"]
            else "human_mention",
            "message_id": row["discord_id"],
            "channel_id": channel_id,
            "author_id": row["author_id"],
            "cooldown_bypassed": True,
        }
        self.store.emit(
            "activation.human_directed",
            {**activation, "coalesced_messages": len(pending)},
            bot_id=bot["id"],
            turn_id=turn_id,
        )
        return activation

    def superseding_attention(self, bot, channel_id, turn_id):
        """Only the current human may replace their own unsent directed answer."""
        pending = self.attention(bot, channel_id)
        claimed = self.store.one(
            "SELECT m.author_id,m.channel_id FROM human_attention_claims h "
            "JOIN messages m ON m.discord_id=h.message_id "
            "WHERE h.bot_id=? AND h.turn_id=? ORDER BY m.seq DESC LIMIT 1",
            (bot["id"], turn_id),
        )
        # Routine drafts still yield to human input. Once a human owns a turn,
        # another human (or another conversation) waits for its completion.
        return [
            row
            for row in pending
            if not claimed
            or (row["author_id"] == claimed["author_id"] and row["channel_id"] == claimed["channel_id"])
        ]

    def stop_superseded_work(self, bot, channel_id, turn_id, boundary, *, remaining_calls=0):
        """Yield at safe boundaries; never interrupt an already-started action."""
        if bot.get("invocation") or bot.get("response_protocol", "narrate") == "end_cycle":
            return
        pending = self.superseding_attention(bot, channel_id, turn_id)
        if not pending:
            return
        self.store.emit(
            "turn.superseded",
            {
                "channel_id": channel_id,
                "boundary": boundary,
                "replacement_message_ids": [row["discord_id"] for row in pending],
                "unstarted_calls": remaining_calls,
                "reason": "New directed input is waiting; completed actions are retained, no further work starts",
            },
            bot_id=bot["id"],
            turn_id=turn_id,
        )
        raise TurnSuperseded("New directed human input is waiting; stop before starting more work")

    def budget_error(self, bot):
        now = time.time()
        n = self.store.one(
            "SELECT count(*) AS n FROM turns WHERE bot_id=? AND started_at>?", (bot["id"], now - 3600)
        )["n"]
        if n >= bot["hourly_turn_limit"]:
            return "Hourly activation limit reached"
        if bot["daily_cost_limit"] is not None:
            day = now - now % 86400
            cost = self.store.one(
                "SELECT coalesce(sum(cost),0) AS cost, sum(CASE WHEN status='completed' AND cost IS NULL THEN 1 ELSE 0 END) AS unknown FROM requests WHERE bot_id=? AND started_at>=?",
                (bot["id"], day),
            )
            if cost["cost"] >= bot["daily_cost_limit"]:
                return "UTC daily cost limit reached"
            if cost["unknown"]:
                return "Cost limit cannot be enforced: provider omitted costs and profile prices are missing"
        return None

    async def tick(self):
        settings = self.store.get("settings", "global")
        if not settings["enabled"]:
            return
        now = time.time()
        bots = self.store.list("bots")
        pending = {bot["id"]: self.attention(bot) for bot in bots if self.available(bot)}
        # A human's intended recipient gets a free scheduler slot before routine chatter.
        for bot in sorted(bots, key=lambda b: not bool(pending.get(b["id"]))):
            attention = pending.get(bot["id"], [])
            if (
                attention
                and bot["id"] in self.tasks
                and bot["id"] in self.delivery_waiting
                and self.superseding_attention(bot, None, self.delivery_waiting[bot["id"]])
            ):
                self.human_superseded.add(self.delivery_waiting[bot["id"]])
                self.tasks[bot["id"]].cancel()
                continue
            if bot["id"] in self.tasks or len(self.tasks) >= settings["max_concurrent_turns"]:
                continue
            if not self.available(bot):
                continue
            completed_job = self.jobs.pending(bot) if self.jobs and not attention else None
            scheduled_wake = self.registry.pending_wake(bot) if not attention and not completed_job else None
            # Human attention, settled jobs and due plugin alarms can wake a
            # timer-off bot. Ordinary chatter still cannot.
            # Do not change pick_channel:
            # owner-requested compaction also uses it to select a context.
            if bot["interval_seconds"] == 0 and not attention and not completed_job and not scheduled_wake:
                continue
            state = self.store.runtime(bot["id"])
            if (
                state["gateway_status"] != "online"
                or state["retry_until"] > now
                or (state["next_at"] > now and not attention and not completed_job and not scheduled_wake)
            ):
                continue
            profile, provider = self.configuration(bot)
            health = self.store.health(provider["id"])
            if health["circuit_until"] > now:
                continue
            channel_id = (
                attention[-1]["channel_id"]
                if attention
                else completed_job["channel_id"]
                if completed_job
                else scheduled_wake["channel_id"]
                if scheduled_wake
                else self.pick_channel(bot)
            )
            if not channel_id:
                continue
            error = self.budget_error(bot)
            if error:
                self.store.execute(
                    "UPDATE bot_runtime SET next_at=?,retry_until=?,error=? WHERE bot_id=?",
                    (now + 60, now + 60, error, bot["id"]),
                )
                self.store.emit(
                    "activation.budget_blocked",
                    {"error": error, "reason": "No turn started; budget will be checked again in 60 seconds"},
                    bot_id=bot["id"],
                    level="warning",
                )
                continue
            self.launch(
                bot,
                channel_id,
                human_directed=bool(attention),
                completed_job=completed_job,
                scheduled_wake=scheduled_wake,
            )

    async def loop(self):
        while not self.closed:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.emit(
                    "runtime.scheduler_error",
                    {"error": error_text(exc), "reason": "Scheduler tick failed; next check in 0.5 seconds"},
                    level="error",
                )
            await asyncio.sleep(0.5)

    def trigger(self, bot, channel_id=None):
        """Owner-requested one-shot admission; no configuration toggles or queued replay."""
        settings = self.store.get("settings", "global")
        if self.closed or not self.available(bot, manual=True):
            raise ControlError("Council or provider is paused, or the bot is being reset", 409)
        if bot["id"] in self.tasks:
            raise ControlError("Bot already has an active turn; Trigger was not queued", 409)
        if len(self.tasks) >= settings["max_concurrent_turns"]:
            raise ControlError("Council turn slots are full; Trigger was not queued", 409)
        now = time.time()
        profile, provider = self.configuration(bot)
        retry_at = max(
            self.store.runtime(bot["id"])["retry_until"],
            self.store.health(provider["id"])["circuit_until"],
        )
        if retry_at > now:
            raise ControlError(
                f"Provider/bot recovery wait is active for {retry_at - now:.1f}s; try later", 409
            )
        error = self.budget_error(bot)
        if error:
            raise ControlError(error, 409)
        channel_id = channel_id or self.manual_channel(bot)
        if not channel_id:
            raise ControlError(
                "No eligible conversation with human activity; let the bot observe a human message "
                "in an allowed room, or specify an eligible channel explicitly"
            )
        return self.launch(bot, channel_id, single_shot=True, human_directed=True)

    def launch(
        self,
        bot,
        channel_id,
        *,
        compact_only=False,
        human_directed=False,
        single_shot=False,
        completed_job=None,
        scheduled_wake=None,
    ):
        if bot["id"] in self.tasks:
            raise ControlError("Bot already has an active turn", 409)
        if not self.available(bot, manual=compact_only or single_shot):
            raise ControlError("Council, bot or provider is paused")
        if not self.channel_allowed(bot, channel_id):
            raise ControlError("Channel is outside this bot's configured scope")
        turn_id = uid("turn_")
        if completed_job or scheduled_wake:
            self.store.execute("BEGIN IMMEDIATE")
        try:
            if scheduled_wake:
                if (
                    scheduled_wake["candidate"]["bot_id"] != bot["id"]
                    or scheduled_wake["channel_id"] != channel_id
                ):
                    raise ControlError("Scheduled wake scope mismatch")
                bot = {**bot, "scheduled_wake": self.registry.claim_wake(scheduled_wake, turn_id)}
            if completed_job:
                self.jobs.check(completed_job)
                if not self.jobs.claim(completed_job, turn_id):
                    raise ControlError("Background completion was already consumed", 409)
                bot = {**bot, "background_completion": self.jobs.notification(completed_job)}
            if single_shot:
                bot = {**bot, "_single_shot_turn": turn_id}
            profile, provider = self.configuration(bot)
            activation = self.claim_attention(bot, channel_id, turn_id) if human_directed else None
            if activation:
                bot = {**bot, "activation": activation}
            self.store.execute(
                "INSERT INTO turns(id,bot_id,channel_id,profile_id,provider_id,model,started_at,status,trigger,revision) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    turn_id,
                    bot["id"],
                    channel_id,
                    profile["id"],
                    provider["id"],
                    profile["model"],
                    time.time(),
                    "running",
                    "manual_compaction"
                    if compact_only
                    else "manual_trigger"
                    if single_shot
                    else activation["kind"]
                    if activation
                    else "background_completion"
                    if completed_job
                    else "scheduled_alarm"
                    if scheduled_wake
                    else "owner_message"
                    if bot["role"] == "hortator"
                    else "timer",
                    bot["revision"],
                ),
            )
            if completed_job or scheduled_wake:
                self.store.execute("COMMIT")
        except BaseException:
            if completed_job or scheduled_wake:
                self.store.execute("ROLLBACK")
            raise
        if scheduled_wake:
            notice = bot["scheduled_wake"]
            self.store.emit(
                "scheduled_alarm.claimed",
                {
                    **{key: value for key, value in notice.items() if key != "message"},
                    "channel_id": channel_id,
                },
                bot_id=bot["id"],
                turn_id=turn_id,
            )
        if completed_job:
            notice = bot["background_completion"]
            self.jobs.emit(
                completed_job,
                "notification_claimed",
                continuation_turn_id=turn_id,
                job_count=len(notice["jobs"]),
                **notice["progress"],
            )
        self.store.execute(
            "UPDATE bot_runtime SET next_at=? WHERE bot_id=?",
            (time.time() + bot["interval_seconds"], bot["id"]),
        )
        self.store.emit(
            "turn.started",
            {
                "reasoning": reasoning_settings(profile["request_json"], self.vault.redact),
                "profile_revision": profile["revision"],
                "channel_id": channel_id,
                "profile_id": profile["id"],
                "provider_id": provider["id"],
                "model": profile["model"],
                **(
                    {"trigger": "manual_trigger", "return_to_paused": not bot["enabled"]}
                    if single_shot
                    else {}
                ),
                **(
                    {"trigger": "background_completion", "job_id": completed_job["id"]}
                    if completed_job
                    else {}
                ),
                **(
                    {"trigger": "scheduled_alarm", "plugin": scheduled_wake["plugin"]}
                    if scheduled_wake
                    else {}
                ),
            },
            bot_id=bot["id"],
            turn_id=turn_id,
        )
        task = self.background.spawn(
            self.run(bot, profile, provider, channel_id, turn_id, compact_only),
            name=f"bot:{bot['id']}:{turn_id}",
            bot_id=bot["id"],
            turn_id=turn_id,
        )
        self.tasks[bot["id"]] = task
        if single_shot:
            self.single_shots[bot["id"]] = turn_id

        def finished(completed):
            if self.tasks.get(bot["id"]) is completed:
                self.tasks.pop(bot["id"], None)
            if self.single_shots.get(bot["id"]) == turn_id:
                self.single_shots.pop(bot["id"], None)
            # Any turn can be cancelled before entering its finalizer. Do not
            # leave durable job handoffs/notifications waiting on a ghost turn.
            state = self.store.one("SELECT status FROM turns WHERE id=?", (turn_id,))
            if state and state["status"] == "running":
                status = "cancelled" if completed.cancelled() else "failed"
                error = "Turn stopped before normal turn completion"
                self.store.execute(
                    "UPDATE turns SET status=?,error=?,ended_at=? WHERE id=?",
                    (status, error, time.time(), turn_id),
                )
                self.store.emit(
                    "turn.ended", {"status": status, "error": error}, bot_id=bot["id"], turn_id=turn_id
                )
            if self.jobs:
                self.jobs.release_turn(turn_id)

        task.add_done_callback(finished)
        return turn_id

    async def show_typing(self, bot, channel_id, turn_id):
        try:
            await self.transport.typing(bot, channel_id, turn_id=turn_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Presence is best effort: a failed indicator must not fail model work or delivery.
            self.store.emit(
                "discord.typing_failed",
                {
                    "channel_id": channel_id,
                    "error": error_text(exc),
                    "reason": "Typing task stopped; the bot turn continues without its indicator",
                },
                bot_id=bot["id"],
                turn_id=turn_id,
                level="warning",
            )

    async def run(self, bot, profile, provider, channel_id, turn_id, compact_only):
        async with task_group() as group:
            await self.run_turn(bot, profile, provider, channel_id, turn_id, compact_only, group)

    def plan_round_tools(self, context, silence_tools, round_index, round_limit, *, handoff=False):
        """Plan one round's schemas without granting extra calls or attachment access."""
        available = (
            []
            if handoff
            else silence_tools + (self.registry.schemas(context) if round_index < round_limit else [])
        )
        can_attach = bool(
            not handoff
            and round_index < round_limit
            and self.store.one(
                "SELECT id FROM artifacts WHERE bot_id=? AND turn_id=? LIMIT 1",
                (context.bot["id"], context.turn_id),
            )
        )
        if can_attach:
            available.append(ATTACH)
        return available, can_attach

    async def finalize_turn(
        self, bot, provider, turn_id, typing_task, *, status, decision, error, retry_delay
    ):
        """Join typing before recording the outcome, releasing workers and restarting cadence."""
        self.human_superseded.discard(turn_id)
        if typing_task:
            await cancel_and_wait(typing_task)
        self.store.execute(
            "UPDATE turns SET status=?,decision=?,error=?,ended_at=? WHERE id=?",
            (status, decision, error, time.time(), turn_id),
        )
        queue_failed_response(self.store, turn_id)
        if self.jobs:
            self.jobs.release_turn(turn_id)
        # No catch-up bursts; activation interval restarts when work settles.
        current = self.store.get("bots", bot["id"])
        interval = current["interval_seconds"] if current else bot["interval_seconds"]
        self.store.execute(
            "UPDATE bot_runtime SET next_at=?,retry_until=? WHERE bot_id=?",
            (
                time.time() + max(interval, retry_delay),
                time.time() + retry_delay if retry_delay else 0,
                bot["id"],
            ),
        )
        self.store.emit(
            "turn.ended",
            {"status": status, "decision": decision, "error": error, "provider_id": provider["id"]},
            bot_id=bot["id"],
            turn_id=turn_id,
        )

    async def run_turn(self, bot, profile, provider, channel_id, turn_id, compact_only, group):
        status, decision, error = "failed", None, None
        retry_delay = 0
        typing_task = None
        engram_candidate = None
        ordinary_delivery_id = None
        response_protocol = bot.get("response_protocol", "narrate")
        cycle_mode = response_protocol == "end_cycle"
        record_actions = not cycle_mode and not bot.get("invocation")
        receipt_offer = None
        receipts_in_last_request = None
        cycle = {"reason": None, "actions": 0, "draft_retained": False, "finalization": False}
        round_index = 0
        generation_rounds = 0
        strikes = 0
        reply_to = None
        context = ToolContext(bot, channel_id, turn_id, owner_verified=bot["role"] == "hortator")
        try:
            if self.is_single_shot(bot):
                await self.transport.prepare_single_shot(bot)
                if not self.valid(bot, profile, provider) or not self.channel_allowed(bot, channel_id):
                    raise asyncio.CancelledError()
            if getattr(self.transport, "typing", None):
                typing_task = group.create_task(
                    self.show_typing(bot, channel_id, turn_id), name=f"typing:{bot['id']}:{turn_id}"
                )
                # Start presence before context preparation, including compaction and provider queues.
                await asyncio.sleep(0)
            silence_tools = [SILENCE] if bot.get("allow_silence", True) else []
            terminal_tools = silence_tools + ([END_CYCLE] if cycle_mode else [])
            tools = terminal_tools + self.registry.schemas(context)
            rows, summary, through, original_meta = await self.contexts.prepare(
                bot, profile, channel_id, turn_id, tools, force=compact_only
            )
            if compact_only:
                status, decision = "completed", "compacted"
                cycle["reason"] = "compacted"
                return
            engram = original_meta.get("_engram_snapshot")
            activation = self.claim_attention(bot, channel_id, turn_id, through=through) or bot.get(
                "activation"
            )
            if activation:
                bot = {**bot, "activation": activation}
                if not self.is_single_shot(bot):
                    self.store.execute("UPDATE turns SET trigger=? WHERE id=?", (activation["kind"], turn_id))
            extras = []
            round_index, round_limit = 0, bot["max_tool_rounds"]
            calls_limit = bot["max_calls_per_round"]
            document_started = False
            document_deadline = None
            task_extension_used = False
            task_plugin = None
            task_seconds = None
            reply_to, artifact_ids = activation["message_id"] if activation else None, []
            if not reply_to and bot.get("background_completion"):
                origin_job = self.jobs.get(bot["background_completion"]["job_id"]) if self.jobs else None
                reply_to = origin_job["reply_to"] if origin_job else None
            reply_repairs = 0
            background_handoff = False
            if response_protocol not in ("narrate", "progress", "end_cycle"):
                raise ControlError("Unknown response protocol; select narrate, progress or end_cycle")
            progress_limit = bot.get("max_progress_messages_per_turn", 1)
            progress_attempts = 0
            progress_notices = set()
            # Finalization closes work. Actual answer drafts are immutable;
            # narration before action results is not an answer draft.
            strike_limit = bot.get("narration_strikes", 2)
            finalizing = False
            retained_draft = None
            answer_pending = None

            async def publish_final(
                content,
                final_engram,
                request_id,
                finish_reason,
                reason,
                *,
                terminal_call_id=None,
                confirmation_request_id=None,
                preceding_end_request_id=None,
                source_meta=None,
            ):
                nonlocal engram_candidate, ordinary_delivery_id, status, decision
                publication_meta = source_meta if source_meta is not None else meta
                complete_final = finish_reason == "stop" or (
                    terminal_call_id is not None and finish_reason == "tool_calls"
                )
                if final_engram and final_engram.get("state") is not None and complete_final:
                    # Coverage describes only the actual represented
                    # transcript/summary, never the room's current tail.
                    represented = set(publication_meta["message_sequences"])
                    full_rows = all(row["seq"] in represented for row in rows)
                    stored_context = self.store.context(bot["id"], channel_id)
                    summary_included = not summary or publication_meta["summary"] == summary
                    if full_rows and summary_included:
                        covered = max(
                            engram["covered_through"],
                            stored_context["checkpoint"] if summary else 0,
                            max(represented, default=0),
                        )
                        engram_candidate = self.registry.engrams.stage(
                            engram,
                            final_engram,
                            request_id,
                            covered_through=covered,
                            summary_checkpoint=stored_context["checkpoint"],
                            summary_hash=hashlib.sha256(stored_context["summary"].encode()).hexdigest(),
                            terminal_call_id=terminal_call_id,
                            confirmation_request_id=confirmation_request_id,
                            preceding_end_request_id=preceding_end_request_id,
                        )
                    else:
                        self.store.emit(
                            "engram.update_skipped",
                            {
                                "channel_id": channel_id,
                                "reason": "incomplete_input_coverage",
                                "coverage_unchanged": True,
                            },
                            bot_id=bot["id"],
                            turn_id=turn_id,
                            request_id=request_id,
                            level="warning",
                        )
                elif final_engram and final_engram.get("state") is not None:
                    self.store.emit(
                        "engram.update_skipped",
                        {
                            "channel_id": channel_id,
                            "reason": "incomplete_final_boundary",
                            "finish_reason": finish_reason,
                            "coverage_unchanged": True,
                        },
                        bot_id=bot["id"],
                        turn_id=turn_id,
                        request_id=request_id,
                        level="warning",
                    )
                ordinary_delivery_id = uid("out_")
                if (
                    engram is not None
                    and final_engram is not None
                    and final_engram.get("invalid")
                    and complete_final
                ):
                    self.registry.engrams.record_miss(
                        engram, request_id, ordinary_delivery_id, final_engram["invalid"]
                    )
                delivery_cancelled = None
                try:
                    await self.deliver(
                        bot,
                        profile,
                        provider,
                        context,
                        content,
                        reply_to,
                        artifact_ids,
                        request_id=request_id,
                        draft_deadline=document_deadline,
                        delivery_id=ordinary_delivery_id,
                    )
                except asyncio.CancelledError as exc:
                    delivery_cancelled = exc
                    raise
                finally:
                    try:
                        # Text can be confirmed before a following voice send is
                        # cancelled. Preserve its finish provenance without replay.
                        if self.store.one(
                            "SELECT 1 FROM outbox WHERE id=? AND status='sent'", (ordinary_delivery_id,)
                        ):
                            cycle["reason"] = reason
                            if preceding_end_request_id is not None:
                                self.store.emit(
                                    "cycle.answer_published",
                                    {
                                        "channel_id": channel_id,
                                        "end_request_id": preceding_end_request_id,
                                        "end_call_id": terminal_call_id,
                                    },
                                    bot_id=bot["id"],
                                    turn_id=turn_id,
                                    request_id=request_id,
                                )
                    except Exception as exc:
                        if delivery_cancelled is not None:
                            # Diagnostic failure must not replace owner cancellation.
                            raise delivery_cancelled from exc
                        raise
                status, decision = "sent", "reply" if reply_to else "speak"
                cycle["reason"] = reason

            def freeze_draft(result, parsed):
                nonlocal retained_draft
                if retained_draft is not None or not result.content.strip() or wrapped_reply(result.content):
                    return
                if result.tool_calls and not any(
                    call["function"]["name"] == "end_cycle" for call in result.tool_calls
                ):
                    # Pre-result narration is withheld, never a candidate answer.
                    return
                retained_draft = {
                    "content": result.content,
                    "parsed_engram": parsed,
                    "request_id": result.request_id,
                    "finish_reason": result.finish_reason,
                    "meta": meta,
                }
                cycle["draft_retained"] = True
                self.store.emit(
                    "cycle.draft_retained",
                    {
                        "channel_id": channel_id,
                        "characters": len(result.content),
                        "content_sha256": hashlib.sha256(result.content.encode()).hexdigest(),
                    },
                    bot_id=bot["id"],
                    turn_id=turn_id,
                    request_id=result.request_id,
                )

            def cycle_failure(result, reason, *, unrecoverable=False, allow_first_answer=False):
                nonlocal strikes, finalizing, round_limit
                strikes += 1
                self.store.emit(
                    "cycle.protocol_failed",
                    {
                        "channel_id": channel_id,
                        "reason": reason,
                        "strikes": strikes,
                        "strike_limit": strike_limit,
                        "draft_request_id": retained_draft["request_id"] if retained_draft else None,
                    },
                    bot_id=bot["id"],
                    turn_id=turn_id,
                    request_id=result.request_id,
                    level="warning",
                )
                can_finalize = retained_draft is not None or finalizing or allow_first_answer
                if unrecoverable or not can_finalize or strikes >= strike_limit:
                    raise CycleIncomplete(
                        f"{reason}; no answer was published. "
                        + (
                            "No answer draft exists; no regeneration requested. "
                            if retained_draft is None
                            else ""
                        )
                        + f"Protocol strikes: {strikes}/{strike_limit}."
                    )
                if not finalizing:
                    finalizing = True
                    cycle["finalization"] = True
                    # The strike budget is independent of task extensions.
                    round_limit = round_index + strike_limit - strikes
                    self.store.emit(
                        "cycle.finalizing",
                        {
                            "channel_id": channel_id,
                            "strikes": strikes,
                            "strike_limit": strike_limit,
                            "draft_request_id": retained_draft["request_id"] if retained_draft else None,
                        },
                        bot_id=bot["id"],
                        turn_id=turn_id,
                        level="warning",
                    )
                extras.append(
                    {
                        "role": "user",
                        "content": "Automatic harness notice (private, not sent to Discord): "
                        f"Your answer was NOT published. {reason}. Strike {strikes} of {strike_limit}. "
                        + (
                            "Your original answer is saved as a frozen draft. Do not regenerate, repeat or replace it. "
                            "Call end_cycle with {} and no other calls to publish that saved answer unchanged. "
                            if retained_draft is not None
                            else "Your tool narration was withheld; it is not an answer draft. "
                            "Read the available tool results, then write your first final answer as ordinary "
                            "assistant text together with end_cycle {}. Do not repeat the narration. "
                        )
                        + "Action tools are removed; completed actions will not run again. "
                        + ("You may instead choose council_silence alone. " if silence_tools else "")
                        + f"{strike_limit - strikes} finalization attempt(s) remain; another failed response "
                        "uses another strike. At the limit this cycle stops without publishing.",
                    }
                )

            async def budgeted(awaitable):
                try:
                    async with asyncio.timeout_at(document_deadline):
                        return await awaitable
                except TimeoutError as exc:
                    if (
                        document_deadline is not None
                        and asyncio.get_running_loop().time() >= document_deadline
                    ):
                        raise ControlError(
                            "Extended task time budget exhausted; saved local files remain available"
                        ) from exc
                    raise

            while round_index <= round_limit:
                self.stop_superseded_work(bot, channel_id, turn_id, "round")
                if not self.valid(bot, profile, provider) or not self.channel_allowed(
                    bot, context.channel_id
                ):
                    raise asyncio.CancelledError()
                if record_actions:
                    # Pending entries are stable, but tool grants can change between rounds.
                    receipt_offer = self.action_receipts.offer(context, self.registry.allowed)
                if document_deadline is not None and asyncio.get_running_loop().time() >= document_deadline:
                    raise ControlError(
                        "Extended task time budget exhausted; saved local files remain available"
                    )
                available_tools, can_attach = self.plan_round_tools(
                    context, terminal_tools, round_index, round_limit, handoff=background_handoff
                )
                if finalizing:
                    available_tools, can_attach = terminal_tools, False
                if answer_pending is not None:
                    available_tools, can_attach = [], False
                budget_bot = {**bot, "max_tool_rounds": round_limit, "max_calls_per_round": calls_limit}
                if background_handoff:
                    budget_bot["background_handoff"] = self.jobs.handoff(context)
                if task_extension_used:
                    budget_bot["active_task_budget"] = {
                        "plugin": task_plugin,
                        "active": document_started,
                        "calls_per_round": calls_limit,
                        "seconds_remaining": max(0, document_deadline - asyncio.get_running_loop().time())
                        if document_deadline is not None
                        else None,
                        "renewable_this_turn": False,
                    }
                # Only future prompt copies are minimized. Historical requests and
                # the immutable result store continue to hold the actual evidence.
                _, base_meta = await self.contexts.assemble(
                    budget_bot,
                    profile,
                    channel_id,
                    rows,
                    summary,
                    round_index,
                    [],
                    available_tools,
                    image_plan=original_meta["image_plan"],
                    engram=engram,
                    audio_plan=original_meta.get("audio_plan"),
                )
                headroom = (
                    int(
                        (profile["context_window"] - profile["response_tokens"] - 1)
                        / original_meta["calibration_factor"]
                    )
                    - base_meta["estimated_tokens"]
                    - 128
                )
                working_limit = bot.get("tool_working_set_chars", DEFAULT_WORKING_SET_CHARS)
                extras, working_meta = await asyncio.to_thread(
                    bound_exchanges,
                    extras,
                    working_limit,
                    available_readers=tuple(tool["function"]["name"] for tool in available_tools),
                    fits_context=lambda exchanges: (
                        self.contexts.estimate_request(exchanges, []) <= max(64, headroom)
                    ),
                )
                messages, meta = await self.contexts.assemble(
                    budget_bot,
                    profile,
                    channel_id,
                    rows,
                    summary,
                    round_index,
                    prompt_exchanges(extras),
                    available_tools,
                    image_plan=original_meta["image_plan"],
                    engram=engram,
                    audio_plan=original_meta.get("audio_plan"),
                )
                meta.update(
                    checkpoint=original_meta["checkpoint"],
                    through_sequence=through,
                    calibration_factor=original_meta["calibration_factor"],
                    document_task=document_started and task_plugin == "document_site",
                    extended_task=document_started,
                    calls_per_round=calls_limit,
                    task_plugin=task_plugin,
                    tool_working_set={**working_meta, "char_limit": working_limit},
                )
                if document_deadline is not None:
                    meta["document_seconds_remaining"] = max(
                        0, document_deadline - asyncio.get_running_loop().time()
                    )
                if (
                    meta["estimated_tokens"] * original_meta["calibration_factor"]
                    + profile["response_tokens"]
                    >= profile["context_window"]
                ):
                    raise ControlError(
                        "Tool results would exceed the context budget; turn stopped without discarding history"
                    )
                recovery = None
                if (
                    round_index == 0
                    and not bot.get("invocation")
                    and self.reporting
                    and self.store.one(
                        "SELECT 1 FROM response_recovery_pending WHERE bot_id=? AND channel_id=?",
                        (bot["id"], channel_id),
                    )
                ):
                    remaining = (
                        int(
                            (profile["context_window"] - profile["response_tokens"] - 1)
                            / original_meta["calibration_factor"]
                        )
                        - meta["estimated_tokens"]
                        - 128
                    )
                    try:
                        recovery = await self.reporting.run(read_note, bot["id"], channel_id, remaining)
                    except Exception as exc:
                        self.store.emit(
                            "response_recovery.deferred",
                            {"channel_id": channel_id, "error": error_text(exc)},
                            bot_id=bot["id"],
                            turn_id=turn_id,
                            level="warning",
                        )
                    if recovery:
                        recovery = {**recovery, "message": self.vault.redact(recovery["message"])}
                        candidate = messages + [recovery["message"]]
                        estimated = await self.contexts.estimate_async(candidate, available_tools)
                        if (
                            estimated * original_meta["calibration_factor"] + profile["response_tokens"]
                            < profile["context_window"]
                        ):
                            messages = candidate
                            meta["estimated_tokens"] = estimated
                            meta["response_recovery"] = {
                                key: recovery[key]
                                for key in ("source_request_id", "excerpted", "estimated_tokens")
                            }
                        else:
                            recovery = None
                receipts_in_last_request = None
                if receipt_offer:
                    # Response-recovery evidence masking owns the final message slot.
                    candidate = (
                        messages[:-1] + [receipt_offer.message] + messages[-1:]
                        if recovery
                        else messages + [receipt_offer.message]
                    )
                    estimated = await self.contexts.estimate_async(candidate, available_tools)
                    if (
                        estimated * original_meta["calibration_factor"] + profile["response_tokens"]
                        < profile["context_window"]
                    ):
                        messages = candidate
                        meta["estimated_tokens"] = estimated
                        meta["action_receipts"] = [list(key) for key in receipt_offer.receipt_ids]
                        receipts_in_last_request = receipt_offer
                self.store.execute(
                    "UPDATE contexts SET estimated_tokens=? WHERE bot_id=? AND channel_id=?",
                    (meta["estimated_tokens"], bot["id"], channel_id),
                )
                generation_rounds += 1
                result = await budgeted(
                    self.pool.complete(
                        bot=bot,
                        profile=profile,
                        messages=messages,
                        tools=available_tools,
                        turn_id=turn_id,
                        context=meta,
                        recovery=recovery,
                        before_attempt=(
                            lambda: self.stop_superseded_work(
                                bot, channel_id, turn_id, "provider_queue_or_retry"
                            )
                        )
                        if record_actions
                        else None,
                    )
                )
                if result.tool_calls:
                    self.stop_superseded_work(
                        bot, channel_id, turn_id, "tool_batch", remaining_calls=len(result.tool_calls)
                    )
                parsed_engram = None
                ending = cycle_mode and any(
                    call["function"]["name"] == "end_cycle" for call in result.tool_calls
                )
                if engram is not None:
                    original_content = result.content
                    result.content = self.registry.engrams.sanitize(engram, original_content)
                    parsed_engram = await self.registry.engrams.parse(
                        engram,
                        original_content,
                        final=not result.tool_calls
                        or ending
                        or (cycle_mode and bool(result.content.strip())),
                    )
                if result.finish_reason in ("length", "content_filter"):
                    cap_name = (
                        "max_completion_tokens"
                        if "max_completion_tokens" in profile["request_json"]
                        else "max_tokens"
                    )
                    cap = profile["request_json"].get(cap_name, "provider default")
                    message = (
                        f"Provider stopped with {result.finish_reason}; incomplete output was withheld "
                        f"(output_tokens={result.usage.get('completion_tokens', 'unknown')}, {cap_name}={cap}). "
                        "Inspect the request's provider diagnostics for partial output and reasoning."
                    )
                    self.store.emit(
                        "request.output_withheld",
                        {"error": message, "finish_reason": result.finish_reason},
                        bot_id=bot["id"],
                        turn_id=turn_id,
                        request_id=result.request_id,
                        level="warning",
                    )
                    raise ControlError(message)
                cycle_response = cycle_mode and not background_handoff
                cycle_error = None
                if cycle_response and answer_pending is not None:
                    # end_cycle already closed all action capability. This is
                    # the first public answer, never a rewrite of a saved draft.
                    if (
                        result.tool_calls
                        or not result.content.strip()
                        or wrapped_reply(result.content)
                        or result.finish_reason != "stop"
                    ):
                        cycle_failure(
                            result,
                            "The one tool-free answer request did not return a complete plain-text answer",
                            unrecoverable=True,
                        )
                    await publish_final(
                        result.content,
                        parsed_engram,
                        result.request_id,
                        result.finish_reason,
                        "end_cycle_then_text",
                        preceding_end_request_id=answer_pending["request_id"],
                        terminal_call_id=answer_pending["call_id"],
                    )
                    break
                if cycle_response and finalizing:
                    # Confirm an existing source draft unchanged. Without a
                    # draft, a valid sole end_cycle follows normal publication
                    # below, using this response's first answer and Engram.
                    calls = result.tool_calls
                    quiet = len(calls) == 1 and calls[0]["function"]["name"] == "council_silence"
                    if quiet:
                        try:
                            quiet = (
                                bool(silence_tools)
                                and feedback(
                                    "council_silence",
                                    parse_arguments(calls[0]["function"]["arguments"]),
                                    SILENCE["function"]["parameters"],
                                    SILENCE["function"]["description"],
                                )
                                is None
                            )
                        except ValueError, TypeError:
                            quiet = False
                    if not quiet:
                        valid_confirmation = len(calls) == 1 and calls[0]["function"]["name"] == "end_cycle"
                        if valid_confirmation:
                            try:
                                valid_confirmation = parse_arguments(calls[0]["function"]["arguments"]) == {}
                            except ValueError, TypeError:
                                valid_confirmation = False
                        if valid_confirmation and retained_draft is not None:
                            source = retained_draft
                            await publish_final(
                                source["content"],
                                source["parsed_engram"],
                                source["request_id"],
                                source["finish_reason"],
                                "draft_published",
                                terminal_call_id=calls[0]["id"],
                                confirmation_request_id=result.request_id,
                                source_meta=source["meta"],
                            )
                            self.store.emit(
                                "tool.completed",
                                {
                                    "name": "end_cycle",
                                    "call_id": calls[0]["id"],
                                    "ok": True,
                                    "executed": True,
                                    "published": True,
                                    "draft": True,
                                    "draft_request_id": source["request_id"],
                                    "ignored_confirmation_characters": len(result.content),
                                },
                                bot_id=bot["id"],
                                turn_id=turn_id,
                                request_id=result.request_id,
                            )
                            break
                        if not valid_confirmation:
                            freeze_draft(result, parsed_engram)
                            error = (
                                "Only end_cycle {} can confirm the saved draft; no action or replacement text was used"
                                if retained_draft is not None
                                else "Only end_cycle {} with your first answer (or council_silence if available) "
                                "can finalize now; no action was executed"
                            )
                            extras.append(result.message())
                            for call in calls:
                                value = {"ok": False, "executed": False, "error": error}
                                self.store.emit(
                                    "tool.failed",
                                    {"name": call["function"]["name"], "call_id": call["id"], **value},
                                    bot_id=bot["id"],
                                    turn_id=turn_id,
                                    request_id=result.request_id,
                                    level="warning",
                                )
                                extras.append(
                                    {"role": "tool", "tool_call_id": call["id"], "content": dumps(value)}
                                )
                            cycle_failure(
                                result, error, unrecoverable=len({c["id"] for c in calls}) != len(calls)
                            )
                            round_index += 1
                            continue
                if cycle_response and not result.tool_calls:
                    freeze_draft(result, parsed_engram)
                    extras.append(result.message())
                    cycle_failure(result, "Response omitted end_cycle")
                    round_index += 1
                    continue
                if not result.tool_calls:
                    if parsed_engram is not None and parsed_engram.get("state") is None:
                        self.store.emit(
                            "engram.update_skipped",
                            {
                                "channel_id": channel_id,
                                "reason": parsed_engram.get("invalid", "invalid update"),
                                "coverage_unchanged": True,
                            },
                            bot_id=bot["id"],
                            turn_id=turn_id,
                            request_id=result.request_id,
                            level="warning",
                        )
                    if wrapped_reply(result.content):
                        self.store.emit(
                            "request.reply_rejected",
                            {
                                "error": REPLY_REPAIR,
                                "repair_available": not reply_repairs and round_index < round_limit,
                            },
                            bot_id=bot["id"],
                            turn_id=turn_id,
                            request_id=result.request_id,
                            level="warning",
                        )
                        if reply_repairs or round_index >= round_limit:
                            raise ControlError(
                                "Model repeated a tool-wrapped answer or exhausted its repair budget; nothing posted"
                            )
                        reply_repairs += 1
                        # The original response remains in the request ledger. Avoid
                        # teaching the malformed wrapper back through the active prompt.
                        repair = self.contexts.prompt(bot, "reply_repair")
                        if not repair:
                            raise ControlError(
                                "Model returned a tool-wrapped answer; reply repair prompt is disabled or empty, nothing posted"
                            )
                        extras.append({"role": repair["role"], "content": repair["content"]})
                        round_index += 1
                        continue
                    if result.content:
                        await publish_final(
                            result.content,
                            parsed_engram,
                            result.request_id,
                            result.finish_reason,
                            "text_only",
                        )
                    else:
                        raise ControlError(
                            "Provider returned no visible answer or tool call. "
                            + (
                                "Use council_silence for intentional silence. "
                                if silence_tools
                                else "Intentional silence is disabled for this bot. "
                            )
                            + "Inspect provider diagnostics for reasoning-only output."
                        )
                    break
                if len({c["id"] for c in result.tool_calls}) != len(result.tool_calls):
                    raise ControlError("Provider returned duplicate tool-call IDs; no actions executed")
                if background_handoff:
                    raise ControlError(
                        "Background dispatch acknowledgement requested text only; provider returned tool calls. "
                        "No further actions executed; accepted jobs continue and retain their completion notifications."
                    )
                names = [call["function"]["name"] for call in result.tool_calls]
                terminal = any(name in ("council_speak", "council_silence") for name in names) or ending
                companions = (
                    [call for call in result.tool_calls if call["function"]["name"] != "end_cycle"]
                    if ending
                    else []
                )
                batch_error = None
                current_bot = self.store.get("bots", bot["id"])
                if "council_silence" in names and (
                    not silence_tools or not current_bot or not current_bot.get("allow_silence", True)
                ):
                    batch_error = "Intentional silence is disabled for this bot; council_silence was not executed. Finish with ordinary assistant text. No actions in this batch were executed."
                elif ending and names.count("end_cycle") > 1:
                    batch_error = "end_cycle must appear exactly once; no actions were executed"
                elif ending and "council_silence" in names:
                    batch_error = "end_cycle and council_silence cannot be combined; no actions were executed"
                elif ending and companions and (finalizing or round_index >= round_limit):
                    batch_error = (
                        "Only terminal tools are available in this finalization round; "
                        "end_cycle companions are not allowed. No actions were executed"
                    )
                elif ending and not all(self.terminal_compatible(call, can_attach) for call in companions):
                    batch_error = (
                        "end_cycle may be combined only with memory/global_memory writes and discord_attach. "
                        "Run reads, searches, fetches, shell or other actions in an earlier round, then finish "
                        "with your answer plus end_cycle. No actions were executed"
                    )
                elif ending and wrapped_reply(result.content):
                    batch_error = "Tool-wrapped text cannot be published; write ordinary assistant text together with end_cycle. No actions were executed"
                elif terminal and not ending and len(names) != 1:
                    batch_error = "A silence decision (or obsolete reply call) must be the only tool call; no partial actions were executed"
                elif len(names) > calls_limit:
                    batch_error = f"Too many tool calls: maximum {calls_limit} per round; none executed"
                elif not terminal and round_index >= round_limit:
                    batch_error = (
                        "Tool round budget exhausted; "
                        + (
                            "finish with your answer plus end_cycle"
                            if cycle_mode
                            else "write an ordinary text answer"
                        )
                        + (" or call council_silence alone" if silence_tools else "")
                    )
                progress_notice = None
                if result.content.strip() and not terminal and not batch_error and not cycle_response:
                    if response_protocol == "narrate":
                        progress_notice = (
                            "narrate",
                            "Your text accompanying tool calls was not posted to Discord. This bot uses "
                            "narrate mode: action text stays private. Read the tool results and continue "
                            "needed work; finish with an ordinary text-only answer.",
                        )
                    elif wrapped_reply(result.content):
                        progress_notice = (
                            "wrapped_text",
                            "Your tool-wrapped narration was not posted. Only ordinary assistant text "
                            "can be a progress message. The actual tool calls still receive results.",
                        )
                    elif progress_attempts >= progress_limit:
                        progress_notice = (
                            "limit",
                            f"Your progress text was not posted: this turn's limit of {progress_limit} "
                            "progress attempts has been reached. Further progress text will also be "
                            "withheld. Tool calls can continue within their existing budget. Read the "
                            "results and give a final text-only answer when ready; that answer is separate.",
                        )
                    else:
                        # Count attempts, including uncertain or unavailable sends.
                        # A task extension never renews the public narration cap.
                        progress_attempts += 1
                        progress_notice = await self.deliver_progress(
                            bot,
                            profile,
                            provider,
                            context,
                            result.content,
                            reply_to,
                            request_id=result.request_id,
                            draft_deadline=document_deadline,
                        )
                    if progress_notice:
                        reason, notice = progress_notice
                        self.store.emit(
                            "request.progress_withheld",
                            {
                                "reason": reason,
                                "response_protocol": response_protocol,
                                "attempts": progress_attempts,
                                "limit": progress_limit,
                                **({"strikes": strikes, "strike_limit": strike_limit} if cycle_mode else {}),
                            },
                            bot_id=bot["id"],
                            turn_id=turn_id,
                            request_id=result.request_id,
                        )
                        if reason in progress_notices and not reason.startswith("end_cycle"):
                            progress_notice = None
                        else:
                            progress_notices.add(reason)
                cycle_reply_attempt = (
                    cycle_response
                    and (ending or bool(result.content.strip()))
                    and "council_silence" not in names
                )
                if cycle_reply_attempt:
                    if not ending:
                        cycle_error = "Response included tool narration without end_cycle"
                    if cycle_error or batch_error:
                        freeze_draft(result, parsed_engram)
                extras.append(result.message())
                finished = False
                ending_call = None
                companion_failed = False
                if self.jobs:
                    self.jobs.begin_batch(turn_id)
                for call_index, call in enumerate(result.tool_calls):
                    self.stop_superseded_work(
                        bot, channel_id, turn_id, "tool", remaining_calls=len(result.tool_calls) - call_index
                    )
                    name = call["function"]["name"]
                    definition = (
                        SILENCE
                        if name == "council_silence"
                        else END_CYCLE
                        if name == "end_cycle" and cycle_mode
                        else ATTACH
                        if name == "discord_attach" and can_attach
                        else None
                    )
                    if not batch_error and definition is END_CYCLE:
                        parameters = definition["function"]["parameters"]
                        description = definition["function"]["description"]
                        try:
                            args = parse_arguments(call["function"]["arguments"])
                        except (ValueError, TypeError) as exc:
                            result_value = syntax_feedback(name, exc, parameters, description)
                        else:
                            if args == {}:
                                # Publication waits for every companion result below.
                                ending_call = call
                                continue
                            result_value = {
                                "ok": False,
                                "executed": False,
                                "error": "end_cycle takes no arguments; call it with {} and put the answer "
                                "in ordinary assistant text",
                                "usage": usage(name, parameters, description),
                            }
                        cycle_error = "end_cycle arguments were invalid; call it with {}"
                        freeze_draft(result, parsed_engram)
                        result_value = self.registry.evidence.record(context, name, call["id"], result_value)
                        self.store.emit(
                            "tool.failed",
                            {"name": name, "call_id": call["id"], **result_value},
                            bot_id=bot["id"],
                            turn_id=turn_id,
                            level="warning",
                        )
                        extras.append(
                            {"role": "tool", "tool_call_id": call["id"], "content": dumps(result_value)}
                        )
                        continue
                    if batch_error:
                        spec = (
                            self.registry.spec_for(name, context)
                            if self.registry.allowed(name, context)
                            else None
                        )
                        parameters = (
                            definition["function"]["parameters"]
                            if definition
                            else spec.parameters
                            if spec
                            else {"type": "object"}
                        )
                        result_value = {
                            "ok": False,
                            "executed": False,
                            "error": batch_error
                            if definition or spec
                            else "Tool is not enabled for this bot and trusted request",
                        }
                        if definition or spec:
                            description = (
                                definition["function"]["description"] if definition else spec.description
                            )
                            try:
                                batch_args = parse_arguments(call["function"]["arguments"])
                            except (ValueError, TypeError) as exc:
                                argument_report = syntax_feedback(name, exc, parameters, description)
                            else:
                                argument_report = feedback(name, batch_args, parameters, description) or {
                                    "usage": usage(name, parameters, description, arguments=batch_args)
                                }
                            result_value.update(
                                usage=argument_report["usage"],
                                errors=[
                                    {"path": "$", "rule": "batch", "message": batch_error},
                                    *argument_report.get("errors", []),
                                ],
                                error_count=1 + len(argument_report.get("errors", [])),
                            )
                    elif name == "council_speak":
                        result_value = {"ok": False, "executed": False, "error": REPLY_REPAIR}
                    elif definition:
                        parameters = definition["function"]["parameters"]
                        description = definition["function"]["description"]
                        try:
                            args = parse_arguments(call["function"]["arguments"])
                        except (ValueError, TypeError) as exc:
                            result_value = syntax_feedback(name, exc, parameters, description)
                        else:
                            result_value = feedback(name, args, parameters, description)
                            if (
                                name == "discord_attach"
                                and isinstance(args, dict)
                                and not (result_value or {}).get("usage_only")
                            ):
                                # Type-invalid fields cannot be inspected safely, but they must
                                # not hide independent semantic failures in the remaining fields.
                                issues = list((result_value or {}).get("errors", []))
                                target = args.get("reply_to") or None
                                if (
                                    isinstance(target, str)
                                    and target
                                    and (
                                        not target.isdigit()
                                        or target not in {row["discord_id"] for row in rows}
                                    )
                                ):
                                    issues.append(
                                        {
                                            "path": "$.reply_to",
                                            "message": "Reply target must be a message ID in this turn's channel context",
                                        }
                                    )
                                selected = args.get("artifact_ids", [])
                                for index, artifact_id in enumerate(
                                    selected if isinstance(selected, list) else []
                                ):
                                    if not isinstance(artifact_id, str):
                                        continue
                                    try:
                                        self.registry.resolve_artifact(artifact_id, context)
                                    except ControlError as exc:
                                        issues.append(
                                            {"path": f"$.artifact_ids[{index}]", "message": str(exc)}
                                        )
                                if issues:
                                    result_value = {
                                        "ok": False,
                                        "executed": False,
                                        "error": "Invalid attachment preparation; fix all errors",
                                        "errors": issues,
                                        "error_count": len(issues),
                                        "usage": usage(name, parameters, description),
                                    }
                            if result_value is None:
                                if name == "council_silence":
                                    status, decision = "silent", "listen"
                                    self.store.emit(
                                        "decision.silence",
                                        {"label": args["label"]},
                                        bot_id=bot["id"],
                                        turn_id=turn_id,
                                    )
                                else:
                                    reply_to, artifact_ids = (
                                        args.get("reply_to", reply_to) or None,
                                        args["artifact_ids"],
                                    )
                                    cycle["actions"] += 1
                                    result_value = {
                                        "ok": True,
                                        "prepared": True,
                                        "artifact_ids": artifact_ids,
                                        "reply_to": reply_to,
                                        "posted": False,
                                        "next": "Write your answer as ordinary assistant content; no reply tool is needed.",
                                    }
                                if name == "council_silence":
                                    finished = True
                                    break
                    else:
                        receipt_id = (
                            self.action_receipts.start(
                                context, name, call["id"], call["function"]["arguments"]
                            )
                            if record_actions
                            else None
                        )
                        result_value = await budgeted(
                            self.registry.call_raw(name, call["function"]["arguments"], context, call["id"])
                        )
                        if receipt_id:
                            self.action_receipts.finish(context, receipt_id, result_value)
                        if (
                            isinstance(result_value, dict)
                            and result_value.get("ok") is not False
                            and not result_value.get("usage_only")
                        ):
                            cycle["actions"] += 1
                        starts_task = isinstance(result_value, dict) and (
                            (name == "document_site" and result_value.get("document_task_started"))
                            or (name == "workspace" and result_value.get("workspace_task_started"))
                            or (name == "web_fetch" and result_value.get("web_task_started"))
                        )
                        if starts_task:
                            field_prefix = "document_task" if name == "document_site" else "work_task"
                            extra_rounds = bot.get(f"{field_prefix}_rounds", 20)
                            if not task_extension_used:
                                task_extension_used = True
                                task_plugin = name
                            else:
                                extra_rounds = 0
                            if extra_rounds:
                                document_started = True
                                round_limit = max(round_limit, round_index + 1 + extra_rounds)
                                calls_limit = bot.get(f"{field_prefix}_calls_per_round", 8)
                                seconds = bot.get(f"{field_prefix}_seconds", 900)
                                task_seconds = seconds
                                document_deadline = asyncio.get_running_loop().time() + seconds
                                self.store.emit(
                                    "tool.task_started",
                                    {
                                        "plugin": name,
                                        "rounds": extra_rounds,
                                        "calls_per_round": calls_limit,
                                        "seconds": seconds,
                                    },
                                    bot_id=bot["id"],
                                    turn_id=turn_id,
                                )
                            result_value = {
                                **result_value,
                                "task_budget": {
                                    "active": document_started,
                                    "rounds_remaining": round_limit - round_index - 1,
                                    "calls_per_round": calls_limit,
                                    "seconds": task_seconds,
                                    "seconds_remaining": max(
                                        0, document_deadline - asyncio.get_running_loop().time()
                                    )
                                    if document_deadline is not None
                                    else None,
                                    "renewable_this_turn": False,
                                },
                            }
                            # Budget metadata is added after the plugin finishes.
                            # Give that exact final model-visible body a fresh
                            # immutable evidence ID; leave the earlier event intact.
                            result_value.pop("result_id", None)
                    result_value = self.vault.redact(result_value)
                    if "result_id" not in result_value:
                        result_value = self.registry.evidence.record(context, name, call["id"], result_value)
                    if definition or batch_error or name == "council_speak":
                        self.store.emit(
                            "tool.help"
                            if result_value.get("usage_only")
                            else "tool.completed"
                            if result_value.get("ok")
                            else "tool.failed",
                            {"name": name, "call_id": call["id"], **result_value},
                            bot_id=bot["id"],
                            turn_id=turn_id,
                            level="info"
                            if result_value.get("usage_only") or result_value.get("ok")
                            else "warning",
                        )
                    if (
                        cycle_reply_attempt
                        and ending
                        and name != "end_cycle"
                        and (result_value.get("ok") is False or result_value.get("usage_only"))
                    ):
                        # A failed finish companion invalidates its answer.
                        # Narration has no answer yet; finalization can report
                        # ordinary action errors/help without replaying work.
                        companion_failed = True
                    extras.append(
                        {"role": "tool", "tool_call_id": call["id"], "content": dumps(result_value)}
                    )
                if ending_call is not None:
                    if companion_failed or cycle_error:
                        ending_value = {
                            "ok": False,
                            "executed": False,
                            "published": False,
                            "error": "No answer was published. "
                            + (
                                "A companion action failed; completed actions are preserved and will not be repeated."
                                if companion_failed
                                else cycle_error
                            ),
                        }
                    elif not result.content.strip():
                        answer_pending = {"request_id": result.request_id, "call_id": ending_call["id"]}
                        cycle["finalization"] = True
                        round_limit = round_index + 1
                        ending_value = {
                            "ok": True,
                            "executed": True,
                            "published": False,
                            "answer_pending": True,
                        }
                        self.store.emit(
                            "cycle.answer_pending",
                            {"channel_id": channel_id, "end_call_id": ending_call["id"]},
                            bot_id=bot["id"],
                            turn_id=turn_id,
                            request_id=result.request_id,
                        )
                    else:
                        await publish_final(
                            result.content,
                            parsed_engram,
                            result.request_id,
                            result.finish_reason,
                            "end_cycle",
                            terminal_call_id=ending_call["id"],
                        )
                        ending_value = {"ok": True, "executed": True, "published": True, "draft": False}
                        finished = True
                    ending_value = self.registry.evidence.record(
                        context, "end_cycle", ending_call["id"], ending_value
                    )
                    self.store.emit(
                        "tool.completed" if ending_value.get("ok") else "tool.failed",
                        {"name": "end_cycle", "call_id": ending_call["id"], **ending_value},
                        bot_id=bot["id"],
                        turn_id=turn_id,
                        level="info" if ending_value.get("ok") else "warning",
                    )
                    extras.append(
                        {"role": "tool", "tool_call_id": ending_call["id"], "content": dumps(ending_value)}
                    )
                if cycle_response and not finished and (cycle_error or batch_error or companion_failed):
                    freeze_draft(result, parsed_engram)
                    cycle_failure(
                        result,
                        "A companion action failed" if companion_failed else batch_error or cycle_error,
                        unrecoverable=companion_failed or bool(batch_error and companions),
                        allow_first_answer=cycle_reply_attempt and not ending,
                    )
                    # Confirmation cannot hand off to a background acknowledgement
                    # or gain more work from task-extension bookkeeping.
                    if self.jobs:
                        self.jobs.release_batch(turn_id)
                    round_index += 1
                    continue
                if answer_pending is not None:
                    extras.append(
                        {
                            "role": "user",
                            "content": "Automatic harness notice (private, not sent to Discord): "
                            "end_cycle was accepted. No public answer draft exists. Tool work is closed. "
                            "Write your first public answer now as ordinary assistant text, using the results "
                            "already available. Do not call any tools or promise further work. This is your "
                            "only answer request; empty output or tool calls end the cycle without an answer.",
                        }
                    )
                    if self.jobs:
                        self.jobs.release_batch(turn_id)
                    round_index += 1
                    continue
                if progress_notice:
                    # Complete the assistant/tool group before adding private
                    # feedback. It travels only in this turn's bounded exchanges.
                    extras.append(
                        {
                            "role": "user",
                            "content": "Automatic harness notice (private, not sent to Discord): "
                            + progress_notice[1],
                        }
                    )
                if finished:
                    break
                handoff = self.jobs.handoff(context) if self.jobs else None
                if handoff:
                    # Complete the accepted batch, then one text-only acknowledgement.
                    # Worker ownership and deadlines never depend on this turn.
                    background_handoff = True
                    round_limit = round_index + 1
                    if typing_task:
                        await cancel_and_wait(typing_task)
                        typing_task = None
                    self.store.emit(
                        "background_job.handoff",
                        {"channel_id": channel_id, **handoff["progress"]},
                        bot_id=bot["id"],
                        turn_id=turn_id,
                    )
                elif self.jobs:
                    self.jobs.release_batch(turn_id)
                round_index += 1
            else:
                if cycle_mode:
                    raise CycleIncomplete(
                        "Tool round budget exhausted without end_cycle; no answer was published"
                    )
                raise ControlError(
                    "Tool round budget exhausted without a valid final answer; no further calls executed"
                )
        except TurnSuperseded:
            status, error = "cancelled", "New human-directed message superseded an unsent draft"
        except asyncio.CancelledError:
            confirmed = ordinary_delivery_id and self.store.one(
                "SELECT 1 FROM outbox WHERE id=? AND status='sent'", (ordinary_delivery_id,)
            )
            if confirmed:
                # Owner cancellation still propagates, but cannot make a delivered
                # answer unread or discard its state because a companion was pending.
                status, decision, error = (
                    "sent",
                    "reply" if reply_to else "speak",
                    "Companion delivery interrupted after the written reply was confirmed",
                )
            else:
                status, error = (
                    "cancelled",
                    "New human-directed message superseded a cooldown wait"
                    if turn_id in self.human_superseded
                    else "Stopped by operator, configuration change, or shutdown",
                )
            raise
        except Exception as exc:
            status, error = "failed", self.vault.redact(error_text(exc))[:3000]
            retry_delay = getattr(exc, "retry_after", 0)
            self.store.execute("UPDATE bot_runtime SET error=? WHERE bot_id=?", (error, bot["id"]))
            self.store.emit(
                "turn.failed",
                {
                    "error": error,
                    "provider_id": provider["id"],
                    "profile_id": profile["id"],
                    "channel_id": channel_id,
                    "error_origin": getattr(exc, "details", {}).get("origin"),
                },
                bot_id=bot["id"],
                turn_id=turn_id,
                request_id=getattr(exc, "request_id", None),
                level="error",
            )
            if isinstance(exc, CycleIncomplete):
                cycle["reason"] = "incomplete"
                try:
                    await self.notify_incomplete_cycle(
                        bot, profile, provider, context, reply_to, generation_rounds
                    )
                except asyncio.CancelledError:
                    status, error = "cancelled", "Stopped during incomplete-cycle notice delivery"
                    cycle["reason"] = "cancelled"
                    raise
        finally:
            if status in ("sent", "silent"):
                self.incomplete_streaks.pop(self.cycle_scope(bot, channel_id), None)
            if cycle is not None:
                self.store.emit(
                    "cycle.ended",
                    {
                        "channel_id": channel_id,
                        "response_protocol": response_protocol,
                        "status": status,
                        "decision": decision,
                        "reason": cycle["reason"]
                        or {"sent": "text_only", "silent": "silence", "cancelled": "cancelled"}.get(
                            status, "failed"
                        ),
                        "rounds": generation_rounds,
                        "strikes": strikes,
                        "actions_executed": cycle["actions"],
                        "published": status == "sent",
                        "draft_retained": cycle["draft_retained"],
                        "finalization": cycle["finalization"],
                    },
                    bot_id=bot["id"],
                    turn_id=turn_id,
                )
            if status in ("sent", "silent"):
                # Seeing another human's question in context does not answer it.
                # Keep it above the read cursor until its own turn claims it.
                waiting = self.attention(bot, channel_id)
                handled_through = min(through, min((row["seq"] - 1 for row in waiting), default=through))
                self.store.execute(
                    "UPDATE contexts SET last_seen=MAX(last_seen,?),updated_at=? WHERE bot_id=? AND channel_id=?",
                    (handled_through, time.time(), bot["id"], channel_id),
                )
                self.store.execute("UPDATE bot_runtime SET error=NULL WHERE bot_id=?", (bot["id"],))
            if engram_candidate is not None and status == "sent" and ordinary_delivery_id:
                self.registry.engrams.bind_outbox(engram_candidate, ordinary_delivery_id)
            await self.finalize_turn(
                bot,
                provider,
                turn_id,
                typing_task,
                status=status,
                decision=decision,
                error=error,
                retry_delay=retry_delay,
            )
            if record_actions:
                self.action_receipts.discard_completed(context, status)
                if status in ("sent", "silent") and receipts_in_last_request:
                    self.action_receipts.acknowledge(context, receipts_in_last_request)
            if engram_candidate is not None and status == "sent":
                self.registry.engrams.finalize(engram_candidate)

    @staticmethod
    def terminal_compatible(call, can_attach):
        """Only writes whose success can precede publication may accompany end_cycle."""
        name = call["function"]["name"]
        if name == "discord_attach":
            return can_attach
        if name not in TERMINAL_COMPATIBLE_WRITES:
            return False
        try:
            args = parse_arguments(call["function"]["arguments"])
        except ValueError, TypeError:
            return False
        return isinstance(args, dict) and args.get("operation") == "write"

    @staticmethod
    def cycle_scope(bot, channel_id):
        return bot["id"], (bot.get("invocation") or {}).get("channel_id", channel_id)

    async def notify_incomplete_cycle(self, bot, profile, provider, context, reply_to, rounds):
        """One short public notice per incomplete cycle, paused after a streak."""
        key = self.cycle_scope(bot, context.channel_id)
        self.incomplete_streaks[key] += 1
        streak = self.incomplete_streaks[key]
        if not bot.get("notify_incomplete_cycle", False):
            return
        if streak > INCOMPLETE_NOTICE_STREAK:
            self.store.emit(
                "cycle.notice_suppressed",
                {"channel_id": context.channel_id, "streak": streak, "limit": INCOMPLETE_NOTICE_STREAK},
                bot_id=bot["id"],
                turn_id=context.turn_id,
                level="warning",
            )
            return
        if (
            not self.valid(bot, profile, provider)
            or not self.channel_allowed(bot, context.channel_id)
            or self.superseding_attention(bot, context.channel_id, context.turn_id)
        ):
            return
        text = (
            f"-# {bot['name']} stopped without an answer after {rounds} model round"
            f"{'' if rounds == 1 else 's'}; the response cycle did not complete."
        )
        try:
            async with asyncio.timeout(PROGRESS_SEND_TIMEOUT):
                await self.deliver(bot, profile, provider, context, text, reply_to, [], notice=True)
        except (DeliveryError, TimeoutError, OSError, ClientError, ControlError) as exc:
            self.store.emit(
                "cycle.notice_failed",
                {"channel_id": context.channel_id, "error": self.vault.redact(error_text(exc))[:500]},
                bot_id=bot["id"],
                turn_id=context.turn_id,
                level="warning",
            )

    async def deliver_progress(
        self, bot, profile, provider, context, content, reply_to, *, request_id, draft_deadline
    ):
        """Optional commentary must not prevent the associated action batch."""

        def check_work():
            if not self.valid(bot, profile, provider) or not self.channel_allowed(bot, context.channel_id):
                raise asyncio.CancelledError()
            if self.superseding_attention(bot, context.channel_id, context.turn_id):
                raise TurnSuperseded("New directed human input is waiting; regenerate from current context")
            if draft_deadline is not None and asyncio.get_running_loop().time() >= draft_deadline:
                raise ControlError("Extended task time budget exhausted before tool execution")

        check_work()
        visible = self.vault.redact(strip_reasoning(content))
        if not visible.strip() or len(visible) > 12000:
            return (
                "invalid_text",
                "Your progress text was not posted because it was empty or exceeded the message limit. "
                "The tool calls still receive results; the final answer must fit the ordinary limit.",
            )
        delivery_id = uid("out_")
        try:
            # This timeout owns only the optional send. Delivery records any
            # in-flight timeout as uncertain; owner cancellation still propagates.
            async with asyncio.timeout(PROGRESS_SEND_TIMEOUT):
                await self.deliver(
                    bot,
                    profile,
                    provider,
                    context,
                    content,
                    reply_to,
                    [],
                    request_id=request_id,
                    draft_deadline=draft_deadline,
                    delivery_id=delivery_id,
                    progress=True,
                )
        except (DeliveryError, TimeoutError, OSError, ClientError) as exc:
            check_work()
            row = self.store.one("SELECT status FROM outbox WHERE id=?", (delivery_id,))
            if row and row["status"] == "sent":
                return None
            uncertain = bool(row and row["status"] == "unknown") or (
                isinstance(exc, DeliveryError) and exc.uncertain
            )
            if uncertain:
                return (
                    "delivery_unknown",
                    "Discord acceptance of your progress message is unknown. Do not assume the user "
                    "saw it or resend it. The tool calls can continue; read their results before answering.",
                )
            return (
                "delivery_unavailable" if isinstance(exc, ProgressUnavailable) else "delivery_failed",
                "Your progress message was not delivered. The tool calls can continue; read their "
                "results before answering. Do not repeat the progress message to retry its delivery.",
            )
        check_work()
        return None

    async def deliver(
        self,
        bot,
        profile,
        provider,
        context,
        content,
        reply_to,
        artifact_ids,
        *,
        request_id=None,
        draft_deadline=None,
        routing=None,
        delivery_id=None,
        progress=False,
        notice=False,
    ):
        destination = routing["target_channel_id"] if routing else context.channel_id
        # Progress and harness notices are optional sends without footer, panels
        # or the reasoning button. Progress skips a busy slot; a notice ignores
        # only the personal cooldown and otherwise waits like a final answer.
        optional = progress or notice
        kind = "progress" if progress else "harness_notice" if notice else None

        def check_deadline():
            if draft_deadline is not None and asyncio.get_running_loop().time() >= draft_deadline:
                raise ControlError("Document task time budget exhausted before Discord dispatch")
            if routing:
                self.registry.discord_dispatch.check(context, routing)

        check_deadline()
        content = self.vault.redact(strip_reasoning(content))
        if not content:
            raise ControlError("No visible content remained after removing reasoning")
        if routing:
            # Explicit cross-post actions retain their single, atomic file receipt.
            # Native companions belong to ordinary conversation answers only.
            paths = [self.registry.resolve_artifact(artifact_id, context) for artifact_id in artifact_ids]
            voice_notes = []
        else:
            paths, voice_notes = self.registry.voice_notes.split(artifact_ids, context)
        voice_ids = {note["artifact_id"] for note in voice_notes}
        regular_ids = [artifact_id for artifact_id in artifact_ids if artifact_id not in voice_ids]
        if len(content) > 12000:
            raise ControlError("Council contribution exceeds 12,000 characters")
        request = (
            self.store.one(
                "SELECT model,input_tokens,output_tokens,ttft_ms,duration_ms,json_extract(response,'$.footer_tokens') AS footer_tokens FROM requests WHERE id=? AND bot_id=? AND turn_id=? AND purpose='generation' AND status='completed'",
                (request_id, bot["id"], context.turn_id),
            )
            if request_id
            else None
        )
        footer = "" if optional else render_footer(bot, profile, provider, request, redact=self.vault.redact)
        if routing:
            marker = (
                "-# "
                + ("Directed reply" if routing["mode"] == "directed" else "Cross-post")
                + " from Hortator"
            )
            footer = marker + ("\n" + footer if footer else "")
        outbox_id = delivery_id or uid("out_")
        self.store.execute(
            "INSERT INTO outbox(id,turn_id,bot_id,channel_id,content,reply_to,artifacts,status,created_at,routing) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                outbox_id,
                context.turn_id,
                bot["id"],
                destination,
                content,
                reply_to,
                dumps(regular_ids),
                "pending",
                time.time(),
                dumps(routing or ({"kind": kind} if kind else {})),
            ),
        )
        self.store.emit(
            "delivery.queued",
            {
                "outbox_id": outbox_id,
                "content": content,
                "reply_to": reply_to,
                "artifact_ids": regular_ids,
                **({"voice_note_artifact_ids": list(voice_ids)} if voice_ids else {}),
                "footer": footer,
                "request_id": request_id,
                **({"kind": kind} if kind else {}),
                **({"routing": routing} if routing else {}),
            },
            bot_id=bot["id"],
            turn_id=context.turn_id,
        )
        try:
            if self.superseding_attention(bot, context.channel_id, context.turn_id):
                raise TurnSuperseded("New directed human input is waiting; regenerate from current context")
            priority = self.store.one(
                "SELECT 1 FROM human_attention_claims WHERE bot_id=? AND turn_id=? LIMIT 1",
                (bot["id"], context.turn_id),
            )
            continuing = self.store.one(
                "SELECT 1 FROM outbox WHERE turn_id=? AND bot_id=? AND status IN ('sent','unknown') "
                "AND json_extract(routing,'$.kind')='progress' LIMIT 1",
                (context.turn_id, bot["id"]),
            )
            # Unknown progress already advances last_sent conservatively. It may
            # have reached Discord, so allow this same turn's final answer just
            # as for confirmed progress, without replaying the uncertain text.
            # A personal cooldown must never hold the channel send lock. Another
            # bot may be responding to a human while this draft waits its cadence.
            wait = (
                0
                if priority or continuing or notice or self.is_single_shot(bot) or bot.get("scheduled_wake")
                else self.store.runtime(bot["id"])["last_sent"] + bot["cooldown_seconds"] - time.time()
            )
            if wait > 0:
                if progress:
                    raise ProgressUnavailable("Progress skipped while the bot's personal cooldown is active")
                self.delivery_waiting[bot["id"]] = context.turn_id
                self.store.emit(
                    "delivery.cooldown",
                    {"outbox_id": outbox_id, "wait_seconds": wait, "scope": "bot"},
                    bot_id=bot["id"],
                    turn_id=context.turn_id,
                )
                try:
                    remaining = (
                        max(0, draft_deadline - asyncio.get_running_loop().time())
                        if draft_deadline is not None
                        else wait
                    )
                    await asyncio.sleep(min(wait, remaining))
                finally:
                    self.delivery_waiting.pop(bot["id"], None)
            if progress and self.room_locks[destination].locked():
                raise ProgressUnavailable("Progress skipped while the channel send slot is busy")
            async with self.room_locks[destination]:
                room = (
                    self.registry.discord_dispatch.check(context, routing)
                    if routing
                    else self.room_for(bot, context.channel_id)
                )
                gap = room["send_gap_seconds"] if room else 1.5
                wait = self.room_last[destination] + gap - time.time()
                if wait > 0:
                    if progress:
                        raise ProgressUnavailable("Progress skipped while the channel send gap is active")
                    self.store.emit(
                        "delivery.cooldown",
                        {"outbox_id": outbox_id, "wait_seconds": wait},
                        bot_id=bot["id"],
                        turn_id=context.turn_id,
                    )
                    remaining = (
                        max(0, draft_deadline - asyncio.get_running_loop().time())
                        if draft_deadline is not None
                        else wait
                    )
                    await asyncio.sleep(min(wait, remaining))
                reasoning = (
                    await self.registry.reasoning_viewer.prepare(
                        bot, context.turn_id, request_id, outbox_id, destination
                    )
                    if not routing and not optional
                    else None
                )
                # Stop before dispatch, but never cancel an in-flight Discord send
                # solely because the drafting deadline passed: acceptance may be uncertain.
                check_deadline()
                if self.superseding_attention(bot, context.channel_id, context.turn_id):
                    raise TurnSuperseded(
                        "New directed human input is waiting; regenerate from current context"
                    )
                if not self.valid(bot, profile, provider) or not self.channel_allowed(
                    bot, context.channel_id
                ):
                    raise asyncio.CancelledError()
                if not self.transport:
                    raise DeliveryError("Discord connector is unavailable")
                self.store.execute("UPDATE outbox SET status='sending' WHERE id=?", (outbox_id,))
                self.store.emit(
                    "delivery.sending", {"outbox_id": outbox_id}, bot_id=bot["id"], turn_id=context.turn_id
                )
                panel = self.registry.panels.prepared(context) if not routing and not optional else None
                if panel:
                    self.registry.panels.dispatch(panel, outbox_id)
                discord_id = await self.transport.send(
                    bot,
                    destination,
                    content,
                    reply_to,
                    paths,
                    nonce=outbox_id,
                    footer=footer,
                    **({"panel": panel} if panel else {}),
                    **({"reasoning": reasoning} if reasoning else {}),
                    **(
                        {
                            "strict_reply": True,
                            "destination_guard": lambda: self.registry.discord_dispatch.check(
                                context, routing
                            ),
                        }
                        if routing
                        else {}
                    ),
                )
                sent = time.time()
                self.store.execute(
                    "UPDATE outbox SET status='sent',sent_at=?,discord_id=? WHERE id=?",
                    (sent, discord_id, outbox_id),
                )
                if panel:
                    self.registry.panels.bind(panel, discord_id, content)
                if reasoning:
                    self.registry.reasoning_viewer.bind(reasoning, discord_id)
                self.store.execute("UPDATE bot_runtime SET last_sent=? WHERE bot_id=?", (sent, bot["id"]))
                self.room_last[destination] = sent
                self.store.ingest(
                    discord_id=discord_id,
                    channel_id=destination,
                    author_id=self.vault.get(f"bot/{bot['id']}/user_id") or bot["application_id"],
                    author_name=bot["name"],
                    content=content,
                    room_id=room["id"] if room else f"owner:{bot['id']}",
                    bot_id=bot["id"],
                    reply_to=reply_to,
                    addressing={
                        "author_kind": "bot",
                        "live": False,
                        "routing": {"mode": routing["mode"], "source_bot_id": bot["id"]},
                    }
                    if routing
                    else None,
                    guild_id=routing["guild_id"] if routing else None,
                    parent_id=room["channel_id"] if routing and destination != room["channel_id"] else None,
                )
                self.store.execute("UPDATE messages SET content=? WHERE discord_id=?", (content, discord_id))
                if routing:
                    observed = self.store.one(
                        "SELECT addressing FROM messages WHERE discord_id=?", (discord_id,)
                    )
                    addressing = json.loads(observed["addressing"])
                    addressing.update(
                        author_kind="bot", routing={"mode": routing["mode"], "source_bot_id": bot["id"]}
                    )
                    self.store.execute(
                        "UPDATE messages SET addressing=? WHERE discord_id=?", (dumps(addressing), discord_id)
                    )
                self.store.emit(
                    "delivery.sent",
                    {
                        "outbox_id": outbox_id,
                        "discord_id": discord_id,
                        "content": content,
                        **({"kind": kind} if kind else {}),
                    },
                    bot_id=bot["id"],
                    turn_id=context.turn_id,
                )
                if voice_notes:

                    def voice_guard():
                        check_deadline()
                        if not self.valid(bot, profile, provider) or not self.channel_allowed(
                            bot, context.channel_id
                        ):
                            raise ControlError(
                                "Bot configuration or channel grant changed before voice-note dispatch"
                            )

                    await self.registry.voice_notes.deliver(
                        self, voice_notes, bot, context, destination, outbox_id, discord_id, room, voice_guard
                    )
                return outbox_id
        except (Exception, asyncio.CancelledError) as exc:
            row = self.store.one("SELECT status FROM outbox WHERE id=?", (outbox_id,))
            if row["status"] == "sent":
                # A cancelled companion voice note cannot undo confirmed text delivery.
                raise
            uncertain = row["status"] == "sending" and (not isinstance(exc, DeliveryError) or exc.uncertain)
            status = (
                "unknown"
                if uncertain
                else "suppressed"
                if isinstance(exc, (asyncio.CancelledError, TurnSuperseded, ProgressUnavailable))
                else "failed"
            )
            if uncertain:
                self.store.execute(
                    "UPDATE bot_runtime SET last_sent=? WHERE bot_id=?", (time.time(), bot["id"])
                )
            self.store.execute(
                "UPDATE outbox SET status=?,error=? WHERE id=?",
                (status, self.vault.redact(error_text(exc)), outbox_id),
            )
            self.store.emit(
                "delivery." + status,
                {
                    "outbox_id": outbox_id,
                    "channel_id": context.channel_id,
                    "error": error_text(exc),
                    "content": content,
                    "reason": (
                        "Discord acceptance is unknown; automatic resend withheld to avoid duplicates"
                        if uncertain
                        else "Message was not delivered"
                    ),
                },
                bot_id=bot["id"],
                turn_id=context.turn_id,
                level="warning" if status == "suppressed" else "error",
            )
            raise

    async def cancel(self, bot_ids, reason):
        bot_ids = list(bot_ids)
        tasks = []
        for bot_id in bot_ids:
            task = self.tasks.get(bot_id)
            if task and task is not asyncio.current_task():
                task.cancel()
                tasks.append(task)
            self.store.execute(
                "UPDATE outbox SET status='suppressed',error=? WHERE bot_id=? AND status='pending'",
                (reason, bot_id),
            )
        if self.jobs:
            await self.jobs.cancel(bot_ids, reason)
        if tasks:
            # Already cancelled above. A second cancellation can interrupt the
            # turn's finally block while it joins typing/provider cleanup.
            await join_tasks(*tasks)

    async def close(self):
        self.closed = True
        if self.task:
            await cancel_and_wait(self.task)
        await self.cancel(list(self.tasks), "Shutdown")
