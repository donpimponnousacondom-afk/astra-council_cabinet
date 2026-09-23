"""Research reports must not silently turn text tool requests into findings."""

import json

import httpx
import pytest

from hortator.research_assistant import literal_tool_call_envelope
from support.background_jobs import finish
from support.provider import Fragments, install_client
from support.research_assistant import call, setup, start


@pytest.mark.parametrize(
    "content,invalid",
    [
        pytest.param(
            "I'll research the prices now.\n<tool_call><function=web_search>"
            "<parameter=query>official prices</parameter></function></tool_call>",
            True,
            id="prefaced-flash-envelope",
        ),
        pytest.param(
            "Let me check sources.\n<tool_call>\n<function=web_search>"
            "<parameter=query>official prices</parameter>",
            True,
            id="prefaced-opus-partial-envelope",
        ),
        pytest.param(
            'Searching now. <function name="web_search"><parameter=query>prices</parameter>',
            True,
            id="standalone-function-envelope",
        ),
        pytest.param("I will search next.\n<tool_call>", True, id="unfinished-tool-open"),
        pytest.param(
            'I will search now. <tool_call>{"name":"web_search","arguments":{"query":"prices"}}</tool_call>',
            True,
            id="prefaced-json-body-envelope",
        ),
        pytest.param(
            '<tool_call>{"name":"web_search","arguments":{}}</tool_call>',
            True,
            id="json-body-envelope-from-start",
        ),
        pytest.param(
            'Checking. <function=web_search>{"query":"prices"}</function>',
            True,
            id="complete-function-json-body",
        ),
        pytest.param(
            'Checking. <function name="web_search">{"query":"prices"}</function>',
            True,
            id="complete-named-function-json-body",
        ),
        pytest.param('{"tool":"council_speak","arguments":{}}', True, id="legacy-json-tool-wrapper"),
        pytest.param(
            '{"function":{"name":"council_speak","arguments":"{}"}}',
            True,
            id="legacy-json-function-wrapper",
        ),
        pytest.param(
            '{"name":"web_search","arguments":{"query":"prices"}}',
            True,
            id="native-json-tool-wrapper",
        ),
        pytest.param(
            '{"tool_calls":[{"function":{"name":"web_search","arguments":"{}"}}]}',
            True,
            id="native-json-tool-calls-wrapper",
        ),
        pytest.param(
            "Official pricing is listed at https://example.com/prices. The page was dated today.",
            False,
            id="plain-sol-report",
        ),
        pytest.param(
            "The source discusses `<tool_call><function=web_search>` as syntax."
            "\n```xml\n<tool_call><function=web_search></function></tool_call>\n```"
            "\n> <tool_call><function=web_search></function></tool_call>",
            False,
            id="quoted-and-fenced-examples",
        ),
    ],
)
@pytest.mark.parametrize("stream", [False, True])
async def test_live_shaped_reports_are_classified_without_repair(kernel, content, invalid, stream):
    context = setup(kernel, stream=stream)
    requests = []

    def respond(request):
        requests.append(request.content)
        message = {"content": content, "reasoning_content": "PRIVATE TRACE"}
        usage = {"prompt_tokens": 31, "completion_tokens": 19, "web_search_usage": {"tool_usage": 1}}
        if stream:
            halfway = len(content) // 2
            packets = [
                {"choices": [{"index": 0, "delta": {"content": content[:halfway]}}]},
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": content[halfway:], "reasoning_content": "PRIVATE TRACE"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": usage,
                },
            ]
            wire = "".join("data: " + json.dumps(packet) + "\n\n" for packet in packets) + "data: [DONE]\n\n"
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Fragments(wire.encode())
            )
        return httpx.Response(
            200, json={"choices": [{"message": message, "finish_reason": "stop"}], "usage": usage}
        )

    await install_client(kernel, respond)
    reply = await call(kernel, context, **start())
    await finish(kernel)
    job = kernel.jobs.get(reply["job_id"])
    saved = json.loads(job["result"])
    assert job["state"] == ("failed" if invalid else "completed")
    assert saved["complete"] is not invalid
    assert saved["report"] == content.strip()
    assert saved["search_usage"] == {"tool_usage": 1}
    assert "PRIVATE TRACE" not in job["result"]
    assert len(requests) == 1
    if invalid:
        assert saved["kind"] == "invalid_researcher_output"
        assert "literal tool-call markup" in saved["validation_error"]
    await call(kernel, context, **start())
    assert len(requests) == 1


def test_markup_mentioned_as_data_is_not_a_tool_call():
    assert not literal_tool_call_envelope("The article shows `<tool_call>` as an example.")
    assert not literal_tool_call_envelope("The article mentions <tool_call>")
    assert not literal_tool_call_envelope(
        'The page printed "<tool_call><function=web_search><parameter=query>x</parameter></function></tool_call>".'
    )
    assert not literal_tool_call_envelope("Quoted source:\n> <tool_call><function=web_search>")
    assert not literal_tool_call_envelope("~~~xml\n<tool_call><function=web_search>\n~~~")
    assert not literal_tool_call_envelope("<tool_call>\n```xml\nexample\n```\n<function=web_search>")
    assert not literal_tool_call_envelope('Example:\n```json\n{"tool":"council_speak","arguments":{}}\n```')
    assert not literal_tool_call_envelope(
        '{"findings":[{"tool":"web_search","result":"Official pricing was found."}]}'
    )
    assert not literal_tool_call_envelope('{"name":{"source":"web_search"},"arguments":{}}')
