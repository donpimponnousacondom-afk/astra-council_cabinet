import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import configured
from hortator.app import create_app
from hortator.diagnostics import read_diagnostics
from hortator.provider import ProviderError
from test_console import output, read_all_evidence
from test_provider import Fragments, call_args, install_client
from test_runtime import completion, settle
from test_runtime_feedback import ready, reply, tool


@pytest.mark.parametrize("streamed", [True, False])
async def test_success_retains_reasoning_alias_details_inline_and_masks_credentials(kernel, streamed):
    bot = configured(kernel)
    secret = "provider-diagnostic-credential-fixture"
    kernel.vault.put("provider/openrouter/api_key", secret)
    message = {
        "content": "<think>Inline diagnostic</think>Visible answer",
        "reasoning": "Native diagnostic " + secret,
        "reasoning_details": [{"type": "reasoning.text", "text": "Native detail", "api_key": secret}],
    }

    def handler(request):
        if not streamed:
            return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}]})
        packet = {"choices": [{"delta": message, "finish_reason": "stop"}]}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=Fragments(("data: " + json.dumps(packet) + "\n\ndata: [DONE]\n\n").encode()),
        )

    await install_client(kernel, handler)
    result = await kernel.pool.complete(**call_args(kernel, bot))
    diagnostics = read_diagnostics(kernel.store, result.request_id)
    assert result.content == "Visible answer"
    assert diagnostics["reasoning_content"] == "Native diagnostic [REDACTED]"
    assert diagnostics["reasoning_details"][0]["text"] == "Native detail"
    assert diagnostics["inline_reasoning"] == [{"tag": "think", "text": "Inline diagnostic"}]
    assert secret not in json.dumps(diagnostics)
    assert diagnostics["capture"] == "recorded" and diagnostics["status"] == "completed"
    limited = read_diagnostics(kernel.store, result.request_id, limit=16)
    assert limited["capture"] == "console_limit" and "Native diagnostic" not in str(limited)
    assert "Native diagnostic" not in str(kernel.store.rows("SELECT * FROM requests"))
    assert "Native diagnostic" not in str(kernel.store.events())


@pytest.mark.parametrize(
    "code,fault",
    [
        ("bad_request", False),
        ("invalid_request_error", False),
        ("context_length_exceeded", False),
        ("rate_limit_error", True),
        ("server_error", True),
    ],
)
@pytest.mark.parametrize("streamed", [True, False])
async def test_error_envelopes_preserve_usage_and_only_provider_faults_affect_shared_health(
    kernel, code, fault, streamed
):
    bot = configured(kernel)
    packet = {
        "error": {"code": code, "message": "Diagnostic fixture"},
        "usage": {"prompt_tokens": 20, "completion_tokens": 5},
    }

    def handler(request):
        if not streamed:
            return httpx.Response(200, json=packet)
        partial = {
            "choices": [
                {"delta": {"reasoning_content": "Partial reasoning", "content": "Incomplete visible"}}
            ]
        }
        raw = "".join("data: " + json.dumps(p) + "\n\n" for p in (partial, packet))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Fragments(raw.encode())
        )

    await install_client(kernel, handler)
    with pytest.raises(ProviderError) as raised:
        await kernel.pool.complete(**call_args(kernel, bot))
    assert raised.value.provider_fault is fault
    request = kernel.store.one("SELECT * FROM requests")
    assert request["status"] == "failed" and request["http_status"] == 200
    assert request["input_tokens"] == 20 and request["output_tokens"] == 5
    diagnostics = read_diagnostics(kernel.store, request["id"])
    assert diagnostics["provider_error"]["envelope"]["code"] == code
    assert diagnostics["provider_error"]["provider_fault"] is fault
    assert diagnostics["reasoning_content"] == ("Partial reasoning" if streamed else "")
    assert kernel.store.health("openrouter")["consecutive_failures"] == int(fault)


async def test_cancelled_stream_retains_partial_reasoning(kernel):
    bot = configured(kernel)
    blocked = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"<analysis>unfinished inline", "reasoning_content":"unfinished native"}}]}\n\n'
            blocked.set()
            await asyncio.Event().wait()

    await install_client(
        kernel, lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())
    )
    task = asyncio.create_task(kernel.pool.complete(**call_args(kernel, bot)))
    await blocked.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    request = kernel.store.one("SELECT * FROM requests")
    diagnostics = read_diagnostics(kernel.store, request["id"])
    assert diagnostics["status"] == "cancelled"
    assert diagnostics["reasoning_content"] == "unfinished native"
    assert diagnostics["inline_reasoning"][0]["text"] == "unfinished inline"
    assert "unfinished" not in request["response"]


def test_restart_marks_checkpointed_diagnostics_interrupted_without_losing_evidence(kernel):
    for request_id, status, ended in (
        ("interrupted-fixture", "running", None),
        ("finished-fixture", "completed", 2),
    ):
        kernel.store.execute(
            "INSERT INTO requests(id,turn_id,bot_id,provider_id,profile_id,model,purpose,started_at,ended_at,status,body,context) "
            "VALUES(?,'turn-fixture','ada','openrouter','balanced','fixture','generation',1,?,?, '{}','{}')",
            (request_id, ended, status),
        )
        kernel.store.execute(
            "INSERT INTO request_diagnostics VALUES(?,?,1)",
            (
                request_id,
                json.dumps(
                    {"capture": "recorded", "status": status, "reasoning_content": "Last stored packet"}
                ),
            ),
        )
    kernel.store.recover()
    assert read_diagnostics(kernel.store, "interrupted-fixture")["status"] == "interrupted"
    assert read_diagnostics(kernel.store, "interrupted-fixture")["reasoning_content"] == "Last stored packet"
    assert read_diagnostics(kernel.store, "finished-fixture")["status"] == "completed"


async def test_replay_is_private_and_console_P_can_page_reasoning_without_secret_leak(kernel):
    ready(kernel, enabled_plugins=["memory"])
    calls = 0
    secret = "private-diagnostic-console-secret"
    kernel.vault.put("provider/openrouter/api_key", secret)
    reasoning = "Diagnostic reasoning " + "trace " * 1400 + " REASONING-END " + secret + "\x1b[2J"

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            response = reply(tool("memory", {"operation": "read"}))
            data = response.json()
            data["choices"][0]["message"]["reasoning_content"] = reasoning
            return httpx.Response(200, json=data)
        assert json.loads(request.content)["messages"][-3]["reasoning_content"] == reasoning
        return completion("Final answer")

    await install_client(kernel, handler)
    await kernel.engine.tick()
    await settle(kernel)
    turn = kernel.store.one("SELECT * FROM turns")
    assert turn["status"] == "sent", turn["error"]
    public = kernel.service.turn(turn["id"])
    assert "REASONING-END" not in str(public)
    private = kernel.service.turn(turn["id"], include_diagnostics=True)
    assert "REASONING-END" in str(private)
    assert secret not in str(private)
    assert private["requests"][1]["diagnostics"]["request_reasoning"][0]["message_index"] >= 1
    with output() as console:
        console.bind(kernel)
        console.key("P")
        assert "REASONING-END" not in console.stream.getvalue()
        console.key("P")
        read_all_evidence(console)
        text = console.stream.getvalue()
        assert "Private provider reasoning" in text and "REASONING-END" in text
        assert secret not in text and "\x1b" not in text and "\\u001b[2J" in text


def test_trajectory_diagnostics_and_export_require_dashboard_authentication(tmp_path):
    app = create_app(tmp_path, start_runtime=False)
    with TestClient(app) as client:
        assert client.get("/api/trajectory/turn-fixture").status_code == 401
        assert client.get("/api/trajectory/turn-fixture/export").status_code == 401
        k = app.state.kernel

        def seed():
            k.store.execute(
                "INSERT INTO turns(id,bot_id,channel_id,profile_id,provider_id,model,started_at,status,trigger,revision) VALUES('turn-fixture','ada','channel','balanced','openrouter','fixture',1,'sent','manual',1)"
            )
            k.store.execute(
                "INSERT INTO requests(id,turn_id,bot_id,provider_id,profile_id,model,purpose,started_at,status,body,context) VALUES('req-fixture','turn-fixture','ada','openrouter','balanced','fixture','generation',1,'completed','{}','{}')"
            )
            k.store.execute(
                "INSERT INTO request_diagnostics VALUES('req-fixture',?,1)",
                (json.dumps({"capture": "recorded", "reasoning_content": "Operator diagnostic fixture"}),),
            )

        client.portal.call(seed)
        password = (tmp_path / "initial-password").read_text().strip()
        assert client.post("/api/auth/login", json={"password": password}).status_code == 200
        for path in ("/api/trajectory/turn-fixture", "/api/trajectory/turn-fixture/export"):
            assert (
                client.get(path).json()["requests"][0]["diagnostics"]["reasoning_content"]
                == "Operator diagnostic fixture"
            )
        assert "Operator diagnostic fixture" not in str(
            client.portal.call(k.service.inspect, "turn", "turn-fixture")
        )
        assert client.portal.call(read_diagnostics, k.store, "missing")["capture"] == "unavailable"
