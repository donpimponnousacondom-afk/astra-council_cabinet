from __future__ import annotations

import asyncio
import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass

from .concurrency import cancel_and_wait, task_group, error_text
from .context import ContextBuilder
from .addressing import human_directed_elsewhere
from .footer import render_footer
from .diagnostics import reasoning_settings
from .models import ControlError, OWNER_ID
from .plugins import ATTACH, SILENCE, ToolContext
from .provider import strip_reasoning
from .store import dumps, uid
from .tool_feedback import feedback, parse_arguments, syntax_feedback, usage
from .working_set import bound_exchanges, prompt_exchanges


REPLY_REPAIR = (
    "Your previous answer was withheld because it used a tool wrapper instead of an answer. "
    "council_speak does not exist. Write only the final Discord message as ordinary assistant "
    "content, without JSON/XML/function wrappers or a description of your tool decision. "
    "Use actual tool_calls only for available actions or terminal decisions."
)


def wrapped_reply(content):
    """Recognize obsolete reply envelopes, never parse or execute text as a tool.

    Deliberately narrow: ordinary JSON, prose about tools, and fenced examples are valid answers.
    """
    text = content.lstrip()
    return bool(
        re.match(r'\{\s*"(?:tool|name)"\s*:\s*"council_speak"', text)
        or re.match(r'\{\s*"function"\s*:\s*\{\s*"name"\s*:\s*"council_speak"', text)
        or re.match(r"<(?:tool_call|function(?:=|\s|>))", text, re.I)
        or ("```" not in text and re.search(r"</(?:parameter|function)\s*>\s*$", text, re.I))
    )


@dataclass
class DeliveryError(Exception):
    message: str
    uncertain: bool = False

    def __str__(self):
        return self.message


class TurnSuperseded(ControlError):
    pass


class Engine:
    def __init__(self, store, vault, pool, registry, transport=None):
        self.store, self.vault, self.pool, self.registry = store, vault, pool, registry
        self.contexts = ContextBuilder(store, pool)
        self.transport = transport
        self.tasks: dict[str, asyncio.Task] = {}
        self.room_locks = defaultdict(asyncio.Lock)
        self.room_last = defaultdict(float)
        self.task = None
        self.closed = False
        self.delivery_waiting = {}
        self.human_superseded = set()

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
            and (bot["enabled"] or manual)
            and profile
            and provider
            and provider["enabled"]
        )

    def valid(self, bot, profile, provider, *, manual=False):
        current = self.store.get("bots", bot["id"])
        return bool(
            current
            and self.available(current, manual=manual)
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

    def pick_channel(self, bot):
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
                not human_directed_elsewhere(self.store, row, bot["id"])
                for row in self.store.rows(
                    "SELECT * FROM messages WHERE channel_id=? AND seq>? AND deleted=0 AND (bot_id IS NULL OR bot_id<>?)",
                    (channel_id, context["last_seen"], bot["id"]),
                )
            )
            if bot["role"] == "hortator":
                unseen = bool(
                    self.store.one(
                        "SELECT 1 FROM messages WHERE channel_id=? AND seq>? AND author_id=? LIMIT 1",
                        (channel_id, context["last_seen"], OWNER_ID),
                    )
                )
            if not unseen and (not bot["evaluate_when_idle"] or bot["role"] == "hortator"):
                continue
            if latest:
                candidates.append((not unseen, context["updated_at"], channel_id))
        return min(candidates)[2] if candidates else None

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
            and (bot["role"] != "hortator" or row["author_id"] == OWNER_ID)
        ]

    def claim_attention(self, bot, channel_id, turn_id, *, through=None):
        pending = self.attention(bot, channel_id, through)
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
            "kind": "human_reply" if "reply" in target["via"] else "human_mention",
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
            if attention and bot["id"] in self.tasks and bot["id"] in self.delivery_waiting:
                self.human_superseded.add(self.delivery_waiting[bot["id"]])
                self.tasks[bot["id"]].cancel()
                continue
            if bot["id"] in self.tasks or len(self.tasks) >= settings["max_concurrent_turns"]:
                continue
            if not self.available(bot):
                continue
            state = self.store.runtime(bot["id"])
            if (
                state["gateway_status"] != "online"
                or state["retry_until"] > now
                or (state["next_at"] > now and not attention)
            ):
                continue
            profile, provider = self.configuration(bot)
            health = self.store.health(provider["id"])
            if health["circuit_until"] > now:
                continue
            channel_id = attention[0]["channel_id"] if attention else self.pick_channel(bot)
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
            self.launch(bot, channel_id, human_directed=bool(attention))

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

    def launch(self, bot, channel_id, *, compact_only=False, human_directed=False):
        if bot["id"] in self.tasks:
            raise ControlError("Bot already has an active turn", 409)
        if not self.available(bot, manual=compact_only):
            raise ControlError("Council, bot or provider is paused")
        if not self.channel_allowed(bot, channel_id):
            raise ControlError("Channel is outside this bot's configured scope")
        turn_id = uid("turn_")
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
                else activation["kind"]
                if activation
                else "owner_message"
                if bot["role"] == "hortator"
                else "timer",
                bot["revision"],
            ),
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
        task.add_done_callback(
            lambda completed: (
                self.tasks.pop(bot["id"], None) if self.tasks.get(bot["id"]) is completed else None
            )
        )
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

    async def run_turn(self, bot, profile, provider, channel_id, turn_id, compact_only, group):
        status, decision, error = "failed", None, None
        retry_delay = 0
        typing_task = None
        context = ToolContext(bot, channel_id, turn_id, owner_verified=bot["role"] == "hortator")
        try:
            if getattr(self.transport, "typing", None):
                typing_task = group.create_task(
                    self.show_typing(bot, channel_id, turn_id), name=f"typing:{bot['id']}:{turn_id}"
                )
                # Start presence before context preparation, including compaction and provider queues.
                await asyncio.sleep(0)
            silence_tools = [SILENCE] if bot.get("allow_silence", True) else []
            tools = silence_tools + self.registry.schemas(context)
            rows, summary, through, original_meta = await self.contexts.prepare(
                bot, profile, channel_id, turn_id, tools, force=compact_only
            )
            if compact_only:
                status, decision = "completed", "compacted"
                return
            activation = self.claim_attention(bot, channel_id, turn_id, through=through) or bot.get(
                "activation"
            )
            if activation:
                bot = {**bot, "activation": activation}
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
            reply_repairs = 0

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
                if not self.valid(bot, profile, provider) or not self.channel_allowed(
                    bot, context.channel_id
                ):
                    raise asyncio.CancelledError()
                if document_deadline is not None and asyncio.get_running_loop().time() >= document_deadline:
                    raise ControlError(
                        "Extended task time budget exhausted; saved local files remain available"
                    )
                available_tools = silence_tools + (
                    self.registry.schemas(context) if round_index < round_limit else []
                )
                # Most chat turns need no attachment schema. Expose preparation only
                # after a real artifact exists, and keep it inside the ordinary budget.
                can_attach = round_index < round_limit and self.store.one(
                    "SELECT id FROM artifacts WHERE bot_id=? AND turn_id=? LIMIT 1",
                    (bot["id"], turn_id),
                )
                if can_attach:
                    available_tools.append(ATTACH)
                budget_bot = {**bot, "max_tool_rounds": round_limit, "max_calls_per_round": calls_limit}
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
                )
                headroom = (
                    int(
                        (profile["context_window"] - profile["response_tokens"] - 1)
                        / original_meta["calibration_factor"]
                    )
                    - base_meta["estimated_tokens"]
                    - 128
                )
                working_limit = min(
                    bot.get("tool_working_set_tokens", 6000),
                    max(64, headroom),
                )
                extras, working_meta = await asyncio.to_thread(
                    bound_exchanges, extras, self.contexts.estimate, working_limit
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
                )
                meta.update(
                    checkpoint=original_meta["checkpoint"],
                    through_sequence=through,
                    calibration_factor=original_meta["calibration_factor"],
                    document_task=document_started and task_plugin == "document_site",
                    extended_task=document_started,
                    calls_per_round=calls_limit,
                    task_plugin=task_plugin,
                    tool_working_set={**working_meta, "token_limit": working_limit},
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
                self.store.execute(
                    "UPDATE contexts SET estimated_tokens=? WHERE bot_id=? AND channel_id=?",
                    (meta["estimated_tokens"], bot["id"], channel_id),
                )
                result = await budgeted(
                    self.pool.complete(
                        bot=bot,
                        profile=profile,
                        messages=messages,
                        tools=available_tools,
                        turn_id=turn_id,
                        context=meta,
                    )
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
                if not result.tool_calls:
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
                        extras.append({"role": "system", "content": REPLY_REPAIR})
                        round_index += 1
                        continue
                    if result.content:
                        await self.deliver(
                            bot,
                            profile,
                            provider,
                            context,
                            result.content,
                            reply_to,
                            artifact_ids,
                            request_id=result.request_id,
                            draft_deadline=document_deadline,
                        )
                        status, decision = "sent", "reply" if reply_to else "speak"
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
                names = [call["function"]["name"] for call in result.tool_calls]
                terminal = any(name in ("council_speak", "council_silence") for name in names)
                batch_error = None
                current_bot = self.store.get("bots", bot["id"])
                if "council_silence" in names and (
                    not silence_tools or not current_bot or not current_bot.get("allow_silence", True)
                ):
                    batch_error = "Intentional silence is disabled for this bot; council_silence was not executed. Finish with ordinary assistant text. No actions in this batch were executed."
                elif terminal and len(names) != 1:
                    batch_error = "A silence decision (or obsolete reply call) must be the only tool call; no partial actions were executed"
                elif len(names) > calls_limit:
                    batch_error = f"Too many tool calls: maximum {calls_limit} per round; none executed"
                elif not terminal and round_index >= round_limit:
                    batch_error = "Tool round budget exhausted; write an ordinary text answer" + (
                        " or call council_silence alone" if silence_tools else ""
                    )
                extras.append(result.message())
                finished = False
                for call in result.tool_calls:
                    name = call["function"]["name"]
                    definition = (
                        SILENCE
                        if name == "council_silence"
                        else ATTACH
                        if name == "discord_attach" and can_attach
                        else None
                    )
                    if batch_error:
                        spec = self.registry.specs.get(name) if self.registry.allowed(name, context) else None
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
                            try:
                                batch_args = parse_arguments(call["function"]["arguments"])
                            except (ValueError, TypeError) as exc:
                                argument_report = syntax_feedback(name, exc, parameters)
                            else:
                                argument_report = feedback(name, batch_args, parameters) or {
                                    "usage": usage(name, parameters, arguments=batch_args)
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
                        result_value = await budgeted(
                            self.registry.call_raw(name, call["function"]["arguments"], context, call["id"])
                        )
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
                    extras.append(
                        {"role": "tool", "tool_call_id": call["id"], "content": dumps(result_value)}
                    )
                if finished:
                    break
                round_index += 1
            else:
                raise ControlError(
                    "Tool round budget exhausted without a valid final answer; no further calls executed"
                )
            if status in ("sent", "silent"):
                self.store.execute(
                    "UPDATE contexts SET last_seen=?,updated_at=? WHERE bot_id=? AND channel_id=?",
                    (through, time.time(), bot["id"], channel_id),
                )
                self.store.execute("UPDATE bot_runtime SET error=NULL WHERE bot_id=?", (bot["id"],))
        except TurnSuperseded:
            status, error = "cancelled", "New human-directed message superseded an unsent draft"
        except asyncio.CancelledError:
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
        finally:
            self.human_superseded.discard(turn_id)
            if typing_task:
                await cancel_and_wait(typing_task)
            self.store.execute(
                "UPDATE turns SET status=?,decision=?,error=?,ended_at=? WHERE id=?",
                (status, decision, error, time.time(), turn_id),
            )
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
    ):
        destination = routing["target_channel_id"] if routing else context.channel_id

        def check_deadline():
            if draft_deadline is not None and asyncio.get_running_loop().time() >= draft_deadline:
                raise ControlError("Document task time budget exhausted before Discord dispatch")
            if routing:
                self.registry.discord_dispatch.check(context, routing)

        check_deadline()
        content = self.vault.redact(strip_reasoning(content))
        if not content:
            raise ControlError("No visible content remained after removing reasoning")
        paths = [self.registry.resolve_artifact(artifact_id, context) for artifact_id in artifact_ids]
        if len(content) > 12000:
            raise ControlError("Council contribution exceeds 12,000 characters")
        request = (
            self.store.one(
                "SELECT model,input_tokens,output_tokens,ttft_ms,duration_ms FROM requests WHERE id=? AND bot_id=? AND turn_id=? AND purpose='generation' AND status='completed'",
                (request_id, bot["id"], context.turn_id),
            )
            if request_id
            else None
        )
        footer = render_footer(bot, profile, provider, request, redact=self.vault.redact)
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
                dumps(artifact_ids),
                "pending",
                time.time(),
                dumps(routing or {}),
            ),
        )
        self.store.emit(
            "delivery.queued",
            {
                "outbox_id": outbox_id,
                "content": content,
                "reply_to": reply_to,
                "artifact_ids": artifact_ids,
                "footer": footer,
                "request_id": request_id,
                **({"routing": routing} if routing else {}),
            },
            bot_id=bot["id"],
            turn_id=context.turn_id,
        )
        try:
            if self.attention(bot, context.channel_id):
                raise TurnSuperseded("New directed human input is waiting; regenerate from current context")
            priority = self.store.one(
                "SELECT 1 FROM human_attention_claims WHERE bot_id=? AND turn_id=? LIMIT 1",
                (bot["id"], context.turn_id),
            )
            # A personal cooldown must never hold the channel send lock. Another
            # bot may be responding to a human while this draft waits its cadence.
            wait = (
                0
                if priority
                else self.store.runtime(bot["id"])["last_sent"] + bot["cooldown_seconds"] - time.time()
            )
            if wait > 0:
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
            async with self.room_locks[destination]:
                room = (
                    self.registry.discord_dispatch.check(context, routing)
                    if routing
                    else self.room_for(bot, context.channel_id)
                )
                gap = room["send_gap_seconds"] if room else 1.5
                wait = self.room_last[destination] + gap - time.time()
                if wait > 0:
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
                # Stop before dispatch, but never cancel an in-flight Discord send
                # solely because the drafting deadline passed: acceptance may be uncertain.
                check_deadline()
                if self.attention(bot, context.channel_id):
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
                discord_id = await self.transport.send(
                    bot,
                    destination,
                    content,
                    reply_to,
                    paths,
                    nonce=outbox_id,
                    footer=footer,
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
                    {"outbox_id": outbox_id, "discord_id": discord_id, "content": content},
                    bot_id=bot["id"],
                    turn_id=context.turn_id,
                )
        except (Exception, asyncio.CancelledError) as exc:
            row = self.store.one("SELECT status FROM outbox WHERE id=?", (outbox_id,))
            uncertain = row["status"] == "sending" and (not isinstance(exc, DeliveryError) or exc.uncertain)
            status = (
                "unknown"
                if uncertain
                else "suppressed"
                if isinstance(exc, (asyncio.CancelledError, TurnSuperseded))
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
        if tasks:
            await cancel_and_wait(*tasks)

    async def close(self):
        self.closed = True
        if self.task:
            await cancel_and_wait(self.task)
        await self.cancel(list(self.tasks), "Shutdown")
