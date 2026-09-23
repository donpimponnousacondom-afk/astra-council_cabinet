"""Split emoji deltas must work; malformed scalars must never break the failure ledger."""

import json

import httpx
import pytest

from conftest import configured
from hortator.provider import ProviderError
from support.provider import call_args, install_client
from support.provider_streaming import packet, respond, stored


def escaped_wire(*packets):
    return (
        "".join("data: " + json.dumps(p, ensure_ascii=True) + "\n\n" for p in packets) + "data: [DONE]\n\n"
    ).encode()


@pytest.mark.parametrize("field", ["content", "reasoning_content", "thinking", "tool_arguments"])
async def test_split_surrogate_pair_across_frames(kernel, field):
    bot = configured(kernel)
    if field == "tool_arguments":
        packets = [
            packet(
                {
                    "tool_calls": [
                        {
                            "index": 3,
                            "id": "test",
                            "function": {"name": "memory", "arguments": '{"value":"\ud83d'},
                        }
                    ]
                }
            ),
            packet({"tool_calls": [{"index": 3, "function": {"arguments": '\ude00"}'}}]}, "tool_calls"),
        ]
    else:
        packets = [packet({field: "before \ud83d"}), packet({}), packet({field: "\ude00 after"}, "stop")]
    await respond(kernel, escaped_wire(*packets))
    result = await kernel.pool.complete(**call_args(kernel, bot))
    text = (
        result.content
        if field == "content"
        else result.tool_calls[0]["function"]["arguments"]
        if field == "tool_arguments"
        else result.reasoning_content
    )
    assert "😀" in text
    row, diag = stored(kernel)
    assert row["status"] == "completed"
    assert diag["response"]["compatibility"]["joined_surrogate_pairs"] == 1
    json.dumps(diag, ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("bad", ["\ud83d", "\ude00", "\ud83dtext", "\ud83d\ud83d"])
async def test_malformed_unicode_is_a_recorded_format_failure(kernel, stream, bad):
    bot = configured(kernel)
    if stream:
        await respond(
            kernel, escaped_wire(packet({"content": "valid prefix"}), packet({"content": bad}, "stop"))
        )
    else:
        body = json.dumps(
            {"choices": [{"message": {"content": bad}, "finish_reason": "stop"}]}, ensure_ascii=True
        ).encode()
        await install_client(
            kernel, lambda _: httpx.Response(200, content=body, headers={"content-type": "application/json"})
        )
    with pytest.raises(ProviderError, match="Unicode"):
        await kernel.pool.complete(**call_args(kernel, bot))
    row, diag = stored(kernel)
    assert row["status"] == "failed"
    assert (
        next(e for e in kernel.store.events() if e["kind"] == "request.failed")["data"]["error_origin"]
        == "response_format"
    )
    assert kernel.store.health("openrouter")["consecutive_failures"] == 0
    assert any(e["kind"] == "request.failed" for e in kernel.store.events())
    json.dumps(diag, ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize(
    "extra", [{"model": "\ud83d"}, {"usage": {"extra": "\ud83d"}}, {"error": {"message": "\ud83d"}}]
)
async def test_invalid_metadata_cannot_poison_diagnostics(kernel, extra):
    bot = configured(kernel)
    await respond(kernel, escaped_wire({**packet({"content": "answer"}, "stop"), **extra}))
    with pytest.raises(ProviderError, match="Unicode"):
        await kernel.pool.complete(**call_args(kernel, bot))
    row, diag = stored(kernel)
    assert row["status"] == "failed"
    assert "\\ud83d" in json.dumps(diag)
    json.dumps(diag, ensure_ascii=False).encode()


def test_json_escaped_argument_surrogates_rejected_before_tool_execution():
    from hortator.tool_feedback import parse_arguments

    with pytest.raises(ValueError, match=r"arguments\['first'\].*arguments\['second'\]"):
        parse_arguments(r'{"first":"\ud83d","second":"\ude00"}')
    assert parse_arguments(r'{"complete":"\ud83d\ude00"}') == {"complete": "😀"}
