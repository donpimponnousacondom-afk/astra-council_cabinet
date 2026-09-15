import json

import pytest

from conftest import configured
from hortator.plugins import ToolContext
from hortator.store import dumps
from hortator.working_set import result_reference


def inspector(kernel):
    bot = configured(kernel, "hortator", enabled_plugins=["council_inspect"])
    return ToolContext(bot, "control", "inspection-turn", owner_verified=True)


async def call(kernel, context, arguments, call_id="inspect"):
    return await kernel.registry.call("council_inspect", arguments, context, call_id)


def large_context(kernel):
    kernel.store.context("ada", "room")
    text = "Conversation observation 🦆 " * 5000 + " FINAL SUMMARY MARKER"
    kernel.store.execute("UPDATE contexts SET summary=? WHERE bot_id='ada'", (text,))
    return text


async def test_roster_and_status_survive_large_first_bot_context_without_dashboard_changes(kernel):
    context = inspector(kernel)
    summary = large_context(kernel)
    bot = kernel.store.get("bots", "ada")
    bot["persona"] = "Lengthy persona " * 8000
    kernel.store.put("bots", bot)
    expected_ids = [b["id"] for b in kernel.store.list("bots")]
    roster = await call(kernel, context, {"resource": "bots"})
    assert [b["id"] for b in roster["value"]] == expected_ids
    assert len(dumps(roster)) < 6000
    ada = next(b for b in roster["value"] if b["id"] == "ada")
    assert ada["model_profile_id"] == "balanced"
    assert ada["model"] == "test-model"
    assert ada["provider_id"] == "openrouter"
    assert ada["rooms"][0]["name"] == kernel.store.get("rooms", "council")["name"]
    assert "contexts" not in ada and "persona" not in ada
    status = await call(kernel, context, {"resource": "status"}, "status")
    assert [b["id"] for b in status["bots"]] == expected_ids
    assert summary not in dumps(status) and "Lengthy persona" not in dumps(status)
    assert not status.get("result_is_paged")
    # Existing dashboard and deterministic-command responses keep their fields.
    dashboard_bot = kernel.service.inspect("bots", "ada")
    assert dashboard_bot["contexts"][0]["summary"] == summary
    assert dashboard_bot["persona"] == bot["persona"]
    assert kernel.service.status()["bots"][0]["contexts"]


async def test_bot_configuration_and_context_are_explicit_separate_inspections(kernel):
    context = inspector(kernel)
    summary = large_context(kernel)
    detail = await call(kernel, context, {"resource": "bots", "id": "ada"})
    assert "persona" in detail and "contexts" not in detail
    result = await call(kernel, context, {"resource": "context", "id": "ada"}, "context")
    assert result["result_is_paged"]
    assert len(dumps(result)) < 1000
    assert not result.get("truncated")
    # The handle is the complete original, not a second record of the notice.
    stored = kernel.store.one("SELECT content FROM tool_result_evidence WHERE id=?", (result["result_id"],))
    assert json.loads(stored["content"])[0]["summary"] == summary
    pieces = []
    args = {**result["read_response"], "length": 18000}
    while args:
        page = await call(kernel, context, args, "page-" + str(len(pieces)))
        assert "error" not in page
        assert len(page["text"]) <= 18000
        pieces.append(page["text"])
        args = page["next"]
    assert "".join(pieces) == stored["content"]
    assert json.loads("".join(pieces))[0]["summary"].endswith("FINAL SUMMARY MARKER")
    assert not kernel.registry.allowed("workspace", context)


@pytest.mark.parametrize("resource", ["bots", "context", "events", "providers", "profiles"])
async def test_unknown_identity_returns_real_ids_and_discovery_instead_of_guesses(kernel, resource):
    context = inspector(kernel)
    result = await call(kernel, context, {"resource": resource, "id": "not-a-configured-id"})
    kind = "bots" if resource in ("context", "events") else resource
    assert result["ok"] is False
    assert '"resource":"' + kind + '"' in result["error"]
    for entity in kernel.store.list(kind):
        assert entity["id"] in result["error"]
    assert "usage" in result
    reference = result_reference({"role": "tool", "content": dumps(result)})
    assert reference["reread"]["resource"] == "read_result"
    page = await call(kernel, context, reference["reread"], "reread-error")
    assert "does not exist" in page["text"]
    if kind == "bots":
        assert "not bot IDs" in result["error"]


@pytest.mark.parametrize("large_page", [False, True])
async def test_read_result_scope_and_revoked_source_grants_are_enforced(kernel, large_page):
    context = inspector(kernel)
    full = kernel.registry.evidence.record(context, "council_inspect", "original", {"evidence": "retained"})
    args = {"resource": "read_result", "result_id": full["result_id"]}
    for replacement in (
        ToolContext(context.bot, "another-channel", context.turn_id, True),
        ToolContext(context.bot, context.channel_id, "another-turn", True),
        ToolContext({**context.bot, "id": "another-bot"}, context.channel_id, context.turn_id, True),
        ToolContext(context.bot, context.channel_id, context.turn_id, False),
    ):
        assert (await call(kernel, replacement, args))["ok"] is False
    # Even an owner inspector must keep transitive grants on copied result pages.
    context.bot["enabled_plugins"].append("memory")
    kernel.store.put("bots", context.bot)
    original = kernel.registry.evidence.record(
        context, "memory", "memory-result", {"note": "\x01" * 18000 if large_page else "retained"}
    )
    page = await call(kernel, context, {**args, "result_id": original["result_id"]}, "memory-page")
    assert "error" not in page
    # Escaped source text remains paged and retains its source grant dependencies.
    if large_page:
        page = await call(kernel, context, {**args, "result_id": original["result_id"], "length": 18000})
        assert "error" not in page
    current = kernel.store.get("bots", context.bot["id"])
    current["enabled_plugins"].remove("memory")
    kernel.store.put("bots", current)
    denied = await call(kernel, context, {**args, "result_id": page["result_id"]}, "revoked")
    assert "original tool grant" in denied["error"]


async def test_large_inspections_redact_secrets_before_persisting_and_paging(kernel):
    context = inspector(kernel)
    secret = "sk-test-inspection-secret-must-not-leak"
    kernel.vault.put("provider/openrouter/api_key", secret)
    prompt = kernel.store.list("prompts")[0]
    prompt["content"] = "Fixture " * 10000 + secret + " end-of-evidence"
    kernel.store.put("prompts", prompt)
    result = await call(kernel, context, {"resource": "prompts", "id": prompt["id"]})
    row = kernel.store.one("SELECT content FROM tool_result_evidence WHERE id=?", (result["result_id"],))
    assert secret not in row["content"]
    assert "[REDACTED] end-of-evidence" in row["content"]
    tail = await call(kernel, context, {**result["read_response"], "offset": len(row["content"]) - 100})
    assert secret not in tail["text"] and "end-of-evidence" in tail["text"]


async def test_native_inspector_handle_survives_working_set_omission(kernel):
    context = inspector(kernel)
    roster = await call(kernel, context, {"resource": "bots"})
    reference = result_reference({"role": "tool", "content": dumps(roster), "tool_call_id": "inspect"})
    assert reference["reread"]["resource"] == "read_result"
    assert "operation" not in reference["reread"]
    page = await call(kernel, context, reference["reread"], "omitted-read")
    assert "error" not in page
    assert '"id":"ada"' in page["text"]


async def test_inspector_usage_and_all_paging_validation_errors(kernel):
    context = inspector(kernel)
    usage = await call(kernel, context, {})
    assert usage["usage_only"] and not usage["executed"]
    assert usage["usage"]["example"] == {"resource": "bots"}
    invalid = await call(kernel, context, {"resource": "read_result", "offset": -1, "length": 18001})
    assert invalid["error_count"] == 3
    assert invalid["usage"]["example"]["resource"] == "read_result"
    assert invalid["usage"]["example"]["result_id"]
