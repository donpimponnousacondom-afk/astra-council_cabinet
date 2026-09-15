import json
import threading
from unittest.mock import patch

import httpx
import pytest
import tiktoken

from conftest import configured
from hortator.footer import render_footer, validate_template
from hortator.footer_tokens import measure_footer_tokens
from test_provider import Fragments, call_args, install_client


TEMPLATE = "R: {{REASONING_TOKENS}} | C: {{COMPLETION_TOKENS}} | T: {{TOTAL_TOKENS}}"
BODY = {"messages": [{"role": "user", "content": "Explain gravity."}]}


def tokens(text):
    return len(tiktoken.get_encoding("cl100k_base").encode(text, disallowed_special=()))


def measure(usage=None, content="Gravity bends spacetime.", reasoning="", details=(), calls=()):
    return measure_footer_tokens(BODY, usage or {}, content, reasoning, details, calls)


def render(counts):
    return render_footer(
        {"footer_enabled": True, "footer_template": TEMPLATE}, request={"footer_tokens": counts}
    )


def test_reported_zero_is_distinct_from_missing_reasoning():
    usage = {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125}
    assert render(measure(usage)) == "-# R: none | C: 25 | T: 125"
    usage["completion_tokens_details"] = {"reasoning_tokens": 0}
    assert (
        render(measure(usage, reasoning="Provider explicitly reported zero.")) == "-# R: 0 | C: 25 | T: 125"
    )
    usage["completion_tokens_details"]["reasoning_tokens"] = 10
    assert render(measure(usage)) == "-# R: 10 | C: 25 | T: 125"
    del usage["total_tokens"]
    assert measure(usage)["total"] == {"value": 125, "source": "derived"}


@pytest.mark.parametrize("bad", [None, -1, True, "24", 1.5])
def test_invalid_reasoning_count_does_not_become_zero(bad):
    assert measure({"completion_tokens_details": {"reasoning_tokens": bad}})["reasoning"]["value"] is None


def test_aliases_and_literal_special_tokens():
    counts = measure({"reasoning_tokens": 7, "output_tokens": 20, "input_tokens": 30})
    assert render(counts) == "-# R: 7 | C: 20 | T: 50"
    bot = {"footer_enabled": True, "footer_template": validate_template("{{thinking_tokens}}")}
    assert render_footer(bot, request={"footer_tokens": json.dumps(counts)}) == "-# 7"
    assert measure(reasoning="<|endoftext|>")["reasoning"]["value"] == tokens("<|endoftext|>")
    assert render_footer(bot) == "-# none"


def test_fixed_tokenizer_no_safety_multiplier_or_double_counting():
    reasoning, content = "Think carefully about gravity 🐱.", "Gravity bends spacetime."
    counts = measure({"prompt_tokens": 100}, content, reasoning)
    assert counts["reasoning"] == {"value": tokens(reasoning), "source": "estimated"}
    assert counts["completion"]["value"] == tokens(content) + tokens(reasoning)
    assert counts["total"]["value"] == 100 + tokens(content) + tokens(reasoning)
    assert "\\~" in render(counts)
    assert measure(content=content, reasoning=reasoning)["completion"] == counts["completion"]
    # Native reasoning, mirrored detail text and inline text are alternative representations.
    mirrored = measure(
        {"prompt_tokens": 100},
        f"<think>{reasoning}</think>{content}",
        reasoning,
        [{"type": "reasoning.text", "text": reasoning}],
    )
    assert mirrored == counts
    assert measure(content=f"<think>{reasoning}</think>{content}")["reasoning"] == counts["reasoning"]
    assert (
        measure(details=[{"type": "reasoning.text", "text": reasoning}])["reasoning"] == counts["reasoning"]
    )


def test_opaque_reasoning_is_unknown_and_images_never_tokenize_as_base64():
    assert measure(details=[{"type": "reasoning.encrypted", "data": "opaque"}])["reasoning"]["value"] is None
    assert (
        measure(details=[{"type": "reasoning.summary", "text": "Short summary"}])["reasoning"]["value"]
        is None
    )
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Explain gravity."},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 100000}},
                ],
            }
        ]
    }
    assert measure_footer_tokens(body, {}, "Gravity bends spacetime.", "", [], []) == measure()


def test_tool_output_and_reported_reasoning_are_counted_once():
    calls = [{"function": {"name": "web_fetch", "arguments": '{"url":"https://example.com"}'}}]
    counts = measure({"reasoning_tokens": 40}, content="", calls=calls)
    assert counts["completion"]["value"] == 40 + tokens("web_fetch") + tokens(
        calls[0]["function"]["arguments"]
    )
    assert counts["reasoning"] == {"value": 40, "source": "reported"}


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("with_usage", [False, True])
async def test_real_request_path_retains_numeric_evidence_without_changing_billing(
    kernel, stream, with_usage
):
    bot = configured(kernel)
    args = call_args(kernel, bot)
    args["profile"]["stream"] = stream
    private, visible = "Private reasoning 🐱: consider curvature.", "Gravity bends spacetime."
    usage = (
        {"prompt_tokens": 100, "completion_tokens": 30, "completion_tokens_details": {"reasoning_tokens": 20}}
        if with_usage
        else {}
    )

    def response(request):
        assert json.loads(request.content)["stream"] is stream
        if stream:
            packets = [
                {"choices": [{"delta": {"reasoning_content": private[:10]}}]},
                {"choices": [{"delta": {"reasoning_content": private[10:]}}]},
                {"choices": [{"delta": {"content": visible}, "finish_reason": "stop"}]},
                {"choices": [], "usage": usage},
            ]
            wire = "".join("data: " + json.dumps(p) + "\n\n" for p in packets) + "data: [DONE]\n\n"
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Fragments(wire.encode())
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": visible, "thinking": private}, "finish_reason": "stop"}],
                "usage": usage,
            },
        )

    await install_client(kernel, response)
    owner_thread = threading.get_ident()

    def measured(*args):
        assert threading.get_ident() != owner_thread
        return measure_footer_tokens(*args)

    with patch("hortator.provider.measure_footer_tokens", side_effect=measured):
        result = await kernel.pool.complete(**args)
    row = kernel.store.one("SELECT * FROM requests WHERE id=?", (result.request_id,))
    evidence = json.loads(row["response"])["footer_tokens"]
    assert evidence["reasoning"]["value"] == (20 if with_usage else tokens(private))
    assert evidence["completion"]["value"] == (30 if with_usage else tokens(private) + tokens(visible))
    assert row["output_tokens"] == (30 if with_usage else None)
    assert row["reasoning_tokens"] == (20 if with_usage else None)
    assert json.loads(row["usage"]) == usage
    assert row["cost"] is None
    assert private not in row["response"] and private not in json.dumps(kernel.store.events())
    assert result.content == visible
    assert (row["ttft_ms"] is not None) == stream
    assert "R: none" not in render(evidence)
