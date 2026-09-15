"""Small model-facing inventories; dashboard configuration remains independent."""

import time

from .models import ControlError, KINDS, OWNER_ID


DESCRIPTION = (
    "Read council evidence for The Boss; no mutations or credentials. First call "
    '{"resource":"bots"} for the configured roster (stable IDs, names, rooms, models/providers). '
    'Use an exact returned ID for details, e.g. {"resource":"bots","id":"returned-bot-id"}. '
    "Human/Discord display names, room names, model names and provider IDs are not bot IDs. "
    "Status is a compact overview; request a resource/id for configuration, context for saved "
    "conversation state, or turn for trajectory. Large results are fully saved: follow read_response "
    'with this same tool using resource="read_result", result_id, offset and length; repeat next '
    "until null. No workspace grant is needed. Version reports the running code."
)


def pick(value, *fields):
    return {field: value.get(field) for field in fields}


def roster(service):
    store = service.store
    profiles = {p["id"]: p for p in store.list("profiles")}
    rooms = {r["id"]: r for r in store.list("rooms")}
    settings = store.get("settings", "global")
    result = []
    for bot in store.list("bots"):
        profile = profiles.get(bot["model_profile_id"], {})
        row = pick(bot, "id", "name", "role", "enabled", "application_id", "model_profile_id", "room_ids")
        row.update(
            model=profile.get("model"),
            provider_id=profile.get("provider_id"),
            rooms=[pick(rooms[r], "id", "name", "channel_id") for r in bot["room_ids"] if r in rooms],
        )
        if bot["role"] == "hortator":
            row["intake"] = {
                "owner_only": True,
                "control_channel_id": settings["control_channel_id"],
                "control_channel_threads": True,
                "owner_dms": True,
            }
        result.append(row)
    return result


def inspect(service, resource, entity_id=None, channel_id=None):
    store = service.store
    target_kind = "bots" if resource in ("context", "events") else resource
    if entity_id and target_kind in KINDS and not store.get(target_kind, entity_id):
        identifiers = [item["id"] for item in store.list(target_kind)]
        available = ", ".join(identifiers[:20]) or "(none configured)"
        more = " (first 20; list the resource for all IDs)" if len(identifiers) > 20 else ""
        distinction = (
            " Human/Discord display names, room names, model names and provider IDs are not bot IDs."
            if target_kind == "bots"
            else " IDs belong to this resource; do not substitute another resource's name."
        )
        raise ControlError(
            f"{target_kind}/{entity_id} does not exist. Available stable IDs: {available}{more}. "
            f'List them with council_inspect {{"resource":"{target_kind}"}} and use an exact returned id.'
            + distinction,
            404,
        )
    if resource == "bots":
        if entity_id:
            return service.public("bots", service.entity("bots", entity_id), include_context=False)
        return roster(service)
    if resource == "status":
        # Do not build Service.status() first: its dashboard view materializes
        # every bot's full context, prompt, schema and configuration.
        bots = roster(service)
        for bot in bots:
            bot["runtime"] = store.runtime(bot["id"])
            bot["active_turn"] = bool(service.engine and bot["id"] in service.engine.tasks)
        return {
            "view": "compact",
            "detail_hint": "Use resource/id for configuration; resource=context with a bot id for conversation state.",
            "version": service.version(),
            "owner_id": OWNER_ID,
            "now": time.time(),
            "settings": store.get("settings", "global"),
            "bots": bots,
            "providers": [
                {**pick(p, "id", "name", "enabled", "kind"), "health": store.health(p["id"])}
                for p in store.list("providers")
            ],
            "profiles": [pick(p, "id", "name", "provider_id", "model") for p in store.list("profiles")],
            "rooms": store.list("rooms"),
            "plugins": [pick(p, "id", "name", "enabled") for p in store.list("plugins")],
            "prompts": [pick(p, "id", "name") for p in store.list("prompts")],
            "background_tasks": service.background.status() if hasattr(service, "background") else {},
            "active_requests": store.rows(
                "SELECT id,bot_id,model,purpose,started_at FROM requests WHERE status='running'"
            ),
            "delivery_counts": store.rows("SELECT status,count(*) AS count FROM outbox GROUP BY status"),
            "last_event_seq": store.one("SELECT coalesce(max(seq),0) AS seq FROM events")["seq"],
        }
    return service.inspect(resource, entity_id, channel_id)
