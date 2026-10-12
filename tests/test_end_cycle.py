"""end_cycle response protocol: publication only with the terminal tool, bounded finalization."""

import json

import httpx
import pytest

from conftest import ingest
from support.provider import install_client
from support.runtime import completion, settle
from support.runtime_feedback import ready, responses, tool

CHANNEL = "222222222222222222"


def mixed(content, *calls, finish="tool_calls"):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"content": content, "tool_calls": list(calls)},
                    "finish_reason": finish,
                }
            ]
        },
    )


def end(call_id="end"):
    return tool("end_cycle", {}, call_id=call_id)


def write_call(call_id="write", **changes):
    return tool(
        "memory", {"operation": "write", "key": "topic", "value": "Saved fact", **changes}, call_id=call_id
    )


def read_call(call_id="read"):
    return tool("memory", {"operation": "read"}, call_id=call_id)


def events(kernel, kind):
    return [
        json.loads(row["data"])
        for row in kernel.store.rows("SELECT data FROM events WHERE kind=? ORDER BY seq", (kind,))
    ]


def tool_names(body):
    return [item["function"]["name"] for item in body.get("tools", [])]


def harness_notices(body):
    return [
        message["content"]
        for message in body["messages"]
        if message["role"] == "user" and message["content"].startswith("Automatic harness notice")
    ]


async def run(kernel):
    await kernel.engine.tick()
    await settle(kernel)


def cycle(kernel):
    ended = events(kernel, "cycle.ended")
    assert len(ended) == 1
    return ended[0]


async def test_text_with_end_cycle_publishes_and_ends(kernel):
    _, transport = ready(kernel, response_protocol="end_cycle")
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        assert "Current response protocol: end_cycle." in str(body["messages"])
        assert "end_cycle" in tool_names(body) and "council_silence" in tool_names(body)
        return mixed("Here is my answer.", end())

    await install_client(kernel, handler)
    await run(kernel)
    assert len(seen) == 1
    assert transport.send.await_count == 1 and transport.send.await_args.args[2] == "Here is my answer."
    assert kernel.store.one("SELECT status,decision FROM turns") == {"status": "sent", "decision": "speak"}
    assert json.loads(kernel.store.one("SELECT routing FROM outbox")["routing"]) == {}
    done = cycle(kernel)
    assert done["reason"] == "end_cycle" and done["published"] is True and done["strikes"] == 0
    assert done["response_protocol"] == "end_cycle" and done["finalization"] is False


async def test_end_cycle_tool_is_absent_outside_its_mode(kernel):
    ready(kernel, response_protocol="progress")
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return completion("Plain answer")

    await install_client(kernel, handler)
    await run(kernel)
    assert "end_cycle" not in tool_names(seen[0])
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"


async def test_text_only_is_retained_then_bare_end_cycle_publishes_it(kernel):
    _, transport = ready(kernel, response_protocol="end_cycle", enabled_plugins=["memory"])
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            assert "memory" in tool_names(body)
            return completion("Draft answer without the tool.")
        # Finalization round: only terminal tools, the draft and a loud private warning.
        assert sorted(tool_names(body)) == ["council_silence", "end_cycle"]
        assistant = [m for m in body["messages"] if m["role"] == "assistant"][-1]
        assert assistant["content"] == "Draft answer without the tool." and "tool_calls" not in assistant
        notice = harness_notices(body)[-1]
        assert "NOT published" in notice and "Call end_cycle with {}" in notice
        return mixed("", end())

    await install_client(kernel, handler)
    await run(kernel)
    assert len(seen) == 2
    assert transport.send.await_count == 1
    assert transport.send.await_args.args[2] == "Draft answer without the tool."
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    assert events(kernel, "cycle.draft_retained")[0]["characters"] == len("Draft answer without the tool.")
    done = cycle(kernel)
    assert done["reason"] == "draft_published" and done["draft_retained"] and done["finalization"]
    assert done["rounds"] == 2


async def test_confirmation_cannot_replace_the_frozen_answer(kernel):
    _, transport = ready(kernel, response_protocol="end_cycle")
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return completion("First draft") if count == 1 else mixed("Replacement must be ignored", end())

    await install_client(kernel, handler)
    await run(kernel)
    assert count == 2
    assert transport.send.await_args.args[2] == "First draft"
    assert cycle(kernel)["reason"] == "draft_published"
    assert events(kernel, "tool.completed")[-1]["ignored_confirmation_characters"] > 0


@pytest.mark.parametrize("limit", [1, 2, 3, 5])
async def test_strikes_bound_all_failed_confirmations(kernel, limit):
    _, transport = ready(kernel, response_protocol="end_cycle", narration_strikes=limit)
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        body = json.loads(request.content)
        if count > 1:
            assert sorted(tool_names(body)) == ["council_silence", "end_cycle"]
            assert "Do not regenerate, repeat or replace" in harness_notices(body)[-1]
        return completion("Original" if count == 1 else "Must not replace the original")

    await install_client(kernel, handler)
    await run(kernel)
    assert count == limit
    transport.send.assert_not_awaited()
    assert cycle(kernel)["strikes"] == limit
    assert len(events(kernel, "cycle.draft_retained")) == 1
    assert cycle(kernel)["reason"] == "incomplete"


async def test_text_again_during_finalization_ends_without_answer(kernel):
    _, transport = ready(kernel, response_protocol="end_cycle")
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return completion("Still no tool" if count == 2 else "Draft")

    await install_client(kernel, handler)
    await run(kernel)
    assert count == 2
    transport.send.assert_not_awaited()
    turn = kernel.store.one("SELECT status,error FROM turns")
    assert turn["status"] == "failed" and "Only end_cycle {} can confirm" in turn["error"]
    assert cycle(kernel)["published"] is False


@pytest.mark.parametrize("write", [False, True])
async def test_empty_finish_preserves_allowed_write_once_without_regeneration(kernel, write):
    _, transport = ready(kernel, response_protocol="end_cycle", enabled_plugins=["memory"])
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        assert count == 1, "The observed empty-finish loop must never recur"
        return mixed("", *([write_call()] if write else []), end())

    await install_client(kernel, handler)
    await run(kernel)
    assert count == 1
    assert len(kernel.store.rows("SELECT * FROM memories")) == int(write)
    transport.send.assert_not_awaited()
    assert cycle(kernel)["strikes"] == 1 and cycle(kernel)["reason"] == "incomplete"
    assert cycle(kernel)["actions_executed"] == int(write)
    assert cycle(kernel)["draft_retained"] is False
    assert "No answer draft exists" in kernel.store.one("SELECT error FROM turns")["error"]


async def test_end_cycle_rejects_arguments(kernel):
    _, transport = ready(kernel, response_protocol="end_cycle")
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return mixed("Answer inside the tool?", tool("end_cycle", {"message": "nope"}, call_id="end"))
        last = responses(json.loads(request.content))[-1]
        assert "takes no arguments" in last["error"] and "usage" in last
        return mixed("Answer outside the tool.", end())

    await install_client(kernel, handler)
    await run(kernel)
    assert transport.send.await_count == 1
    assert transport.send.await_args.args[2] == "Answer inside the tool?"


async def test_write_companion_runs_before_publication(kernel):
    _, transport = ready(kernel, response_protocol="end_cycle", enabled_plugins=["memory"])

    async def send(*args, **kwargs):
        assert kernel.store.one("SELECT value FROM memories")["value"] == "Saved fact"
        return "888888888888888881"

    transport.send.side_effect = send
    await install_client(kernel, lambda _: mixed("Saved your preference.", write_call(), end()))
    await run(kernel)
    assert transport.send.await_count == 1
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    done = cycle(kernel)
    assert done["actions_executed"] == 1 and done["reason"] == "end_cycle"


async def test_failed_companion_stops_without_rewriting_or_repeating_successes(kernel):
    _, transport = ready(kernel, response_protocol="end_cycle", enabled_plugins=["memory"])
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        assert count == 1
        return mixed(
            "Both saved.", write_call(call_id="good"), write_call(call_id="bad", key="other", value=42), end()
        )

    await install_client(kernel, handler)
    await run(kernel)
    assert count == 1
    assert kernel.store.rows("SELECT key,value FROM memories") == [{"key": "topic", "value": "Saved fact"}]
    transport.send.assert_not_awaited()
    assert cycle(kernel)["actions_executed"] == 1
    assert cycle(kernel)["reason"] == "incomplete" and cycle(kernel)["draft_retained"]
    failed = events(kernel, "tool.failed")
    assert any(e.get("call_id") == "bad" for e in failed)


async def test_read_companion_rejects_the_whole_batch(kernel):
    _, transport = ready(kernel, response_protocol="end_cycle", enabled_plugins=["memory"])
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return mixed("Checking notes and answering.", read_call(), write_call(), end())
        results = responses(json.loads(request.content))
        assert len(results) == 3 and all(not r["ok"] and not r["executed"] for r in results)
        assert "may be combined only with" in results[0]["errors"][0]["message"]
        return mixed("", read_call())

    await install_client(kernel, handler)
    await run(kernel)
    assert not kernel.store.rows("SELECT * FROM memories")
    assert count == 1 and transport.send.await_count == 0


async def test_silence_still_works_during_draft_confirmation(kernel):
    _, transport = ready(kernel, response_protocol="end_cycle")
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return completion("Draft") if count == 1 else completion(silence=True)

    await install_client(kernel, handler)
    await run(kernel)
    assert count == 2
    transport.send.assert_not_awaited()
    assert kernel.store.one("SELECT status,decision FROM turns") == {"status": "silent", "decision": "listen"}
    assert cycle(kernel)["reason"] == "silence"


async def test_narrated_write_is_saved_once_then_only_its_draft_can_be_confirmed(kernel):
    _, transport = ready(kernel, response_protocol="end_cycle", enabled_plugins=["memory"], max_tool_rounds=6)
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return mixed("Saved fact.", write_call())
        assert len(seen) == 2
        assert sorted(tool_names(body)) == ["council_silence", "end_cycle"]
        assert responses(body)[-1]["saved"] is True
        assert "Strike 1 of 2" in harness_notices(body)[-1]
        assert "Action tools are removed" in harness_notices(body)[-1]
        return mixed("", end())

    await install_client(kernel, handler)
    await run(kernel)
    assert len(seen) == 2
    assert len(kernel.store.rows("SELECT * FROM memories")) == 1
    assert transport.send.await_count == 1 and transport.send.await_args.args[2] == "Saved fact."
    done = cycle(kernel)
    assert done["strikes"] == 1 and done["actions_executed"] == 1 and done["finalization"]
    assert done["reason"] == "draft_published"


async def test_single_strike_is_a_hard_failure_limit(kernel):
    _, transport = ready(
        kernel, response_protocol="end_cycle", enabled_plugins=["memory"], narration_strikes=1
    )
    await install_client(kernel, lambda _: mixed("Saving!", write_call()))
    await run(kernel)
    assert kernel.store.one("SELECT count(*) AS n FROM requests")["n"] == 1
    assert len(kernel.store.rows("SELECT * FROM memories")) == 1
    assert cycle(kernel)["strikes"] == 1 and cycle(kernel)["reason"] == "incomplete"
    transport.send.assert_not_awaited()


async def test_actions_during_finalization_are_refused_and_cycle_ends_incomplete(kernel):
    _, transport = ready(
        kernel, response_protocol="end_cycle", enabled_plugins=["memory"], narration_strikes=2
    )
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return mixed("Saving!", write_call(call_id="w1"))
        return mixed("One more!", write_call(call_id="w2", key="other"))

    await install_client(kernel, handler)
    await run(kernel)
    assert count == 2
    assert len(kernel.store.rows("SELECT * FROM memories")) == 1
    transport.send.assert_not_awaited()
    turn = kernel.store.one("SELECT status,error FROM turns")
    assert turn["status"] == "failed" and "confirm the saved draft" in turn["error"]
    refused = [item for item in events(kernel, "tool.failed") if item.get("call_id") == "w2"]
    assert refused and "no action or replacement text was used" in refused[0]["error"]


async def test_tool_only_rounds_do_not_strike(kernel):
    _, transport = ready(kernel, response_protocol="end_cycle", enabled_plugins=["memory"], max_tool_rounds=4)
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count < 4:
            return mixed("", write_call(call_id=f"w{count}", key=f"k{count}"))
        assert not harness_notices(json.loads(request.content))
        return mixed("Three notes saved.", end())

    await install_client(kernel, handler)
    await run(kernel)
    assert count == 4 and transport.send.await_count == 1
    assert cycle(kernel)["strikes"] == 0 and cycle(kernel)["actions_executed"] == 3


async def test_incomplete_cycle_notice_is_public_subtext_and_pauses_after_streak(kernel):
    _, transport = ready(
        kernel,
        response_protocol="end_cycle",
        notify_incomplete_cycle=True,
        interval_seconds=1,
        cooldown_seconds=3600,
    )
    await install_client(kernel, lambda _: completion("Never calls the tool"))
    posted = []

    async def send(bot, channel_id, content, reply_to, paths, **kwargs):
        posted.append((content, reply_to, kwargs.get("footer")))
        return str(888888888888888880 + len(posted))

    transport.send.side_effect = send
    for index in range(5):
        if index:
            ingest(kernel, discord_id=str(555555555555555560 + index))
            kernel.store.execute("UPDATE bot_runtime SET next_at=0 WHERE bot_id='ada'")
        await run(kernel)
    turns = kernel.store.rows("SELECT status FROM turns")
    assert len(turns) == 5 and all(row["status"] == "failed" for row in turns)
    assert len(posted) == 3
    content, reply_to, footer = posted[0]
    assert content.startswith("-# Ada stopped without an answer after 2 model rounds")
    assert reply_to is None and footer == ""
    notices = kernel.store.rows(
        "SELECT status FROM outbox WHERE json_extract(routing,'$.kind')='harness_notice'"
    )
    assert [row["status"] for row in notices] == ["sent"] * 3
    suppressed = events(kernel, "cycle.notice_suppressed")
    assert [item["streak"] for item in suppressed] == [4, 5]
    assert all(item["reason"] == "incomplete" for item in events(kernel, "cycle.ended"))


async def test_incomplete_cycle_notice_is_off_by_default(kernel):
    _, transport = ready(kernel, response_protocol="end_cycle")
    await install_client(kernel, lambda _: completion("Never calls the tool"))
    await run(kernel)
    transport.send.assert_not_awaited()
    assert kernel.store.one("SELECT status FROM turns")["status"] == "failed"
    assert not events(kernel, "cycle.notice_suppressed")


async def test_notice_streak_resets_after_a_published_answer(kernel):
    _, transport = ready(
        kernel,
        response_protocol="end_cycle",
        notify_incomplete_cycle=True,
        interval_seconds=1,
        cooldown_seconds=1,
    )

    def handler(request):
        # Turn rows exist before their first request: recover only in the fourth activation.
        fourth = kernel.store.one("SELECT count(*) AS n FROM turns")["n"] == 4
        return mixed("Recovered.", end()) if fourth else completion("No tool")

    await install_client(kernel, handler)
    for index in range(5):
        if index:
            ingest(kernel, discord_id=str(555555555555555570 + index))
            kernel.store.execute("UPDATE bot_runtime SET next_at=0 WHERE bot_id='ada'")
        await run(kernel)
    sent = [call.args[2] for call in transport.send.await_args_list]
    assert [text.startswith("-# ") for text in sent] == [True, True, True, False, True]
    assert sent[3] == "Recovered."


async def test_narrate_and_progress_bots_keep_their_behaviour(kernel):
    _, transport = ready(kernel, response_protocol="narrate", enabled_plugins=["memory"])
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        body = json.loads(request.content)
        assert "end_cycle" not in tool_names(body)
        return mixed("Saving", write_call()) if count == 1 else completion("Saved.")

    await install_client(kernel, handler)
    await run(kernel)
    assert transport.send.await_count == 1 and transport.send.await_args.args[2] == "Saved."
    done = cycle(kernel)
    assert done["response_protocol"] == "narrate" and done["reason"] == "text_only"
    assert done["strikes"] == 0 and done["finalization"] is False


@pytest.mark.parametrize("protocol", ["narrate", "progress", "end_cycle"])
async def test_cycle_ended_is_recorded_for_every_protocol(kernel, protocol):
    ready(kernel, response_protocol=protocol)
    await install_client(
        kernel, lambda _: mixed("Done.", end()) if protocol == "end_cycle" else completion("Done.")
    )
    await run(kernel)
    done = cycle(kernel)
    assert done["response_protocol"] == protocol and done["published"] is True
    assert done["channel_id"] == CHANNEL and done["rounds"] == 1
