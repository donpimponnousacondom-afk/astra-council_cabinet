import asyncio
import json
import time

import pytest

from conftest import configured, ingest
from hortator.engrams import (
    DEFAULTS,
    PLUGIN_ID,
    Engrams,
    parse_response,
    register,
    sanitize_response,
    summary_hash,
    validate_config,
)
from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.store import dumps


NONCE = "abcdef1234567890abcdef1234567890"


def wire(state=None, *, nonce=NONCE, answer="Public answer."):
    state = (
        {"MEM": "Next: confirm the date.", "FACTS": "The owner prefers concise answers."}
        if state is None
        else state
    )
    return f"{answer}\n[^ENGRAM:{nonce}]\n{dumps(state)}\n[^END:{nonce}]"


@pytest.fixture
def memory(kernel):
    if PLUGIN_ID not in kernel.registry.specs:
        register(kernel.registry)
        kernel.service.seed_plugins()
    return kernel.registry.engrams


def enabled(kernel, *, bot_id="ada", config=None):
    plugin = kernel.store.get("plugins", PLUGIN_ID)
    if not plugin["enabled"] or config is not None:
        kernel.store.put("plugins", {**plugin, "enabled": True, "config": {**DEFAULTS, **(config or {})}})
    return configured(kernel, bot_id=bot_id, enabled_plugins=[PLUGIN_ID])


def generation(kernel, snapshot, request_id="req", *, status="running"):
    store = kernel.store
    store.execute(
        "INSERT INTO turns(id,bot_id,channel_id,profile_id,provider_id,model,started_at,status,trigger,revision) "
        "VALUES(?,?,?,'balanced','openrouter','test-model',?,?,'manual_trigger',1)",
        (snapshot["turn_id"], snapshot["bot_id"], snapshot["channel_id"], time.time(), status),
    )
    store.execute(
        "INSERT INTO requests(id,turn_id,bot_id,provider_id,profile_id,model,purpose,started_at,status,body,context,response) "
        "VALUES(?,?,?,'openrouter','balanced','test-model','generation',?,'completed','{}','{}',?)",
        (
            request_id,
            snapshot["turn_id"],
            snapshot["bot_id"],
            time.time(),
            dumps({"content": "Public answer.", "finish_reason": "stop"}),
        ),
    )
    return request_id


def delivered(kernel, snapshot, *, status="sent", turn_status="sent", outbox_id="out"):
    kernel.store.execute(
        "INSERT INTO outbox(id,turn_id,bot_id,channel_id,content,status,created_at,discord_id,routing) "
        "VALUES(?,?,?,?,'Public answer.',?,?,?,'{}')",
        (
            outbox_id,
            snapshot["turn_id"],
            snapshot["bot_id"],
            snapshot["channel_id"],
            status,
            time.time(),
            "discord-confirmed" if status == "sent" else None,
        ),
    )
    kernel.store.execute("UPDATE turns SET status=? WHERE id=?", (turn_status, snapshot["turn_id"]))
    return outbox_id


async def staged(
    kernel, memory, bot, *, channel_id="room", turn_id="turn", request_id="req", covered=20, state=None
):
    snapshot = memory.capture(bot, channel_id, turn_id)
    parsed = await memory.parse(snapshot, wire(state, nonce=snapshot["nonce"]))
    generation(kernel, snapshot, request_id)
    candidate = memory.stage(snapshot, parsed, request_id, covered_through=covered)
    return snapshot, parsed, candidate


def test_wire_roundtrip_escaping_unicode_and_public_answer_are_separate():
    state = {"MEM": 'Facts contain "quotes", newlines\nand [^ENGRAM:example].', "FACTS": "Åda 🦆\x00"}
    parsed = parse_response(wire(state), NONCE, DEFAULTS)
    assert parsed["content"] == "Public answer."
    assert parsed["state"] == state
    assert parsed["state_chars"] == sum(map(len, state.values()))
    assert parsed["state_tokens"] > 0
    assert parsed["invalid"] is None


@pytest.mark.parametrize(
    "payload,reason",
    [
        ('{"MEM":"a","FACTS":"b","MEM":"changed"}', "invalid_state_json"),
        ('{"MEM":"a"}', "invalid_state_fields"),
        ('{"MEM":[],"FACTS":"b"}', "invalid_state_fields"),
        ('{"MEM":"a","FACTS":"b","owner":"other"}', "invalid_state_fields"),
        ('{"MEM":"\\ud800","FACTS":"b"}', "invalid_state_unicode"),
        ('{"MEM":"a","FACTS":"b"', "invalid_state_json"),
        ('```json\n{"MEM":"a","FACTS":"b"}\n```', "invalid_state_json"),
    ],
)
def test_malformed_state_keeps_only_public_prefix(payload, reason):
    response = f"Visible.\n[^ENGRAM:{NONCE}]\n{payload}\n[^END:{NONCE}]"
    parsed = parse_response(response, NONCE, DEFAULTS)
    assert parsed["content"] == "Visible."
    assert parsed["state"] is None and parsed["invalid"] == reason
    assert sanitize_response(response, NONCE) == "Visible."


def test_missing_truncated_duplicate_wrong_nonce_and_trailing_text_do_not_commit():
    assert parse_response("Normal answer.", NONCE, DEFAULTS)["invalid"] == "missing_state"
    samples = [
        wire()[:-10],
        wire() + "\nextra public-looking suffix",
        wire() + "\n" + wire(),
        wire(nonce="f" * 32),
        f"Public answer.\n[^END:{NONCE}]\nsecret-looking discarded text",
    ]
    for response in samples:
        parsed = parse_response(response, NONCE, DEFAULTS)
        assert parsed["state"] is None and parsed["invalid"]
        assert "prefers concise" not in parsed["content"]


def test_unrelated_examples_survive_but_known_current_nonce_fails_closed():
    example = "Example:\n```text\n[^ENGRAM:example]\nnot a live state\n[^END:example]\n```\nDone."
    assert sanitize_response(example, NONCE) == example
    quote = "> [^ENGRAM:example]\n> quoted footnote"
    assert sanitize_response(quote, NONCE) == quote
    assert (
        sanitize_response("Explain `[^ENGRAM:example]` literally.", NONCE)
        == "Explain `[^ENGRAM:example]` literally."
    )
    assert "prefers concise" not in sanitize_response("Public.\n```text\n" + wire(answer=""), NONCE)


@pytest.mark.parametrize(
    "opener",
    [
        f"[^ENGRAM:{NONCE}",
        f"[^ENGRAM:{NONCE[:10]}",
        "[^ENGRAM:",
        "[^ENGRAM",
        f"[^END:{NONCE}",
        f"[^engram:{NONCE}]",
        f"  [^ENGRAM:{NONCE}]",
    ],
)
def test_incomplete_or_malformed_reserved_opener_never_exposes_state(opener):
    text = "Visible.\n" + opener + '\n{"MEM":"private state","FACTS":"sensitive fact"}'
    assert sanitize_response(text, NONCE) == "Visible."
    parsed = parse_response(text, NONCE, DEFAULTS)
    assert parsed["state"] is None and parsed["content"] == "Visible." and parsed["invalid"]


def test_current_nonce_is_private_even_when_model_omits_line_boundary():
    text = f'Visible. [^ENGRAM:{NONCE}]{{"MEM":"private state","FACTS":"fact"}}'
    assert sanitize_response(text, NONCE) == "Visible."
    assert parse_response(text, NONCE, DEFAULTS)["state"] is None


def test_exact_nonce_inside_unclosed_code_example_cannot_update_state():
    text = "Public example:\n```text\n" + wire(answer="")
    parsed = parse_response(text, NONCE, DEFAULTS)
    assert parsed["state"] is None and parsed["invalid"] == "fenced_state"
    assert "prefers concise" not in parsed["content"]


def test_state_quotas_count_only_values_and_intermediate_response_never_updates():
    value = {"MEM": "é\x00🦆", "FACTS": "a"}
    parsed = parse_response(wire(value), NONCE, {**DEFAULTS, "state_char_limit": 4})
    assert parsed["state"] == value
    assert (
        parse_response(wire(value), NONCE, {**DEFAULTS, "state_char_limit": 3})["invalid"]
        == "state_char_limit"
    )
    assert (
        parse_response(wire(value), NONCE, {**DEFAULTS, "state_token_limit": 1})["invalid"]
        == "state_token_limit"
    )
    assert parse_response(wire(), NONCE, DEFAULTS, final=False)["state"] is None
    assert parse_response(wire(), NONCE, DEFAULTS, final=False)["content"] == "Public answer."
    assert parse_response("Tool preamble", NONCE, DEFAULTS, final=False)["invalid"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"state_char_limit": 0},
        {"state_char_limit": True},
        {"state_char_limit": 128001},
        {"state_token_limit": 0},
        {"state_token_limit": 32001},
        {"recent_messages": 1.5},
        {"recent_messages": 0},
        {"recent_messages": 1001},
        {"reduce_history": "false"},
        {"unknown": 1},
    ],
)
def test_invalid_settings_are_rejected(changes):
    with pytest.raises(ControlError):
        validate_config(changes)


async def test_registration_is_disabled_non_tool_and_requires_current_grants(kernel, memory):
    assert kernel.store.get("plugins", PLUGIN_ID)["enabled"] is False
    bot = configured(kernel, enabled_plugins=[PLUGIN_ID])
    assert memory.capture(bot, "room", "turn") is None
    bot = enabled(kernel)
    assert memory.capture(bot, "room", "turn")
    context = ToolContext(bot, "room", "turn")
    assert PLUGIN_ID not in {item["function"]["name"] for item in kernel.registry.schemas(context)}
    assert (await kernel.registry.call(PLUGIN_ID, {}, context, "call"))["ok"] is False
    assert memory.capture({**bot, "invocation": {"kind": "slash"}}, "room", "turn") is None
    assert memory.capture(bot, "slash:ada:room", "turn") is None
    assert memory.capture(bot, "panel:ada:room", "turn") is None
    kernel.store.put("bots", {**bot, "enabled_plugins": []})
    assert memory.capture(bot, "room", "turn") is None


@pytest.mark.parametrize("customization", ["edit_default", "bot_override"])
async def test_durable_identity_rule_survives_custom_engram_prompts(kernel, memory, owner, customization):
    bot = enabled(kernel)
    overrides = {}
    for layer, content in (
        ("engram_instructions", "Keep the next state concise."),
        ("engram_state", "Prior notes: {engram_state}"),
    ):
        template_id = "runtime-" + layer.replace("_", "-")
        if customization == "bot_override":
            template_id = "custom-" + layer
            overrides[layer] = template_id
            await kernel.service.save(
                owner,
                "prompts",
                template_id,
                {"name": layer, "runtime_layer": layer, "role": "system", "content": content},
                create=True,
            )
        else:
            await kernel.service.save(owner, "prompts", template_id, {"content": content})
    bot = await kernel.service.save(
        owner,
        "bots",
        bot["id"],
        {"transcript_format": "conversation", "prompt_layer_overrides": overrides},
    )
    channel_id = "222222222222222222"
    ingest(kernel)
    profile = kernel.store.get("profiles", "balanced")
    rows, summary, _, original_meta = await kernel.engine.contexts.prepare(
        bot, profile, channel_id, "identity-trial", []
    )
    messages, meta = await kernel.engine.contexts.assemble(
        bot, profile, channel_id, rows, summary, engram=original_meta["_engram_snapshot"]
    )
    layers = {layer["id"]: layer for layer in meta["prompt_layers"]}
    assert layers["engram_instructions"]["content"] == "Keep the next state concise."
    assert layers["engram_state"]["content"].startswith("Prior notes: ")
    protocol = layers["engram_protocol"]["content"]
    assert "Identify people by their names and user IDs" in protocol
    assert "never store temporary transcript P labels in MEM or FACTS" in protocol
    assert "those labels are reassigned in every request" in protocol
    assert messages[-1] == {"role": "system", "content": protocol}


async def test_final_state_replaces_atomically_only_after_confirmed_delivery(kernel, memory):
    bot = enabled(kernel)
    snapshot, parsed, candidate = await staged(kernel, memory, bot)
    assert memory.inspect(bot["id"])["states"] == []
    assert memory.finalize(candidate) is False
    delivered(kernel, snapshot)
    assert memory.finalize(candidate) is True
    saved = memory.inspect(bot["id"])["states"][0]
    assert saved["revision"] == 1 and saved["covered_through"] == 20
    assert saved["MEM"] == parsed["state"]["MEM"]
    assert memory.finalize(candidate) is True
    assert memory.inspect(bot["id"])["states"][0]["revision"] == 1
    next_snapshot, _, next_candidate = await staged(
        kernel,
        memory,
        bot,
        turn_id="next",
        request_id="next-req",
        covered=22,
        state={"MEM": "Only the corrected replacement remains.", "FACTS": ""},
    )
    delivered(kernel, next_snapshot, outbox_id="next-out")
    assert memory.finalize(next_candidate)
    saved = memory.inspect(bot["id"])["states"][0]
    assert saved["revision"] == 2 and saved["covered_through"] == 22 and saved["FACTS"] == ""
    assert "prefers concise" not in json.dumps(kernel.store.events())


@pytest.mark.parametrize(
    "box_status,turn_status", [("unknown", "sent"), ("failed", "failed"), ("sent", "cancelled")]
)
async def test_uncertain_failed_and_cancelled_delivery_preserve_old_state(
    kernel, memory, box_status, turn_status
):
    bot = enabled(kernel)
    snapshot, _, candidate = await staged(kernel, memory, bot)
    delivered(kernel, snapshot, status=box_status, turn_status=turn_status)
    assert memory.finalize(candidate) is False
    assert memory.inspect(bot["id"])["states"] == []
    assert memory.recover()["committed"] == 0


async def test_recovery_uses_durable_confirmed_delivery_and_never_resends(kernel, memory):
    bot = enabled(kernel)
    snapshot, _, candidate = await staged(kernel, memory, bot)
    delivered(kernel, snapshot)
    restored_service = Engrams(kernel.store, kernel.vault)
    assert restored_service.recover() == {"examined": 1, "committed": 1}
    assert restored_service.inspect(bot["id"])["states"][0]["covered_through"] == 20
    assert len(kernel.store.rows("SELECT * FROM outbox")) == 1
    assert (
        kernel.store.one("SELECT status FROM engram_candidates WHERE id=?", (candidate,))["status"]
        == "committed"
    )


async def test_stale_revision_reset_cutoff_and_grant_change_reject_candidates(kernel, memory):
    bot = enabled(kernel)
    snapshot, parsed, candidate = await staged(kernel, memory, bot)
    delivered(kernel, snapshot)
    memory.reset(bot["id"], "room")
    assert not memory.finalize(candidate)
    assert memory.inspect(bot["id"])["states"] == []
    with pytest.raises(ControlError):
        memory.stage(snapshot, parsed, "req", covered_through=20)
    fresh, _, other = await staged(kernel, memory, bot, turn_id="turn2", request_id="req2")
    delivered(kernel, fresh, outbox_id="out2")
    kernel.store.execute(
        "INSERT OR REPLACE INTO context_resets(bot_id,channel_id,after_seq,after_at) VALUES(?,?,?,?)",
        (bot["id"], "*", 10, time.time()),
    )
    assert not memory.finalize(other)
    third, _, revoked = await staged(kernel, memory, bot, turn_id="turn3", request_id="req3")
    delivered(kernel, third, outbox_id="out3")
    kernel.store.put("bots", {**bot, "enabled_plugins": []})
    assert not memory.finalize(revoked)


async def test_revision_conflict_does_not_overwrite_newer_state_and_scope_cannot_bind(kernel, memory):
    bot = enabled(kernel)
    first, _, a = await staged(kernel, memory, bot)
    second, _, b = await staged(kernel, memory, bot, turn_id="parallel", request_id="req2", covered=21)
    delivered(kernel, first)
    delivered(kernel, second, outbox_id="out2")
    with pytest.raises(ControlError, match="scope"):
        memory.bind_outbox(a, "out2")
    assert memory.finalize(a)
    assert not memory.finalize(b)
    assert memory.inspect(bot["id"])["states"][0]["covered_through"] == 20


async def test_plugin_config_revision_change_preserves_previous_state(kernel, memory):
    bot = enabled(kernel)
    snapshot, _, candidate = await staged(kernel, memory, bot)
    delivered(kernel, snapshot)
    plugin = kernel.store.get("plugins", PLUGIN_ID)
    kernel.store.put("plugins", {**plugin, "config": {**DEFAULTS, "state_char_limit": 10}})
    assert not memory.finalize(candidate)
    assert memory.inspect(bot["id"])["states"] == []


@pytest.mark.parametrize(
    "response",
    [
        {"finish_reason": "length"},
        {"finish_reason": None},
        {"finish_reason": "stop", "tool_calls": [{"id": "call"}]},
    ],
)
async def test_staging_defensively_rejects_intermediate_or_incomplete_request(kernel, memory, response):
    bot = enabled(kernel)
    snapshot = memory.capture(bot, "room", "turn")
    parsed = await memory.parse(snapshot, wire(nonce=snapshot["nonce"]))
    generation(kernel, snapshot)
    kernel.store.execute("UPDATE requests SET response=? WHERE id='req'", (dumps(response),))
    with pytest.raises(ControlError, match="complete final answer"):
        memory.stage(snapshot, parsed, "req", covered_through=20)
    assert memory.inspect(bot["id"])["pending_candidates"] == 0


async def test_state_is_private_to_bot_and_channel(kernel, memory):
    bot = enabled(kernel)
    other = enabled(kernel, bot_id="socrates")
    snapshot, _, candidate = await staged(kernel, memory, bot)
    delivered(kernel, snapshot)
    assert memory.finalize(candidate)
    assert memory.capture(bot, "another-room", "next")["state"] == {"MEM": "", "FACTS": ""}
    assert memory.capture(other, "room", "next")["state"] == {"MEM": "", "FACTS": ""}
    assert memory.inspect(other["id"])["states"] == []


async def test_reduction_retains_all_uncovered_messages_and_is_independent_of_summary(kernel, memory):
    bot = enabled(kernel, config={"reduce_history": True, "recent_messages": 3})
    snapshot, _, candidate = await staged(kernel, memory, bot, covered=20)
    delivered(kernel, snapshot)
    assert memory.finalize(candidate)
    fresh = memory.capture(bot, "room", "next")
    rows = [{"seq": i} for i in range(1, 51)]
    assert [row["seq"] for row in memory.select_rows(fresh, rows)] == list(range(18, 51))
    assert memory.can_replace_summary(fresh, 0, "")
    assert not memory.can_replace_summary(fresh, 0, "Changed summary")
    assert not memory.can_replace_summary(fresh, 21, "")
    empty = memory.capture(bot, "new-room", "next")
    assert memory.select_rows(empty, rows) == rows
    shadow = {**fresh, "config": {**fresh["config"], "reduce_history": False}}
    assert memory.select_rows(shadow, rows) == rows


async def test_scope_reset_isolated_and_all_reset_invalidates_previously_absent_scope(kernel, memory):
    bot = enabled(kernel)
    for room, turn in [("room", "one"), ("another", "two")]:
        snapshot, _, candidate = await staged(
            kernel, memory, bot, channel_id=room, turn_id=turn, request_id=turn
        )
        delivered(kernel, snapshot, outbox_id=turn)
        assert memory.finalize(candidate)
    absent = memory.capture(bot, "absent", "future")
    memory.reset(bot["id"], "room")
    states = {row["channel_id"]: row for row in memory.inspect(bot["id"])["states"]}
    assert states["room"]["MEM"] == "" and states["room"]["covered_through"] == 0
    assert states["another"]["MEM"]
    memory.reset(bot["id"])
    assert memory.capture(bot, "absent", "future")["epoch"] != absent["epoch"]
    assert all(not row["MEM"] for row in memory.inspect(bot["id"])["states"])


async def test_reset_and_commit_join_outer_transaction_and_roll_back(kernel, memory):
    bot = enabled(kernel)
    snapshot, _, candidate = await staged(kernel, memory, bot)
    delivered(kernel, snapshot)
    kernel.store.execute("BEGIN IMMEDIATE")
    assert memory.finalize(candidate)
    kernel.store.execute("ROLLBACK")
    assert memory.inspect(bot["id"])["states"] == []
    assert (
        kernel.store.one("SELECT status FROM engram_candidates WHERE id=?", (candidate,))["status"]
        == "staged"
    )
    assert memory.finalize(candidate)
    before = memory.inspect(bot["id"])
    kernel.store.execute("BEGIN IMMEDIATE")
    memory.reset(bot["id"])
    kernel.store.execute("ROLLBACK")
    assert memory.inspect(bot["id"]) == before


async def test_parser_redacts_known_secrets_and_cancelled_worker_never_mutates_state(
    kernel, memory, monkeypatch
):
    bot = enabled(kernel)
    kernel.vault.put("test/engram", "credential-example-not-for-models")
    snapshot = memory.capture(bot, "room", "turn")
    parsed = await memory.parse(
        snapshot, wire({"MEM": "credential-example-not-for-models", "FACTS": ""}, nonce=snapshot["nonce"])
    )
    assert "credential-example-not-for-models" not in dumps(parsed)
    assert memory.inspect(bot["id"])["states"] == []

    async def cancelled_worker(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "to_thread", cancelled_worker)
    with pytest.raises(asyncio.CancelledError):
        await memory.parse(snapshot, wire(nonce=snapshot["nonce"]))
    assert memory.inspect(bot["id"])["states"] == []
    assert summary_hash("") == snapshot["context_summary_hash"]
