"""Progress text is not an immutable answer before its tool results exist."""

import json

import pytest

from support.provider import install_client
from support.runtime_feedback import ready, responses, tool
from test_end_cycle import cycle, end, events, mixed, read_call, run, tool_names, write_call


@pytest.mark.parametrize("action", ["read", "write"])
async def test_narration_is_withheld_then_final_answer_uses_tool_result(kernel, action):
    _, transport = ready(kernel, response_protocol="end_cycle", enabled_plugins=["memory"])
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return mixed(
                "Let me check your notes." if action == "read" else "I will save that.",
                read_call() if action == "read" else write_call(),
            )
        assert len(requests) == 2
        assert sorted(tool_names(body)) == ["council_silence", "end_cycle"]
        receipt = responses(body)[-1]
        answer = "There are no saved notes." if receipt.get("notes") == [] else "The note is saved."
        return mixed(answer, end())

    await install_client(kernel, handler)
    await run(kernel)
    assert len(requests) == 2
    expected = "There are no saved notes." if action == "read" else "The note is saved."
    assert transport.send.await_count == 1
    assert transport.send.await_args.args[2] == expected
    assert cycle(kernel)["strikes"] == 1 and cycle(kernel)["actions_executed"] == 1
    assert cycle(kernel)["reason"] == "end_cycle"
    assert not events(kernel, "cycle.draft_retained")
    answer_request = kernel.store.one("SELECT id FROM requests ORDER BY started_at DESC LIMIT 1")["id"]
    assert events(kernel, "delivery.queued")[0]["request_id"] == answer_request


@pytest.mark.parametrize("outcome", ["invalid", "usage", "partial"])
async def test_narration_can_report_failed_or_help_results_without_replaying(kernel, outcome):
    _, transport = ready(kernel, response_protocol="end_cycle", enabled_plugins=["memory"])
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            bad = write_call(call_id="bad", key="bad", value=42)
            calls = (
                [tool("memory", {}, call_id="help")]
                if outcome == "usage"
                else [write_call(call_id="good"), bad]
                if outcome == "partial"
                else [bad]
            )
            return mixed("I will save that.", *calls)
        assert count == 2
        body = json.loads(request.content)
        assert sorted(tool_names(body)) == ["council_silence", "end_cycle"]
        receipts = responses(body)
        if outcome == "usage":
            assert receipts[-1]["usage_only"]
            answer = "I only requested tool instructions; nothing was saved."
        else:
            assert receipts[-1]["ok"] is False
            if outcome == "partial":
                assert receipts[0]["saved"] is True
            answer = (
                "One note was saved, but the other failed." if outcome == "partial" else "The write failed."
            )
        return mixed(answer, end())

    await install_client(kernel, handler)
    await run(kernel)
    assert count == 2 and transport.send.await_count == 1
    assert transport.send.await_args.args[2] != "I will save that."
    expected = [{"key": "topic", "value": "Saved fact"}] if outcome == "partial" else []
    assert kernel.store.rows("SELECT key,value FROM memories") == expected
    assert cycle(kernel)["strikes"] == 1 and cycle(kernel)["reason"] == "end_cycle"
    assert cycle(kernel)["actions_executed"] == (1 if outcome == "partial" else 0)
    assert not events(kernel, "cycle.draft_retained")


async def test_narration_engram_is_discarded_and_final_answer_supplies_state(kernel):
    from test_engram_integration import enable, response, state

    bot, transport = enable(kernel)
    kernel.store.put(
        "bots", {**bot, "response_protocol": "end_cycle", "enabled_plugins": ["engram", "memory"]}
    )
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return mixed(
                response(body, content="Let me read your notes.", mem="Unconfirmed narration state"),
                read_call(),
            )
        assert len(seen) == 2
        assert responses(body)[-1]["notes"] == []
        return mixed(response(body, content="There are no notes.", mem="Observed no saved notes"), end())

    await install_client(kernel, handler)
    await run(kernel)
    assert transport.send.await_args.args[2] == "There are no notes."
    assert state(kernel)[0]["MEM"] == "Observed no saved notes"
    assert not events(kernel, "cycle.draft_retained")
    final_id = kernel.store.one("SELECT id FROM requests ORDER BY started_at DESC LIMIT 1")["id"]
    assert kernel.store.one("SELECT request_id,status FROM engram_candidates") == {
        "request_id": final_id,
        "status": "committed",
    }


@pytest.mark.parametrize("limit", [2, 3, 5])
async def test_narration_finalization_remains_bounded_and_cannot_replay_actions(kernel, limit):
    _, transport = ready(
        kernel, response_protocol="end_cycle", enabled_plugins=["memory"], narration_strikes=limit
    )
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count > 1:
            assert sorted(tool_names(json.loads(request.content))) == ["council_silence", "end_cycle"]
        return mixed("Working on it", write_call(call_id=f"write{count}", value=f"Attempt {count}"))

    await install_client(kernel, handler)
    await run(kernel)
    assert count == limit
    assert kernel.store.one("SELECT value FROM memories")["value"] == "Attempt 1"
    assert cycle(kernel)["actions_executed"] == 1 and cycle(kernel)["strikes"] == limit
    assert not events(kernel, "cycle.draft_retained")
    transport.send.assert_not_awaited()


async def test_first_real_text_only_answer_after_narration_is_retained_once(kernel):
    from support.runtime import completion

    _, transport = ready(
        kernel, response_protocol="end_cycle", enabled_plugins=["memory"], narration_strikes=3
    )
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return mixed("Let me check.", read_call())
        if count == 2:
            return completion("There are no saved notes.")
        assert count == 3
        return mixed("Do not replace the saved answer with this", end())

    await install_client(kernel, handler)
    await run(kernel)
    assert count == 3
    assert transport.send.await_args.args[2] == "There are no saved notes."
    assert cycle(kernel)["reason"] == "draft_published"
    assert cycle(kernel)["strikes"] == 2
    assert len(events(kernel, "cycle.draft_retained")) == 1


async def test_narration_finalization_keeps_the_approved_finish_first_order(kernel):
    from support.runtime import completion

    _, transport = ready(kernel, response_protocol="end_cycle", enabled_plugins=["memory"], max_tool_rounds=1)
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        body = json.loads(request.content)
        if count == 1:
            return mixed("Let me check.", read_call())
        if count == 2:
            assert sorted(tool_names(body)) == ["council_silence", "end_cycle"]
            return mixed("", end())
        assert count == 3 and tool_names(body) == []
        return completion("There are no saved notes.")

    await install_client(kernel, handler)
    await run(kernel)
    assert count == 3
    assert transport.send.await_args.args[2] == "There are no saved notes."
    assert cycle(kernel)["reason"] == "end_cycle_then_text" and cycle(kernel)["strikes"] == 1
    assert not events(kernel, "cycle.draft_retained")
