from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import tiktoken

from .concurrency import error_text
from .models import ControlError, OWNER_ID
from .memory_budget import budget_for
from .store import dumps
from .vision import (
    ImageCache,
    IMAGE_TOKEN_RESERVE,
    image_count,
    image_bytes,
    image_limits,
    image_limits_exceeded,
)
from .prompt_templates import layer as prompt_layer, render
from .addressing import for_viewer
from .vision_turn import select_images, current_inputs, with_inputs, attachment_metadata


class ContextBuilder:
    def __init__(self, store, pool, global_memory=None, application_emojis=None):
        self.store, self.pool = store, pool
        self.global_memory = global_memory
        self.application_emojis = application_emojis
        self.images = ImageCache(store)
        self.encoder = tiktoken.get_encoding("cl100k_base")
        self.token_slots = asyncio.Semaphore(2)

    def estimate(self, value):
        # Explicit approximation: alternate model tokenizers can differ. Actual usage stays separate.
        return int(len(self.encoder.encode(dumps(value), disallowed_special=())) * 1.15) + 32

    def estimate_request(self, messages, tools):
        return (
            self.estimate({"messages": messages, "tools": tools})
            + len(messages) * 8
            + image_count(messages) * IMAGE_TOKEN_RESERVE
        )

    async def estimate_async(self, messages, tools):
        # Only detached value snapshots enter worker threads. SQLite/vault and
        # state changes always remain on the event-loop owner thread.
        async with self.token_slots:
            return await asyncio.to_thread(self.estimate_request, messages, tools)

    async def conversation_async(self, rows, bot, inputs=()):
        result = []
        for offset in range(0, len(rows), 64):
            result.extend(self.conversation(rows[offset : offset + 64], bot, inputs))
            await asyncio.sleep(0)
        return result

    def count_summary_tokens(self, text):
        # Local retention accounting, not provider billing or the model's native
        # tokenizer. Count only the exact text that would become context.
        return len(self.encoder.encode(text, disallowed_special=()))

    async def summary_tokens_async(self, text):
        async with self.token_slots:
            return await asyncio.to_thread(self.count_summary_tokens, text)

    def compaction_batch(self, prefix, rows, transcript, budget, profile=None):
        # No database access. Probe the complete prefix first (the usual case),
        # then bisect instead of re-tokenizing each growing prefix O(n²) times.
        probes = 0

        def candidate(count):
            nonlocal probes
            probes += 1
            template = (profile or {}).get(
                "_compaction_transcript", {"role": "user", "content": "{transcript}"}
            )
            payload = prefix + [
                {
                    "role": template["role"],
                    "content": render(
                        template["content"],
                        {
                            **(profile or {}).get("_compaction_values", {}),
                            "transcript": dumps(transcript[:count]),
                        },
                    ),
                }
            ]
            estimate = self.estimate_request(payload, [])
            return payload, estimate, estimate <= budget

        high = len(rows)
        payload, estimate, fits = candidate(high)
        if fits:
            return high, payload, estimate, probes
        low, selected, selected_estimate = 0, None, None
        while low + 1 < high:
            count = (low + high) // 2
            payload, estimate, fits = candidate(count)
            if fits:
                low, selected, selected_estimate = count, payload, estimate
            else:
                high = count
        return low, selected, selected_estimate, probes

    def prompt_values(self, bot, values=None):
        settings = self.store.get("settings", "global")
        base = {
            "bot_name": bot["name"],
            "bot_id": bot["id"],
            "boss_id": OWNER_ID,
            "discord_user_id": self.pool.vault.get(f"bot/{bot['id']}/user_id")
            or bot["application_id"]
            or "unconfigured",
            "global_prompt": settings["global_prompt"],
            "persona": bot["persona"],
            "timezone": settings["timezone"],
        }
        return {**base, **(values or {})}

    def prompt(self, bot, key, values=None, *, variant="", raw=False):
        return prompt_layer(self.store, bot, key, self.prompt_values(bot, values), variant=variant, raw=raw)

    def layers(self, bot, channel_id):
        layers = []
        for key in ("identity", "tool_guidance", "image_guidance", "image_disabled", "director", "universal"):
            if key == "image_disabled" and bot.get("allow_images", True):
                continue
            if key == "director" and bot["role"] != "hortator":
                continue
            layers.append(self.prompt(bot, key))
        for prompt_id in bot["prompt_ids"]:
            prompt = self.store.get("prompts", prompt_id)
            if prompt:
                layers.append(
                    {
                        "id": f"prompt:{prompt_id}",
                        "template_id": prompt_id,
                        "revision": prompt["revision"],
                        "role": prompt.get("role", "system"),
                        "content": prompt["content"],
                    }
                )
        layers.append(self.prompt(bot, "persona"))
        layers.append(
            self.prompt(bot, "silence_policy", variant="" if bot.get("allow_silence", True) else "disabled")
        )
        plugin = self.store.get("plugins", "memory") or {}
        current = self.store.get("bots", bot["id"]) or {}
        enabled = (
            plugin.get("enabled", False)
            and "memory" in bot["enabled_plugins"]
            and "memory" in current.get("enabled_plugins", [])
        )
        if enabled:
            budget = budget_for(self.store, bot, channel_id)
            values = {k: f"{v:,}" if type(v) is int else v for k, v in budget.items()}
            layers.append(
                self.prompt(
                    bot, "memory_budget", values, variant="over" if budget["must_consolidate"] else ""
                )
            )
            notes = self.store.rows(
                "SELECT key,value FROM memories WHERE bot_id=? AND channel_id=? ORDER BY key",
                (bot["id"], channel_id),
            )
            if notes:
                layers.append(self.prompt(bot, "memory", {"notes": dumps(notes)}))
        if self.global_memory is not None:
            layers.extend(self.global_memory.prompt_layers(bot))
        return [item for item in layers if item and item["content"].strip()]

    def dynamic_layers(self, bot, profile, channel_id, round_index, estimated):
        state = self.store.runtime(bot["id"])
        timezone = self.store.get("settings", "global")["timezone"]
        values = {
            "now": datetime.now(ZoneInfo(timezone)).isoformat(),
            "timezone": timezone,
            "bot_name": bot["name"],
            "boss_id": OWNER_ID,
            "channel_id": channel_id,
            "round": round_index,
            "rounds_remaining": max(0, bot["max_tool_rounds"] - round_index),
            "allow_silence": bot.get("allow_silence", True),
            "allow_images": bot.get("allow_images", True),
            "context_tokens": estimated,
            "context_window": profile["context_window"],
            "seconds_since_last_message": round(time.time() - state["last_sent"])
            if state["last_sent"]
            else "never",
        }
        if "active_task_budget" in bot:
            values["task_budget"] = bot["active_task_budget"]
        if bot.get("activation"):
            values["activation"] = bot["activation"]
        if bot.get("background_completion"):
            values["background_completion"] = bot["background_completion"]
        if bot.get("background_handoff"):
            values["background_handoff"] = bot["background_handoff"]
        if self.application_emojis is not None:
            values["application_emojis"] = self.application_emojis.prompt(bot)
        custom = render(bot["dynamic_prompt"], values)
        facts = dict(values)
        for key in ("background_completion", "background_handoff"):
            if key in facts:
                facts[key] = {
                    "progress": facts[key].get("progress"),
                    "origin_turn_id": facts[key].get("origin_turn_id"),
                }
        values.update(
            runtime_facts=dumps(facts),
            dynamic_prompt=custom,
            silence_action=" or call council_silence alone" if bot.get("allow_silence", True) else "",
        )
        layers = [
            item for key in ("runtime_facts", "dynamic_prompt") if (item := self.prompt(bot, key, values))
        ]
        if bot.get("background_completion"):
            item = self.prompt(
                bot, "background_completion", {"background_job": dumps(bot["background_completion"])}
            )
            if item:
                layers.append(item)
        background_state = bot.get("background_handoff") or bot.get("background_completion")
        if background_state:
            if not bot.get("background_handoff"):
                # Full receipts already live in background_completion. Do not
                # repeat them three times through facts and guidance layers.
                background_state = {
                    "progress": background_state.get("progress"),
                    "newly_settled": len(background_state.get("jobs", [])),
                    "previously_claimed": background_state.get("previously_claimed", 0),
                }
            item = self.prompt(
                bot,
                "background_updates",
                {
                    "background_phase": "dispatch" if bot.get("background_handoff") else "completion",
                    "background_state": dumps(background_state),
                },
            )
            if item:
                layers.append(item)
        return layers

    def dynamic(self, bot, profile, channel_id, round_index, estimated):
        return "\n".join(
            item["content"] for item in self.dynamic_layers(bot, profile, channel_id, round_index, estimated)
        )

    def visible_rows(self, bot, rows):
        return self.store.filter_context_rows(bot["id"], rows)

    def conversation(self, rows, bot=None, inputs=()):
        if bot is not None:
            rows = self.visible_rows(bot, rows)
        timezone = ZoneInfo(self.store.get("settings", "global")["timezone"])
        now = time.time()
        return [
            {
                "id": row["discord_id"],
                "seq": row["seq"],
                "at": datetime.fromtimestamp(row["at"], timezone).isoformat(),
                "seconds_ago": max(0, round(now - row["at"])),
                "author_id": row["author_id"],
                "author": row["author_name"],
                "bot_id": row["bot_id"],
                "is_boss": row["author_id"] == OWNER_ID
                and for_viewer(self.store, row)["author_kind"] in ("human", "unknown"),
                "reply_to": row["reply_to"],
                "addressing": for_viewer(self.store, row, bot["id"] if bot else None),
                "replyable": row["discord_id"].isdigit(),
                "content": "[message deleted]" if row["deleted"] else row["content"],
                "attachments": []
                if row["deleted"]
                else attachment_metadata(row["discord_id"], row["attachments"], inputs),
            }
            for row in rows
        ]

    async def assemble(
        self, bot, profile, channel_id, rows, summary, round_index=0, extras=None, tools=None, image_plan=None
    ):
        rows = self.visible_rows(bot, rows)
        if image_plan is None or not bot.get("allow_images", True):
            image_plan = select_images(
                rows,
                self.store.context(bot["id"], channel_id)["last_seen"],
                profile,
                allow_images=bot.get("allow_images", True),
            )
        inputs = current_inputs(self.store, channel_id, image_plan)
        layers = self.layers(bot, channel_id)
        messages = []

        def append(item):
            if not item:
                return
            # Keep separate roles for operator-selected templates, compact adjacent
            # instruction layers in one message to preserve the previous layout.
            if messages and messages[-1]["role"] == item["role"] and isinstance(messages[-1]["content"], str):
                messages[-1]["content"] += "\n\n" + item["content"]
            else:
                messages.append({"role": item["role"], "content": item["content"]})

        for item in layers:
            append(item)
        if summary:
            item = self.prompt(bot, "summary", {"summary": summary})
            if item:
                layers.append(item)
                messages.append({"role": item["role"], "content": item["content"]})
        transcript = await self.conversation_async(rows, bot, inputs)
        values = {
            "transcript": dumps(transcript),
            "latest_message": dumps(transcript[-1]) if transcript else "",
            "latest_content": transcript[-1]["content"] if transcript else "",
            "image_omissions": "\nImage input budget omitted these new attachments (metadata remains; do not claim to see their pixels): "
            + dumps(image_plan["budget_omitted"])
            if image_plan["budget_omitted"]
            else "",
        }
        item = self.prompt(bot, "transcript", values)
        included_rows = []
        if item:
            if "transcript" in item["variables"]:
                included_rows = rows
            elif {"latest_message", "latest_content"}.intersection(item["variables"]):
                included_rows = rows[-1:]
            selected_ids = {r["discord_id"] for r in included_rows}
            # Text compaction may remove a fresh message from rows while its
            # selected pixels remain valid for this same turn. Full conversation
            # input preserves that plan; a last-message-only template narrows it.
            selected_inputs = (
                inputs
                if "transcript" in item["variables"]
                else [entry for entry in inputs if entry["message_id"] in selected_ids]
            )
            layers.append(item)
            messages.append({"role": item["role"], "content": with_inputs(item["content"], selected_inputs)})
        if extras:
            messages.extend(extras)
        estimated = await self.estimate_async(messages, tools or [])
        dynamic = self.dynamic_layers(bot, profile, channel_id, round_index, estimated)
        # The changing tail follows tool exchanges, regardless of static roles.
        for index, item in enumerate(dynamic):
            if index == 0:
                messages.append({"role": item["role"], "content": item["content"]})
            else:
                append(item)
        layers.extend(dynamic)
        if not messages:
            raise ControlError(
                "All prompt layers are disabled or empty; enable conversation input or supply a prompt"
            )
        estimate = await self.estimate_async(messages, tools or [])
        meta = {
            "channel_id": channel_id,
            "message_ids": [r["discord_id"] for r in included_rows],
            "message_sequences": [r["seq"] for r in included_rows],
            "source_message_ids": [r["discord_id"] for r in rows],
            "disabled_prompt_layers": bot.get("disabled_prompt_layers", []),
            "prompt_layers": [
                {**layer, "sha256": hashlib.sha256(layer["content"].encode()).hexdigest()} for layer in layers
            ],
            "summary": summary if any(p["id"] == "summary" for p in layers) else "",
            "stored_summary_present": bool(summary),
            "estimated_tokens": estimate,
            "estimator": "cl100k_base + 15% + framing + 4096 reserve/image; approximate, not provider image usage",
            "image_count": image_count(messages),
            "image_plan": image_plan,
            "context_window": profile["context_window"],
            "output_reserve": profile["response_tokens"],
            "round": round_index,
            "rounds_remaining": max(0, bot["max_tool_rounds"] - round_index),
        }
        return messages, meta

    async def prepare(self, bot, profile, channel_id, turn_id, tools, force=False):
        context = self.store.context(bot["id"], channel_id)
        tail = (
            self.store.one("SELECT max(seq) AS seq FROM messages WHERE channel_id=?", (channel_id,))["seq"]
            or 0
        )
        # Page through all uncompressed input. Do not silently drop old messages when a busy room grows.
        rows = []
        cursor = context["checkpoint"]
        while cursor < tail:
            page = self.store.transcript(channel_id, after=cursor, through=tail, limit=1000)
            if not page:
                break
            rows.extend(self.store.filter_context_rows(bot["id"], page))
            cursor = page[-1]["seq"]
            await asyncio.sleep(0)
        if bot.get("allow_images", True):
            try:
                async with asyncio.timeout(60):
                    await self.images.prepare_rows(
                        [r for r in rows if r["seq"] > context["last_seen"]], bot_id=bot["id"]
                    )
            except TimeoutError as exc:
                raise ControlError(
                    "New-message image capture exceeded 60 seconds; cached progress is retained, retry preparation"
                ) from exc
        image_plan = select_images(
            rows, context["last_seen"], profile, allow_images=bot.get("allow_images", True)
        )
        messages, meta = await self.assemble(
            bot, profile, channel_id, rows, context["summary"], tools=tools, image_plan=image_plan
        )
        if image_plan["historical_images"] or image_plan["budget_omitted"] or image_plan["disabled_images"]:
            self.store.emit(
                "context.image_selection",
                {
                    "channel_id": channel_id,
                    "policy": image_plan["policy"],
                    "selected_images": len(image_plan["inputs"]),
                    "selected_image_bytes": sum(p["image"]["size"] for p in image_plan["inputs"]),
                    "historical_images": image_plan["historical_images"],
                    "disabled_images": image_plan["disabled_images"],
                    "budget_omitted_images": len(image_plan["budget_omitted"]),
                    "image_limit": image_limits(profile)[0],
                    "image_byte_limit": image_limits(profile)[1],
                    "omission_reasons": sorted(
                        {r for a in image_plan["budget_omitted"] for r in a["reasons"]}
                    ),
                    "reason": (
                        "Image inputs disabled for this bot; attachments remain metadata only"
                        if not bot.get("allow_images", True)
                        else "Historical pixels expire after a handled turn; extra new images remain metadata only"
                    ),
                },
                bot_id=bot["id"],
                turn_id=turn_id,
                level="warning" if image_plan["budget_omitted"] else "debug",
            )
        threshold = min(
            int(profile["context_window"] * profile["compact_threshold"]),
            profile["context_window"] - profile["response_tokens"],
        )
        # Calibrate conservatively from this bot/profile's latest reported generation usage.
        measured = self.store.one(
            "SELECT input_tokens,context FROM requests WHERE bot_id=? AND profile_id=? AND model=? AND json_extract(context,'$.profile_revision')=? AND purpose='generation' AND input_tokens IS NOT NULL ORDER BY started_at DESC LIMIT 1",
            (bot["id"], profile["id"], profile["model"], profile["revision"]),
        )
        factor = 1.0
        if measured:
            import json

            previous_estimate = json.loads(measured["context"]).get("estimated_tokens") or 1
            factor = max(1.0, measured["input_tokens"] / previous_estimate)
        meta["calibration_factor"] = factor
        max_images, max_image_bytes = image_limits(profile)
        triggers = []
        if force:
            triggers.append("manual")
        if meta["estimated_tokens"] * factor >= threshold:
            triggers.append("token_threshold")
        if image_count(messages) > max_images:
            triggers.append("image_count")
        if image_bytes(messages) > max_image_bytes:
            triggers.append("image_bytes")
        if triggers:
            if not rows:
                raise ControlError(
                    "No uncompacted messages to summarize; shorten the prompts or scoped memory"
                )
            original_estimate = meta["estimated_tokens"]
            keep = min(profile["keep_recent_messages"], max(0, len(rows) - 1))
            # If the tail itself exceeds the target, compact progressively more of it.
            while keep > 0:
                tail_messages, tail_meta = await self.assemble(
                    bot,
                    profile,
                    channel_id,
                    rows[-keep:],
                    context["summary"],
                    tools=tools,
                    image_plan=image_plan,
                )
                if tail_meta["estimated_tokens"] * factor + profile[
                    "summary_tokens"
                ] < threshold and not image_limits_exceeded(tail_messages, profile):
                    break
                keep //= 2
            old_rows, recent = (rows[:-keep], rows[-keep:]) if keep else (rows, [])
            summary = context["summary"]
            pending = list(old_rows)
            compaction_id = "cmp_" + turn_id
            last_request_id = None
            output_cap = profile.get("compaction_max_tokens")
            output_policy = "explicit_max_tokens" if output_cap is not None else "provider_default"
            self.store.emit(
                "compaction.started",
                {
                    "compaction_id": compaction_id,
                    "before_tokens": original_estimate,
                    "trigger": ",".join(triggers),
                    "calibrated_tokens": int(original_estimate * factor),
                    "token_threshold": threshold,
                    "calibration_factor": factor,
                    "image_count": image_count(messages),
                    "image_limit": max_images,
                    "image_bytes": image_bytes(messages),
                    "image_byte_limit": max_image_bytes,
                    "profile_id": profile["id"],
                    "profile_revision": profile.get("revision"),
                    "channel_id": channel_id,
                    "before_summary": summary,
                    "retained_summary_token_limit": profile["summary_tokens"],
                    "summary_tokenizer": "cl100k_base",
                    "compaction_output_policy": output_policy,
                    "compaction_max_tokens": output_cap,
                    "message_ids": [r["discord_id"] for r in old_rows],
                },
                bot_id=bot["id"],
                turn_id=turn_id,
            )
            try:
                while pending:
                    last_request_id = None
                    values = {
                        "summary": summary,
                        "summary_tokens": profile["summary_tokens"],
                        "channel_id": channel_id,
                    }
                    instruction = self.prompt(bot, "compaction_instructions", values)
                    retained = self.prompt(bot, "compaction_summary", values)
                    template = self.prompt(bot, "compaction_transcript", values, raw=True)
                    if not template or "transcript" not in template["variables"]:
                        raise ControlError(
                            "Compaction transcript prompt is disabled, empty or omits {transcript}; previous context retained"
                        )
                    if summary and (not retained or "summary" not in retained["variables"]):
                        raise ControlError(
                            "Compaction summary prompt is disabled, empty or omits {summary}; previous context retained"
                        )
                    prompt_layers = [item for item in (instruction, retained) if item]
                    prefix = [{"role": item["role"], "content": item["content"]} for item in prompt_layers]
                    batch_profile = {
                        **profile,
                        "_compaction_transcript": template,
                        "_compaction_values": self.prompt_values(bot, values),
                    }
                    output_reserve = max(profile["summary_tokens"], output_cap or 0)
                    budget = int((profile["context_window"] - output_reserve) / factor * 0.85)
                    prepared_at = time.perf_counter()
                    transcript = await self.conversation_async(pending, bot)
                    async with self.token_slots:
                        count, payload, estimated, probes = await asyncio.to_thread(
                            self.compaction_batch, prefix, pending, transcript, budget, batch_profile
                        )
                    if not count:
                        first = prefix + [
                            {
                                "role": template["role"],
                                "content": render(
                                    template["content"],
                                    {
                                        **batch_profile["_compaction_values"],
                                        "transcript": dumps(transcript[:1]),
                                    },
                                ),
                            }
                        ]
                        needed = await self.estimate_async(first, [])
                        raise ControlError(
                            f"Compaction cannot fit message {pending[0]['discord_id']} with the existing summary: "
                            f"{needed} estimated input tokens required, {budget} allowed. Compaction sends no image pixels. "
                            "Adjust Model profiles → Context & retained summary; previous context is retained."
                        )
                    batch = pending[:count]
                    self.store.emit(
                        "compaction.batch_prepared",
                        {
                            "compaction_id": compaction_id,
                            "message_count": count,
                            "duration_ms": (time.perf_counter() - prepared_at) * 1000,
                            "tokenization_probes": probes,
                            "estimated_tokens": estimated,
                        },
                        bot_id=bot["id"],
                        turn_id=turn_id,
                    )
                    result = await self.pool.complete(
                        bot=bot,
                        profile=profile,
                        messages=payload,
                        tools=[],
                        turn_id=turn_id,
                        purpose="compaction",
                        context={
                            "compaction_id": compaction_id,
                            "channel_id": channel_id,
                            "message_ids": [r["discord_id"] for r in batch],
                            "estimated_tokens": estimated,
                            "previous_summary": summary,
                            "prompt_layers": prompt_layers
                            + [{**template, "content": payload[-1]["content"]}],
                            "disabled_prompt_layers": bot.get("disabled_prompt_layers", []),
                        },
                    )
                    last_request_id = result.request_id
                    summary_size = await self.summary_tokens_async(result.content or "")
                    reported = (
                        self.store.one(
                            "SELECT output_tokens,reasoning_tokens FROM requests WHERE id=?",
                            (result.request_id,),
                        )
                        or {}
                    )
                    incomplete = (
                        not (result.content or "").strip()
                        or bool(result.tool_calls)
                        or result.finish_reason
                        in ("length", "content_filter", "tool_calls", "insufficient_system_resource")
                    )
                    oversized = summary_size > profile["summary_tokens"]
                    self.store.emit(
                        "compaction.summary_checked",
                        {
                            "compaction_id": compaction_id,
                            "summary_tokens": summary_size,
                            "retained_summary_token_limit": profile["summary_tokens"],
                            "summary_tokenizer": "cl100k_base",
                            "output_tokens": reported.get("output_tokens"),
                            "reasoning_tokens": reported.get("reasoning_tokens"),
                            "finish_reason": result.finish_reason,
                            "accepted": not incomplete and not oversized,
                            "compaction_output_policy": output_policy,
                            "compaction_max_tokens": output_cap,
                        },
                        bot_id=bot["id"],
                        turn_id=turn_id,
                        request_id=result.request_id,
                    )
                    if incomplete:
                        raise ControlError(
                            "Compaction did not return a complete summary: "
                            f"finish_reason={result.finish_reason}, visible_chars={len(result.content or '')}, "
                            f"output_tokens={reported.get('output_tokens')}, reasoning_tokens={reported.get('reasoning_tokens')}, "
                            f"retained_summary_tokens={summary_size}, retained_summary_token_limit={profile['summary_tokens']}. "
                            + (
                                f"Hortator sent max_tokens={output_cap} for reasoning plus summary. "
                                if output_cap is not None
                                else "No total-output cap was sent by Hortator; the provider default applies. "
                            )
                            + "Previous context is retained; inspect the provider completion boundary, output allowance and context limit."
                        )
                    if oversized:
                        raise ControlError(
                            f"Completed compaction summary exceeds its retained-text limit: {summary_size} > {profile['summary_tokens']} tokens (cl100k_base). "
                            "Private reasoning is excluded. The complete candidate is saved in the request; previous context is retained and no text was truncated."
                        )
                    summary = result.content
                    pending = pending[count:]
                messages, meta = await self.assemble(
                    bot, profile, channel_id, recent, summary, tools=tools, image_plan=image_plan
                )
                if (
                    meta["estimated_tokens"] * factor + profile["response_tokens"]
                    >= profile["context_window"]
                ):
                    raise ControlError(
                        "Compacted context still exceeds the model budget; previous context is retained"
                    )
                checkpoint = old_rows[-1]["seq"]
                # Commit only after every batch succeeds. The original transcript is never rewritten.
                self.store.execute(
                    "UPDATE contexts SET summary=?,checkpoint=?,compactions=compactions+1,estimated_tokens=?,updated_at=? WHERE bot_id=? AND channel_id=?",
                    (summary, checkpoint, meta["estimated_tokens"], time.time(), bot["id"], channel_id),
                )
                self.store.emit(
                    "compaction.completed",
                    {
                        "compaction_id": compaction_id,
                        "before_tokens": original_estimate,
                        "after_tokens": meta["estimated_tokens"],
                        "summary_tokens": summary_size,
                        "retained_summary_token_limit": profile["summary_tokens"],
                        "summary_tokenizer": "cl100k_base",
                        "before_summary": context["summary"],
                        "summary": summary,
                        "checkpoint": checkpoint,
                        "retained_message_ids": [r["discord_id"] for r in recent],
                        "compacted_message_ids": [r["discord_id"] for r in old_rows],
                    },
                    bot_id=bot["id"],
                    turn_id=turn_id,
                )
                rows = recent
                context = self.store.context(bot["id"], channel_id)
            except BaseException as exc:
                cancelled = isinstance(exc, asyncio.CancelledError)
                self.store.emit(
                    "compaction.cancelled" if cancelled else "compaction.failed",
                    {
                        "compaction_id": compaction_id,
                        "error": error_text(exc),
                        "channel_id": channel_id,
                        "reason": "Previous context retained; checkpoint was not advanced",
                        "previous_context_retained": True,
                    },
                    bot_id=bot["id"],
                    turn_id=turn_id,
                    request_id=getattr(exc, "request_id", None) or last_request_id,
                    level="warning" if cancelled else "error",
                )
                raise
        if meta["estimated_tokens"] * factor + profile["response_tokens"] >= profile["context_window"]:
            raise ControlError("System prompts, memory and context exceed the model's input budget")
        meta.update(checkpoint=context["checkpoint"], through_sequence=tail, calibration_factor=factor)
        self.store.execute(
            "UPDATE contexts SET estimated_tokens=?,updated_at=? WHERE bot_id=? AND channel_id=?",
            (meta["estimated_tokens"], time.time(), bot["id"], channel_id),
        )
        self.store.emit("context.assembled", meta, bot_id=bot["id"], turn_id=turn_id)
        return rows, context["summary"], tail, meta
