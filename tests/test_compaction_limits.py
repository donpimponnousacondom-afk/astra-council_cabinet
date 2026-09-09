"""Compaction budgets retained text independently of provider reasoning usage."""

import copy
import json
import threading

import httpx
import pytest
from pydantic import ValidationError

from conftest import configured, ingest
from hortator.diagnostics import read_diagnostics
from hortator.models import ControlError, Profile
from test_provider import Fragments, call_args, install_client

CHANNEL = "222222222222222222"
CAPS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


@pytest.mark.parametrize("purpose", ["generation", "compaction"])
@pytest.mark.parametrize("stream", [True, False])
async def test_compaction_omits_total_caps_without_mutating_saved_generation_or_reasoning(
    kernel, purpose, stream
):
    bot = configured(kernel)
    args = call_args(kernel, bot)
    profile = args["profile"]
    profile.update(
        stream=stream,
        request_json={
            "max_tokens": 1024,
            "max_completion_tokens": 2048,
            "reasoning_effort": "high",
            "vendor": {"x": 7},
        },
        compaction_request_json={"max_tokens": 9, "max_output_tokens": 10, "thinking": {"type": "enabled"}},
    )
    original = copy.deepcopy(profile)
    captured = []

    async def response(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Complete."}, "finish_reason": "stop"}]}
        )

    await install_client(kernel, response)
    result = await kernel.pool.complete(**args, purpose=purpose)
    body = captured[0]
    assert body["stream"] is stream and body["vendor"] == {"x": 7}
    assert body["reasoning_effort"] == "high" and profile == original
    row = kernel.store.one("SELECT body,context FROM requests WHERE id=?", (result.request_id,))
    saved = json.loads(row["body"])
    if purpose == "compaction":
        assert all(key not in body and key not in saved for key in CAPS)
        assert body["thinking"] == {"type": "enabled"}
        meta = json.loads(row["context"])
        assert meta["omitted_output_cap_fields"] == list(CAPS)
        assert meta["retained_summary_token_limit"] == profile["summary_tokens"]
        assert read_diagnostics(kernel.store, result.request_id)["output_limits"] == {}
    else:
        assert body["max_tokens"] == 1024 and body["max_completion_tokens"] == 2048
        assert "thinking" not in body


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("reported_usage", [True, False])
async def test_large_reasoning_is_retained_privately_and_does_not_consume_summary_budget(
    kernel, monkeypatch, stream, reported_usage
):
    bot = configured(kernel)
    ingest(kernel)
    profile = kernel.store.get("profiles", "balanced")
    profile.update(summary_tokens=128, stream=stream)
    reasoning = "private deliberation " * 700
    summary = "Socrates disagreed with Ada; the Boss asked them to test both views."
    usage = {"completion_tokens": 200000, "completion_tokens_details": {"reasoning_tokens": 199980}}
    captured = []
    owner = threading.get_ident()
    count_threads = []
    counter = kernel.engine.contexts.count_summary_tokens

    def counted(text):
        count_threads.append(threading.get_ident())
        return counter(text)

    monkeypatch.setattr(kernel.engine.contexts, "count_summary_tokens", counted)

    async def response(request):
        body = json.loads(request.content)
        captured.append(body)
        assert all(key not in body for key in CAPS)
        assert "128 visible text tokens" in body["messages"][0]["content"]
        if stream:
            packets = [
                {"choices": [{"delta": {"reasoning_content": reasoning}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": summary}, "finish_reason": "stop"}]},
            ]
            if reported_usage:
                packets.append({"choices": [], "usage": usage})
            raw = "".join("data: " + json.dumps(p) + "\n\n" for p in packets) + "data: [DONE]\n\n"
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Fragments(raw.encode())
            )
        data = {
            "choices": [
                {"message": {"content": summary, "reasoning_content": reasoning}, "finish_reason": "stop"}
            ]
        }
        if reported_usage:
            data["usage"] = usage
        return httpx.Response(200, json=data)

    await install_client(kernel, response)
    await kernel.engine.contexts.prepare(bot, profile, CHANNEL, "reasoning-unbounded", [], force=True)
    saved = kernel.store.context(bot["id"], CHANNEL)
    assert saved["summary"] == summary and saved["checkpoint"] == 1
    assert count_threads and all(thread != owner for thread in count_threads)
    request = kernel.store.one("SELECT * FROM requests")
    assert request["output_tokens"] == (200000 if reported_usage else None)
    assert read_diagnostics(kernel.store, request["id"])["reasoning_content"] == reasoning
    assert "private deliberation" not in request["response"]
    assert "private deliberation" not in str(kernel.store.events())
    checked = next(e for e in kernel.store.events() if e["kind"] == "compaction.summary_checked")
    assert checked["data"]["summary_tokens"] == counter(summary)
    assert checked["data"]["accepted"] is True and len(captured) == 1


@pytest.mark.parametrize("over_by", [0, 1])
async def test_exact_visible_text_boundary_preserves_full_candidate_and_old_checkpoint_on_overflow(
    kernel, over_by
):
    bot = configured(kernel)
    ingest(kernel)
    profile = kernel.store.get("profiles", "balanced")
    context = kernel.engine.contexts
    boundary = ("word " * 128).strip()
    limit = context.count_summary_tokens(boundary)
    candidate = boundary if not over_by else boundary + " more"
    assert context.count_summary_tokens(candidate) == limit + over_by
    profile["summary_tokens"] = limit
    kernel.store.execute("UPDATE contexts SET summary='Old intact memory'")
    await install_client(
        kernel,
        lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": candidate}, "finish_reason": "stop"}]}
        ),
    )
    if over_by:
        with pytest.raises(ControlError, match="retained-text limit"):
            await context.prepare(bot, profile, CHANNEL, "too-long", [], force=True)
        saved = kernel.store.context(bot["id"], CHANNEL)
        assert saved["summary"] == "Old intact memory" and saved["checkpoint"] == 0
    else:
        await context.prepare(bot, profile, CHANNEL, "fits", [], force=True)
        assert kernel.store.context(bot["id"], CHANNEL)["summary"] == candidate
    request = kernel.store.one("SELECT response FROM requests")
    assert json.loads(request["response"])["content"] == candidate
    assert len(kernel.store.rows("SELECT seq FROM messages")) == 1


@pytest.mark.parametrize("finish", ["length", "content_filter", "insufficient_system_resource", "tool_calls"])
async def test_incomplete_upstream_summary_is_not_accepted_even_when_its_text_fits(kernel, finish):
    bot = configured(kernel)
    ingest(kernel)
    await install_client(
        kernel,
        lambda request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Unfinished conclusion"}, "finish_reason": finish}]},
        ),
    )
    with pytest.raises(ControlError, match="No total-output cap was sent"):
        await kernel.engine.contexts.prepare(
            bot, kernel.store.get("profiles", "balanced"), CHANNEL, "incomplete", [], force=True
        )
    assert kernel.store.context(bot["id"], CHANNEL)["checkpoint"] == 0


def test_retained_summary_limit_has_no_fixed_32000_ceiling_but_still_requires_context_space():
    profile = Profile(
        id="large",
        name="Large",
        provider_id="provider",
        model="model",
        context_window=262144,
        summary_tokens=65536,
    )
    assert profile.summary_tokens == 65536
    schema = Profile.model_json_schema()["properties"]["summary_tokens"]
    assert "maximum" not in schema and schema["minimum"] == 128
    with pytest.raises(ValidationError, match="60% of the context window"):
        Profile(
            id="invalid",
            name="Invalid",
            provider_id="provider",
            model="model",
            context_window=32768,
            summary_tokens=32768,
        )
