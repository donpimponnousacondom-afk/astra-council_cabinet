import asyncio
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import ingest
from hortator.app import create_app
from hortator.models import ControlError
from hortator.plugins import ToolContext
from hortator.runtime import DeliveryError
from support.provider import install_client
from support.runtime import completion, settle
from support.runtime_feedback import ready, tool
from support.slash_commands import enable as enable_slash, interaction


CHANNEL = "222222222222222222"
PRIVATE = "Private factual engram fixture"


def enable(kernel, **config):
    plugin = kernel.store.get("plugins", "engram")
    plugin.pop("revision")
    plugin.update(enabled=True, config={**plugin["config"], **config})
    kernel.store.put("plugins", plugin)
    return ready(kernel, enabled_plugins=["engram"], cooldown_seconds=0)


def response(body, *, content="Public answer", mem=PRIVATE, facts="Owner asked for a fixture"):
    nonce = re.search(r"\[\^ENGRAM:([0-9a-f]{32})\]", str(body))[1]
    return content + f"\n[^ENGRAM:{nonce}]\n" + json.dumps({"MEM": mem, "FACTS": facts}) + f"\n[^END:{nonce}]"


def state(kernel):
    return kernel.registry.engrams.inspect("ada")["states"]


async def run(kernel):
    kernel.store.execute("UPDATE bot_runtime SET next_at=0 WHERE bot_id='ada'")
    await kernel.engine.tick()
    await settle(kernel)


async def test_disabled_is_inert_and_never_a_model_tool(kernel):
    bot, transport = ready(kernel, enabled_plugins=[])
    assert not kernel.store.get("plugins", "engram")["enabled"]
    assert all("engram" not in b["enabled_plugins"] for b in kernel.store.list("bots"))
    assert "engram" not in [
        item["function"]["name"] for item in kernel.registry.schemas(ToolContext(bot, CHANNEL, "t"))
    ]
    text = "An ordinary [^MEM] reference with a code example."
    await install_client(kernel, lambda _: completion(text))
    await run(kernel)
    assert transport.send.await_args.args[2] == text
    context = json.loads(kernel.store.one("SELECT context FROM requests")["context"])
    assert "engram" not in context
    assert not state(kernel)


async def test_confirmed_final_only_commits_private_state_and_sanitizes_inspection(kernel):
    _, transport = enable(kernel)
    await install_client(kernel, lambda request: completion(response(json.loads(request.content))))
    await run(kernel)
    assert transport.send.await_args.args[2] == "Public answer"
    saved = state(kernel)[0]
    assert saved["MEM"] == PRIVATE
    assert saved["revision"] == 1
    assert saved["covered_through"] == 1
    assert kernel.store.one("SELECT content,status FROM outbox") == {
        "content": "Public answer",
        "status": "sent",
    }
    assert PRIVATE not in str(kernel.store.rows("SELECT content FROM messages"))
    turn = kernel.store.one("SELECT id FROM turns")["id"]
    assert PRIVATE not in str(kernel.service.inspect_model("turn", turn))
    assert PRIVATE in str(kernel.service.turn(turn, include_diagnostics=True))
    # A later input contains the prior state, which remains owner-only evidence.
    ingest(kernel, content="Another input", discord_id="555555555555555556")
    transport.send.return_value = "888888888888888889"
    await run(kernel)
    later = kernel.store.one("SELECT id FROM turns ORDER BY started_at DESC LIMIT 1")["id"]
    assert PRIVATE not in str(kernel.service.inspect_model("turn", later))
    assert PRIVATE not in str(kernel.service.inspect_model("events", "ada"))
    assert PRIVATE in str(kernel.service.turn(later, include_diagnostics=True))


@pytest.mark.parametrize(
    "failure", [DeliveryError("refused"), DeliveryError("timeout", uncertain=True), asyncio.CancelledError()]
)
async def test_failure_uncertainty_and_cancellation_do_not_acknowledge(kernel, failure):
    _, transport = enable(kernel)
    transport.send.side_effect = failure
    await install_client(kernel, lambda request: completion(response(json.loads(request.content))))
    await run(kernel)
    assert not state(kernel)
    assert kernel.store.context("ada", CHANNEL)["last_seen"] == 0
    assert kernel.registry.engrams.recover()["committed"] == 0


@pytest.mark.parametrize("invalid", ["missing", "malformed", "length"])
async def test_invalid_or_incomplete_final_cannot_advance_coverage(kernel, invalid):
    _, transport = enable(kernel)

    def handler(request):
        content = response(json.loads(request.content))
        if invalid == "missing":
            return completion("Public answer")
        if invalid == "malformed":
            return completion(content.replace('{"MEM":', '{"BROKEN":'))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}, "finish_reason": "length"}]}
        )

    await install_client(kernel, handler)
    await run(kernel)
    assert not state(kernel)
    stored = json.loads(kernel.store.one("SELECT response FROM requests")["response"])
    assert PRIVATE not in str(stored)
    if invalid == "length":
        transport.send.assert_not_awaited()
    else:
        assert transport.send.await_args.args[2] == "Public answer"


async def test_intermediate_tool_suffix_is_stripped_but_never_staged(kernel):
    bot, transport = enable(kernel)
    bot.pop("revision")
    bot["enabled_plugins"].append("memory")
    kernel.store.put("bots", bot)
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": response(body),
                                "tool_calls": [tool("memory", {"operation": "read"})],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            )
        assert PRIVATE not in str(body)
        return completion("Public final without an update")

    await install_client(kernel, handler)
    await run(kernel)
    assert len(calls) == 2
    assert transport.send.await_args.args[2] == "Public final without an update"
    assert not state(kernel)
    assert not kernel.store.rows("SELECT * FROM engram_candidates")


async def test_partial_provider_failure_keeps_suffix_out_of_model_evidence(kernel):
    _, transport = enable(kernel)

    def handler(request):
        content = response(json.loads(request.content))
        frame = {"choices": [{"delta": {"content": content}}]}
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text="data: " + json.dumps(frame) + "\n\ndata: {not-json}\n\n",
        )

    await install_client(kernel, handler)
    await run(kernel)
    request = kernel.store.one("SELECT response,status,turn_id FROM requests")
    assert request["status"] == "failed"
    assert json.loads(request["response"])["content"] == "Public answer"
    assert PRIVATE not in str(kernel.service.inspect_model("turn", request["turn_id"]))
    assert PRIVATE in str(kernel.service.turn(request["turn_id"], include_diagnostics=True))
    assert not state(kernel)
    transport.send.assert_not_awaited()


async def test_messages_arriving_during_generation_remain_uncovered(kernel):
    bot, _ = enable(kernel, reduce_history=True)

    def handler(request):
        for index in range(1, 6):
            ingest(
                kernel, content=f"Arrived after request {index}", discord_id=str(555555555555555555 + index)
            )
        return completion(response(json.loads(request.content)))

    await install_client(kernel, handler)
    await run(kernel)
    assert state(kernel)[0]["covered_through"] == 1
    profile = kernel.store.get("profiles", bot["model_profile_id"])
    rows, _, _, _ = await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "next", [])
    assert [row["seq"] for row in rows] == list(range(1, 8))


async def test_reduction_keeps_recent_acknowledged_plus_every_uncovered_message(kernel):
    bot, transport = enable(kernel, reduce_history=True, recent_messages=12)
    for index in range(1, 30):
        ingest(kernel, content=f"History {index}", discord_id=str(555555555555555555 + index))
    await install_client(kernel, lambda request: completion(response(json.loads(request.content))))
    await run(kernel)
    covered = state(kernel)[0]["covered_through"]
    for index in range(30, 49):
        ingest(kernel, content=f"Uncovered {index}", discord_id=str(555555555555555555 + index))
    profile = kernel.store.get("profiles", bot["model_profile_id"])
    rows, _, _, meta = await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "preview", [])
    expected = kernel.store.transcript(CHANNEL, limit=1000)
    assert [r["seq"] for r in rows] == [r["seq"] for r in expected if r["seq"] <= covered][-12:] + [
        r["seq"] for r in expected if r["seq"] > covered
    ]
    assert len(meta["message_sequences"]) > 12
    assert kernel.store.context("ada", CHANNEL)["checkpoint"] == 0
    # Disabling immediately restores the ordinary uncompacted transcript.
    plugin = kernel.store.get("plugins", "engram")
    plugin.pop("revision")
    kernel.store.put("plugins", {**plugin, "enabled": False})
    rows, _, _, _ = await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "ordinary", [])
    assert len(rows) == len(expected)


async def test_summary_stays_until_acknowledged_and_reduction_never_changes_checkpoint(kernel):
    bot, _ = enable(kernel, reduce_history=True)
    kernel.store.execute("UPDATE contexts SET summary='Previous retained summary',checkpoint=1")
    ingest(kernel, content="New input", discord_id="555555555555555556")
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return completion(response(body))

    await install_client(kernel, handler)
    await run(kernel)
    assert "Previous retained summary" in str(seen[0])
    profile = kernel.store.get("profiles", bot["model_profile_id"])
    _, summary, _, meta = await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "next", [])
    assert summary == ""
    assert meta["summary"] == ""
    assert kernel.store.context("ada", CHANNEL)["summary"] == "Previous retained summary"
    assert kernel.store.context("ada", CHANNEL)["checkpoint"] == 1


async def test_training_can_compact_and_captures_the_final_summary_signature(kernel):
    enable(kernel)
    profile = kernel.store.get("profiles", "balanced")
    profile.pop("revision")
    profile.update(
        context_window=12000,
        response_tokens=2048,
        compact_threshold=0.35,
        summary_tokens=100,
        keep_recent_messages=2,
    )
    kernel.store.put("profiles", profile)
    for index in range(1, 30):
        ingest(
            kernel,
            content=("Earlier conversation facts " * 50) + str(index),
            discord_id=str(555555555555555555 + index),
        )

    def handler(request):
        body = json.loads(request.content)
        return completion(
            response(body) if "ENGRAM WIRE CONTRACT" in str(body) else "Retained prior conversation"
        )

    await install_client(kernel, handler)
    await run(kernel)
    context = kernel.store.context("ada", CHANNEL)
    assert context["compactions"] >= 1
    assert state(kernel)[0]["summary_checkpoint"] == context["checkpoint"]
    assert state(kernel)[0]["covered_through"] == 30


def test_owner_engram_routes_require_auth_csrf_and_exact_confirmation(tmp_path):
    with TestClient(create_app(tmp_path, start_runtime=False)) as client:
        endpoint = "/api/engrams/ada"
        assert client.get(endpoint).status_code == 401
        password = (tmp_path / "initial-password").read_text().strip()
        assert client.post("/api/auth/login", json={"password": password}).status_code == 200
        view = client.get(endpoint).json()
        assert view["states"] == [] and not view["enabled"] and view["ordinary_only"]
        assert client.get("/api/engrams/missing").status_code == 404
        assert client.post(endpoint + "/reset", json={"confirm_bot_id": "ada"}).status_code == 403
        headers = {"X-CSRF-Token": client.get("/api/auth/session").json()["csrf"]}
        assert (
            client.post(endpoint + "/reset", json={"confirm_bot_id": "wrong"}, headers=headers).status_code
            == 400
        )
        result = client.post(endpoint + "/reset", json={"confirm_bot_id": "ada"}, headers=headers)
        assert result.json() == {"bot_id": "ada", "channel_id": None, "reset": True}


@pytest.mark.parametrize("layer", ["transcript", "engram_state", "engram_instructions"])
async def test_disabled_required_input_fails_before_provider_without_discarding(kernel, layer):
    bot, transport = enable(kernel, reduce_history=True)
    bot.pop("revision")
    kernel.store.put("bots", {**bot, "disabled_prompt_layers": [layer]})
    await install_client(kernel, lambda _: pytest.fail("Disabled context must fail before inference"))
    await run(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "failed"
    assert not state(kernel)
    assert kernel.store.context("ada", CHANNEL)["checkpoint"] == 0
    transport.send.assert_not_awaited()


async def test_actual_request_budget_keeps_oversized_uncovered_backlog_intact(kernel):
    bot, _ = enable(kernel, reduce_history=True)
    ingest(kernel, content="Uncovered long input " * 4000, discord_id="555555555555555556")
    profile = kernel.store.get("profiles", bot["model_profile_id"])
    profile.update(context_window=4096, response_tokens=1024)
    with pytest.raises(ControlError, match="exceed"):
        await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "budget", [])
    assert kernel.store.context("ada", CHANNEL)["checkpoint"] == 0
    assert not kernel.store.rows("SELECT * FROM requests")
    assert not state(kernel)


async def test_owner_reset_clears_only_target_and_clean_slate_also_clears(kernel, owner):
    enable(kernel)
    await install_client(kernel, lambda request: completion(response(json.loads(request.content))))
    await run(kernel)
    with pytest.raises(ControlError, match="Confirm"):
        await kernel.service.engram_reset(owner, "ada", {"confirm_bot_id": "other"})
    await kernel.service.engram_reset(owner, "ada", {"confirm_bot_id": "ada", "channel_id": CHANNEL})
    assert state(kernel)[0]["MEM"] == ""
    assert kernel.store.transcript(CHANNEL)
    ingest(kernel, content="After memory reset", discord_id="555555555555555556")
    await run(kernel)
    assert state(kernel)[0]["MEM"] == PRIVATE
    await kernel.service.control(
        owner, {"action": "reset_context", "kind": "bots", "id": "ada", "data": {"confirm_bot_id": "ada"}}
    )
    assert state(kernel)[0]["MEM"] == ""
    assert state(kernel)[0]["covered_through"] == 0


async def test_slash_stays_outside_ordinary_engram_checkpoint(kernel):
    bot = enable_slash(kernel)
    bot.pop("revision")
    bot["enabled_plugins"].append("engram")
    kernel.store.put("bots", bot)
    bot = kernel.store.get("bots", "ada")
    plugin = kernel.store.get("plugins", "engram")
    plugin.pop("revision")
    kernel.store.put("plugins", {**plugin, "enabled": True})

    def handler(request):
        assert "ENGRAM WIRE CONTRACT" not in str(json.loads(request.content))
        return completion("Slash answer")

    await install_client(kernel, handler)
    item = interaction(bot)
    await kernel.connector.slash.receive("ada", item)
    await settle(kernel)
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    assert not state(kernel)
    assert not kernel.store.rows("SELECT * FROM engram_candidates")
