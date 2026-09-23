import json

import httpx
import pytest

from conftest import configured
from support.console import output
from support.provider import Fragments, call_args, install_client


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("purpose", ["generation", "compaction"])
@pytest.mark.parametrize("reported_reasoning", [None, 0, 7])
async def test_each_response_logs_numeric_counts_without_changing_raw_usage_or_footer(
    kernel, stream, purpose, reported_reasoning
):
    bot = configured(kernel)
    args = call_args(kernel, bot)
    args["profile"]["stream"] = stream
    args["purpose"] = purpose
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    if reported_reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reported_reasoning}
    private = "Private chain of thought: count the choices."
    message = {"content": "An answer.", "reasoning_content": private}

    def handle(request):
        packet = {
            "choices": [{"delta" if stream else "message": message, "finish_reason": "stop"}],
            "usage": usage,
        }
        if not stream:
            return httpx.Response(200, json=packet)
        wire = "data: " + json.dumps(packet) + "\n\ndata: [DONE]\n\n"
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Fragments(wire.encode())
        )

    await install_client(kernel, handle)
    result = await kernel.pool.complete(**args)
    row = kernel.store.one("SELECT * FROM requests WHERE id=?", (result.request_id,))
    event = kernel.store.one(
        "SELECT data FROM events WHERE request_id=? AND kind='request.completed'", (result.request_id,)
    )
    data = json.loads(event["data"])
    assert data["reasoning_tokens"] == row["reasoning_tokens"] == reported_reasoning
    assert json.loads(row["usage"]) == usage
    assert data["total_tokens"] == 120
    assert data["token_counts"]["reasoning"]["source"] == (
        "estimated" if reported_reasoning is None else "reported"
    )
    assert data["token_counts"]["reasoning"]["value"] == (
        reported_reasoning if reported_reasoning is not None else 9
    )
    if stream:
        assert data["tps"] == pytest.approx(20 * 1000 / (row["duration_ms"] - row["ttft_ms"]))
        assert data["tps_source"] == "reported"
    else:
        assert data["tps"] is None and data["tps_source"] == "unavailable"
    assert json.loads(row["response"])["footer_tokens"] == (
        data["token_counts"] if purpose == "generation" else None
    )
    assert private not in json.dumps(kernel.store.events())


async def test_tool_round_and_final_answer_have_separate_counts_and_estimated_tps(kernel):
    bot = configured(kernel)
    args = call_args(kernel, bot)
    responses = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "memory", "arguments": '{"operation":"list"}'},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 30,
                "completion_tokens_details": {"reasoning_tokens": 25},
            },
        },
        {"choices": [{"delta": {"content": "Done."}, "finish_reason": "stop"}]},
    ]

    def handle(request):
        wire = "data: " + json.dumps(responses.pop(0)) + "\n\ndata: [DONE]\n\n"
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Fragments(wire.encode())
        )

    await install_client(kernel, handle)
    with output() as console:
        console.bind(kernel)
        await kernel.pool.complete(**args)
        await kernel.pool.complete(**args)
        lines = [line for line in console.stream.getvalue().splitlines() if "request.completed" in line]
    assert len(lines) == 2
    assert "finish_reason=tool_calls · reasoning_tokens=25 · total_tokens=130 · tps=" in lines[0]
    assert "finish_reason=stop · reasoning_tokens=none · total_tokens=~" in lines[1]
    assert " · tps=~" in lines[1]
    rows = kernel.store.rows("SELECT * FROM requests ORDER BY started_at")
    assert [row["output_tokens"] for row in rows] == [30, None]
    assert json.loads(rows[1]["response"])["footer_tokens"]["reasoning"]["value"] is None


@pytest.mark.parametrize("level", ["info", "warning", "error"])
def test_completion_console_keeps_old_field_order_and_full_metric_suffix(level):
    event = {
        "kind": "request.completed",
        "at": 1,
        "scope": "providers",
        "level": level,
        "seq": 1,
        "bot_id": "loki",
        "data": {
            "provider_id": "cheapseek",
            "duration_ms": 15167.24,
            "ttft_ms": 4162.43,
            "attempt": 1,
            "max_attempts": 4,
            "model": "deepseek-v4.1-flash",
            "reasoning": 'reasoning_effort:"high",reasoning.effort:"high",reasoning.max_tokens:2048,thinking.type:"enabled"',
            "profile_revision": 9,
            "input_tokens": 204263,
            "output_tokens": 1977,
            "request_body_bytes": 704165,
            "finish_reason": "stop",
        },
    }
    with output() as console:
        console.render(event)
        original = console.stream.getvalue().splitlines()[-1]
        event["data"].update(
            {
                "reasoning_tokens": 1200,
                "total_tokens": 206240,
                "tps": 179.646,
                "tps_source": "reported",
                "message": "Completed",
            }
        )
        console.render(event)
        updated = console.stream.getvalue().splitlines()[-1]
    # The old line exceeds the folded limit; neither its prefix nor the new
    # measurements can be hidden by an ellipsis, and messages still come last.
    assert "…" not in original and "finish_reason=stop" in original
    assert (
        updated == original + " · reasoning_tokens=1200 · total_tokens=206240 · tps=179.6 · message=Completed"
    )
    fields = "duration_ms=15167.2 · ttft_ms=4162.4 · attempt=1 · max_attempts=4 · model="
    assert fields in updated


@pytest.mark.parametrize("reasoning,expected", [(None, "none"), (0, "0"), (7, "~7")])
def test_console_distinguishes_missing_zero_estimate_and_buffered_tps(reasoning, expected):
    with output() as console:
        console.render(
            {
                "kind": "request.completed",
                "at": 1,
                "scope": "providers",
                "level": "info",
                "data": {
                    "token_counts": {
                        "reasoning": {
                            "value": reasoning,
                            "source": "estimated" if reasoning == 7 else "reported",
                        }
                    },
                    "tps": None,
                },
            }
        )
        assert console.stream.getvalue().rstrip().endswith(f"reasoning_tokens={expected} · tps=none")
