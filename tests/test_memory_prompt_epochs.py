"""Exercise durable note pins through context assembly and real mocked tool rounds."""

import json
from copy import deepcopy
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from conftest import configured, ingest
from hortator.app import Kernel
from hortator.models import OWNER_ID, ControlError
from hortator.plugins import ToolContext
from hortator.slash_commands import SlashContexts
from support.provider import install_client
from support.runtime import completion, settle


CHANNEL = "222222222222222222"
OTHER_CHANNEL = "222222222222222223"
SCOPES = ("memory", "global_memory")


def setup(kernel, bot_id="ada"):
    bot = configured(kernel, bot_id=bot_id, enabled_plugins=list(SCOPES), allow_images=False)
    for scope in SCOPES:
        kernel.store.put("plugins", {**kernel.store.get("plugins", scope), "enabled": True})
    template(kernel, "transcript", "TRANSCRIPT:{transcript}", "user")
    return bot


def template(kernel, key, content, role="system"):
    prompt = kernel.store.get("prompts", "runtime-" + key.replace("_", "-"))
    return kernel.store.put("prompts", {**prompt, "content": content, "role": role})


async def change(kernel, bot, scope, key, value=None, *, channel=CHANNEL, delete=False):
    args = {"operation": "delete" if delete else "write", "key": key}
    if not delete:
        args["value"] = value
    result = await kernel.registry.call(scope, args, ToolContext(bot, channel, "test-turn"), "test-change")
    assert result.get("ok", True) and result["saved"], result
    return result


async def assemble(kernel, bot, *, channel=CHANNEL, snapshot=None, extras=None):
    return await kernel.engine.contexts.assemble(
        bot,
        kernel.store.get("profiles", "balanced"),
        channel,
        kernel.store.transcript(channel),
        "retained continuity",
        extras=extras,
        memory_snapshot=snapshot,
    )


def layer(meta, key):
    return next(item for item in meta["prompt_layers"] if item["id"] == key)


def delta(meta, scope):
    return json.loads(layer(meta, scope + "_delta")["content"].rsplit("\n", 1)[-1])


def containing(messages, marker):
    return next(index for index, message in enumerate(messages) if marker in (message.get("content") or ""))


def through_history(messages):
    return messages[: containing(messages, "TRANSCRIPT:") + 1]


@pytest.mark.parametrize("scope", SCOPES)
async def test_writes_and_deletes_preserve_exact_prefix_with_complete_sorted_late_changes(kernel, scope):
    bot = setup(kernel)
    for key, value in (("z-delete", "remove this"), ("a-edit", "old value"), ("m-stay", "unchanged")):
        await change(kernel, bot, scope, key, value)
    ingest(kernel, content="UNCHANGED_CONVERSATION")
    snapshot = kernel.engine.contexts.memory_prompt.prepare(bot, CHANNEL)
    extras = [{"role": "assistant", "content": "UNCHANGED_EXCHANGE"}]
    before, initial = await assemble(kernel, bot, snapshot=snapshot, extras=extras)
    assert delta(initial, scope) == []

    await change(kernel, bot, scope, "a-edit", "new é\x00 value {notes}")
    await change(kernel, bot, scope, "b-new", "brand new")
    await change(kernel, bot, scope, "z-delete", delete=True)
    after, current = await assemble(kernel, bot, snapshot=snapshot, extras=extras)
    end = containing(before, "UNCHANGED_EXCHANGE") + 1
    assert before[:end] == after[:end]
    assert layer(initial, scope) == layer(current, scope)
    changes = delta(current, scope)
    assert [
        (entry["operation"], entry.get("key", entry.get("note", {}).get("key"))) for entry in changes
    ] == [
        ("replace", "a-edit"),
        ("replace", "b-new"),
        ("delete", "z-delete"),
    ]
    assert [entry["note"]["value"] for entry in changes[:2]] == ["new é\x00 value {notes}", "brand new"]
    if scope == "global_memory":
        for entry in changes[:2]:
            note = entry["note"]
            assert set(note) == {"key", "value", "source_channel_id", "updated_at"}
            assert note["source_channel_id"] == CHANNEL
            assert datetime.fromisoformat(note["updated_at"]).utcoffset() is not None
    else:
        assert all(set(entry["note"]) == {"key", "value"} for entry in changes[:2])
    assert current["memory_snapshot"]["epoch_id"] == snapshot["epoch_id"]
    assert kernel.engine.contexts.memory_prompt.epochs.load(bot["id"], CHANNEL) == snapshot
    assert layer(current, scope + "_delta")["content"] in after[end]["content"]
    assert "m-stay" not in json.dumps(changes)


async def test_history_growth_and_repeated_preparation_do_not_rotate_memory(kernel):
    bot = setup(kernel)
    for scope in SCOPES:
        await change(kernel, bot, scope, "topic", scope + " pinned note")
    ingest(kernel, content="First history message")
    contexts = kernel.engine.contexts
    profile = kernel.store.get("profiles", "balanced")
    _, _, _, prepared = await contexts.prepare(bot, profile, CHANNEL, "prepare-first", [])
    snapshot = prepared["_memory_snapshot"]
    before, initial = await assemble(kernel, bot, snapshot=snapshot)
    ingest(kernel, content="Second history message", discord_id="555555555555555556")
    _, _, _, repeated = await contexts.prepare(bot, profile, CHANNEL, "prepare-second", [])
    after, current = await assemble(kernel, bot, snapshot=repeated["_memory_snapshot"])
    boundary = containing(before, "TRANSCRIPT:")
    assert before[:boundary] == after[:boundary]
    assert "Second history message" in after[boundary]["content"]
    assert repeated["_memory_snapshot"] == snapshot
    for scope in SCOPES:
        assert layer(initial, scope) == layer(current, scope)
        assert delta(current, scope) == []


@pytest.mark.parametrize("scope", SCOPES)
async def test_repeated_edits_coalesce_and_empty_note_is_not_a_delete(kernel, scope):
    bot = setup(kernel)
    await change(kernel, bot, scope, "topic", "original")
    snapshot = kernel.engine.contexts.memory_prompt.prepare(bot, CHANNEL)
    for value in ("intermediate", "another intermediate", ""):
        await change(kernel, bot, scope, "topic", value)
    await change(kernel, bot, scope, "transient", "created and removed")
    await change(kernel, bot, scope, "transient", delete=True)
    _, current = await assemble(kernel, bot, snapshot=snapshot)
    changes = delta(current, scope)
    assert len(changes) == 1
    assert changes[0]["operation"] == "replace"
    assert changes[0]["note"]["key"] == "topic" and changes[0]["note"]["value"] == ""
    await change(kernel, bot, scope, "topic", delete=True)
    _, deleted = await assemble(kernel, bot, snapshot=snapshot)
    assert delta(deleted, scope) == [{"operation": "delete", "key": "topic"}]


@pytest.mark.parametrize("scope", SCOPES)
async def test_empty_initial_snapshot_keeps_new_notes_entirely_in_the_delta(kernel, scope):
    bot = setup(kernel)
    ingest(kernel)
    snapshot = kernel.engine.contexts.memory_prompt.prepare(bot, CHANNEL)
    before, initial = await assemble(kernel, bot, snapshot=snapshot)
    assert delta(initial, scope) == []
    assert scope not in {item["id"] for item in initial["prompt_layers"]}
    await change(kernel, bot, scope, "first", "NEW_NOTE_AFTER_EMPTY_PIN")
    after, current = await assemble(kernel, bot, snapshot=snapshot)
    assert through_history(before) == through_history(after)
    assert scope not in {item["id"] for item in current["prompt_layers"]}
    assert delta(current, scope)[0]["note"]["value"] == "NEW_NOTE_AFTER_EMPTY_PIN"
    assert current["memory_snapshot"]["epoch_id"] == snapshot["epoch_id"]


async def test_global_writes_cross_channels_but_private_notes_and_bot_pins_are_isolated(kernel):
    bot = setup(kernel)
    other = setup(kernel, "socrates")
    for target in (bot, other):
        for channel in (CHANNEL, OTHER_CHANNEL):
            await change(kernel, target, "memory", "private", target["id"] + "/" + channel, channel=channel)
        await change(kernel, target, "global_memory", "shared", target["id"] + " global original")
    pins = {
        (target["id"], channel): kernel.engine.contexts.memory_prompt.prepare(target, channel)
        for target in (bot, other)
        for channel in (CHANNEL, OTHER_CHANNEL)
    }
    await change(kernel, bot, "memory", "private", "ADA_FIRST_CHANNEL_NEW_PRIVATE")
    await change(kernel, bot, "global_memory", "shared", "ADA_NEW_GLOBAL", channel=OTHER_CHANNEL)
    for target in (bot, other):
        for channel in (CHANNEL, OTHER_CHANNEL):
            messages, meta = await assemble(
                kernel, target, channel=channel, snapshot=pins[target["id"], channel]
            )
            global_changes = delta(meta, "global_memory")
            private_changes = delta(meta, "memory")
            if target["id"] == bot["id"]:
                assert global_changes[0]["note"]["value"] == "ADA_NEW_GLOBAL"
                assert global_changes[0]["note"]["source_channel_id"] == OTHER_CHANNEL
                assert bool(private_changes) == (channel == CHANNEL)
            else:
                assert global_changes == private_changes == []
                assert "ADA_NEW_GLOBAL" not in json.dumps(messages)
            if channel != CHANNEL or target["id"] != bot["id"]:
                assert "ADA_FIRST_CHANNEL_NEW_PRIVATE" not in json.dumps(messages)
            assert meta["memory_snapshot"]["epoch_id"] == pins[target["id"], channel]["epoch_id"]


async def test_restart_restores_exact_early_render_and_pending_deltas(tmp_path):
    first = Kernel(tmp_path)
    async with first.lifetime():
        bot = setup(first)
        for scope in SCOPES:
            await change(first, bot, scope, "topic", "original " + scope)
        ingest(first)
        snapshot = first.engine.contexts.memory_prompt.prepare(bot, CHANNEL)
        before, initial = await assemble(first, bot, snapshot=snapshot)
        for scope in SCOPES:
            await change(first, bot, scope, "topic", "pending " + scope)
    restarted = Kernel(tmp_path)
    async with restarted.lifetime():
        bot = restarted.store.get("bots", "ada")
        assert restarted.engine.contexts.memory_prompt.prepare(bot, CHANNEL) == snapshot
        after, current = await assemble(restarted, bot)
        assert through_history(before) == through_history(after)
        for scope in SCOPES:
            assert layer(initial, scope) == layer(current, scope)
            assert delta(current, scope)[0]["note"]["value"] == "pending " + scope


@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize("gate", ["captured_grant", "current_grant", "global_switch"])
async def test_persisted_and_captured_notes_obey_revoked_grants(kernel, scope, gate):
    bot = setup(kernel)
    for name in SCOPES:
        await change(kernel, bot, name, "topic", f"PRIVATE_PIN[{name}]")
    snapshot = kernel.engine.contexts.memory_prompt.prepare(bot, CHANNEL)
    await change(kernel, bot, scope, "topic", f"PRIVATE_DELTA[{scope}]")
    reduced = [name for name in bot["enabled_plugins"] if name != scope]
    if gate == "captured_grant":
        bot = {**bot, "enabled_plugins": reduced}
    elif gate == "current_grant":
        kernel.store.put("bots", {**bot, "enabled_plugins": reduced})
    else:
        kernel.store.put("plugins", {**kernel.store.get("plugins", scope), "enabled": False})
    messages, current = await assemble(kernel, bot, snapshot=snapshot)
    keys = {item["id"] for item in current["prompt_layers"]}
    assert not {scope, scope + "_budget", scope + "_delta"} & keys
    assert f"PRIVATE_PIN[{scope}]" not in json.dumps(messages)
    assert f"PRIVATE_DELTA[{scope}]" not in json.dumps(messages)
    surviving = next(name for name in SCOPES if name != scope)
    assert {surviving, surviving + "_budget", surviving + "_delta"} <= keys
    assert kernel.engine.contexts.memory_prompt.epochs.load(bot["id"], CHANNEL) == snapshot


@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize("mode", ["disabled", "empty", "no_placeholder"])
async def test_note_wrapper_opt_out_never_leaks_persisted_notes_through_delta(kernel, scope, mode):
    bot = setup(kernel)
    await change(kernel, bot, scope, "topic", "HIDDEN_PINNED_VALUE")
    snapshot = kernel.engine.contexts.memory_prompt.prepare(bot, CHANNEL)
    await change(kernel, bot, scope, "topic", "HIDDEN_CURRENT_VALUE")
    if mode == "disabled":
        bot = kernel.store.put("bots", {**bot, "disabled_prompt_layers": [scope, scope + "_delta"]})
    else:
        template(kernel, scope, "" if mode == "empty" else "Notes are intentionally hidden.")
        # An inactive note wrapper does not require a delta template.
        template(kernel, scope + "_delta", "")
    messages, current = await assemble(kernel, bot, snapshot=snapshot)
    assert scope + "_delta" not in {item["id"] for item in current["prompt_layers"]}
    assert "HIDDEN_PINNED_VALUE" not in json.dumps(messages)
    assert "HIDDEN_CURRENT_VALUE" not in json.dumps(messages)
    if mode == "no_placeholder":
        assert layer(current, scope)["content"] == "Notes are intentionally hidden."


@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize("role", ["system", "user"])
async def test_custom_note_and_delta_roles_survive_pinning_and_late_placement(kernel, scope, role):
    bot = setup(kernel)
    template(kernel, scope, "CUSTOM_NOTES:{notes}", role)
    template(kernel, scope + "_delta", "CUSTOM_DELTA:\n{{MEMORY_DELTA}}", role)
    await change(kernel, bot, scope, "topic", "before")
    snapshot = kernel.engine.contexts.memory_prompt.prepare(bot, CHANNEL)
    await change(kernel, bot, scope, "topic", "after")
    extras = [{"role": "assistant", "content": "FINISHED_TOOL_EXCHANGE"}]
    messages, meta = await assemble(kernel, bot, snapshot=snapshot, extras=extras)
    note_index = containing(messages, "CUSTOM_NOTES:")
    delta_index = containing(messages, "CUSTOM_DELTA:")
    assert note_index < containing(messages, "TRANSCRIPT:") < containing(messages, "FINISHED_TOOL_EXCHANGE")
    assert containing(messages, "FINISHED_TOOL_EXCHANGE") < delta_index
    assert messages[note_index]["role"] == messages[delta_index]["role"] == role
    assert layer(meta, scope)["role"] == layer(meta, scope + "_delta")["role"] == role
    assert delta(meta, scope)[0]["note"]["value"] == "after"


@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize("mode", ["disabled", "empty", "missing_placeholder"])
async def test_invalid_delta_template_fails_runtime_before_any_provider_request(kernel, scope, mode):
    bot = setup(kernel)
    ingest(kernel)
    if mode == "disabled":
        bot = kernel.store.put("bots", {**bot, "disabled_prompt_layers": [scope + "_delta"]})
    else:
        template(kernel, scope + "_delta", "" if mode == "empty" else "No current note data here.")
    provider = AsyncMock(return_value=completion("Must not be requested"))
    await install_client(kernel, provider)
    with pytest.raises(ControlError, match=scope + "_delta"):
        await assemble(kernel, bot)
    await kernel.engine.tick()
    await settle(kernel)
    provider.assert_not_awaited()
    assert kernel.store.one("SELECT status FROM turns")["status"] == "failed"
    assert not kernel.store.rows("SELECT id FROM requests")
    assert not kernel.store.rows("SELECT * FROM memory_snapshots")


@pytest.mark.parametrize("scope", SCOPES)
async def test_editing_note_template_replaces_pin_only_at_explicit_preparation(kernel, scope):
    bot = setup(kernel)
    await change(kernel, bot, scope, "topic", "initial")
    memory = kernel.engine.contexts.memory_prompt
    original = memory.prepare(bot, CHANNEL)
    await change(kernel, bot, scope, "topic", "current")
    template(kernel, scope, "REVISED_WRAPPER:{{NOTES}}", "user")
    messages, current = await assemble(kernel, bot, snapshot=original)
    assert layer(current, scope)["content"].startswith("REVISED_WRAPPER:")
    assert layer(current, scope)["role"] == "user"
    assert "current" in layer(current, scope)["content"]
    assert delta(current, scope) == []
    assert "initial" not in layer(current, scope)["content"]
    assert containing(messages, "REVISED_WRAPPER:") < containing(messages, "TRANSCRIPT:")
    assert memory.epochs.load(bot["id"], CHANNEL) == original
    replacement = memory.prepare(bot, CHANNEL)
    assert replacement["epoch_id"] != original["epoch_id"]
    assert replacement["notes"][scope]["topic"]["value"] == "current"
    assert memory.epochs.load(bot["id"], CHANNEL) == replacement


async def test_repeated_assembly_is_read_only_with_and_without_a_persisted_pin(kernel):
    bot = setup(kernel)
    await change(kernel, bot, "memory", "topic", "first")
    memory = kernel.engine.contexts.memory_prompt
    for _ in range(2):
        await assemble(kernel, bot)
        assert memory.epochs.load(bot["id"], CHANNEL) is None
    snapshot = memory.prepare(bot, CHANNEL)
    detached = deepcopy(snapshot)
    await change(kernel, bot, "memory", "topic", "second")
    for explicit in (None, snapshot, snapshot):
        _, meta = await assemble(kernel, bot, snapshot=explicit)
        assert delta(meta, "memory") == [
            {"operation": "replace", "note": {"key": "topic", "value": "second"}}
        ]
        assert memory.epochs.load(bot["id"], CHANNEL) == detached
    assert snapshot == detached


async def test_new_vault_secret_redacts_preexisting_global_pin_and_its_complete_delta(kernel):
    bot = setup(kernel)
    secret = "late-known-fixture-credential-0123456789"
    await change(kernel, bot, "global_memory", "topic", "Old saved reference: " + secret)
    memory = kernel.engine.contexts.memory_prompt
    snapshot = memory.prepare(bot, CHANNEL)
    before, initial = await assemble(kernel, bot, snapshot=snapshot)
    assert secret in json.dumps(before)
    assert secret in layer(initial, "global_memory")["content"]

    kernel.vault.put("plugin/test/api_key", secret)
    after, current = await assemble(kernel, bot, snapshot=snapshot)
    assert secret not in json.dumps(after)
    assert secret not in json.dumps(current["prompt_layers"])
    assert "[REDACTED]" in layer(current, "global_memory")["content"]
    changes = delta(current, "global_memory")
    assert len(changes) == 1 and changes[0]["operation"] == "replace"
    assert changes[0]["note"]["key"] == "topic"
    assert changes[0]["note"]["value"] == "Old saved reference: [REDACTED]"
    assert current["memory_snapshot"]["epoch_id"] == snapshot["epoch_id"]
    assert memory.epochs.load(bot["id"], CHANNEL) == snapshot
    # Current projection redacts without rewriting private historical storage.
    assert secret in kernel.registry.global_memory.notes(bot["id"])[0]["value"]


@pytest.mark.parametrize("kind", ["slash", "panel"])
async def test_repeated_invocations_reuse_their_own_pins_and_refresh_scoped_deltas(kernel, kind):
    bot = setup(kernel)
    channel = f"{kind}:{bot['id']}:{CHANNEL}"
    other_kind = "panel" if kind == "slash" else "slash"
    other_scope = f"{other_kind}:{bot['id']}:{CHANNEL}"
    template(kernel, kind + "_invocation", "INVOCATION:{invocation}", "user")
    await change(kernel, bot, "memory", "topic", "ORDINARY_CHANNEL_ONLY")
    await change(kernel, bot, "memory", "topic", "OTHER_INVOCATION_KIND_ONLY", channel=other_scope)
    await change(kernel, bot, "memory", "topic", "PINNED_INVOCATION_NOTE", channel=channel)
    await change(kernel, bot, "global_memory", "topic", "PINNED_GLOBAL_NOTE")
    ordinary = kernel.engine.contexts.memory_prompt.prepare(bot, CHANNEL)
    another_kind = kernel.engine.contexts.memory_prompt.prepare(bot, other_scope)
    ingest(kernel, content="ORDINARY_TRANSCRIPT_ONLY")
    assembled = []
    prepared = []
    profile = kernel.store.get("profiles", "balanced")

    for index in range(2):
        turn_id = f"{kind}-turn-{index}"
        invocation = {
            "kind": kind,
            "interaction_id": f"98765432101234567{index}",
            "channel_id": CHANNEL,
            "bot_id": bot["id"],
        }
        captured = {**bot, "invocation": invocation}
        kernel.store.execute(
            "INSERT INTO slash_invocations "
            "(interaction_id,bot_id,application_id,channel_id,actor_id,prompt,private,status,turn_id,started_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                invocation["interaction_id"],
                bot["id"],
                bot["application_id"],
                CHANNEL,
                OWNER_ID,
                f"Invocation prompt {index}",
                1,
                "running",
                turn_id,
                1,
            ),
        )
        contexts = SlashContexts(kernel.store, kernel.pool, invocation, kernel.registry.global_memory)
        rows, summary, _, meta = await contexts.prepare(captured, profile, channel, turn_id, [])
        prepared.append(meta["_memory_snapshot"])
        assembled.append(
            await contexts.assemble(
                captured, profile, channel, rows, summary, memory_snapshot=meta["_memory_snapshot"]
            )
        )
        if index == 0:
            await change(kernel, captured, "memory", "topic", "CURRENT_INVOCATION_NOTE", channel=channel)
            await change(kernel, captured, "global_memory", "topic", "CURRENT_GLOBAL_NOTE", channel=channel)

    (before, initial), (after, current) = assembled
    assert prepared[0] == prepared[1]
    boundary = containing(before, "INVOCATION:")
    assert before[:boundary] == after[:boundary]
    assert "Invocation prompt 0" in before[containing(before, "TRANSCRIPT:")]["content"]
    assert "Invocation prompt 1" in after[containing(after, "TRANSCRIPT:")]["content"]
    assert "Invocation prompt 0" not in json.dumps(after)
    for scope, value in (("memory", "CURRENT_INVOCATION_NOTE"), ("global_memory", "CURRENT_GLOBAL_NOTE")):
        assert layer(initial, scope) == layer(current, scope)
        assert delta(initial, scope) == []
        assert delta(current, scope)[0]["note"]["value"] == value
    assert delta(current, "global_memory")[0]["note"]["source_channel_id"] == CHANNEL
    assert all(
        hidden not in json.dumps(after)
        for hidden in ("ORDINARY_CHANNEL_ONLY", "OTHER_INVOCATION_KIND_ONLY", "ORDINARY_TRANSCRIPT_ONLY")
    )
    assert kernel.engine.contexts.memory_prompt.epochs.load(bot["id"], CHANNEL) == ordinary
    assert kernel.engine.contexts.memory_prompt.epochs.load(bot["id"], other_scope) == another_kind


@pytest.mark.parametrize("scope", SCOPES)
async def test_runtime_two_memory_writes_keep_prefix_and_tool_schema_stable(kernel, scope):
    bot = setup(kernel)
    await change(kernel, bot, scope, "topic", "PINNED_ORIGINAL")
    ingest(kernel, content="Keep working until both note edits are done.")
    requests = []

    async def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) <= 2:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "edit-" + str(len(requests)),
                                        "type": "function",
                                        "function": {
                                            "name": scope,
                                            "arguments": json.dumps(
                                                {
                                                    "operation": "write",
                                                    "key": "topic",
                                                    "value": "revision " + str(len(requests)),
                                                }
                                            ),
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            )
        return completion("Both edits are saved.")

    await install_client(kernel, handle)
    kernel.engine.transport = AsyncMock()
    kernel.engine.transport.send.return_value = "888888888888888888"
    await kernel.engine.tick()
    await settle(kernel)
    assert len(requests) == 3
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    evidence = [
        json.loads(row["context"])
        for row in kernel.store.rows("SELECT context FROM requests WHERE purpose='generation' ORDER BY rowid")
    ]
    assert len(evidence) == 3
    assert delta(evidence[0], scope) == []
    assert [delta(meta, scope)[0]["note"]["value"] for meta in evidence[1:]] == ["revision 1", "revision 2"]
    assert len({meta["memory_snapshot"]["epoch_id"] for meta in evidence}) == 1
    assert all(
        through_history(body["messages"]) == through_history(requests[0]["messages"]) for body in requests
    )
    assert all(body["tools"] == requests[0]["tools"] for body in requests)
    assert all(layer(meta, scope) == layer(evidence[0], scope) for meta in evidence)
    assert kernel.engine.transport.send.call_args.args[2] == "Both edits are saved."
