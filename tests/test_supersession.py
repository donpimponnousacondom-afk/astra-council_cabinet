"""Safe supersession and truthful action handoff using mocked provider/delivery."""

import asyncio
from dataclasses import replace
import json
import math

import httpx
import pytest

from hortator.action_receipts import NOTICE as RECEIPT_NOTICE
from hortator.models import OWNER_ID
from hortator.plugins import SILENCE, ToolContext
from hortator.response_recovery import NOTICE as RECOVERY_NOTICE
from support.provider import install_client
from support.runtime import completion, settle
from support.runtime_feedback import ready, reply, tool


CHANNEL = "222222222222222222"
FIRST = "555555555555555561"
SECOND = "555555555555555562"
THIRD = "555555555555555563"
NOTE_VALUE = "Private saved note fixture: never copy this into an outcome notice."


def directed(kernel, bot, message_id, content="Updated request"):
    kernel.store.ingest(
        discord_id=message_id,
        channel_id=CHANNEL,
        room_id="council",
        author_id=OWNER_ID,
        author_name="The Boss",
        content=content,
        addressing={
            "author_kind": "human",
            "live": True,
            "targets": [{"bot_id": bot["id"], "via": ["mention"]}],
        },
    )


def setup(kernel, protocol="progress"):
    bot, transport = ready(
        kernel,
        response_protocol=protocol,
        enabled_plugins=["memory"],
        cooldown_seconds=0,
        interval_seconds=0,
    )
    directed(kernel, bot, FIRST, "Save the requested notes.")
    return bot, transport


def write(key, call_id):
    return tool("memory", {"operation": "write", "key": key, "value": NOTE_VALUE}, call_id=call_id)


def notice(body):
    found = [
        message["content"]
        for message in body["messages"]
        if isinstance(message.get("content"), str) and message["content"].startswith(RECEIPT_NOTICE)
    ]
    assert len(found) == 1
    assert NOTE_VALUE not in found[0]
    return found[0]


def receipt_facts(text):
    return json.loads(text[len(RECEIPT_NOTICE) :])


def journal_rows(kernel):
    return kernel.store.rows("SELECT turn_id,body FROM turn_action_receipts ORDER BY updated_at")


def turn_states(kernel):
    return [row["status"] for row in kernel.store.rows("SELECT status FROM turns ORDER BY started_at")]


@pytest.mark.parametrize("protocol", ["narrate", "progress"])
async def test_new_input_during_generation_prevents_entire_returned_batch(kernel, protocol):
    bot, transport = setup(kernel, protocol)
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            entered.set()
            await release.wait()
            return reply(write("must-not-run", "stale-call"))
        assert "Answer the new question instead" in str(body["messages"])
        return completion("Answer to the new question.")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await asyncio.wait_for(entered.wait(), 2)
    directed(kernel, bot, SECOND, "Answer the new question instead")
    # Existing provider work settles; there is still only one active bot task.
    await kernel.engine.tick()
    assert len(requests) == 1 and not kernel.engine.tasks[bot["id"]].cancelling()
    release.set()
    await asyncio.wait_for(settle(kernel), 3)
    assert turn_states(kernel) == ["cancelled"]
    assert not kernel.store.rows("SELECT * FROM memories")
    assert not kernel.store.one("SELECT 1 FROM events WHERE kind='tool.started'")
    assert not journal_rows(kernel)
    transport.send.assert_not_awaited()
    event = kernel.store.one("SELECT data FROM events WHERE kind='turn.superseded'")
    assert json.loads(event["data"])["unstarted_calls"] == 1
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 3)
    assert len(requests) == 2
    assert turn_states(kernel) == ["cancelled", "sent"]
    transport.send.assert_awaited_once()
    assert transport.send.await_args.args[3] == SECOND


async def test_running_memory_write_commits_then_skips_sibling_and_carries_receipt_until_final(
    kernel, monkeypatch
):
    bot, transport = setup(kernel)
    entered, release = asyncio.Event(), asyncio.Event()
    original = kernel.registry.specs["memory"]

    async def slow_write(args, context, config, key):
        if args["operation"] == "write" and args["key"] == "completed":
            entered.set()
            await release.wait()
        return await original.handler(args, context, config, key)

    monkeypatch.setitem(kernel.registry.specs, "memory", replace(original, handler=slow_write))
    requests, notices = [], []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply(write("completed", "first-action"), write("must-not-run", "second-action"))
        notices.append(notice(body))
        # Receiving a provider request never acknowledges the interrupted writes.
        assert journal_rows(kernel)
        if len(requests) == 2:
            return reply(tool("memory", {"operation": "read", "key": "completed"}, call_id="verify"))
        assert len(requests) == 3
        return completion("The earlier note is saved; I checked its current state.")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await asyncio.wait_for(entered.wait(), 2)
    directed(kernel, bot, SECOND, "Check what was saved before doing anything else.")
    release.set()
    await asyncio.wait_for(settle(kernel), 3)
    assert len(requests) == 1
    assert turn_states(kernel) == ["cancelled"]
    assert kernel.store.rows("SELECT key,value FROM memories") == [{"key": "completed", "value": NOTE_VALUE}]
    source_turn = journal_rows(kernel)[0]["turn_id"]
    stored = json.loads(journal_rows(kernel)[0]["body"])
    assert len(stored["receipts"]) == 1
    assert stored["receipts"][0]["outcome"] == "saved"
    assert stored["receipts"][0]["key"] == "completed"
    assert NOTE_VALUE not in journal_rows(kernel)[0]["body"]
    assert not kernel.store.one(
        "SELECT 1 FROM events WHERE kind='tool.started' AND json_extract(data,'$.call_id')='second-action'"
    )
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 3)
    assert notices[0] == notices[1]
    from hortator.reporting import turn_detail

    receiving_turn = kernel.store.one("SELECT id FROM turns WHERE status='sent'")["id"]
    detail = turn_detail(kernel.store, receiving_turn)
    assert all(
        request["context"]["action_receipts"] == [[source_turn, stored["receipts"][0]["receipt_id"]]]
        for request in detail["requests"]
    )
    assert len(requests) == 3 and turn_states(kernel) == ["cancelled", "sent"]
    assert not journal_rows(kernel)
    assert kernel.store.one("SELECT count(*) AS n FROM events WHERE kind='memory.changed'")["n"] == 1
    transport.send.assert_awaited_once()


async def test_operator_cancellation_after_committed_write_preserves_saved_receipt(kernel):
    bot, transport = setup(kernel)
    second_request = asyncio.Event()
    never_release = asyncio.Event()
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply(write("operator-interrupted", "save"))
        if len(requests) == 2:
            second_request.set()
            await never_release.wait()
            raise AssertionError("Cancelled generation must not resume")
        entries = receipt_facts(notice(body))["cycles"][0]["receipts"]
        assert entries[0]["outcome"] == "saved" and entries[0]["key"] == "operator-interrupted"
        return completion("The earlier saved note survived the interruption.")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await asyncio.wait_for(second_request.wait(), 2)
    await kernel.engine.cancel([bot["id"]], "Operator interruption fixture")
    await settle(kernel)
    assert turn_states(kernel) == ["cancelled"]
    assert kernel.store.one("SELECT value FROM memories")["value"] == NOTE_VALUE
    assert json.loads(journal_rows(kernel)[0]["body"])["receipts"][0]["outcome"] == "saved"
    transport.send.assert_not_awaited()
    directed(kernel, bot, SECOND, "What survived the interruption?")
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 3)
    assert len(requests) == 3 and turn_states(kernel) == ["cancelled", "sent"]
    assert not journal_rows(kernel)


async def test_repeated_supersession_keeps_earlier_receipts_until_one_confirmed_final(kernel, monkeypatch):
    bot, transport = setup(kernel)
    original = kernel.registry.specs["memory"]

    async def write_then_new_input(args, context, config, key):
        result = await original.handler(args, context, config, key)
        if args.get("operation") == "write":
            next_id = SECOND if args["key"] == "first" else THIRD
            directed(kernel, bot, next_id, "Continue with the newest question.")
        return result

    monkeypatch.setitem(kernel.registry.specs, "memory", replace(original, handler=write_then_new_input))
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply(write("first", "write"))
        entries = [item for cycle in receipt_facts(notice(body))["cycles"] for item in cycle["receipts"]]
        if len(requests) == 2:
            assert {item["key"] for item in entries} == {"first"}
            return reply(write("second", "write"))
        assert {item["key"] for item in entries} == {"first", "second"}
        assert all(item["outcome"] == "saved" for item in entries)
        return completion("Both earlier writes completed.")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 3)
    assert turn_states(kernel) == ["cancelled"] and len(journal_rows(kernel)) == 1
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 3)
    assert turn_states(kernel) == ["cancelled", "cancelled"] and len(journal_rows(kernel)) == 2
    transport.send.assert_not_awaited()
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 3)
    assert turn_states(kernel) == ["cancelled", "cancelled", "sent"]
    assert len(requests) == 3 and not journal_rows(kernel)
    assert kernel.store.one("SELECT count(*) AS n FROM events WHERE kind='memory.changed'")["n"] == 2
    transport.send.assert_awaited_once()


async def test_receipt_and_private_recovery_coexist_without_leaking_recovery_into_public_request(kernel):
    bot, transport = setup(kernel)
    private_marker = "PRIVATE_REASONING_RECOVERY_FIXTURE_73d8"
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply(write("before-filter", "save"))
        if len(requests) == 2:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "Unfinished answer", "reasoning_content": private_marker},
                            "finish_reason": "content_filter",
                        }
                    ],
                },
            )
        assert private_marker in body["messages"][-1]["content"]
        assert body["messages"][-1]["content"].startswith(RECOVERY_NOTICE)
        assert body["messages"][-2]["content"] == notice(body)
        return completion("The prior note is saved. Here is a fresh answer.")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 3)
    assert turn_states(kernel) == ["failed"] and journal_rows(kernel)
    assert kernel.store.one("SELECT 1 FROM response_recovery_pending")
    directed(kernel, bot, SECOND, "Continue using the saved note.")
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 3)
    assert len(requests) == 3 and turn_states(kernel) == ["failed", "sent"]
    final_request = kernel.store.one("SELECT body FROM requests ORDER BY started_at DESC,rowid DESC LIMIT 1")
    public_body = json.loads(final_request["body"])
    assert private_marker not in final_request["body"]
    assert "original in private request diagnostics" in public_body["messages"][-1]["content"]
    assert public_body["messages"][-2]["content"].startswith(RECEIPT_NOTICE)
    assert not journal_rows(kernel)
    transport.send.assert_awaited_once()


async def test_revoked_global_plugin_hides_prior_receipt_next_round_and_does_not_ack_it(kernel, monkeypatch):
    bot, _ = setup(kernel)
    bot_revision = bot["revision"]
    original = kernel.registry.specs["memory"]

    async def mutate_then_change_context(args, context, config, key):
        result = await original.handler(args, context, config, key)
        if args["operation"] == "write":
            directed(kernel, bot, SECOND)
        else:
            # Bypass the service's broad cancellation deliberately: exercise the
            # per-round current global grant check with the same bot revision.
            plugin = kernel.store.get("plugins", "memory")
            kernel.store.put("plugins", {**plugin, "enabled": False})
        return result

    monkeypatch.setitem(
        kernel.registry.specs, "memory", replace(original, handler=mutate_then_change_context)
    )
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply(write("survives-revocation", "save"))
        if len(requests) == 2:
            assert notice(body)
            return reply(tool("memory", {"operation": "read", "key": "survives-revocation"}, call_id="read"))
        if len(requests) == 3:
            assert RECEIPT_NOTICE not in str(body["messages"])
            assert "memory" not in {entry["function"]["name"] for entry in body.get("tools", [])}
            assert kernel.store.get("bots", bot["id"])["revision"] == bot_revision
            return completion("The note was previously saved; memory access is now disabled.")
        assert notice(body)
        return completion("Memory access is available again; the prior receipt is acknowledged.")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 3)
    source_turn = journal_rows(kernel)[0]["turn_id"]
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 3)
    assert turn_states(kernel) == ["cancelled", "sent"]
    assert [row["turn_id"] for row in journal_rows(kernel)] == [source_turn]
    plugin = kernel.store.get("plugins", "memory")
    kernel.store.put("plugins", {**plugin, "enabled": True})
    directed(kernel, bot, THIRD)
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 3)
    assert len(requests) == 4 and not journal_rows(kernel)


async def test_receipt_omitted_for_real_input_capacity_is_not_acknowledged_by_final(kernel, monkeypatch):
    bot, transport = setup(kernel)
    original = kernel.registry.specs["memory"]

    async def save_then_supersede(args, context, config, key):
        result = await original.handler(args, context, config, key)
        directed(kernel, bot, SECOND, "Answer the newest question.")
        return result

    monkeypatch.setitem(kernel.registry.specs, "memory", replace(original, handler=save_then_supersede))
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply(write("saved-before-capacity-check", "save"))
        assert RECEIPT_NOTICE not in str(body["messages"])
        return completion("Answer fits within the available input budget.")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 3)
    source_turn = journal_rows(kernel)[0]["turn_id"]
    # Measure real ordinary context and real notice token costs on these fixtures.
    # No estimator is stubbed and no provider request is made by this preparation.
    measured_bot = {
        **kernel.store.get("bots", bot["id"]),
        "activation": {
            "kind": "human_mention",
            "message_id": SECOND,
            "channel_id": CHANNEL,
            "author_id": OWNER_ID,
            "cooldown_bypassed": True,
        },
    }
    profile = kernel.store.get("profiles", measured_bot["model_profile_id"])
    context = ToolContext(measured_bot, CHANNEL, "capacity-preflight")
    schemas = [SILENCE] + kernel.registry.schemas(context)
    rows, summary, _, prepared = await kernel.engine.contexts.prepare(
        measured_bot, profile, CHANNEL, context.turn_id, schemas
    )
    messages, _ = await kernel.engine.contexts.assemble(
        measured_bot,
        profile,
        CHANNEL,
        rows,
        summary,
        0,
        [],
        schemas,
        image_plan=prepared["image_plan"],
        engram=prepared.get("_engram_snapshot"),
        audio_plan=prepared.get("audio_plan"),
    )
    offer = kernel.engine.action_receipts.offer(context, kernel.registry.allowed)
    assert offer is not None
    ordinary = await kernel.engine.contexts.estimate_async(messages, schemas)
    with_receipt = await kernel.engine.contexts.estimate_async(messages + [offer.message], schemas)
    factor = prepared["calibration_factor"]
    assert (with_receipt - ordinary) * factor > 128
    constrained = {
        **profile,
        "context_window": math.ceil(ordinary * factor) + profile["response_tokens"] + 64,
        "compact_threshold": 1.0,
    }
    kernel.store.put("profiles", constrained)
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 3)
    assert len(requests) == 2 and turn_states(kernel) == ["cancelled", "sent"]
    assert [row["turn_id"] for row in journal_rows(kernel)] == [source_turn]
    request = kernel.store.one("SELECT context FROM requests ORDER BY started_at DESC,rowid DESC LIMIT 1")
    assert not json.loads(request["context"]).get("action_receipts")
    transport.send.assert_awaited_once()


async def test_supersession_releases_one_accepted_background_job_without_running_sibling(kernel, monkeypatch):
    from support.background_handoff import configure
    from support.background_jobs import finish
    from support.research_assistant import ID, start
    from support.research_fanout import worker

    configure(kernel, 2)
    bot = kernel.store.get("bots", "ada")
    kernel.store.put("bots", {**bot, "interval_seconds": 0, "response_protocol": "progress"})
    bot = kernel.store.get("bots", "ada")
    directed(kernel, bot, FIRST, "Start these two research tasks.")
    entered, release = asyncio.Event(), asyncio.Event()
    executions = []

    async def work(job):
        executions.append(job["id"])
        entered.set()
        await release.wait()
        return {"report": "Independent worker completed once."}

    worker(kernel, work)
    original = kernel.registry.specs[ID]

    async def accept_then_supersede(args, context, config, key):
        result = await original.handler(args, context, config, key)
        assert not entered.is_set()  # The accepted batch still owns its worker gate.
        directed(kernel, bot, SECOND, "Answer this newer question first.")
        return result

    monkeypatch.setitem(kernel.registry.specs, ID, replace(original, handler=accept_then_supersede))
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply(
                tool(ID, start(submission_id="accepted"), call_id="first-job"),
                tool(ID, start(submission_id="must-not-start"), call_id="second-job"),
            )
        if len(requests) == 2:
            return completion("Answer to the newer human question.")
        assert "background_completion" in str(body["messages"])
        return completion("The one accepted background job has finished.")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 3)
    await asyncio.wait_for(entered.wait(), 2)
    assert len(requests) == 1 and turn_states(kernel) == ["cancelled"]
    jobs = kernel.store.rows("SELECT id,state,submission_id FROM background_jobs")
    assert len(jobs) == 1 and jobs[0]["submission_id"] == "accepted"
    assert jobs[0]["state"] == "running" and executions == [jobs[0]["id"]]
    assert not kernel.jobs.origin_gates
    assert not kernel.store.one(
        "SELECT 1 FROM events WHERE kind='tool.started' AND json_extract(data,'$.call_id')='second-job'"
    )
    release.set()
    await asyncio.wait_for(finish(kernel), 3)
    assert kernel.jobs.get(jobs[0]["id"])["notification"] == "pending"
    # Human priority answers first; the accepted job's completion remains pending.
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 3)
    assert kernel.jobs.get(jobs[0]["id"])["notification"] == "pending"
    await kernel.engine.tick()
    await asyncio.wait_for(settle(kernel), 3)
    assert kernel.jobs.get(jobs[0]["id"])["notification"] == "claimed"
    await kernel.engine.tick()
    assert len(requests) == 3 and executions == [jobs[0]["id"]]
    assert turn_states(kernel) == ["cancelled", "sent", "sent"]


async def test_superseding_input_stops_idle_retry_wait_without_waiting_for_backoff(kernel):
    bot, transport = setup(kernel)
    provider = kernel.store.get("providers", "openrouter")
    kernel.store.put("providers", {**provider, "retry_count": 3, "retry_delay_seconds": 60})
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={"error": "transient fixture"})

    await install_client(kernel, handler)
    await kernel.engine.tick()
    async with asyncio.timeout(3):
        while not kernel.store.one("SELECT 1 FROM events WHERE kind='request.retry_scheduled'"):
            await asyncio.sleep(0.001)
        directed(kernel, bot, SECOND, "Answer this new question instead.")
        await settle(kernel)
    assert len(calls) == 1 and turn_states(kernel) == ["cancelled"]
    assert kernel.pool.active["openrouter"] == 0
    assert not kernel.store.one("SELECT 1 FROM events WHERE kind='provider.failure'")
    event = kernel.store.one("SELECT data FROM events WHERE kind='turn.superseded'")
    assert json.loads(event["data"])["boundary"] == "provider_queue_or_retry"
    transport.send.assert_not_awaited()
