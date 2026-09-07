import asyncio
import json

import httpx
import pytest

from conftest import configured
from hortator.provider import ProviderError, strip_reasoning


class Fragments(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        for start in range(0, len(self.data), 7):
            yield self.data[start : start + 7]
            await asyncio.sleep(0)


async def install_client(k, handler):
    await k.pool.client.aclose()
    k.pool.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def call_args(k, bot):
    return dict(
        bot=bot,
        profile=k.store.get("profiles", "balanced"),
        messages=[{"role": "system", "content": "test"}],
        tools=[],
        turn_id="turn-test",
        context={"estimated_tokens": 30},
    )


async def test_fragmented_stream_preserves_options_and_records_usage(kernel):
    bot = configured(kernel)
    profile = kernel.store.get("profiles", "balanced")
    profile.pop("revision")
    profile["request_json"] = {
        "temperature": 0.37,
        "reasoning": {"effort": "low", "exclude": True},
        "top_p": 0.8,
        "vendor_option": {"nested": [1, 2]},
    }
    kernel.store.put("profiles", profile)
    packets = [
        {"choices": [{"delta": {"reasoning_content": "secret reasoning"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call1",
                                "type": "function",
                                "function": {"name": "council_speak", "arguments": '{"content":'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"hello"}'}}]},
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "completion_tokens_details": {"reasoning_tokens": 9},
                "prompt_tokens_details": {"cached_tokens": 30},
                "cost": 0.0003,
            },
        },
    ]
    payload = ("".join("data: " + json.dumps(p) + "\n\n" for p in packets) + "data: [DONE]\n\n").encode()

    async def handle(request):
        body = json.loads(request.content)
        assert body["reasoning"]["effort"] == "low"
        assert body["vendor_option"] == {"nested": [1, 2]}
        assert str(request.url) == "https://provider.test/v1/chat/completions"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Fragments(payload))

    await install_client(kernel, handle)
    result = await kernel.pool.complete(**call_args(kernel, bot))
    assert json.loads(result.tool_calls[0]["function"]["arguments"]) == {"content": "hello"}
    row = kernel.store.one("SELECT * FROM requests")
    assert (row["input_tokens"], row["output_tokens"], row["reasoning_tokens"], row["cached_tokens"]) == (
        100,
        20,
        9,
        30,
    )
    assert row["ttft_ms"] is not None and row["first_visible_ms"] is None
    assert row["cost"] == 0.0003
    assert "secret reasoning" not in str(kernel.store.rows("SELECT * FROM requests"))
    assert "secret reasoning" not in str(kernel.store.events())


async def test_buffered_usage_missing_remains_unknown(kernel):
    bot = configured(kernel)

    async def handle(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "<think>private</think>Visible"}, "finish_reason": "stop"}
                ]
            },
        )

    await install_client(kernel, handle)
    result = await kernel.pool.complete(**call_args(kernel, bot))
    assert result.content == "Visible"
    row = kernel.store.one("SELECT * FROM requests")
    assert row["input_tokens"] is None and row["ttft_ms"] is None and row["cost"] is None


async def test_provider_circuit_and_profile_failure_attribution(kernel):
    bot = configured(kernel)

    async def handle(request):
        return httpx.Response(503, text="temporarily unavailable")

    await install_client(kernel, handle)
    for _ in range(3):
        with pytest.raises(ProviderError):
            await kernel.pool.complete(**call_args(kernel, bot))
    health = kernel.store.health("openrouter")
    assert health["consecutive_failures"] == 3 and health["circuit_until"] > 0
    with pytest.raises(ProviderError, match="circuit"):
        await kernel.pool.complete(**call_args(kernel, bot))
    assert len(kernel.store.rows("SELECT * FROM requests")) == 3
    kernel.store.execute("UPDATE provider_health SET consecutive_failures=0,circuit_until=0")
    await install_client(kernel, lambda r: httpx.Response(400, text="unsupported parameter"))
    with pytest.raises(ProviderError):
        await kernel.pool.complete(**call_args(kernel, bot))
    assert kernel.store.health("openrouter")["consecutive_failures"] == 0


async def test_stream_disconnect_is_failed_not_delivered(kernel):
    bot = configured(kernel)
    payload = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
    await install_client(
        kernel,
        lambda r: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Fragments(payload)
        ),
    )
    with pytest.raises(ProviderError, match="disconnected"):
        await kernel.pool.complete(**call_args(kernel, bot))
    assert kernel.store.one("SELECT status FROM requests")["status"] == "failed"


def test_reasoning_filter_handles_unclosed_blocks():
    assert strip_reasoning("before <analysis>private") == "before"
    assert strip_reasoning("<think>x</think>yes <reasoning>y</reasoning>") == "yes"


async def test_provider_concurrency_is_bounded(kernel):
    bot = configured(kernel)
    provider = kernel.store.get("providers", "openrouter")
    provider.pop("revision")
    kernel.store.put("providers", {**provider, "max_concurrency": 2})
    active = peak = 0

    async def handle(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.015)
        active -= 1
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Ready"}, "finish_reason": "stop"}]}
        )

    await install_client(kernel, handle)
    results = await asyncio.gather(*(kernel.pool.complete(**call_args(kernel, bot)) for _ in range(6)))
    assert len(results) == 6 and peak == 2
    assert kernel.pool.active["openrouter"] == 0
    contexts = [
        json.loads(r["context"])
        for r in kernel.store.rows("SELECT context FROM requests ORDER BY started_at")
    ]
    assert contexts[-1]["queue_ms"] > contexts[0]["queue_ms"]


async def test_circuit_half_open_admits_only_one_probe(kernel):
    bot = configured(kernel)
    kernel.store.health("openrouter")
    kernel.store.execute("UPDATE provider_health SET consecutive_failures=3,circuit_until=1")
    started, release = asyncio.Event(), asyncio.Event()

    async def handle(request):
        started.set()
        await release.wait()
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Recovered"}, "finish_reason": "stop"}]}
        )

    await install_client(kernel, handle)
    task = asyncio.create_task(kernel.pool.complete(**call_args(kernel, bot)))
    await started.wait()
    with pytest.raises(ProviderError, match="circuit"):
        await kernel.pool.complete(**call_args(kernel, bot))
    release.set()
    await task
    assert kernel.store.health("openrouter")["consecutive_failures"] == 0
    assert any(e["kind"] == "provider.recovered" for e in kernel.store.events())


async def test_partial_response_is_retained_with_provider_identity(kernel):
    bot = configured(kernel)
    payload = b'data: {"id":"upstream-request","model":"actual-served-model","choices":[{"delta":{"content":"Partial visible draft"}}]}\n\n'
    await install_client(
        kernel,
        lambda r: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Fragments(payload)
        ),
    )
    with pytest.raises(ProviderError):
        await kernel.pool.complete(**call_args(kernel, bot))
    response = json.loads(kernel.store.one("SELECT response FROM requests")["response"])
    assert response["partial"] is True and response["content"] == "Partial visible draft"
    assert response["provider_metadata"]["model"] == "actual-served-model"


async def test_invalid_bot_override_does_not_break_shared_provider(kernel):
    bot = configured(kernel)
    kernel.vault.put("bot/ada/provider_key", "test-bot-override-key")
    await install_client(kernel, lambda r: httpx.Response(401, text="Invalid credential"))
    for _ in range(3):
        with pytest.raises(ProviderError):
            await kernel.pool.complete(**call_args(kernel, bot))
    assert kernel.store.health("openrouter")["consecutive_failures"] == 0
    assert (
        kernel.service.public("providers", kernel.store.get("providers", "openrouter"))["recent"]["failures"]
        == 3
    )


async def test_cost_threshold_checked_between_requests(kernel):
    bot = configured(kernel, daily_cost_limit=0.001)
    await install_client(
        kernel,
        lambda r: httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Done"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 100, "cost": 0.002},
            },
        ),
    )
    await kernel.pool.complete(**call_args(kernel, bot))
    with pytest.raises(ProviderError, match="cost threshold"):
        await kernel.pool.complete(**call_args(kernel, bot))
    assert len(kernel.store.rows("SELECT * FROM requests")) == 1
