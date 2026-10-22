import json

import httpx
import pytest

from conftest import configured
from hortator.models import Bot
from support.provider import install_client
from support.runtime import completion, settle
from support.runtime_feedback import ready, reply, responses, tool


async def test_default_legacy_and_saved_bot_policy_are_independent(kernel, owner):
    ada = configured(kernel)
    ada.pop("allow_silence", None)
    kernel.store.put("bots", ada)
    ada = kernel.store.get("bots", "ada")
    assert Bot.model_validate({k: v for k, v in ada.items() if k != "revision"}).allow_silence is True
    assert "allow_silence" not in ada
    assert kernel.service.public("bots", ada)["allow_silence"] is True
    before = kernel.store.get("bots", "hortator")
    result = await kernel.service.save(owner, "bots", "ada", {"allow_silence": False})
    assert result["allow_silence"] is False
    assert kernel.store.get("bots", "ada")["allow_silence"] is False
    assert kernel.store.get("bots", "hortator") == before
    assert result["enabled_plugins"] == ada["enabled_plugins"]
    result = await kernel.service.save(owner, "bots", "ada", {"persona": "Changed only my persona"})
    assert result["allow_silence"] is False
    result = await kernel.service.save(owner, "bots", "ada", {"allow_silence": True})
    assert result["allow_silence"] is True


@pytest.mark.parametrize("rounds", [0, 3])
async def test_disabled_silence_omits_tool_schema_and_requests_text_answer(kernel, rounds):
    _, transport = ready(kernel, allow_silence=False, enabled_plugins=[], max_tool_rounds=rounds)

    def respond(request):
        body = json.loads(request.content)
        if rounds:
            assert {t["function"]["name"] for t in body["tools"]} == {"discord_attach"}
        else:
            assert "tools" not in body and "tool_choice" not in body
        system = body["messages"][0]["content"]
        assert "The operator has disabled intentional silence" in system
        assert "overrides generic persona/shared advice" in system
        assert '"allow_silence":false' in body["messages"][-1]["content"]
        assert "or call council_silence" not in body["messages"][-1]["content"]
        return completion("Here is a contribution for this activation.")

    await install_client(kernel, respond)
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    transport.send.assert_awaited_once()


async def test_disabled_silence_preserves_action_tools_and_final_text_opportunity(kernel):
    _, transport = ready(kernel, allow_silence=False, enabled_plugins=["memory"], max_tool_rounds=1)
    bodies = []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            assert {t["function"]["name"] for t in body["tools"]} == {"memory", "discord_attach"}
            return reply(tool("memory", {"operation": "write", "key": "note", "value": "Testing"}))
        assert "tools" not in body
        return completion("Saved my note; here is my answer.")

    await install_client(kernel, respond)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(bodies) == 2
    assert kernel.store.one("SELECT value FROM memories WHERE key='note'")["value"] == "Testing"
    transport.send.assert_awaited_once()


@pytest.mark.parametrize("arguments", [{}, {"label": "Listen"}, {"label": 42, "extra": True}, '{"label":'])
async def test_disabled_silence_is_rejected_with_usage_and_bounded_repair(kernel, arguments):
    _, transport = ready(kernel, allow_silence=False, max_tool_rounds=1)
    bodies = []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert all(t["function"]["name"] != "council_silence" for t in body.get("tools", []))
        if len(bodies) == 1:
            return reply(tool("council_silence", arguments))
        report = responses(body)[-1]
        assert not report["ok"] and not report["executed"]
        assert not report.get("usage_only")
        assert "disabled" in report["error"] and report["usage"]["tool"] == "council_silence"
        if isinstance(arguments, dict) and "extra" in arguments:
            assert report["error_count"] == 3
        return completion("I will contribute instead.")

    await install_client(kernel, respond)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(bodies) == 2
    assert not any(e["kind"] == "decision.silence" for e in kernel.store.events())
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    transport.send.assert_awaited_once()


async def test_repeated_disabled_calls_fail_at_existing_budget_and_never_become_silence(kernel):
    _, transport = ready(kernel, allow_silence=False, max_tool_rounds=1)
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return reply(tool("council_silence", {"label": "No"}))

    await install_client(kernel, respond)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(bodies) == 2
    assert kernel.store.one("SELECT status FROM turns")["status"] == "failed"
    assert not any(e["kind"] == "decision.silence" for e in kernel.store.events())
    transport.send.assert_not_awaited()


async def test_disabling_silence_cancels_in_progress_turn_without_changing_other_bots(kernel, owner):
    import asyncio

    _, transport = ready(kernel)
    pending = asyncio.Event()

    async def respond(request):
        pending.set()
        await asyncio.Event().wait()

    await install_client(kernel, respond)
    await kernel.engine.tick()
    await asyncio.wait_for(pending.wait(), 3)
    await kernel.service.save(owner, "bots", "ada", {"allow_silence": False})
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "cancelled"
    transport.send.assert_not_awaited()


@pytest.mark.parametrize("allow_silence", [False, True])
@pytest.mark.parametrize(
    "message,usage",
    [
        ({"content": ""}, {}),
        ({"content": None}, {"completion_tokens": 0, "completion_tokens_details": {"reasoning_tokens": 0}}),
        (
            {"content": None, "reasoning_content": "Private native reasoning fixture"},
            {"completion_tokens": 139, "completion_tokens_details": {"reasoning_tokens": 137}},
        ),
        ({"content": "<think>Private inline reasoning fixture</think>"}, {}),
    ],
)
async def test_empty_response_has_factual_error_independent_of_silence_policy(
    kernel, allow_silence, message, usage
):
    _, transport = ready(kernel, allow_silence=allow_silence)
    await install_client(
        kernel,
        lambda _: httpx.Response(
            200,
            json={"choices": [{"message": message, "finish_reason": "stop"}], "usage": usage},
        ),
    )
    await kernel.engine.tick()
    await settle(kernel)
    turn = kernel.store.one("SELECT * FROM turns")
    assert turn["status"] == "failed" and "no publishable answer or tool calls" in turn["error"]
    assert "silence" not in turn["error"].lower()
    assert "reasoning-only" not in turn["error"]
    requests = kernel.store.rows("SELECT * FROM requests")
    assert len(requests) == 1  # No retry, invented answer, or silence decision.
    request = requests[0]
    assert request["status"] == "completed" and request["http_status"] == 200
    event = next(e for e in kernel.store.events() if e["kind"] == "turn.failed")
    assert event["level"] == "error" and event["request_id"] == request["id"]
    assert event["turn_id"] == turn["id"]
    assert event["data"]["error_origin"] == "missing_answer"
    assert event["data"]["http_status"] == 200
    assert event["data"]["finish_reason"] == "stop"
    assert event["data"]["answer_chars"] == event["data"]["tool_calls"] == 0
    assert event["data"]["output_tokens"] == usage.get("completion_tokens")
    assert event["data"]["reasoning_tokens"] == usage.get("completion_tokens_details", {}).get(
        "reasoning_tokens"
    )
    for private_text in ("Private native reasoning fixture", "Private inline reasoning fixture"):
        assert private_text not in json.dumps(kernel.store.events())
    if "Private" in str(message):
        assert "Private" in str(kernel.service.turn(turn["id"], include_diagnostics=True))
    assert kernel.store.health("openrouter")["consecutive_failures"] == 0
    assert not kernel.store.rows("SELECT * FROM response_recovery_pending")
    assert not any(e["kind"] == "decision.silence" for e in kernel.store.events())
    transport.send.assert_not_awaited()
