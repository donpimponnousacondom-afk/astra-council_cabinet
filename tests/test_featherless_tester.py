import asyncio
import json
from contextlib import suppress

import httpx
import pytest

import featherless_tester as probe


def options(**changes):
    opts = probe.parser().parse_args(["--model", "test/model", "--yes"])
    for key, value in changes.items():
        setattr(opts, key, value)
    return opts


def model():
    return {
        "id": "test/model",
        "context_length": 32768,
        "concurrency_cost": 2,
        "availability": {"tier": "warm"},
    }


def test_selection_rejects_mistyped_ranges_without_silent_expansion():
    assert probe.parse_selection("2-4,6,3", 7) == [1, 2, 3, 5]
    assert probe.parse_selection("all", 3) == [0, 1, 2]
    for bad in ("0", "2-8", "4-2", "1;3", ""):
        with pytest.raises(ValueError):
            probe.parse_selection(bad, 7)


def test_native_parameters_preserve_vendor_settings_and_output_policy():
    opts = options(params={"min_p": 0.1, "chat_template_kwargs": {"preserve_thinking": True}}, thinking="off")
    assert probe.parameters(opts) == {
        "min_p": 0.1,
        "max_tokens": 512,
        "chat_template_kwargs": {"preserve_thinking": True, "enable_thinking": False},
    }
    opts.omit_max_tokens = True
    assert "max_tokens" not in probe.parameters(opts)
    opts.params = {"messages": []}
    with pytest.raises(ValueError, match="CLI controls"):
        probe.parameters(opts)
    assert probe.ceiling(model(), {"max_context_length": 16000}) == 16000


async def test_weighted_gate_releases_cancelled_waiters_and_never_exceeds_units():
    gate = probe.Units(4, 4)
    started, release = asyncio.Event(), asyncio.Event()

    async def holder():
        async with gate.slot(4):
            started.set()
            await release.wait()

    async def waiter():
        async with gate.slot(2):
            pytest.fail("Waiter entered while all units were in use")

    async with asyncio.TaskGroup() as group:
        group.create_task(holder())
        await started.wait()
        task = group.create_task(waiter())
        await asyncio.sleep(0)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert gate.active == 1 and gate.used == 4
        release.set()
    assert gate.active == gate.used == 0


async def test_catalog_is_filtered_bounded_and_detail_failure_is_visible(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/v1/plan":
            return httpx.Response(200, json={"max_context_length": 32768, "concurrency": 4})
        if request.url.path == "/v1/models":
            assert request.url.params["q"] == "qwen"
            assert request.url.params["per_page"] == "1"
            # Their live endpoint can ignore per_page. Enforce it locally too.
            return httpx.Response(200, json={"data": [{"id": "test/model"}, {"id": "test/extra"}]})
        assert request.url.path == "/v1/models/test/model"
        return httpx.Response(200, json=model())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tester = probe.Tester(
            client, probe.Evidence(tmp_path / "out"), options(model=None, filter=["qween"], limit=1)
        )
        models = await tester.discover()
        assert len(models) == 1
        with pytest.raises(ValueError, match="search filter"):
            await tester.api("GET", "/v1/models")
    assert len(calls) == 3


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        for item in [
            b": keepalive\n\ndata: null\n\n",
            b'data: {"choices":[{"delta":{"reasoning_content":"Think"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":" there"},"finish_reason":"stop"}],"usage":{"prompt_tokens":9,"completion_tokens":6,"completion_tokens_details":{"reasoning_tokens":2}}}\n\n',
            b"data: [DONE]\n\n",
        ]:
            await asyncio.sleep(0.001)
            yield item


async def test_sse_metrics_distinguish_reasoning_visible_output_and_usage(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks())
        )
    ) as client:
        tester = probe.Tester(client, probe.Evidence(tmp_path / "out"), options())
        tester.gate = probe.Units(1, 4)
        result = await tester.request(
            model(), [{"role": "user", "content": "Hi"}], {"max_tokens": 512}, stream=True
        )
    assert result["status"] == "completed"
    assert 0 < result["ttft_ms"] < result["first_visible_ms"]
    assert result["stream_tps"] > 0
    assert result["reasoning_tokens"] == 2
    assert result["reasoning_content"] == "Think"
    assert result["content"] == "Hello there"
    assert result["input_tokens"] == 9


@pytest.mark.parametrize(
    "reply,expected",
    [
        (
            {
                "choices": [{"message": {"content": "abc"}, "finish_reason": "length"}],
                "usage": {"completion_tokens": 3},
            },
            "length",
        ),
        ({"error": {"code": "bad_request", "message": "upstream detail"}}, "failed"),
        ({"choices": [{"message": {"content": "partial"}}]}, "failed"),
    ],
)
async def test_json_preserves_error_and_truncation_and_never_invents_ttft(tmp_path, reply, expected):
    def handler(request):
        assert "stream_options" not in json.loads(request.content)
        return httpx.Response(200, json=reply)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tester = probe.Tester(client, probe.Evidence(tmp_path / "out"), options())
        tester.gate = probe.Units(1, 4)
        result = await tester.request(model(), [{"role": "user", "content": "Hi"}], {}, stream=False)
    assert result["status"] == expected
    assert result["ttft_ms"] is result["stream_tps"] is None
    if "error" in reply:
        assert json.loads(result["error"]) == reply["error"]


async def test_filler_measures_complete_prompt_and_overflow_does_not_call_inference(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path.endswith("/debug/chat-format")
        b = json.loads(request.content)
        return httpx.Response(
            200, json={"token_count": sum(probe.local_tokens(m["content"]) + 5 for m in b["messages"])}
        )

    opts = options(input_tokens=1000, token_tolerance=16, context_limit=1000)
    evidence = probe.Evidence(tmp_path / "out")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tester = probe.Tester(client, evidence, opts)
        messages, count, source = await tester.messages(model(), probe.make_filler(1, 4000), {})
        assert abs(count - 1000) <= 16
        assert messages[0]["content"] == opts.system
        assert messages[-1]["content"].endswith(opts.prompt)
        assert source == "provider_chat_template"
        await tester.benchmark(model(), probe.make_filler(1, 4000), {"max_tokens": 512})
        assert not evidence.rows
    assert all(r.url.path.endswith("/debug/chat-format") for r in calls)


def test_evidence_redacts_credentials_and_is_private(tmp_path):
    evidence = probe.Evidence(tmp_path / "out", ["synthetic-secret-value"])
    path = evidence.write(
        "sample.json", {"error": "Contains synthetic-secret-value", "authorization": "another value"}
    )
    assert "synthetic-secret-value" not in open(path).read()
    assert (evidence.root / "sample.json").stat().st_mode & 0o777 == 0o600


def test_warm_tier_does_not_hide_absence_of_live_or_recent_worker():
    value = model()
    value["availability"].update(is_hot_live=False, is_hot_recent=False)
    assert probe.needs_warmup(value)
    assert probe.availability_matches(value, "not-hot")
    assert probe.availability_matches(value, "warm")
    value["availability"]["is_hot_live"] = True
    assert not probe.needs_warmup(value)


async def test_failed_warmup_polls_metadata_before_running_benchmark(tmp_path, monkeypatch):
    value = model()
    value["availability"].update(is_hot_live=False, is_hot_recent=False)
    ready = model()
    ready["availability"]["is_hot_live"] = True
    calls = []
    async with httpx.AsyncClient() as client:
        tester = probe.Tester(client, probe.Evidence(tmp_path / "out"), options(poll_seconds=0))

        async def request(model, messages, parameters, **kwargs):
            calls.append(kwargs["purpose"])
            assert parameters["max_tokens"] == 1 and messages[0]["content"]
            return {"status": "failed", "http_status": 503}

        async def detail(model_id):
            calls.append("metadata")
            return ready

        monkeypatch.setattr(tester, "request", request)
        monkeypatch.setattr(tester, "detail", detail)
        assert await tester.ready(value) == ready
    assert calls == ["warmup", "metadata"]


async def test_sse_capacity_envelope_retries_without_losing_first_attempt(tmp_path):
    attempts = []

    def handler(request):
        if request.url.path.endswith("/debug/chat-format"):
            return httpx.Response(200, json={"token_count": 10})
        attempts.append(json.loads(request.content))
        packet = (
            {"error": {"code": "capacity_exhausted", "message": "busy"}}
            if len(attempts) == 1
            else {"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]}
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="data: " + json.dumps(packet) + "\n\ndata: [DONE]\n\n",
        )

    evidence = probe.Evidence(tmp_path / "out")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tester = probe.Tester(client, evidence, options(retries=1, retry_delay=0))
        tester.gate = probe.Units(1, 4)
        await tester.benchmark(model(), "", {"max_tokens": 512})
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert [row["status"] for row in evidence.rows] == ["failed", "completed"]
    assert evidence.rows[0]["upstream_error_code"] == "capacity_exhausted"
