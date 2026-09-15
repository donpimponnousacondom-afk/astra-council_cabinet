import copy
import json
import asyncio

import httpx
import pytest

import featherless_tester as probe
import featherless_tools as lab
from test_featherless_tester import model, options


def call(name, arguments, call_id="call1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


@pytest.mark.parametrize("name", lab.MEMORY_NAMES)
def test_memory_aliases_crud_replacement_usage_and_full_validation(tmp_path, name):
    evidence = probe.Evidence(tmp_path / name)
    sim = lab.SimulatedTools([name], lab.BASE_MEMORY, {}, evidence, "test")
    assert sim.execute(call(name, {}))["usage_only"]
    assert sim.memory == lab.BASE_MEMORY
    bad = sim.execute(call(name, {"operation": "write", "key": 4}))
    assert not bad["executed"] and bad["error_count"] >= 2 and "usage" in bad
    assert sim.execute(call(name, {"operation": "read"}))["notes"]
    for value in ("blue", "green"):
        assert sim.execute(call(name, {"operation": "write", "key": "test_note", "value": value}))["ok"]
    assert sim.execute(call(name, {"operation": "delete", "key": "obsolete"}))["ok"]
    assert sim.execute(call(name, {"operation": "read"}))["notes"]
    assert all(lab.grade(name, sim.calls, "green", sim.memory, sim.initial).values())
    assert json.loads((evidence.root / "memory-test.json").read_text()) == {
        "project": "Orion",
        "test_note": "green",
    }
    assert lab.BASE_MEMORY == {"project": "Orion", "obsolete": "old fixture"}


def test_unknown_tools_and_malformed_json_never_execute(tmp_path):
    sim = lab.SimulatedTools(["memory"], lab.BASE_MEMORY, {}, probe.Evidence(tmp_path / "e"), "c")
    assert not sim.execute(call("send_email", {}))["executed"]
    item = call("memory", {})
    item["function"]["arguments"] = '{"operation":"write","operation":"delete"}'
    assert "Duplicate" in sim.execute(item)["error"]
    assert sim.memory == lab.BASE_MEMORY


def test_web_and_email_are_local_fixtures_with_selectable_empty_results(tmp_path):
    sim = lab.SimulatedTools(
        ["web_fetch", "web_search", "send_email"],
        {},
        {"web_search": {"results": []}},
        probe.Evidence(tmp_path / "e"),
        "c",
    )
    assert sim.execute(call("web_fetch", {"url": "http://127.0.0.1/private"}))["simulated"]
    assert sim.execute(call("web_search", {"query": "anything"}))["results"] == []
    assert sim.execute(call("send_email", {"to": "nobody@example.test", "subject": "x", "body": "x"}))[
        "simulated"
    ]
    assert all(c["result"]["simulated"] for c in sim.calls)


def test_alias_schema_only_name_changes_and_simple_still_requires_correct_operation(tmp_path):
    one, two = lab.spec("notes"), lab.spec("annotations")
    two["function"]["name"] = "notes"
    assert one == two
    assert "allOf" not in lab.spec("memory", simple=True)["function"]["parameters"]
    sim = lab.SimulatedTools(["memory"], {}, {}, probe.Evidence(tmp_path / "e"), "c", simple=True)
    result = sim.execute(call("memory", {"operation": "write"}))
    assert result["error_count"] == 2 and not result["executed"]
    wrapped = lab.SimulatedTools(
        ["memory"], {}, {}, probe.Evidence(tmp_path / "wrapper"), "c", usage_wrapper=True
    )
    assert "anyOf" in wrapped.specs[0]["function"]["parameters"]
    assert wrapped.execute(call("memory", {}))["usage_only"]


@pytest.mark.parametrize("layout", ["merged", "separate"])
async def test_tool_loop_native_ids_reasoning_replay_and_per_case_baseline(tmp_path, layout):
    evidence = probe.Evidence(tmp_path / "e")
    opts = options(
        tools="memory", tool_suite="baseline", tool_guidance="simple", mode="json", tool_system_layout=layout
    )
    inputs = []
    actions = [
        {"operation": "read"},
        {"operation": "write", "key": "test_note", "value": "blue"},
        {"operation": "write", "key": "test_note", "value": "green"},
        {"operation": "delete", "key": "obsolete"},
        {"operation": "read"},
    ]

    def handle(request):
        body = json.loads(request.content)
        inputs.append(copy.deepcopy(body))
        index = len(inputs) - 1
        if index:
            assert body["messages"][-1]["role"] == "tool"
            assert body["messages"][-1]["tool_call_id"] == f"c{index - 1}"
            assert body["messages"][-2]["reasoning_content"] == "Think about the next operation."
        message = (
            {
                "content": None,
                "reasoning_content": "Think about the next operation.",
                "tool_calls": [call("memory", actions[index], f"c{index}")],
            }
            if index < 5
            else {"content": "green"}
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": message, "finish_reason": "tool_calls" if index < 5 else "stop"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        tester = probe.Tester(client, evidence, opts)
        result = await lab.run_case(
            tester,
            model(),
            {"max_tokens": 384},
            stream=False,
            name="memory",
            enabled=["memory"],
            prompt="Do the memory tasks",
        )
    assert result["success"] and len(result["requests"]) == 6
    assert all(result["checks"].values())
    assert result["memory"] == {"project": "Orion", "test_note": "green"}
    assert result["requests"][0].endswith(".json")
    assert inputs[0]["tool_choice"] == "auto"
    system = [m for m in inputs[0]["messages"] if m["role"] == "system"]
    assert len(system) == (1 if layout == "merged" else 3)
    assert lab.SIMPLE_TOOL_GUIDANCE in "\n\n".join(m["content"] for m in system)
    assert "Current private notes" in system[-1]["content"]
    assert all(r["reasoning_count_source"] in ("estimated", "unavailable") for r in result["metrics"])


async def test_text_shaped_tool_is_failure_and_no_fake_action(tmp_path):
    evidence = probe.Evidence(tmp_path / "e")
    opts = options(tools="memory", tool_guidance="simple")

    def response(request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "call:memory{operation:``}"}, "finish_reason": "stop"}]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        result = await lab.run_case(
            probe.Tester(client, evidence, opts),
            model(),
            {},
            stream=False,
            name="memory",
            enabled=["memory"],
            prompt="Write a note",
        )
    assert result["failure"] == "text_instead_of_tool" and not result["success"]
    assert result["memory"] == lab.BASE_MEMORY and not result["calls"]


def test_report_escapes_model_output_and_excludes_private_reasoning(tmp_path):
    from featherless_report import build_report

    result = {
        "case_id": "x",
        "model": "fixture/model",
        "case": "memory",
        "mode": "sse",
        "guidance": "runtime",
        "schema": "runtime",
        "success": False,
        "failure": "text_instead_of_tool",
        "checks": {},
        "argument_errors": 0,
        "usage_calls": 0,
        "calls": [],
        "final": "<img src=x onerror=alert(1)>\ud83d",
        "metrics": [],
        "requests": [],
    }
    (tmp_path / "case-result-x.json").write_text(json.dumps(result))
    (tmp_path / "request-private.json").write_text(
        json.dumps({"reasoning_content": "NEVER_EMBED_THIS_REASONING"})
    )
    report = build_report(tmp_path).read_text()
    assert "<img src=x" not in report and "&lt;img src=x" in report
    assert r"\ud83d" in report
    assert "NEVER_EMBED_THIS_REASONING" not in report
    assert "text_instead_of_tool" in report


async def test_missing_sse_finish_is_format_failure_with_partial_evidence(tmp_path):
    payload = 'data: {"choices":[{"delta":{"content":"green"}}]}\n\ndata: [DONE]\n\n'
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text=payload, headers={"content-type": "text/event-stream"})
        )
    ) as client:
        tester = probe.Tester(client, probe.Evidence(tmp_path / "e"), options())
        row = await tester.request(model(), [{"role": "user", "content": "test"}], {}, stream=True)
    assert row["status"] == "failed" and row["error_origin"] == "response_format"
    assert row["content"] == "green" and row["finish_reason"] is None


@pytest.mark.parametrize(
    "status,code,delay", [(429, "model_switching_limit_exceeded", 65), (524, "edge_timeout", 1)]
)
async def test_transient_retry_reuses_context_without_replaying_executed_tool(
    tmp_path, monkeypatch, status, code, delay
):
    waits, inputs = [], []

    async def sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(asyncio, "sleep", sleep)

    def handle(request):
        body = json.loads(request.content)
        inputs.append(body)
        if len(inputs) == 1:
            message = {
                "content": None,
                "tool_calls": [call("web_fetch", {"url": "https://example.test/orion"})],
            }
            return httpx.Response(
                200, json={"choices": [{"message": message, "finish_reason": "tool_calls"}]}
            )
        if len(inputs) == 2:
            return httpx.Response(status, json={"error": {"code": code, "message": "wait"}})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "AMBER-42"}, "finish_reason": "stop"}]}
        )

    opts = options(tools="web_fetch", retries=1, retry_delay=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await lab.run_case(
            probe.Tester(client, probe.Evidence(tmp_path / "e"), opts),
            model(),
            {},
            stream=False,
            name="web_fetch",
            enabled=["web_fetch"],
            prompt="Fetch it",
        )
    assert result["success"] and len(result["calls"]) == 1
    assert inputs[1] == inputs[2] and waits == [delay]


def test_report_discloses_excluded_cases_without_counting_them(tmp_path):
    from featherless_report import build_report

    case = {
        "case_id": "e",
        "model": "m",
        "case": "memory",
        "mode": "sse",
        "guidance": "runtime",
        "schema": "runtime",
        "success": False,
        "failure": "transport",
        "checks": {},
        "argument_errors": 0,
        "usage_calls": 0,
        "calls": [],
        "final": "",
        "metrics": [],
        "requests": [],
    }
    (tmp_path / "case-result-e.json").write_text(json.dumps(case))
    (tmp_path / "exclude-from-comparison.json").write_text(json.dumps({"reason": "Invalid test schedule"}))
    report = build_report(tmp_path).read_text()
    assert "EXCLUDED: Invalid test schedule" in report
    assert "Disclosed exclusions" in report and ">0/1</td>" not in report


def test_report_counts_tokens_from_failed_attempts(tmp_path):
    from featherless_report import build_report

    case = {
        "case_id": "f",
        "model": "m",
        "case": "memory",
        "mode": "sse",
        "guidance": "runtime",
        "schema": "runtime",
        "success": False,
        "failure": "response_format",
        "checks": {},
        "argument_errors": 0,
        "usage_calls": 0,
        "calls": [],
        "final": "",
        "requests": [],
        "metrics": [
            {
                "status": "failed",
                "reasoning_tokens": 13,
                "output_tokens": 17,
                "visible_tokens_estimate": 4,
                "stream_tps": 999,
            }
        ],
    }
    (tmp_path / "case-result-f.json").write_text(json.dumps(case))
    report = build_report(tmp_path).read_text()
    assert "<td>13</td><td>17</td><td>4</td>" in report
    assert "13 (1/1 requests measured)" in report
    assert "<td>999.0</td>" not in report  # No complete timing sample.


def test_report_matched_comparison_keeps_tuning_failures_visible(tmp_path):
    from featherless_report import build_report

    case = {
        "case_id": "x",
        "model": "m",
        "case": "memory",
        "mode": "sse",
        "guidance": "runtime",
        "schema": "runtime",
        "success": True,
        "failure": None,
        "checks": {"state": True},
        "argument_errors": 0,
        "usage_calls": 0,
        "calls": [],
        "final": "green",
        "metrics": [],
        "requests": [],
    }
    for directory, successful in (("baseline", True), ("tuning", False)):
        target = tmp_path / directory
        target.mkdir()
        (target / "case-result-x.json").write_text(
            json.dumps(case | {"success": successful, "failure": None if successful else "output_limit"})
        )
    (tmp_path / "comparison.json").write_text(
        json.dumps({"runs": ["baseline"], "description": "Matched baseline only"})
    )
    report = build_report(tmp_path).read_text()
    matrix = report.split('id="matrix"', 1)[1].split("</table>", 1)[0]
    assert ">1/1</td>" in matrix and ">1/2</td>" not in matrix
    assert "Matched baseline only" in report and "tuning" in report and "output_limit" in report


def test_report_standalone_control_excludes_reasoning_and_raw_continuation(tmp_path):
    from featherless_report import build_report

    (tmp_path / "request-control.json").write_text(
        json.dumps(
            {
                "model": "reference/model",
                "status": "failed",
                "mode": "json",
                "purpose": "benchmark",
                "finish_reason": None,
                "content": "OK",
                "reasoning_content": "PRIVATE_REASONING",
                "raw_response": "PRIVATE_RAW",
                "request_body": {"messages": [{"reasoning_content": "PRIVATE_CONTINUATION"}]},
                "reasoning_tokens": 42,
            }
        )
    )
    (tmp_path / "findings.md").write_text("## Control\n\n<script>bad()</script>")
    report = build_report(tmp_path).read_text()
    assert "reference/model" in report and "Standalone controls" in report
    assert "PRIVATE_" not in report
    assert "reasoning_tokens&quot;: 42" in report and "&lt;script&gt;" in report
