import io
import json

import pytest

from conftest import configured
from hortator.console import OperationalConsole
from hortator.models import ControlError
from hortator.plugins import ToolContext


@pytest.mark.parametrize("name", ["memory", "global_memory"])
@pytest.mark.parametrize("actor", ["model", "operator"])
async def test_memory_change_metrics_count_stored_characters_and_preserve_scope(kernel, owner, name, actor):
    bot = configured(kernel, enabled_plugins=[name], memory_char_limit=100, global_memory_char_limit=100)
    plugin = kernel.store.get("plugins", name)
    kernel.store.put("plugins", {**plugin, "enabled": True})
    kernel.store.context(bot["id"], "room")
    context = ToolContext(bot, "room", "metrics")
    # Other bots and the other kind of memory must never inflate this scope.
    kernel.store.execute("INSERT INTO memories VALUES('socrates','room','other','other bot',0)")
    kernel.store.execute("INSERT INTO memories VALUES('ada','another-room','other','other room',0)")
    kernel.store.execute("INSERT INTO global_memories VALUES('socrates','other','other bot',NULL,0)")

    async def change(args):
        if actor == "model":
            return await kernel.registry.call(name, args, context, "metrics")
        if name == "global_memory":
            return await kernel.service.global_memory_change(owner, bot["id"], args)
        return await kernel.service.control(
            owner,
            {
                "action": "memory",
                "kind": "bots",
                "id": bot["id"],
                "data": {"channel_id": "room", "key": args["key"], "value": args.get("value", "")},
            },
        )

    def changes():
        return [e for e in kernel.store.events(limit=500) if e["kind"] == f"{name}.changed"]

    steps = [
        ({"operation": "write", "key": "kept", "value": "é\0🙂"}, 3, 3),
        ({"operation": "write", "key": "topic", "value": "abcde"}, 5, 8),
        ({"operation": "write", "key": "topic", "value": "xy"}, 2, 5),
        ({"operation": "write", "key": "topic", "value": "ab"}, 2, 5),
        ({"operation": "delete", "key": "topic"}, 2, 3),
        ({"operation": "delete", "key": "missing"}, 0, 3),
        ({"operation": "write", "key": "topic", "value": "x" * 102}, 102, 105),
    ]
    for args, affected, total in steps:
        assert (await change(args))["saved"]
        event = changes()[0]
        data = event["data"]
        assert (data["units_affected"], data["unit_total"], data["unit"]) == (affected, total, "characters")
        assert data["used_chars"] == total
        assert event["bot_id"] == bot["id"] and data["actor"] == actor
        assert "value" not in data
    warning = next(e for e in kernel.store.events() if e["kind"] == f"{name}.budget_warning")
    assert (warning["data"]["units_affected"], warning["data"]["unit_total"]) == (102, 105)
    before = changes()
    await kernel.registry.call(name, {"operation": "read"}, context, "read")
    await kernel.registry.call(name, {}, context, "usage")
    rejected = {"operation": "write", "key": "extra", "value": "not saved"}
    if actor == "operator":
        with pytest.raises(ControlError):
            await change(rejected)
    else:
        assert (await change(rejected))["ok"] is False
    assert changes() == before

    await change({"operation": "delete", "key": "topic"})
    secret = "fake-private-secret-for-metric-redaction-0123456789"
    kernel.vault.put("plugin/test/api_key", secret)
    await change({"operation": "write", "key": "topic", "value": secret})
    data = changes()[0]["data"]
    stored_length = len(kernel.vault.redact(secret))
    assert (data["units_affected"], data["unit_total"]) == (stored_length, stored_length + 3)
    assert secret not in json.dumps(data)


@pytest.mark.parametrize("kind", ["memory.changed", "global_memory.changed"])
def test_console_appends_memory_metrics_after_existing_fields_before_messages(kind):
    stream = io.StringIO()
    with OperationalConsole(stream=stream, keys=False, color=False) as console:
        for level in ["info", "warning", "error"]:
            console.render(
                console.event(
                    {
                        "at": 1,
                        "level": level,
                        "kind": kind,
                        "bot_id": "loki",
                        "data": {
                            "operation": "delete",
                            "channel_id": "room",
                            "units_affected": 3393,
                            "unit_total": 39299,
                            "message": "Note change saved",
                        },
                    }
                )
            )
    lines = [line for line in stream.getvalue().splitlines() if kind in line]
    assert len(lines) == 3
    suffix = "operation=delete · channel_id=room · units_affected=3393 · unit_total=39299 · message=Note change saved"
    assert all(suffix in line for line in lines)
