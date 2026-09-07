from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass

from jsonschema import Draft202012Validator

from .context import ContextBuilder
from .models import ControlError, OWNER_ID
from .plugins import SILENCE, SPEAK, ToolContext
from .provider import strip_reasoning
from .store import dumps, uid


@dataclass
class DeliveryError(Exception):
    message: str
    uncertain: bool = False

    def __str__(self):
        return self.message


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

    def start(self):
        self.task = asyncio.create_task(self.loop(), name="council-scheduler")

    def configuration(self, bot):
        profile = self.store.get("profiles", bot["model_profile_id"])
        provider = self.store.get("providers", profile["provider_id"]) if profile else None
        return profile, provider

    def available(self, bot, *, manual=False):
        settings = self.store.get("settings", "global")
        profile, provider = self.configuration(bot)
        return bool(settings["enabled"] and (bot["enabled"] or manual) and profile and provider and provider["enabled"])

    def valid(self, bot, profile, provider, *, manual=False):
        current = self.store.get("bots", bot["id"])
        return bool(current and self.available(current, manual=manual) and current["revision"] == bot["revision"] and
                    self.store.get("profiles", profile["id"])["revision"] == profile["revision"] and
                    self.store.get("providers", provider["id"])["revision"] == provider["revision"])

    def channel_allowed(self, bot, channel_id):
        context = self.store.one("SELECT 1 FROM contexts WHERE bot_id=? AND channel_id=?", (bot["id"], channel_id))
        if not context:
            return False
        room_ids = {r["id"] for r in self.store.list("rooms") if r["id"] in bot["room_ids"]}
        row = self.store.one("SELECT room_id,author_id FROM messages WHERE channel_id=? ORDER BY seq DESC LIMIT 1", (channel_id,))
        if row and row["room_id"] in room_ids:
            return True
        # Hortator control-channel/owner DM namespaces are created only by the owner-gated connector.
        return bot["role"] == "hortator" and bool(row) and row["room_id"] == f"owner:{bot['id']}"

    def pick_channel(self, bot):
        contexts = self.store.rows("SELECT * FROM contexts WHERE bot_id=? ORDER BY updated_at ASC", (bot["id"],))
        candidates = []
        for context in contexts:
            channel_id = context["channel_id"]
            if not self.channel_allowed(bot, channel_id):
                continue
            latest = self.store.one("SELECT max(seq) AS seq FROM messages WHERE channel_id=?", (channel_id,))["seq"] or 0
            unseen = latest > context["last_seen"]
            if bot["role"] == "hortator":
                unseen = bool(self.store.one("SELECT 1 FROM messages WHERE channel_id=? AND seq>? AND author_id=? LIMIT 1", (channel_id, context["last_seen"], OWNER_ID)))
            if not unseen and (not bot["evaluate_when_idle"] or bot["role"] == "hortator"):
                continue
            if latest:
                candidates.append((not unseen, context["updated_at"], channel_id))
        return min(candidates)[2] if candidates else None

    def budget_error(self, bot):
        now = time.time()
        n = self.store.one("SELECT count(*) AS n FROM turns WHERE bot_id=? AND started_at>?", (bot["id"], now-3600))["n"]
        if n >= bot["hourly_turn_limit"]:
            return "Hourly activation limit reached"
        if bot["daily_cost_limit"] is not None:
            day = now - now % 86400
            cost = self.store.one("SELECT coalesce(sum(cost),0) AS cost, sum(CASE WHEN status='completed' AND cost IS NULL THEN 1 ELSE 0 END) AS unknown FROM requests WHERE bot_id=? AND started_at>=?", (bot["id"], day))
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
        for bot in self.store.list("bots"):
            if bot["id"] in self.tasks or len(self.tasks) >= settings["max_concurrent_turns"]:
                continue
            if not self.available(bot):
                continue
            state = self.store.runtime(bot["id"])
            if state["gateway_status"] != "online" or state["next_at"] > now:
                continue
            profile, provider = self.configuration(bot)
            health = self.store.health(provider["id"])
            if health["circuit_until"] > now:
                continue
            channel_id = self.pick_channel(bot)
            if not channel_id:
                continue
            error = self.budget_error(bot)
            if error:
                self.store.execute("UPDATE bot_runtime SET next_at=?,error=? WHERE bot_id=?", (now + 60, error, bot["id"]))
                self.store.emit("activation.budget_blocked", {"error": error}, bot_id=bot["id"], level="warning")
                continue
            self.launch(bot, channel_id)

    async def loop(self):
        while not self.closed:
            try:
                await self.tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.store.emit("runtime.scheduler_error", {"error": str(exc)}, level="error")
            await asyncio.sleep(.5)

    def launch(self, bot, channel_id, *, compact_only=False):
        if bot["id"] in self.tasks:
            raise ControlError("Bot already has an active turn", 409)
        if not self.available(bot, manual=compact_only):
            raise ControlError("Council, bot or provider is paused")
        if not self.channel_allowed(bot, channel_id):
            raise ControlError("Channel is outside this bot's configured scope")
        turn_id = uid("turn_")
        profile, provider = self.configuration(bot)
        self.store.execute("INSERT INTO turns(id,bot_id,channel_id,profile_id,provider_id,model,started_at,status,trigger,revision) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (turn_id, bot["id"], channel_id, profile["id"], provider["id"], profile["model"], time.time(), "running", "manual_compaction" if compact_only else "owner_message" if bot["role"] == "hortator" else "timer", bot["revision"]))
        self.store.execute("UPDATE bot_runtime SET next_at=? WHERE bot_id=?", (time.time() + bot["interval_seconds"], bot["id"]))
        self.store.emit("turn.started", {"channel_id": channel_id, "profile_id": profile["id"]}, bot_id=bot["id"], turn_id=turn_id)
        task = asyncio.create_task(self.run(bot, profile, provider, channel_id, turn_id, compact_only), name=f"bot:{bot['id']}:{turn_id}")
        self.tasks[bot["id"]] = task
        task.add_done_callback(lambda completed: self.tasks.pop(bot["id"], None) if self.tasks.get(bot["id"]) is completed else None)
        return turn_id

    async def run(self, bot, profile, provider, channel_id, turn_id, compact_only):
        status, decision, error = "failed", None, None
        context = ToolContext(bot, channel_id, turn_id, owner_verified=bot["role"] == "hortator")
        try:
            tools = [SPEAK, SILENCE] + self.registry.schemas(context)
            rows, summary, through, original_meta = await self.contexts.prepare(bot, profile, channel_id, turn_id, tools, force=compact_only)
            if compact_only:
                status, decision = "completed", "compacted"
                return
            extras = []
            for round_index in range(bot["max_tool_rounds"] + 1):
                if not self.valid(bot, profile, provider):
                    raise asyncio.CancelledError()
                available_tools = [SPEAK, SILENCE] + (self.registry.schemas(context) if round_index < bot["max_tool_rounds"] else [])
                messages, meta = self.contexts.assemble(bot, profile, channel_id, rows, summary, round_index, extras, available_tools)
                meta.update(checkpoint=original_meta["checkpoint"], through_sequence=through,
                            calibration_factor=original_meta["calibration_factor"])
                if meta["estimated_tokens"] * original_meta["calibration_factor"] + profile["response_tokens"] >= profile["context_window"]:
                    raise ControlError("Tool results would exceed the context budget; turn stopped without discarding history")
                result = await self.pool.complete(bot=bot, profile=profile, messages=messages, tools=available_tools,
                                                  turn_id=turn_id, context=meta)
                if result.finish_reason in ("length", "content_filter"):
                    raise ControlError(f"Provider stopped with {result.finish_reason}; incomplete output was withheld")
                if not result.tool_calls:
                    if result.content:
                        await self.deliver(bot, profile, provider, context, result.content, None, [])
                        status, decision = "sent", "speak"
                    else:
                        status, decision = "silent", "empty_completion"
                    break
                names = [call["function"]["name"] for call in result.tool_calls]
                if "council_speak" in names or "council_silence" in names:
                    if len(names) != 1:
                        raise ControlError("Terminal speak/silence decision must be the only tool call; no partial actions were executed")
                    call = result.tool_calls[0]
                    args = json.loads(call["function"]["arguments"])
                    definition = SPEAK if names[0] == "council_speak" else SILENCE
                    errors = list(Draft202012Validator(definition["function"]["parameters"]).iter_errors(args))
                    if errors:
                        raise ControlError("Invalid council decision: " + errors[0].message)
                    if names[0] == "council_silence":
                        status, decision = "silent", "listen"
                        self.store.emit("decision.silence", {"label": args["label"]}, bot_id=bot["id"], turn_id=turn_id)
                    else:
                        reply_to = args.get("reply_to") or None
                        if reply_to and reply_to not in {row["discord_id"] for row in rows}:
                            raise ControlError("Reply target is not in this turn's channel context")
                        await self.deliver(bot, profile, provider, context, args["content"], reply_to, args.get("artifact_ids", []))
                        status, decision = "sent", "reply" if reply_to else "speak"
                    break
                if round_index >= bot["max_tool_rounds"]:
                    raise ControlError("Model called a tool after its round budget was exhausted")
                if len(result.tool_calls) > bot["max_calls_per_round"]:
                    raise ControlError("Too many tool calls in one round; none executed")
                if len({c["id"] for c in result.tool_calls}) != len(result.tool_calls):
                    raise ControlError("Provider returned duplicate tool-call IDs")
                extras.append(result.message())
                for call in result.tool_calls:
                    try:
                        args = json.loads(call["function"]["arguments"])
                    except ValueError:
                        result_value = {"error": "Tool arguments must be valid JSON"}
                        self.store.emit("tool.failed", {"name": call["function"]["name"], "call_id": call["id"], **result_value}, bot_id=bot["id"], turn_id=turn_id, level="error")
                    else:
                        result_value = await self.registry.call(call["function"]["name"], args, context, call["id"])
                    extras.append({"role": "tool", "tool_call_id": call["id"], "content": dumps(result_value)})
            if status in ("sent", "silent"):
                self.store.execute("UPDATE contexts SET last_seen=?,updated_at=? WHERE bot_id=? AND channel_id=?", (through, time.time(), bot["id"], channel_id))
                self.store.execute("UPDATE bot_runtime SET error=NULL WHERE bot_id=?", (bot["id"],))
        except asyncio.CancelledError:
            status, error = "cancelled", "Stopped by operator, configuration change, or shutdown"
        except Exception as exc:
            status, error = "failed", self.vault.redact(f"{type(exc).__name__}: {exc}")[:3000]
            self.store.execute("UPDATE bot_runtime SET error=? WHERE bot_id=?", (error, bot["id"]))
            self.store.emit("turn.failed", {"error": error, "provider_id": provider["id"], "profile_id": profile["id"]}, bot_id=bot["id"], turn_id=turn_id, level="error")
        finally:
            self.store.execute("UPDATE turns SET status=?,decision=?,error=?,ended_at=? WHERE id=?", (status, decision, error, time.time(), turn_id))
            # No catch-up bursts; activation interval restarts when work settles.
            current = self.store.get("bots", bot["id"])
            interval = current["interval_seconds"] if current else bot["interval_seconds"]
            self.store.execute("UPDATE bot_runtime SET next_at=? WHERE bot_id=?", (time.time() + interval, bot["id"]))
            self.store.emit("turn.ended", {"status": status, "decision": decision, "error": error}, bot_id=bot["id"], turn_id=turn_id)

    async def deliver(self, bot, profile, provider, context, content, reply_to, artifact_ids):
        content = self.vault.redact(strip_reasoning(content))
        if not content:
            raise ControlError("No visible content remained after removing reasoning")
        paths = [self.registry.resolve_artifact(artifact_id, context) for artifact_id in artifact_ids]
        if len(content) > 12000:
            raise ControlError("Council contribution exceeds 12,000 characters")
        outbox_id = uid("out_")
        self.store.execute("INSERT INTO outbox(id,turn_id,bot_id,channel_id,content,reply_to,artifacts,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (outbox_id, context.turn_id, bot["id"], context.channel_id, content, reply_to, dumps(artifact_ids), "pending", time.time()))
        self.store.emit("delivery.queued", {"outbox_id": outbox_id, "content": content, "reply_to": reply_to, "artifact_ids": artifact_ids}, bot_id=bot["id"], turn_id=context.turn_id)
        try:
            async with self.room_locks[context.channel_id]:
                state = self.store.runtime(bot["id"])
                room = next((r for r in self.store.list("rooms") if r["id"] in bot["room_ids"] and
                             self.store.one("SELECT 1 FROM messages WHERE channel_id=? AND room_id=? LIMIT 1", (context.channel_id, r["id"]))), None)
                gap = room["send_gap_seconds"] if room else 1.5
                wait = max(state["last_sent"] + bot["cooldown_seconds"], self.room_last[context.channel_id] + gap) - time.time()
                if wait > 0:
                    self.store.emit("delivery.cooldown", {"outbox_id": outbox_id, "wait_seconds": wait}, bot_id=bot["id"], turn_id=context.turn_id)
                    await asyncio.sleep(wait)
                if not self.valid(bot, profile, provider):
                    raise asyncio.CancelledError()
                if not self.transport:
                    raise DeliveryError("Discord connector is unavailable")
                self.store.execute("UPDATE outbox SET status='sending' WHERE id=?", (outbox_id,))
                self.store.emit("delivery.sending", {"outbox_id": outbox_id}, bot_id=bot["id"], turn_id=context.turn_id)
                discord_id = await self.transport.send(bot, context.channel_id, content, reply_to, paths)
                sent = time.time()
                self.store.execute("UPDATE outbox SET status='sent',sent_at=?,discord_id=? WHERE id=?", (sent, discord_id, outbox_id))
                self.store.execute("UPDATE bot_runtime SET last_sent=? WHERE bot_id=?", (sent, bot["id"]))
                self.room_last[context.channel_id] = sent
                self.store.ingest(discord_id=discord_id, channel_id=context.channel_id, author_id=bot["application_id"],
                                  author_name=bot["name"], content=content, room_id=room["id"] if room else f"owner:{bot['id']}", bot_id=bot["id"], reply_to=reply_to)
                self.store.emit("delivery.sent", {"outbox_id": outbox_id, "discord_id": discord_id, "content": content}, bot_id=bot["id"], turn_id=context.turn_id)
        except (Exception, asyncio.CancelledError) as exc:
            row = self.store.one("SELECT status FROM outbox WHERE id=?", (outbox_id,))
            uncertain = row["status"] == "sending" and (not isinstance(exc, DeliveryError) or exc.uncertain)
            status = "unknown" if uncertain else "suppressed" if isinstance(exc, asyncio.CancelledError) else "failed"
            if uncertain:
                self.store.execute("UPDATE bot_runtime SET last_sent=? WHERE bot_id=?", (time.time(), bot["id"]))
            self.store.execute("UPDATE outbox SET status=?,error=? WHERE id=?", (status, self.vault.redact(str(exc) or "Cancelled"), outbox_id))
            self.store.emit("delivery." + status, {"outbox_id": outbox_id, "error": str(exc) or "Cancelled", "content": content}, bot_id=bot["id"], turn_id=context.turn_id, level="warning" if status == "suppressed" else "error")
            raise

    async def cancel(self, bot_ids, reason):
        tasks = []
        for bot_id in bot_ids:
            task = self.tasks.get(bot_id)
            if task and task is not asyncio.current_task():
                task.cancel()
                tasks.append(task)
            self.store.execute("UPDATE outbox SET status='suppressed',error=? WHERE bot_id=? AND status='pending'", (reason, bot_id))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.cancel(list(self.tasks), "Shutdown")
