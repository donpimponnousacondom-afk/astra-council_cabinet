from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlencode

from pydantic import ValidationError

from .footer import footer_settings
from .models import ControlError, KINDS, OWNER_ID, SCHEMAS
from .store import uid
from .version import runtime_version


class Service:
    def __init__(self, store, vault, pool):
        self.store, self.vault, self.pool = store, vault, pool
        self.engine = self.registry = self.connector = None
        self.lock = asyncio.Lock()
        self._version = runtime_version()

    def version(self):
        return self.vault.redact(self._version)

    def seed(self):
        if self.store.get("settings", "global"):
            return
        initial = {
            "settings": [{"id": "global"}],
            "providers": [
                {
                    "id": "openrouter",
                    "name": "OpenRouter",
                    "kind": "openrouter",
                    "base_url": "https://openrouter.ai/api/v1",
                }
            ],
            "profiles": [
                {
                    "id": "balanced",
                    "name": "Balanced · configure model",
                    "provider_id": "openrouter",
                    "model": "your-model-id",
                }
            ],
            "rooms": [{"id": "council", "name": "The council"}],
            "prompts": [
                {
                    "id": "council-etiquette",
                    "name": "Council etiquette",
                    "content": "Build on specific points from other participants. Ask real questions. Cite fetched sources when relevant. Disagree with ideas, never insult participants. Avoid restating an entire discussion. Take a pause when you have nothing to add.",
                }
            ],
            "bots": [
                {
                    "id": "hortator",
                    "name": "Hortator",
                    "role": "hortator",
                    "model_profile_id": "balanced",
                    "interval_seconds": 15,
                    "cooldown_seconds": 15,
                    "persona": "You are the measured, technically precise council director. Help The Boss understand the council and diagnose operational failures. Support claims with inspected data.",
                    "enabled_plugins": ["council_inspect"],
                    "color": "#d7c2ef",
                },
                {
                    "id": "ada",
                    "name": "Ada",
                    "model_profile_id": "balanced",
                    "room_ids": ["council"],
                    "prompt_ids": ["council-etiquette"],
                    "persona": "You are Ada: analytical, inventive, and curious about how ideas become practical systems. Look for testable claims and useful connections. Ask concise, probing questions.",
                    "color": "#b9de89",
                },
                {
                    "id": "socrates",
                    "name": "Socrates",
                    "model_profile_id": "balanced",
                    "room_ids": ["council"],
                    "prompt_ids": ["council-etiquette"],
                    "interval_seconds": 90,
                    "cooldown_seconds": 90,
                    "persona": "You are Socrates: warm, skeptical, and interested in assumptions. Ask questions that reveal what others mean. Offer your own position too; do not make every response a question.",
                    "color": "#e4bb82",
                },
            ],
        }
        for kind, items in initial.items():
            for item in items:
                self.store.put(kind, SCHEMAS[kind].model_validate(item).model_dump())
        self.store.emit(
            "system.initialized",
            {
                "owner_id": OWNER_ID,
                "note": "Draft bots are disabled. Enter credentials and Discord IDs before activation.",
            },
        )

    def seed_plugins(self):
        for name, spec in self.registry.specs.items():
            if not self.store.get("plugins", name):
                self.store.put(
                    "plugins",
                    SCHEMAS["plugins"]
                    .model_validate(
                        {
                            "id": name,
                            "name": spec.name,
                            "description": spec.description,
                            "config": spec.defaults,
                            "enabled": name in ("council_inspect", "memory", "web_fetch"),
                        }
                    )
                    .model_dump(),
                )

    def entity(self, kind, entity_id):
        if kind not in KINDS:
            raise ControlError("Unknown resource", 404)
        entity = self.store.get(kind, entity_id)
        if not entity:
            raise ControlError(f"{kind}/{entity_id} does not exist", 404)
        return entity

    def public(self, kind, entity):
        value = dict(entity)
        if kind == "providers":
            value["key_configured"] = bool(self.vault.get(f"provider/{value['id']}/api_key"))
            value["health"] = self.store.health(value["id"])
            value["recent"] = self.store.one(
                "SELECT count(*) AS requests,coalesce(sum(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) AS failures FROM requests WHERE provider_id=? AND started_at>?",
                (value["id"], time.time() - 900),
            )
        elif kind == "bots":
            value.update(footer_settings(value))
            for field in ("document_task_rounds", "document_task_calls_per_round", "document_task_seconds"):
                value.setdefault(field, SCHEMAS["bots"].model_fields[field].default)
            value["token_configured"] = bool(self.vault.get(f"bot/{value['id']}/token"))
            value["key_override_configured"] = bool(self.vault.get(f"bot/{value['id']}/provider_key"))
            value["plugin_keys_configured"] = [
                name for name in self.registry.specs if self.vault.get(f"bot/{value['id']}/plugin:{name}")
            ]
            value["runtime"] = self.store.runtime(value["id"])
            value["active_turn"] = bool(self.engine and value["id"] in self.engine.tasks)
            value["contexts"] = self.store.rows(
                "SELECT * FROM contexts WHERE bot_id=? ORDER BY updated_at DESC", (value["id"],)
            )
            value["readiness"] = self.readiness(value)
            if value["application_id"]:
                # View, Send, Embed, Attach, Read History, Create Public Threads, Send in Threads.
                permissions = (
                    (1 << 10) | (1 << 11) | (1 << 14) | (1 << 15) | (1 << 16) | (1 << 35) | (1 << 38)
                )
                value["invite_url"] = "https://discord.com/oauth2/authorize?" + urlencode(
                    {"client_id": value["application_id"], "scope": "bot", "permissions": str(permissions)}
                )
        elif kind == "plugins":
            value["key_configured"] = bool(self.vault.get(f"plugin/{value['id']}/api_key"))
            spec = self.registry.specs.get(value["id"])
            value["schema"] = spec.parameters if spec else None
            value["installed"] = bool(spec)
        return self.vault.redact(value)

    def readiness(self, bot):
        issues = []
        if not bot["application_id"]:
            issues.append("Add a Discord application ID")
        if not self.vault.get(f"bot/{bot['id']}/token"):
            issues.append("Add and verify the bot token")
        profile = self.store.get("profiles", bot["model_profile_id"])
        provider = self.store.get("providers", profile["provider_id"]) if profile else None
        if not profile or profile["model"] == "your-model-id":
            issues.append("Choose a model in the model profile")
        if not provider:
            issues.append("Configure a provider")
        elif provider["requires_key"] and not self.pool.key(provider, bot["id"]):
            issues.append("Add a provider key or a bot key override")
        if bot["role"] == "council":
            if not bot["room_ids"]:
                issues.append("Assign at least one room")
            for room_id in bot["room_ids"]:
                room = self.store.get("rooms", room_id)
                if not room or not room["channel_id"] or not room["guild_id"]:
                    issues.append(f"Configure guild and channel IDs for {room_id}")
        else:
            settings = self.store.get("settings", "global")
            if not settings["control_channel_id"] or not settings["control_guild_id"]:
                issues.append("Configure Hortator's reporting guild and channel")
        return issues

    def validate_references(self, kind, entity):
        def exists(k, value):
            if not self.store.get(k, value):
                raise ControlError(f"Referenced {k}/{value} does not exist")

        if kind == "profiles":
            exists("providers", entity["provider_id"])
        if kind == "bots":
            exists("profiles", entity["model_profile_id"])
            for k, field in (
                ("rooms", "room_ids"),
                ("prompts", "prompt_ids"),
                ("plugins", "enabled_plugins"),
            ):
                for value in entity[field]:
                    exists(k, value)
            for other in self.store.list("bots"):
                if other["id"] != entity["id"]:
                    if entity["application_id"] and other["application_id"] == entity["application_id"]:
                        raise ControlError("Each bot must have a unique Discord application", 409)
                    if entity["role"] == other["role"] == "hortator":
                        raise ControlError("There can be only one Hortator application", 409)
            if entity["enabled"]:
                issues = self.readiness(entity)
                if issues:
                    raise ControlError("Bot is not ready: " + "; ".join(issues))
        if (
            kind == "settings"
            and entity["control_channel_id"]
            and any(r["channel_id"] == entity["control_channel_id"] for r in self.store.list("rooms"))
        ):
            raise ControlError("Use a separate reporting channel for Hortator")
        if kind == "rooms" and entity["channel_id"]:
            if self.store.get("settings", "global")["control_channel_id"] == entity["channel_id"]:
                raise ControlError("Council rooms and Hortator reporting must use separate channels")
            if any(
                r["id"] != entity["id"] and r["channel_id"] == entity["channel_id"]
                for r in self.store.list("rooms")
            ):
                raise ControlError("This channel already belongs to a room", 409)
        document_config = None
        if kind == "plugins" and entity["id"] == "document_site":
            document_config = entity["config"]
        elif kind == "bots":
            document_config = entity.get("plugin_config", {}).get("document_site")
        if document_config is not None:
            from .documents import public_base

            if not isinstance(document_config, dict):
                raise ControlError("document_site configuration must be an object")
            for field in ("local_base_url", "public_base_url"):
                if field in document_config:
                    value = document_config[field]
                    if not isinstance(value, str):
                        raise ControlError(f"document_site.{field} must be a string URL")
                    if field == "local_base_url" and not value:
                        raise ControlError("document_site.local_base_url must not be empty")
                    public_base(value)
        if kind == "plugins" and entity["id"] not in self.registry.specs:
            raise ControlError("Install a Python plugin entry point before configuring it")

    def affected(self, kind, entity_id):
        bots = self.store.list("bots")
        if kind == "bots":
            return [entity_id]
        if kind == "profiles":
            return [b["id"] for b in bots if b["model_profile_id"] == entity_id]
        if kind == "providers":
            profiles = {p["id"] for p in self.store.list("profiles") if p["provider_id"] == entity_id}
            return [b["id"] for b in bots if b["model_profile_id"] in profiles]
        if kind == "prompts":
            return [b["id"] for b in bots if entity_id in b["prompt_ids"]]
        if kind == "plugins":
            return [b["id"] for b in bots if entity_id in b["enabled_plugins"]]
        if kind == "rooms":
            return [b["id"] for b in bots if entity_id in b["room_ids"]]
        return [b["id"] for b in bots]

    async def save(self, actor, kind, entity_id, data, create=False):
        actor.require_owner()
        if kind not in SCHEMAS:
            raise ControlError("Unknown resource", 404)
        async with self.lock:
            old = self.store.get(kind, entity_id)
            if create and self.store.one(
                "SELECT 1 FROM entity_tombstones WHERE kind=? AND id=?", (kind, entity_id)
            ):
                raise ControlError(
                    "That identifier is retired; use a new ID to keep historical identities separate", 409
                )
            if create and old:
                raise ControlError("That identifier already exists", 409)
            if not create and not old:
                raise ControlError("Resource not found", 404)
            expected = data.get("revision")
            if old and expected is not None and expected != old["revision"]:
                raise ControlError("Configuration changed in another session. Reload before saving.", 409)
            if "id" in data and data["id"] != entity_id:
                raise ControlError("Identifiers cannot be changed; clone instead")
            raw = {**(old or {}), **data, "id": entity_id}
            raw.pop("revision", None)
            try:
                entity = SCHEMAS[kind].model_validate(raw).model_dump()
            except ValidationError as exc:
                raise ControlError(
                    "; ".join(
                        ".".join(map(str, e["loc"])) + ": " + e["msg"]
                        for e in exc.errors(include_input=False)
                    )
                ) from exc
            self.validate_references(kind, entity)
            if (
                kind == "bots"
                and old
                and old["application_id"] != entity["application_id"]
                and self.vault.get(f"bot/{entity_id}/token")
            ):
                raise ControlError("Remove the existing bot token before changing its application ID")
            result = self.store.put(kind, entity)
            self.store.emit(
                "config.created" if create else "config.updated",
                {"actor": actor.label, "resource": kind, "id": entity_id, "before": old, "after": result},
            )
            if self.engine:
                await self.engine.cancel(self.affected(kind, entity_id), f"{kind}/{entity_id} changed")
            return self.public(kind, result)

    async def delete(self, actor, kind, entity_id):
        actor.require_owner()
        async with self.lock:
            deleted_entity = self.entity(kind, entity_id)
            if kind in ("settings", "plugins"):
                raise ControlError("This registry entry cannot be deleted; disable it instead")
            references = []
            for k in ("bots", "profiles"):
                for row in self.store.list(k):
                    if k == kind and row["id"] == entity_id:
                        continue
                    fields = {
                        "providers": ("provider_id",),
                        "profiles": ("model_profile_id",),
                        "rooms": ("room_ids",),
                        "prompts": ("prompt_ids",),
                    }.get(kind, ())
                    for field in fields:
                        value = row.get(field)
                        if value == entity_id or isinstance(value, list) and entity_id in value:
                            references.append(f"{k}/{row['id']}")
            if references:
                raise ControlError("Still referenced by: " + ", ".join(references), 409)
            affected = self.affected(kind, entity_id)
            self.store.execute(
                "INSERT OR REPLACE INTO entity_tombstones VALUES(?,?,?)", (kind, entity_id, time.time())
            )
            self.store.execute("DELETE FROM entities WHERE kind=? AND id=?", (kind, entity_id))
            self.store.emit(
                "config.deleted",
                {"actor": actor.label, "resource": kind, "id": entity_id, "before": deleted_entity},
            )
            if self.engine:
                await self.engine.cancel(affected, "Configuration deleted")
            prefix = {"bots": "bot", "providers": "provider"}.get(kind)
            if prefix:
                for scope in list(self.vault.values):
                    if scope.startswith(f"{prefix}/{entity_id}/"):
                        self.vault.put(scope, "")
            return {"deleted": entity_id, "history_retained": True}

    async def credential(self, actor, kind, entity_id, field, value):
        actor.require_owner()
        if actor.source != "dashboard":
            raise ControlError("Credentials can only be entered from the dashboard", 403)
        if not isinstance(value, str) or len(value) > 16000:
            raise ControlError("Invalid credential")
        entity = self.entity(kind, entity_id)
        prefix = {"providers": "provider", "bots": "bot", "plugins": "plugin"}.get(kind)
        allowed = (
            field == "api_key"
            if kind in ("providers", "plugins")
            else field in ("token", "provider_key")
            or field.startswith("plugin:")
            and field[7:] in self.registry.specs
        )
        if not prefix or not allowed:
            raise ControlError("Unknown credential field")
        if kind == "bots" and field == "token" and value:
            if not self.connector:
                raise ControlError("Discord connector is unavailable")
            identity = await self.connector.validate_token(value)
            if entity["application_id"] and identity["application_id"] != entity["application_id"]:
                raise ControlError("Token belongs to a different Discord application")
            async with self.lock:
                entity = self.entity(kind, entity_id)
                if entity["application_id"] and identity["application_id"] != entity["application_id"]:
                    raise ControlError("Application changed while token was being verified", 409)
                candidate = {**entity, "application_id": identity["application_id"]}
                candidate.pop("revision")
                self.validate_references("bots", {**candidate, "enabled": False})
                self.store.put("bots", candidate)
                self.vault.put(f"bot/{entity_id}/user_id", identity["user_id"])
        self.vault.put(f"{prefix}/{entity_id}/{field}", value.strip())
        if kind == "bots" and field == "token" and not value:
            self.vault.put(f"bot/{entity_id}/user_id", "")
        self.store.emit(
            "credential.updated",
            {
                "actor": actor.label,
                "resource": kind,
                "id": entity_id,
                "field": field,
                "configured": bool(value),
            },
        )
        if self.engine:
            await self.engine.cancel(self.affected(kind, entity_id), "Credential changed")
        return {"configured": bool(value)}

    def stats(self, hours=24):
        since = time.time() - hours * 3600
        fields = """count(*) AS requests, sum(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failures,
          sum(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancellations,
          sum(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
          sum(input_tokens) AS input_tokens,sum(output_tokens) AS output_tokens,
          sum(reasoning_tokens) AS reasoning_tokens,sum(cached_tokens) AS cached_tokens,sum(cost) AS cost,
          sum(CASE WHEN cost IS NOT NULL THEN 1 ELSE 0 END) AS cost_known_requests,
          sum(CASE WHEN cost_source='estimated' THEN 1 ELSE 0 END) AS estimated_cost_requests,
          sum(CASE WHEN input_tokens IS NOT NULL THEN 1 ELSE 0 END) AS usage_known_requests,
          avg(ttft_ms) AS avg_ttft_ms,avg(duration_ms) AS avg_duration_ms"""
        total = self.store.one(f"SELECT {fields} FROM requests WHERE started_at>=?", (since,))
        grouped = {}
        for group in ("bot_id", "provider_id", "profile_id", "model", "purpose"):
            grouped[group] = self.store.rows(
                f"SELECT {group} AS id,{fields} FROM requests WHERE started_at>=? GROUP BY {group}", (since,)
            )
        grouped["configuration"] = self.store.rows(
            f"SELECT profile_id || '@r' || coalesce(json_extract(context,'$.profile_revision'),0) AS id,{fields} FROM requests WHERE started_at>=? GROUP BY profile_id,coalesce(json_extract(context,'$.profile_revision'),0)",
            (since,),
        )
        latency = self.store.rows(
            "SELECT ttft_ms,duration_ms FROM requests WHERE started_at>=? AND status='completed' ORDER BY started_at DESC LIMIT 10000",
            (since,),
        )
        for key in ("ttft_ms", "duration_ms"):
            vals = sorted(r[key] for r in latency if r[key] is not None)
            total["p95_" + key] = vals[min(len(vals) - 1, int(len(vals) * 0.95))] if vals else None
        total["latency_sample_size"] = len(latency)
        history = self.store.rows(
            f"SELECT CAST(started_at/3600 AS INTEGER)*3600 AS at,{fields} FROM requests WHERE started_at>=? GROUP BY CAST(started_at/3600 AS INTEGER) ORDER BY at",
            (since,),
        )
        turns = self.store.rows(
            "SELECT status,count(*) AS count FROM turns WHERE started_at>=? GROUP BY status", (since,)
        )
        tools = self.store.rows(
            "SELECT json_extract(data,'$.name') AS name,kind,count(*) AS count,avg(json_extract(data,'$.duration_ms')) AS avg_duration_ms FROM events WHERE at>=? AND kind IN ('tool.completed','tool.failed') GROUP BY name,kind",
            (since,),
        )
        return {
            "hours": hours,
            "total": total,
            "by": grouped,
            "history": history,
            "turns": turns,
            "tools": tools,
        }

    def status(self):
        return {
            "version": self.version(),
            "owner_id": OWNER_ID,
            "now": time.time(),
            "settings": self.store.get("settings", "global"),
            "bots": [self.public("bots", b) for b in self.store.list("bots")],
            "providers": [self.public("providers", p) for p in self.store.list("providers")],
            "profiles": self.store.list("profiles"),
            "rooms": self.store.list("rooms"),
            "plugins": [self.public("plugins", p) for p in self.store.list("plugins")],
            "prompts": self.store.list("prompts"),
            "active_requests": self.store.rows(
                "SELECT id,bot_id,model,purpose,started_at FROM requests WHERE status='running'"
            ),
            "delivery_counts": self.store.rows("SELECT status,count(*) AS count FROM outbox GROUP BY status"),
            "last_event_seq": self.store.one("SELECT coalesce(max(seq),0) AS seq FROM events")["seq"],
        }

    def turns(self, bot_id=None, before=None, status=None, limit=60):
        conditions, args = ["started_at<?"], [before or time.time() + 1]
        if bot_id:
            conditions.append("bot_id=?")
            args.append(bot_id)
        if status:
            conditions.append("status=?")
            args.append(status)
        return self.store.rows(
            "SELECT * FROM turns WHERE " + " AND ".join(conditions) + " ORDER BY started_at DESC LIMIT ?",
            (*args, min(limit, 200)),
        )

    def turn(self, turn_id):
        turn = self.store.one("SELECT * FROM turns WHERE id=?", (turn_id,))
        if not turn:
            raise ControlError("Turn not found", 404)
        requests = self.store.rows("SELECT * FROM requests WHERE turn_id=? ORDER BY started_at", (turn_id,))
        for request in requests:
            for key in ("body", "context", "usage", "response"):
                request[key] = json.loads(request[key]) if request[key] else None
        outbox = self.store.rows("SELECT * FROM outbox WHERE turn_id=? ORDER BY created_at", (turn_id,))
        # Detail includes the full turn. Global history remains cursor-paginated.
        events = self.store.rows("SELECT * FROM events WHERE turn_id=? ORDER BY seq", (turn_id,))
        for event in events:
            event["data"] = json.loads(event["data"])
        return self.vault.redact({"turn": turn, "requests": requests, "events": events, "outbox": outbox})

    def context(self, bot_id, channel_id):
        self.entity("bots", bot_id)
        value = self.store.one("SELECT * FROM contexts WHERE bot_id=? AND channel_id=?", (bot_id, channel_id))
        if not value:
            raise ControlError("Context has not been created for this bot/channel", 404)
        value["memories"] = self.store.rows(
            "SELECT key,value,updated_at FROM memories WHERE bot_id=? AND channel_id=?", (bot_id, channel_id)
        )
        value["messages"] = self.store.transcript(channel_id, after=value["checkpoint"], limit=500)
        value["message_count"] = self.store.one(
            "SELECT count(*) AS n FROM messages WHERE channel_id=? AND seq>?",
            (channel_id, value["checkpoint"]),
        )["n"]
        value["compaction_events"] = [
            e
            for e in self.store.events(bot_id=bot_id, limit=500)
            if e["kind"] == "compaction.completed"
            and e["turn_id"]
            in {
                t["id"]
                for t in self.store.rows(
                    "SELECT id FROM turns WHERE bot_id=? AND channel_id=?", (bot_id, channel_id)
                )
            }
        ][:30]
        return value

    def inspect(self, resource, entity_id=None, channel_id=None):
        if resource == "version":
            return self.version()
        if resource == "status":
            return self.status()
        if resource == "stats":
            return self.stats()
        if resource == "events":
            return self.store.events(bot_id=entity_id, limit=40)
        if resource == "turn":
            return self.turn(entity_id)
        if resource == "context":
            return (
                self.context(entity_id, channel_id)
                if channel_id
                else self.store.rows("SELECT * FROM contexts WHERE bot_id=?", (entity_id,))
            )
        if resource in KINDS:
            return (
                self.public(resource, self.entity(resource, entity_id))
                if entity_id
                else [self.public(resource, item) for item in self.store.list(resource)]
            )
        raise ControlError("Unknown inspection resource")

    async def control(self, actor, command):
        actor.require_owner()
        action = command.get("action")
        kind, target, data = command.get("kind"), command.get("id"), command.get("data", {})
        if action in ("save", "create"):
            return await self.save(
                actor, kind, target or data.get("id", uid("new_")), data, create=action == "create"
            )
        if action == "delete":
            return await self.delete(actor, kind, target)
        if action in ("start", "stop"):
            enabled = action == "start"
            if not target or target == "all":
                kind, target = "settings", "global"
            kind = kind or "bots"
            if kind not in ("bots", "providers", "plugins", "settings"):
                raise ControlError("Start/stop accepts bots, providers, plugins or all")
            return await self.save(actor, kind, target, {"enabled": enabled})
        if action == "clone":
            kind = kind or "profiles"
            source = self.entity(kind, target)
            if kind not in ("profiles", "bots", "prompts", "providers"):
                raise ControlError("That resource cannot be cloned")
            source.pop("revision")
            source.update(
                id=data.get("id", uid(target[:30] + "_")), name=data.get("name", source["name"] + " · copy")
            )
            if kind == "bots":
                source.update(application_id="", enabled=False, role="council")
            return await self.save(actor, kind, source["id"], source, create=True)
        if action == "probe":
            result = await self.pool.probe(self.entity("providers", target))
        elif action == "restart":
            self.entity("bots", target)
            await self.engine.cancel([target], "Discord reconnect requested")
            await self.connector.restart(target)
            result = {"reconnecting": target}
        elif action == "reset_circuit":
            self.entity("providers", target)
            self.store.health(target)
            self.store.execute(
                "UPDATE provider_health SET consecutive_failures=0,circuit_until=0 WHERE provider_id=?",
                (target,),
            )
            result = {"reset": target}
        elif action == "compact":
            bot = self.entity("bots", target)
            channel_id = data.get("channel_id") or self.engine.pick_channel(bot)
            if not channel_id:
                raise ControlError("Choose a channel with an existing context")
            result = {"turn_id": self.engine.launch(bot, channel_id, compact_only=True)}
        elif action == "memory":
            self.entity("bots", target)
            channel_id, key, value = (
                data.get("channel_id"),
                data.get("key", "operator"),
                data.get("value", ""),
            )
            self.context(target, channel_id)
            if (
                not isinstance(key, str)
                or not 1 <= len(key) <= 100
                or not isinstance(value, str)
                or len(value) > 8000
            ):
                raise ControlError("Memory key/value exceeds limits")
            from .plugins import ToolContext

            result = await self.registry.memory(
                {"operation": "write" if value else "delete", "key": key, "value": value},
                ToolContext(self.entity("bots", target), channel_id, "operator"),
                {},
                "",
            )
            await self.engine.cancel([target], "Memory changed")
        elif action == "thread":
            room = self.entity("rooms", target)
            result = await self.connector.create_thread(room, data.get("name", "Council discussion"))
        else:
            raise ControlError("Unknown control action")
        self.store.emit(
            "control.executed", {"actor": actor.label, "action": action, "id": target, "data": data}
        )
        return result
