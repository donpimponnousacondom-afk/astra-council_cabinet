"""Interrupted-cycle receipt metadata, bounds, isolation and durability."""

import json
import time
from types import SimpleNamespace

import pytest

from hortator.store import Store


from hortator import action_receipts as DRAFT
from hortator.action_receipts import ActionReceipts


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "receipts.sqlite3")
    try:
        yield value
    finally:
        value.close()


def context(
    store,
    turn_id,
    *,
    status="running",
    bot_id="ada",
    channel="channel",
    mode="progress",
    trigger="human_mention",
):
    store.execute(
        "INSERT INTO turns(id,bot_id,channel_id,profile_id,provider_id,model,started_at,ended_at,status,trigger,revision) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            turn_id,
            bot_id,
            channel,
            "profile",
            "provider",
            "model",
            time.time(),
            None if status == "running" else time.time(),
            status,
            trigger,
            1,
        ),
    )
    return SimpleNamespace(bot={"id": bot_id, "response_protocol": mode}, channel_id=channel, turn_id=turn_id)


def finish_turn(store, context, status="cancelled"):
    store.execute("UPDATE turns SET status=?,ended_at=? WHERE id=?", (status, time.time(), context.turn_id))


def saved(journal, context, key="note"):
    ref = journal.start(
        context, "global_memory", "call", {"operation": "write", "key": key, "value": "PRIVATE NOTE"}
    )
    journal.finish(context, ref, {"saved": True, "key": key, "notes": [{"value": "PRIVATE NOTE"}]})
    return ref


def all_allowed(name, context):
    return True


def facts(offer):
    return json.loads(offer.message["content"][len(DRAFT.NOTICE) :])


def test_memory_receipt_is_metadata_only_and_survives_cancel(store):
    journal = ActionReceipts(store)
    original = context(store, "first")
    ref = saved(journal, original)
    journal.discard_completed(original, "cancelled")
    finish_turn(store, original)
    receiving = context(store, "second")
    offer = journal.offer(receiving, all_allowed)
    assert offer.receipt_ids == (("first", ref),)
    entry = facts(offer)["cycles"][0]["receipts"][0]
    assert entry == {
        "call_id": "call",
        "tool": "global_memory",
        "outcome": "saved",
        "operation": "write",
        "key": "note",
    }
    assert "PRIVATE NOTE" not in offer.message["content"]
    assert "PRIVATE NOTE" not in store.one("SELECT body FROM turn_action_receipts")["body"]
    assert store.one("SELECT 1 FROM turn_action_receipts")  # Selection does not consume.
    journal.acknowledge(receiving, offer)
    assert not store.one("SELECT 1 FROM turn_action_receipts")


@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        ({"executed": False, "ok": False, "error": "PRIVATE ERROR"}, "refused"),
        ({"usage_only": True}, "refused"),
        ({"ok": False, "error": "MAY HAVE PARTIAL EFFECTS"}, "error"),
        ({"stdout": "PRIVATE STDOUT", "exit_code": 1}, "returned"),
        ({"ok": True, "url": "https://private.example", "result_id": "OLD_RESULT"}, "returned"),
    ],
)
def test_generic_outcomes_do_not_claim_successful_external_effects(store, result, outcome):
    journal = ActionReceipts(store)
    original = context(store, "first")
    ref = journal.start(original, "shell", "call", {"operation": "run", "command": "PRIVATE COMMAND"})
    journal.finish(original, ref, result)
    finish_turn(store, original)
    offer = journal.offer(context(store, "second"), all_allowed)
    entry = facts(offer)["cycles"][0]["receipts"][0]
    assert entry["outcome"] == outcome
    assert set(entry) == {"call_id", "tool", "outcome", "operation"}
    assert not any(token in offer.message["content"] for token in ("PRIVATE", "OLD_RESULT", "https://"))


def test_memory_delete_proof_and_wrong_key_cannot_be_called_saved(store):
    journal = ActionReceipts(store)
    original = context(store, "first")
    a = journal.start(original, "memory", "same-call-id", {"operation": "delete", "key": "gone"})
    journal.finish(original, a, {"saved": True, "key": "gone"})
    b = journal.start(original, "memory", "same-call-id", {"operation": "write", "key": "target"})
    journal.finish(original, b, {"saved": True, "key": "different"})
    assert a != b
    body = json.loads(store.one("SELECT body FROM turn_action_receipts")["body"])
    assert [r["outcome"] for r in body["receipts"]] == ["deleted", "returned"]


def test_restart_preserves_unknown_and_confirmed_receipts(tmp_path):
    path = tmp_path / "restart.sqlite3"
    store = Store(path)
    journal = ActionReceipts(store)
    original = context(store, "first")
    saved(journal, original)
    journal.start(original, "web_fetch", "pending", {"operation": "fetch", "url": "https://private.example"})
    store.close()
    reopened = Store(path)
    try:
        reopened.recover()
        receiving = context(reopened, "second")
        offer = ActionReceipts(reopened).offer(receiving, all_allowed)
        assert facts(offer)["cycles"][0]["status"] == "interrupted"
        assert [item["outcome"] for item in facts(offer)["cycles"][0]["receipts"]] == ["saved", "unknown"]
    finally:
        reopened.close()


def test_multiple_interrupted_cycles_accumulate_and_acknowledge_only_selected(store):
    journal = ActionReceipts(store)
    first = context(store, "first")
    saved(journal, first)
    finish_turn(store, first)
    second = context(store, "second")
    inherited = journal.offer(second, all_allowed)
    assert len(inherited.receipt_ids) == 1
    second_ref = journal.start(second, "memory", "later", {"operation": "write", "key": "later"})
    journal.finish(second, second_ref, {"saved": True, "key": "later"})
    finish_turn(store, second, "failed")
    third = context(store, "third")
    offer = journal.offer(third, lambda name, _: name == "memory")
    assert offer.receipt_ids == (("second", second_ref),)
    journal.acknowledge(third, offer)
    assert [r["turn_id"] for r in store.rows("SELECT turn_id FROM turn_action_receipts")] == ["first"]
    assert journal.offer(third, all_allowed).receipt_ids == inherited.receipt_ids


@pytest.mark.parametrize("mode", ["end_cycle"])
def test_excluded_mode_records_and_offers_nothing(store, mode):
    journal = ActionReceipts(store)
    ctx = context(store, "one", mode=mode)
    assert journal.start(ctx, "memory", "call", {}) is None
    assert journal.offer(ctx, all_allowed) is None


def test_invocations_are_excluded_and_stored_trigger_defensively_checked(store):
    journal = ActionReceipts(store)
    ctx = context(store, "one")
    ctx.bot["invocation"] = {"kind": "slash"}
    assert journal.start(ctx, "memory", "call", {}) is None
    ctx.bot.pop("invocation")
    saved(journal, ctx)
    finish_turn(store, ctx)
    store.execute("UPDATE turns SET trigger='slash_prompt' WHERE id='one'")
    assert journal.offer(context(store, "two"), all_allowed) is None


def test_scope_grants_and_clean_slate_prevent_disclosure(store):
    journal = ActionReceipts(store)
    original = context(store, "first")
    saved(journal, original)
    finish_turn(store, original)
    assert journal.offer(context(store, "other-bot", bot_id="loki"), all_allowed) is None
    assert journal.offer(context(store, "other-channel", channel="other"), all_allowed) is None
    receiving = context(store, "same")
    assert journal.offer(receiving, lambda *_: False) is None
    store.execute("INSERT INTO context_resets VALUES(?,?,?,?)", ("ada", "channel", 1, time.time()))
    assert journal.offer(receiving, all_allowed) is None


def test_partial_offer_bounds_and_selected_acknowledgement(store):
    journal = ActionReceipts(store)
    original = context(store, "first")
    for index in range(40):
        saved(journal, original, "note-" + str(index) + "x" * 180)
    body = json.loads(store.one("SELECT body FROM turn_action_receipts")["body"])
    assert len(body["receipts"]) == 32 and body["omitted_count"] == 8
    finish_turn(store, original)
    receiving = context(store, "second")
    assert journal.offer(receiving, all_allowed, max_chars=10) is None
    offer = journal.offer(receiving, all_allowed, max_chars=2500)
    assert len(offer.message["content"]) <= 2500
    assert facts(offer)["selection_bounded"] is True
    assert 0 < len(offer.receipt_ids) < 32
    assert facts(offer)["cycles"][0]["omitted_older_receipts"] == 8
    journal.acknowledge(receiving, offer)
    remainder = json.loads(store.one("SELECT body FROM turn_action_receipts")["body"])
    assert len(remainder["receipts"]) == 32 - len(offer.receipt_ids)
    assert remainder["omitted_count"] == 0


def test_completed_turn_cleanup_and_other_context_cannot_ack(store):
    journal = ActionReceipts(store)
    original = context(store, "first")
    saved(journal, original)
    finish_turn(store, original)
    receiving = context(store, "second")
    offer = journal.offer(receiving, all_allowed)
    other = context(store, "other", bot_id="loki")
    journal.acknowledge(other, offer)
    assert store.one("SELECT 1 FROM turn_action_receipts")
    journal.discard_completed(original, "sent")
    assert not store.one("SELECT 1 FROM turn_action_receipts")


def test_current_credentials_redacted_again_when_offered(store):
    journal = ActionReceipts(store)
    original = context(store, "first")
    saved(journal, original, "LATER_SECRET")
    finish_turn(store, original)
    journal.redact = lambda value: json.loads(json.dumps(value).replace("LATER_SECRET", "[redacted]"))
    offer = journal.offer(context(store, "second"), all_allowed)
    assert "LATER_SECRET" not in offer.message["content"]
    assert "[redacted]" in offer.message["content"]


def test_diagnostic_write_failure_does_not_mask_work_or_log_values(store, monkeypatch, caplog):
    journal = ActionReceipts(store)
    original = context(store, "first")

    def fail(*args, **kwargs):
        raise RuntimeError("SECRET_IN_EXCEPTION")

    monkeypatch.setattr(store, "execute", fail)
    assert journal.start(original, "memory", "call", {}) is None
    assert "RuntimeError" in caplog.text and "SECRET_IN_EXCEPTION" not in caplog.text


@pytest.mark.parametrize("channel_only", [False, True])
async def test_clean_slate_discards_only_selected_receipts(kernel, owner, channel_only):
    from conftest import configured

    configured(kernel)
    for index, (bot, channel) in enumerate((("ada", "one"), ("ada", "two"), ("socrates", "one"))):
        kernel.store.context(bot, channel)
        source = context(kernel.store, f"reset-{index}", bot_id=bot, channel=channel)
        saved(kernel.engine.action_receipts, source)
        finish_turn(kernel.store, source)
    await kernel.service.control(
        owner,
        {
            "action": "reset_context",
            "kind": "bots",
            "id": "ada",
            "data": {"confirm_bot_id": "ada", **({"channel_id": "one"} if channel_only else {})},
        },
    )
    remaining = kernel.store.rows("SELECT bot_id,channel_id FROM turn_action_receipts ORDER BY bot_id")
    assert remaining == ([{"bot_id": "ada", "channel_id": "two"}] if channel_only else []) + [
        {"bot_id": "socrates", "channel_id": "one"}
    ]
