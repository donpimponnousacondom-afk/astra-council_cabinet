"""Protocol regressions: compatibility must not conceal incomplete work or upstream errors."""

import asyncio
import json

import httpx
import pytest

from conftest import configured
from hortator.chat_response import ChatResponse, SSEReader
from hortator.diagnostics import read_diagnostics
from hortator.provider import ProviderError
from test_provider import Fragments, call_args, install_client


def packet(delta=None, finish=None):
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def wire(*packets, done=True):
    text = "".join("data: " + json.dumps(p, ensure_ascii=False) + "\n\n" for p in packets)
    return (text + ("data: [DONE]\n\n" if done else "")).encode()


async def respond(kernel, data, **kwargs):
    await install_client(
        kernel,
        lambda r: httpx.Response(
            200,
            headers={"content-type": "Text/Event-Stream; charset=utf-8"},
            stream=Fragments(data),
            **kwargs,
        ),
    )


def stored(kernel):
    row = kernel.store.one("SELECT * FROM requests ORDER BY started_at DESC LIMIT 1")
    return row, read_diagnostics(kernel.store, row["id"])


@pytest.mark.parametrize("newline", ["\n", "\r", "\r\n"])
@pytest.mark.parametrize("chunk_size", [1, 2, 7, 4096])
async def test_sse_framing_utf8_bom_line_endings_and_multiline(newline, chunk_size):
    # U+2028/U+2029 are string characters, not SSE line separators.
    text = '\ufeff: comment\nretry: 500\nid: fixture\nunknown: ignored\nevent: message\ndata: {"value":\ndata: "é猫\u2028\u2029"}\n\ndata: [DONE]\n\n'
    raw = text.replace("\n", newline).encode()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for start in range(0, len(raw), chunk_size):
                yield raw[start : start + chunk_size]

    trace = {}
    frames = [f async for f in SSEReader(trace).frames(httpx.Response(200, stream=Stream()))]
    assert len(frames) == 2
    assert json.loads(frames[0].data) == {"value": "é猫\u2028\u2029"}
    assert frames[0].id == frames[1].id == "fixture"
    assert frames[1].data == "[DONE]"
    assert trace["compatibility"] == {"comments": 1}
    assert trace["response_bytes"] == len(raw)


async def test_nullable_frames_metadata_usage_and_thinking_are_compatible(kernel):
    bot = configured(kernel)
    packets = [
        None,
        {},
        {"id": "fixture", "model": "fixture", "object": "chat.completion.chunk"},
        {"choices": None, "usage": None},
        {"choices": [None]},
        packet(),
        packet({"role": "assistant", "content": None, "tool_calls": None, "reasoning_details": None}),
        packet({"thinking": [{"type": "text", "text": "private reasoning"}]}),
        packet({"content": [{"type": "text", "text": "Answer 🐱"}]}, "stop"),
        {"choices": [], "usage": {"prompt_tokens": 15, "completion_tokens": 7}},
    ]
    await respond(kernel, b": keepalive\n\nevent: ping\ndata: ping\n\ndata:\n\n" + wire(*packets))
    result = await kernel.pool.complete(**call_args(kernel, bot))
    row, diagnostic = stored(kernel)
    assert result.content == "Answer 🐱"
    assert row["output_tokens"] == 7 and row["ttft_ms"] is not None
    assert diagnostic["reasoning_status"] == "present"
    assert diagnostic["reasoning_content"] == "private reasoning"
    assert diagnostic["response"]["compatibility"]["null_packets"] == 1
    assert diagnostic["response"]["completion_boundary"] == "done"
    assert kernel.store.health("openrouter")["consecutive_failures"] == 0
    assert "private reasoning" not in row["response"]


@pytest.mark.parametrize("purpose", ["generation", "compaction"])
@pytest.mark.parametrize("actual_sse", [True, False])
async def test_buffered_profile_owns_wire_mode_without_losing_json_usage_or_reasoning(
    kernel, purpose, actual_sse
):
    bot = configured(kernel)
    args = call_args(kernel, bot)
    args["purpose"] = purpose
    args["profile"].update(
        stream=False,
        include_usage=True,
        request_json={"stream": True, "stream_options": {"include_usage": True}, "vendor": {"x": 7}},
        compaction_request_json={"stream_options": {"include_usage": True}},
    )

    def handler(request):
        sent = json.loads(request.content)
        assert sent["stream"] is False and "stream_options" not in sent
        assert sent["vendor"] == {"x": 7}
        message = {"content": "Final", "reasoning": "Buffered reasoning"}
        usage = {"prompt_tokens": 100, "completion_tokens": 20}
        if actual_sse:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=Fragments(wire(packet(message, "stop"), {"usage": usage})),
            )
        return httpx.Response(
            200, json={"choices": [{"message": message, "finish_reason": "stop"}], "usage": usage}
        )

    await install_client(kernel, handler)
    result = await kernel.pool.complete(**args)
    row, diagnostic = stored(kernel)
    assert result.content == "Final"
    assert row["ttft_ms"] is None and row["first_visible_ms"] is None
    assert row["output_tokens"] == 20 and row["input_tokens"] == 100
    assert diagnostic["reasoning_content"] == "Buffered reasoning"
    assert diagnostic["response"]["requested_stream"] is False
    assert diagnostic["response"]["response_format"] == ("sse" if actual_sse else "json")
    assert args["profile"]["include_usage"] is True
    assert args["profile"]["request_json"]["stream_options"] == {"include_usage": True}
    assert not any(e["kind"] == "request.first_token" for e in kernel.store.events())


async def test_indexed_interleaved_tool_fragments_and_null_function(kernel):
    bot = configured(kernel)
    await respond(
        kernel,
        wire(
            packet({"tool_calls": [None]}),
            packet(
                {
                    "tool_calls": [
                        {"index": 0, "id": "one", "function": {"name": "web_fetch", "arguments": '{"url":'}},
                        {
                            "index": 1,
                            "id": "two",
                            "function": {"name": "memory", "arguments": '{"operation":'},
                        },
                    ]
                }
            ),
            packet({"tool_calls": [{"index": 1, "function": None}]}),
            packet(
                {
                    "tool_calls": [
                        {"index": 1, "function": {"arguments": '"read"}'}},
                        {"index": 0, "function": {"arguments": '"https://example.com"}'}},
                    ]
                },
                "tool_calls",
            ),
        ),
    )
    result = await kernel.pool.complete(**call_args(kernel, bot))
    assert [t["id"] for t in result.tool_calls] == ["one", "two"]
    assert [json.loads(t["function"]["arguments"]) for t in result.tool_calls] == [
        {"url": "https://example.com"},
        {"operation": "read"},
    ]


async def test_missing_tool_index_only_inferred_when_unambiguous(kernel):
    bot = configured(kernel)
    await respond(
        kernel,
        wire(
            packet(
                {"tool_calls": [{"id": "one", "function": {"name": "memory", "arguments": '{"operation":'}}]}
            ),
            packet({"tool_calls": [{"function": {"arguments": '"read"}'}}]}, "tool_calls"),
        ),
    )
    result = await kernel.pool.complete(**call_args(kernel, bot))
    assert json.loads(result.tool_calls[0]["function"]["arguments"]) == {"operation": "read"}
    assert result.response_diagnostics["compatibility"]["inferred_single_tool_index"] == 2


@pytest.mark.parametrize(
    "bad,field",
    [
        (b"data: []\n\n", "packet"),
        (b"data: {oops}\n\n", "data"),
        (b'data: {"usage":{"prompt_tokens":NaN}}\n\n', "data"),
        (b'data: {"choices":{}}\n\n', "choices"),
        (wire(packet({"content": 5})), "choices[0].delta.content"),
        (wire(packet({"reasoning_content": False})), "choices[0].delta.reasoning_content"),
        (wire(packet({"tool_calls": {}})), "choices[0].delta.tool_calls"),
        (wire(packet({"reasoning_details": "invalid"})), "choices[0].delta.reasoning_details"),
        (wire(packet({"tool_calls": [{"index": True}]})), "choices[0].delta.tool_calls[0].index"),
        (
            wire(packet({"tool_calls": [{"id": "one"}, {"id": "two"}]})),
            "choices[0].delta.tool_calls[0].index",
        ),
        (
            wire(packet({"tool_calls": [{"index": 0, "function": {"arguments": {}}}]})),
            "choices[0].delta.tool_calls[0].function.arguments",
        ),
        (b"data: \xff\n\n", "SSE body"),
    ],
)
async def test_format_failure_has_scope_retains_partial_reasoning_and_never_trips_circuit(kernel, bad, field):
    bot = configured(kernel)
    await respond(kernel, wire(packet({"reasoning_content": "Partial diagnostic"}), done=False) + bad)
    with pytest.raises(ProviderError) as error:
        await kernel.pool.complete(**call_args(kernel, bot))
    assert error.value.provider_fault is False
    assert error.value.details["origin"] == "response_format"
    assert error.value.details["field"] == field
    row, diagnostic = stored(kernel)
    assert row["status"] == "failed" and row["http_status"] == 200
    assert diagnostic["reasoning_status"] == "present"
    assert diagnostic["reasoning_content"] == "Partial diagnostic"
    assert diagnostic["provider_error"]["origin"] == "response_format"
    event = next(e for e in kernel.store.events() if e["kind"] == "request.failed")
    assert event["data"]["field"] == field
    assert event["data"]["error_origin"] == "response_format"
    assert kernel.store.health("openrouter")["consecutive_failures"] == 0
    assert not any(e["kind"] == "provider.failure" for e in kernel.store.events())


async def test_offending_frame_is_private_bounded_and_redacted_before_truncation(kernel):
    bot = configured(kernel)
    secret = "very-private-parser-fixture"
    kernel.vault.put("provider/openrouter/api_key", secret)
    await respond(
        kernel, wire({"choices": {}, "reasoning": "PRIVATE-FRAME", "key": secret, "padding": "x" * 6000})
    )
    with pytest.raises(ProviderError):
        await kernel.pool.complete(**call_args(kernel, bot))
    row, diagnostic = stored(kernel)
    frame = diagnostic["response"]["offending_frame"]
    assert frame["frame_index"] == 1 and frame["truncated"] is True
    assert len(frame["data_excerpt"]) == 4000
    assert secret not in str(diagnostic)
    assert "[REDACTED]" in frame["data_excerpt"]
    assert "PRIVATE-FRAME" in frame["data_excerpt"]
    assert "PRIVATE-FRAME" not in str(kernel.store.events()) + str(row)


@pytest.mark.parametrize(
    "body",
    [
        b'event: error\ndata: {"code":"server_error","message":"upstream failed"}\n\n',
        b'event: error\ndata: "upstream failed"\n\n',
        b"event: error\ndata: upstream failed\n\n",
        b"event: error\ndata: null\n\n",
        b"event: error\n\n",
    ],
)
async def test_named_upstream_error_is_never_misreported_as_parser_failure(kernel, body):
    bot = configured(kernel)
    await respond(kernel, body)
    with pytest.raises(ProviderError) as error:
        await kernel.pool.complete(**call_args(kernel, bot))
    assert error.value.provider_fault is True
    row, diagnostic = stored(kernel)
    assert diagnostic["provider_error"]["origin"] == "upstream_error"
    assert row["http_status"] == 200
    assert kernel.store.health("openrouter")["consecutive_failures"] == 1


@pytest.mark.parametrize("finish", [None, "stop"])
@pytest.mark.parametrize("broken_transport", [False, True])
async def test_eof_requires_completion_boundary_and_never_delivers_a_partial_answer(
    kernel, finish, broken_transport
):
    bot = configured(kernel)

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield wire(packet({"reasoning": "Private", "content": "Answer"}, finish), done=False)
            if broken_transport:
                raise httpx.RemoteProtocolError("peer disconnected")

    await install_client(
        kernel, lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())
    )
    if finish:
        result = await kernel.pool.complete(**call_args(kernel, bot))
        assert result.content == "Answer"
        if broken_transport:
            assert result.response_diagnostics["compatibility"]["transport_closed_after_finish"] == 1
    else:
        with pytest.raises(ProviderError) as error:
            await kernel.pool.complete(**call_args(kernel, bot))
        assert error.value.provider_fault is True
        row, diagnostic = stored(kernel)
        assert row["status"] == "failed"
        assert diagnostic["provider_error"]["origin"] == "transport"
        assert diagnostic["reasoning_content"] == "Private"


async def test_eof_unterminated_final_event_is_explicit_compatibility(kernel):
    bot = configured(kernel)
    await respond(kernel, wire(packet({"content": "Full"}, "stop"), done=False).rstrip(b"\n"))
    result = await kernel.pool.complete(**call_args(kernel, bot))
    assert result.content == "Full"
    assert result.response_diagnostics["compatibility"]["final_event_without_separator"] == 1


async def test_done_without_a_choice_fails_format(kernel):
    bot = configured(kernel)
    await respond(kernel, wire(None))
    with pytest.raises(ProviderError, match="no assistant choice") as error:
        await kernel.pool.complete(**call_args(kernel, bot))
    assert error.value.provider_fault is False


async def test_python_adapter_error_is_local_with_safe_location(kernel, monkeypatch):
    bot = configured(kernel)

    def broken(*args, **kwargs):
        raise TypeError("fixture adapter bug")

    monkeypatch.setattr(ChatResponse, "packet", broken)
    await respond(kernel, wire(packet({"content": "Full"}, "stop")))
    with pytest.raises(ProviderError, match="Local provider-client failure") as error:
        await kernel.pool.complete(**call_args(kernel, bot))
    assert error.value.provider_fault is False
    _, diagnostic = stored(kernel)
    assert diagnostic["provider_error"]["origin"] == "local_client"
    assert diagnostic["provider_error"]["details"]["location"][-1]["function"] == "broken"
    assert kernel.store.health("openrouter")["consecutive_failures"] == 0


async def test_keepalives_cannot_extend_total_request_deadline(kernel):
    bot = configured(kernel)
    provider = kernel.store.get("providers", "openrouter")
    provider["timeout_seconds"] = 0.03
    # Store fixtures bypass the operator minimum to exercise the real deadline quickly.
    kernel.store.put("providers", provider)

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b": alive\n\ndata: null\n\n"
                await asyncio.sleep(0.001)

    await install_client(
        kernel, lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())
    )
    with pytest.raises(ProviderError, match="timed out"):
        await kernel.pool.complete(**call_args(kernel, bot))
    _, diagnostic = stored(kernel)
    assert diagnostic["provider_error"]["origin"] == "transport"
    assert diagnostic["reasoning_status"] == "none_received"


async def test_sse_byte_limit_includes_non_content_frames(kernel, monkeypatch):
    bot = configured(kernel)
    monkeypatch.setattr("hortator.chat_response.RESPONSE_LIMIT", 30)
    await respond(kernel, b": keepalive\n\n" * 10)
    with pytest.raises(ProviderError, match="response limit") as error:
        await kernel.pool.complete(**call_args(kernel, bot))
    assert error.value.details["origin"] == "response_format"
    assert error.value.provider_fault is False


@pytest.mark.parametrize("capture_enabled", [False, True])
def test_missing_diagnostics_reports_this_request_without_claiming_reasoning_was_discarded(
    kernel, capture_enabled
):
    context = {"diagnostic_capture_version": 2} if capture_enabled else {}
    kernel.store.execute(
        "INSERT INTO requests(id,turn_id,bot_id,provider_id,profile_id,model,purpose,started_at,status,body,context) VALUES('req-old','turn-old','ada','openrouter','balanced','fixture','generation',1,'completed','{}',?)",
        (json.dumps(context),),
    )
    diagnostic = read_diagnostics(kernel.store, "req-old")
    assert diagnostic["request_id"] == "req-old"
    assert diagnostic["provider_id"] == "openrouter"
    assert diagnostic["request_started_at"] == "1970-01-01T00:00:01+00:00"
    assert diagnostic["reasoning_status"] == ("capture_missing" if capture_enabled else "not_recorded")
    assert "discarded" not in diagnostic["note"]


async def test_buffered_no_reasoning_distinguishes_reported_tokens_from_returned_text(kernel):
    bot = configured(kernel)
    await install_client(
        kernel,
        lambda r: httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Answer"}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 20, "completion_tokens_details": {"reasoning_tokens": 5}},
            },
        ),
    )
    await kernel.pool.complete(**call_args(kernel, bot))
    row, diagnostic = stored(kernel)
    assert row["ttft_ms"] is None
    assert diagnostic["reasoning_status"] == "not_returned"
    assert "5 reasoning tokens" in diagnostic["note"]
    assert diagnostic["capture"] == "recorded"
