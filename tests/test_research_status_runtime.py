import json
from unittest.mock import AsyncMock

from hortator.research_assistant import ID
from support.provider import install_client
from support.research_assistant import setup
from support.runtime import completion, settle
from support.runtime_feedback import reply, tool


async def test_research_runtime_status_is_sent_late_and_removed_from_tool_free_round(kernel):
    context = setup(kernel)
    kernel.store.put(
        "bots",
        {
            **context.bot,
            "interval_seconds": 1,
            "max_tool_rounds": 1,
            "allow_silence": False,
        },
    )
    transport = AsyncMock()
    transport.send.return_value = "888888888888888888"
    kernel.engine.transport = transport
    bodies = []

    def facts(body):
        marker = "Runtime facts (trusted): "
        content = next(
            m["content"]
            for m in body["messages"]
            if isinstance(m.get("content"), str) and marker in m["content"]
        )
        return json.JSONDecoder().raw_decode(content.split(marker, 1)[1])[0]

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            runtime = facts(body)
            status = runtime["tool_status"][ID]
            assert "0/3 used" in status and "available slots: 4" in status
            advertised = next(t["function"] for t in body["tools"] if t["function"]["name"] == ID)
            assert "0/3 used" not in advertised["description"]
            assert "available slots: 4" not in advertised["description"]
            assert "tool_status" not in body["messages"][0]["content"]
            return reply(tool(ID, {}))
        assert len(bodies) == 2
        assert not body.get("tools")
        assert "tool_status" not in facts(body)
        assert any(m["role"] == "tool" for m in body["messages"])
        return completion("Ready to research when needed.")

    await install_client(kernel, respond)
    await kernel.engine.tick()
    await settle(kernel)
    assert len(bodies) == 2
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    assert not kernel.store.rows("SELECT * FROM background_jobs")
    transport.send.assert_awaited_once()
