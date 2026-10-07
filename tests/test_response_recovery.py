import asyncio
import json
import re

import httpx
import pytest

from conftest import ingest
from hortator.models import ControlError
from hortator.response_recovery import NOTICE, queue_failed_response, read_note
from support.provider import Fragments, install_client
from support.runtime import completion, settle
from support.runtime_feedback import ready, reply, tool


CHANNEL = "222222222222222222"
THOUGHT = "Private reasoning fixture: verify the source before answering."
DRAFT = "Unfinished factual answer fixture"
MARKER = NOTICE.splitlines()[0]


async def run(kernel):
    kernel.store.execute("UPDATE bot_runtime SET next_at=0,retry_until=0 WHERE bot_id='ada'")
    await kernel.engine.tick()
    await settle(kernel)


def stopped(content=DRAFT, reasoning=THOUGHT, finish="content_filter"):
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": content, "reasoning_content": reasoning}, "finish_reason": finish}
            ]
        },
    )


async def failed_turn(kernel, *, finish="content_filter", content=DRAFT):
    bot, transport = ready(kernel, enabled_plugins=["memory"], cooldown_seconds=0)
    await install_client(kernel, lambda _: stopped(content=content, finish=finish))
    await run(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "failed"
    transport.send.assert_not_awaited()
    return bot, transport


@pytest.mark.parametrize("engram", [False, True])
@pytest.mark.parametrize("finish", ["content_filter", "length"])
async def test_one_use_tail_contains_evidence_but_never_enters_history_or_tool_followups(
    kernel, engram, finish
):
    bot, transport = ready(
        kernel, enabled_plugins=["memory"] + (["engram"] if engram else []), cooldown_seconds=0
    )
    if engram:
        plugin = kernel.store.get("plugins", "engram")
        plugin.pop("revision")
        plugin["enabled"] = True
        kernel.store.put("plugins", plugin)
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            draft = DRAFT
            if engram:
                nonce = re.search(r"\[\^ENGRAM:([0-9a-f]{32})\]", str(body))[1]
                draft += f'\n[^ENGRAM:{nonce}]\n{{"MEM":"unfinished state fixture'
            return stopped(content=draft, finish=finish)
        if len(bodies) == 2:
            return reply(tool("memory", {"operation": "read"}))
        return completion("Fresh considered answer")

    await install_client(kernel, handler)
    await run(kernel)
    source = kernel.store.one("SELECT id FROM requests")["id"]
    assert kernel.store.one("SELECT request_id FROM response_recovery_pending")["request_id"] == source
    transport.send.assert_not_awaited()
    await run(kernel)
    assert len(bodies) == 3
    tail = bodies[1]["messages"][-1]
    assert tail["role"] == "user" and tail["content"].startswith(NOTICE)
    assert all(text in tail["content"] for text in (DRAFT, THOUGHT, source, finish))
    assert bodies[0]["messages"][0] == bodies[1]["messages"][0]
    assert all(MARKER not in str(message) for message in bodies[1]["messages"][:-1])
    assert MARKER not in str(bodies[2]) and THOUGHT not in str(bodies[2])
    if engram:
        assert "unfinished state fixture" in tail["content"]
        assert not kernel.registry.engrams.inspect(bot["id"])["states"]
    assert not kernel.store.rows("SELECT * FROM response_recovery_pending")
    assert THOUGHT not in str(kernel.store.rows("SELECT * FROM requests"))
    assert THOUGHT not in str(kernel.store.rows("SELECT * FROM messages"))
    assert THOUGHT not in str(kernel.store.rows("SELECT * FROM contexts"))
    assert THOUGHT not in str(kernel.store.events())
    assert DRAFT not in str(transport.send.await_args_list)
    captures = [json.loads(row["body"]) for row in kernel.store.rows("SELECT body FROM request_diagnostics")]
    assert sum("request_recovery" in row for row in captures) == 1
    assert THOUGHT in str(next(row["request_recovery"] for row in captures if "request_recovery" in row))
    ingest(kernel, content="A later request", discord_id="666666666666666666")
    await run(kernel)
    assert len(bodies) == 4 and MARKER not in str(bodies[3])


async def test_broken_stream_recovers_received_text_and_partial_call_without_executing_it(kernel):
    _, transport = ready(kernel, enabled_plugins=["memory"], cooldown_seconds=0)
    packet = {
        "choices": [
            {
                "delta": {
                    "content": DRAFT,
                    "reasoning_content": THOUGHT,
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "broken-call",
                            "type": "function",
                            "function": {
                                "name": "memory",
                                "arguments": '{"operation":"write","key":"unexecuted"',
                            },
                        }
                    ],
                }
            }
        ]
    }
    await install_client(
        kernel,
        lambda _: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=Fragments(("data: " + json.dumps(packet) + "\n\n").encode()),
        ),
    )
    await run(kernel)
    transport.send.assert_not_awaited()
    assert not kernel.store.rows("SELECT * FROM memories")
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return completion("I will verify before continuing")

    await install_client(kernel, handler)
    await run(kernel)
    text = bodies[0]["messages"][-1]["content"]
    assert all(value in text for value in (DRAFT, THOUGHT, "broken-call", "unexecuted"))
    assert not kernel.store.rows("SELECT * FROM memories")


async def test_retry_keeps_same_note_but_only_one_consumption_receipt(kernel):
    await failed_turn(kernel)
    provider = kernel.store.get("providers", "openrouter")
    provider.pop("revision")
    provider.update(retry_count=1, retry_delay_seconds=0)
    kernel.store.put("providers", provider)
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return (
            httpx.Response(503, text="temporary fixture outage")
            if len(bodies) == 1
            else completion("Recovered")
        )

    await install_client(kernel, handler)
    await run(kernel)
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert MARKER in str(bodies[0])
    assert len([e for e in kernel.store.events() if e["kind"] == "response_recovery.offered"]) == 1
    assert not kernel.store.rows("SELECT * FROM response_recovery_pending")


async def test_recovered_internal_retry_does_not_queue_a_failed_response(kernel):
    ready(kernel, cooldown_seconds=0)
    provider = kernel.store.get("providers", "openrouter")
    provider.pop("revision")
    provider.update(retry_count=1, retry_delay_seconds=0)
    kernel.store.put("providers", provider)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="temporary fixture outage") if calls == 1 else completion("Succeeded")

    await install_client(kernel, handler)
    await run(kernel)
    assert calls == 2 and not kernel.store.rows("SELECT * FROM response_recovery_pending")


async def test_recovery_is_scoped_and_survives_restart_then_reset_clears_it(kernel, owner):
    await failed_turn(kernel)
    assert await kernel.reporting.run(read_note, "socrates", CHANNEL, 6000) is None
    assert await kernel.reporting.run(read_note, "ada", "other-channel", 6000) is None
    kernel.store.recover()
    assert await kernel.reporting.run(read_note, "ada", CHANNEL, 6000)
    await kernel.service.control(
        owner,
        {
            "action": "reset_context",
            "kind": "bots",
            "id": "ada",
            "data": {"confirm_bot_id": "ada", "channel_id": CHANNEL},
        },
    )
    assert not kernel.store.rows("SELECT * FROM response_recovery_pending")


async def test_budget_excerpts_are_explicit_and_keep_both_ends(kernel):
    await failed_turn(kernel, content="START-OF-DRAFT " + "abcdefghij " * 20000 + " END-OF-DRAFT")
    note = await kernel.reporting.run(read_note, "ada", CHANNEL, 1200)
    assert note and note["estimated_tokens"] <= 1200 and note["excerpted"]
    assert "START-OF-DRAFT" in note["message"]["content"]
    assert "END-OF-DRAFT" in note["message"]["content"]
    assert "omitted for recovery budget" in note["message"]["content"]
    assert await kernel.reporting.run(read_note, "ada", CHANNEL, 1) is None
    assert kernel.store.one("SELECT 1 FROM response_recovery_pending")


async def test_busy_reader_does_not_fail_ordinary_answer_or_consume_note(kernel, monkeypatch):
    _, transport = await failed_turn(kernel)
    original = kernel.reporting.run

    async def busy(*args, **kwargs):
        raise ControlError("Reporting reads are busy", 503)

    monkeypatch.setattr(kernel.reporting, "run", busy)
    await install_client(kernel, lambda _: completion("Current answer without diagnostics"))
    await run(kernel)
    assert transport.send.await_count == 1
    assert kernel.store.one("SELECT 1 FROM response_recovery_pending")
    monkeypatch.setattr(kernel.reporting, "run", original)


async def test_cancel_before_provider_slot_preserves_pending_note(kernel):
    await failed_turn(kernel)
    kernel.pool.active["openrouter"] = 999
    await kernel.engine.tick()
    await asyncio.sleep(0)
    # Force the scheduled next turn explicitly; the previous failure respects cadence.
    kernel.store.execute("UPDATE bot_runtime SET next_at=0 WHERE bot_id='ada'")
    await kernel.engine.tick()
    for _ in range(30):
        if kernel.engine.tasks:
            await asyncio.sleep(0.01)
        else:
            break
    await kernel.engine.cancel(["ada"], "test cancellation before network")
    assert kernel.store.one("SELECT 1 FROM response_recovery_pending")
    assert not [e for e in kernel.store.events() if e["kind"] == "response_recovery.offered"]
    kernel.pool.active["openrouter"] = 0


async def test_other_purposes_and_fresh_invocations_do_not_queue(kernel):
    await failed_turn(kernel)
    turn = kernel.store.one("SELECT id FROM turns")["id"]
    kernel.store.execute("DELETE FROM response_recovery_pending")
    kernel.store.execute("UPDATE turns SET trigger='slash_prompt' WHERE id=?", (turn,))
    queue_failed_response(kernel.store, turn)
    assert not kernel.store.rows("SELECT * FROM response_recovery_pending")
    kernel.store.execute("UPDATE turns SET trigger='timer' WHERE id=?", (turn,))
    kernel.store.execute("UPDATE requests SET purpose='compaction' WHERE turn_id=?", (turn,))
    queue_failed_response(kernel.store, turn)
    assert not kernel.store.rows("SELECT * FROM response_recovery_pending")


async def test_compaction_does_not_read_or_consume_recovery(kernel):
    bot, _ = await failed_turn(kernel)
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return completion("A compact factual summary")

    await install_client(kernel, handler)
    kernel.engine.launch(bot, CHANNEL, compact_only=True)
    await settle(kernel)
    assert len(bodies) == 1 and MARKER not in str(bodies[0]) and THOUGHT not in str(bodies[0])
    assert THOUGHT not in kernel.store.context("ada", CHANNEL)["summary"]
    assert kernel.store.one("SELECT 1 FROM response_recovery_pending")


async def test_current_known_secret_is_redacted_when_recovering_old_capture(kernel):
    secret = "credential-installed-after-the-failure-fixture"
    await failed_turn(kernel, content=DRAFT + " " + secret)
    kernel.vault.put("provider/new/api_key", secret)
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return completion("Considered reply")

    await install_client(kernel, handler)
    await run(kernel)
    assert MARKER in str(bodies[0]) and secret not in str(bodies[0])
    assert "[REDACTED]" in bodies[0]["messages"][-1]["content"]


async def test_empty_response_still_reports_the_real_failure_without_invented_text(kernel):
    ready(kernel, cooldown_seconds=0)
    await install_client(kernel, lambda _: httpx.Response(503, text="upstream fixture unavailable"))
    await run(kernel)
    note = await kernel.reporting.run(read_note, "ada", CHANNEL, 6000)
    payload = json.loads(note["message"]["content"][len(NOTICE) :])
    assert payload["http_status"] == 503 and payload["error_origin"] == "upstream_http"
    assert payload["received_content"] == payload["received_private_reasoning"] == ""
    assert "upstream fixture unavailable" in payload["error"]


async def test_restart_queues_only_newly_interrupted_work_and_never_reoffers_consumed_note(kernel):
    await failed_turn(kernel)
    source = kernel.store.one("SELECT id FROM requests")["id"]
    kernel.store.execute("DELETE FROM response_recovery_pending")
    kernel.store.execute("UPDATE turns SET status='running',ended_at=NULL")
    kernel.store.execute("UPDATE requests SET status='running',ended_at=NULL")
    kernel.store.recover()
    assert kernel.store.one("SELECT request_id FROM response_recovery_pending")["request_id"] == source
    kernel.store.execute("DELETE FROM response_recovery_pending")
    kernel.store.recover()
    assert not kernel.store.rows("SELECT * FROM response_recovery_pending")


async def test_oversized_capture_is_reported_without_unbounded_python_decode(kernel, monkeypatch):
    from hortator import response_recovery

    await failed_turn(kernel)
    row = kernel.store.one("SELECT request_id,body FROM request_diagnostics")
    value = json.loads(row["body"])
    value["reasoning_content"] = "large private diagnostic " * 1000
    kernel.store.execute(
        "UPDATE request_diagnostics SET body=? WHERE request_id=?", (json.dumps(value), row["request_id"])
    )
    monkeypatch.setattr(response_recovery, "CAPTURE_READ_LIMIT", 4096)
    note = await kernel.reporting.run(read_note, "ada", CHANNEL, 6000)
    assert note["excerpted"] and DRAFT in note["message"]["content"]
    assert "Private capture exceeds" in note["message"]["content"]


@pytest.mark.parametrize("last_attempt", ["http_error", "local_error", "cancelled"])
async def test_last_empty_retry_keeps_previous_attempts_received_work(kernel, monkeypatch, last_attempt):
    from hortator import provider as provider_module

    ready(kernel, cooldown_seconds=0)
    provider = kernel.store.get("providers", "openrouter")
    provider.pop("revision")
    provider.update(retry_count=1, retry_delay_seconds=0)
    kernel.store.put("providers", provider)
    calls = 0
    encodes = 0
    original_encode = provider_module.encode_request

    def encode(*args):
        nonlocal encodes
        encodes += 1
        if encodes == 2 and last_attempt != "http_error":
            if last_attempt == "cancelled":
                raise asyncio.CancelledError()
            raise RuntimeError("final retry local fixture failure")
        return original_encode(*args)

    monkeypatch.setattr(provider_module, "encode_request", encode)

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            packets = [
                {"choices": [{"delta": {"content": DRAFT, "reasoning_content": THOUGHT}}]},
                {"error": {"code": "server_error", "message": "stream fixture stopped"}},
            ]
            raw = "".join("data: " + json.dumps(p) + "\n\n" for p in packets)
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Fragments(raw.encode())
            )
        return httpx.Response(503, text="final retry fixture unavailable")

    await install_client(kernel, handler)
    await run(kernel)
    assert calls == (2 if last_attempt == "http_error" else 1)
    note = await kernel.reporting.run(read_note, "ada", CHANNEL, 6000)
    assert note is not None
    payload = json.loads(note["message"]["content"][len(NOTICE) :])
    assert payload["received_content"] == DRAFT and payload["received_private_reasoning"] == THOUGHT
    assert payload["source_request_id"] != payload["capture_request_id"]
    if last_attempt == "http_error":
        assert payload["http_status"] == 503 and "final retry fixture unavailable" in payload["error"]
    else:
        assert payload["http_status"] is None
        assert payload["error_phase"] == "serialize_request"
        assert payload["request_status"] == ("cancelled" if last_attempt == "cancelled" else "failed")


async def test_optional_queue_failure_preserves_original_turn_error(kernel, monkeypatch, caplog):
    secret = "queue-failure-credential-fixture"
    kernel.vault.put("provider/fake/api_key", secret)
    original = kernel.store.execute

    def unavailable(sql, args=()):
        if sql.startswith("INSERT INTO response_recovery_pending"):
            raise RuntimeError("queue unavailable " + secret)
        return original(sql, args)

    monkeypatch.setattr(kernel.store, "execute", unavailable)
    await failed_turn(kernel)
    error = kernel.store.one("SELECT error FROM turns")["error"]
    assert "content_filter" in error and "queue unavailable" not in error
    assert "Could not queue response recovery" in caplog.text and secret not in caplog.text


async def test_local_pre_http_failure_does_not_replace_unoffered_work(kernel, monkeypatch):
    from hortator import provider

    await failed_turn(kernel)
    source = kernel.store.one("SELECT request_id FROM response_recovery_pending")["request_id"]

    def cannot_encode(*args):
        raise RuntimeError("local serialization fixture")

    monkeypatch.setattr(provider, "encode_request", cannot_encode)
    await run(kernel)
    assert kernel.store.one("SELECT request_id FROM response_recovery_pending")["request_id"] == source
    latest = kernel.store.one("SELECT context,error FROM requests ORDER BY started_at DESC LIMIT 1")
    assert not json.loads(latest["context"])["http_attempt_started"]
    assert "local serialization fixture" in latest["error"]
    assert not [e for e in kernel.store.events() if e["kind"] == "response_recovery.offered"]
