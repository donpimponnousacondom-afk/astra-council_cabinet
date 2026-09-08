import json

import httpx
import pytest

from test_provider import install_client
from test_runtime import completion, settle
from test_runtime_feedback import ready, reply, responses, tool


@pytest.mark.parametrize(
    "content",
    [
        "A normal **Discord** answer.",
        [{"type": "text", "text": "First "}, {"type": "text", "text": "second"}],
    ],
)
async def test_plain_content_is_the_final_answer_without_a_reply_tool(kernel, content):
    _, transport = ready(kernel, enabled_plugins=[])
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert {t["function"]["name"] for t in body["tools"]} == {"council_silence"}
        assert (
            "write your contribution directly as ordinary assistant content" in body["messages"][0]["content"]
        )
        return completion(content)

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(requests) == 1
    transport.send.assert_awaited_once()
    assert transport.send.await_args.args[2] == (content if isinstance(content, str) else "First second")


@pytest.mark.parametrize(
    "wrapper",
    [
        '{"tool":"council_speak","arguments":{"content":"bad "quotes""}}\n</parameter>\n</function>',
        '{"name": "council_speak", "arguments": {"content": "hello"}}',
        "<function=council_speak><parameter=content>Hello</parameter></function>",
        '<tool_call>{"name":"council_speak"}</tool_call>',
    ],
)
async def test_legacy_text_wrappers_are_withheld_and_repaired_once(kernel, wrapper):
    _, transport = ready(kernel)
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return completion(wrapper)
        assert any("previous answer was withheld" in (m.get("content") or "") for m in body["messages"])
        assert wrapper not in str(body["messages"])
        return completion("Actual answer")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(requests) == 2
    transport.send.assert_awaited_once()
    assert transport.send.await_args.args[2] == "Actual answer"
    assert (
        wrapper
        == json.loads(kernel.store.one("SELECT response FROM requests ORDER BY started_at")["response"])[
            "content"
        ]
    )
    assert kernel.store.one("SELECT count(*) AS n FROM events WHERE kind='request.reply_rejected'")["n"] == 1


async def test_repeat_wrapper_cannot_extend_budget_or_publish(kernel):
    _, transport = ready(kernel, max_tool_rounds=8)
    await install_client(kernel, lambda r: completion('{"tool":"council_speak","arguments":{}}'))
    await kernel.engine.tick()
    await settle(kernel)
    assert kernel.store.one("SELECT count(*) AS n FROM requests")["n"] == 2
    assert kernel.store.one("SELECT status FROM turns")["status"] == "failed"
    transport.send.assert_not_awaited()


@pytest.mark.parametrize(
    "content",
    [
        'Example:\n```json\n{"tool":"council_speak","arguments":{}}\n```',
        '{"status":"ready","files":["index.html"]}',
        "The old council_speak tool was removed.",
        "```xml\n<function>example</function>\n```",
    ],
)
async def test_real_code_examples_and_json_answers_remain_valid(kernel, content):
    _, transport = ready(kernel)
    await install_client(kernel, lambda r: completion(content))
    await kernel.engine.tick()
    await settle(kernel)
    transport.send.assert_awaited_once()
    assert transport.send.await_args.args[2] == content


async def test_obsolete_native_speak_is_rejected_and_normal_content_can_follow(kernel):
    _, transport = ready(kernel)
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply(tool("council_speak", {"content": "Never dispatch this"}))
        report = responses(body)[-1]
        assert report["executed"] is False and "does not exist" in report["error"]
        return completion("Correct final answer")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(requests) == 2
    transport.send.assert_awaited_once()
    assert transport.send.await_args.args[2] == "Correct final answer"


@pytest.mark.parametrize("reason", ["length", "content_filter", "stop"])
async def test_incomplete_or_reasoning_only_output_is_diagnostic_not_a_silence_decision(kernel, reason):
    _, transport = ready(kernel)

    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "reasoning_content": "Provider diagnostic fixture",
                            "content": "Partial" if reason != "stop" else None,
                        },
                        "finish_reason": reason,
                    }
                ],
                "usage": {"completion_tokens": 2002},
            },
        )

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await settle(kernel)
    turn = kernel.store.one("SELECT * FROM turns")
    assert turn["status"] == "failed"
    if reason == "length":
        assert "max_tokens=2048" in turn["error"] and "output_tokens=2002" in turn["error"]
    if reason == "stop":
        assert "no visible answer" in turn["error"]
    assert "Provider diagnostic fixture" in str(kernel.service.turn(turn["id"], include_diagnostics=True))
    assert "Provider diagnostic fixture" not in str(kernel.service.inspect("turn", turn["id"]))
    assert kernel.store.health("openrouter")["consecutive_failures"] == 0
    transport.send.assert_not_awaited()
