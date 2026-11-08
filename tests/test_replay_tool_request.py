"""Offline evaluator checks: never call real providers or dispatch returned tools."""

import argparse
import copy
import itertools
import json
from pathlib import Path

import httpx
from jsonschema import Draft202012Validator
import pytest

from hortator.tool_feedback import with_usage
from scripts import replay_tool_request as replay


@pytest.fixture
def source():
    parameters = {
        "type": "object",
        "properties": {
            "operation": {"enum": ["run", "status"]},
            "command": {"type": "string", "minLength": 1},
            "job_id": {"type": "string"},
        },
        "required": ["operation"],
        "additionalProperties": False,
        "allOf": [{"if": {"properties": {"operation": {"const": "run"}}}, "then": {"required": ["command"]}}],
    }
    return {
        "model": replay.MODEL,
        "messages": [{"role": "user", "content": "Inspect files"}],
        "stream": True,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "shell",
                    "description": "Run Bash",
                    "parameters": with_usage(parameters),
                },
            }
        ],
    }


def call(arguments, name="shell"):
    return {"id": "call_1", "type": "function", "function": {"name": name, "arguments": arguments}}


def test_categories_do_not_reward_help_or_no_action(source):
    calls = replay.classify(
        [
            call("{}"),
            call('{"command":"pwd"}'),
            call('{"operation":"run","command":"pwd"}'),
            call('{"operation":"bad"}'),
            call('{"operation":"run","operation":"run"}'),
            call("{}", "unknown"),
        ],
        source["tools"],
    )
    assert [c["category"] for c in calls] == [
        "help",
        "missing_operation",
        "valid_action",
        "other_invalid",
        "other_invalid",
        "other_invalid",
    ]
    assert calls[0]["schema_valid"]
    assert replay.outcomes(calls)["mixed_categories"]
    assert not replay.outcomes(calls)["all_valid_actions"]
    assert replay.outcomes(calls[:1])["help_only"]
    assert not replay.outcomes(calls[:1])["any_valid_action"]
    assert replay.outcomes([])["no_tool_calls"]
    assert not replay.outcomes([])["all_valid_actions"]


def test_end_cycle_empty_object_is_an_action():
    tool = {"function": {"name": "end_cycle", "parameters": {"type": "object"}}}
    assert replay.classify([call("{}", "end_cycle")], [tool])[0]["category"] == "valid_action"


def test_variants_preserve_inputs_and_acceptance(source):
    original = copy.deepcopy(source)
    old = Draft202012Validator(source["tools"][0]["function"]["parameters"])
    values = [None, False, 1, "run", "status", "bad", "", "pwd", []]
    cases = [None, [], 1, "run", {}]
    for size in (1, 2, 3):
        for fields in itertools.combinations(("operation", "command", "job_id", "unknown"), size):
            cases.extend(dict(zip(fields, vals)) for vals in itertools.product(values, repeat=size))
    for variant in replay.VARIANTS:
        body = replay.variant_body(source, variant)
        new = Draft202012Validator(body["tools"][0]["function"]["parameters"])
        # This diagnostic intentionally stops advertising help as schema-valid.
        assert all(
            old.is_valid(value) == new.is_valid(value)
            for value in cases
            if variant != "root_required" or value != {}
        )
        assert new.is_valid({}) == (variant != "root_required")
        assert replay.classify([call("{}")], body["tools"])[0]["category"] == "help"
        assert not new.is_valid({"command": "pwd"})
        if variant == "instruction":
            assert body["messages"][:-1] == source["messages"]
            assert body["tools"] == source["tools"]
        else:
            assert body["messages"] == source["messages"]
    assert source == original
    assert replay.variant_body(source, "baseline") == source
    expected = copy.deepcopy(source)
    expected["tools"][0]["function"]["description"] = replay.SHELL_PREFIX + "Run Bash"
    assert replay.variant_body(source, "description") == expected
    placeholder = replay.classify([call('{"operation":"run","command":"YOUR_COMMAND"}')], source["tools"])[0]
    assert placeholder["category"] == "valid_action"
    assert placeholder["copied_command_placeholder"]


def test_schedule_balances_all_three_positions():
    rows = replay.schedule(["baseline", "description", "required"], 6)
    assert len(rows) == 18
    assert {r["block"] for r in rows} == set(range(1, 7))
    for offset in range(3):
        assert (
            sorted(r["variant"] for r in rows[offset::3])
            == ["baseline"] * 2 + ["description"] * 2 + ["required"] * 2
        )


def test_four_arm_schedule_balances_without_factorial_blocks():
    variants = ["baseline", "op_field", "root_required", "instruction"]
    rows = replay.schedule(variants, 8)
    for offset in range(4):
        assert sorted(r["variant"] for r in rows[offset::4]) == sorted(variants * 2)


@pytest.mark.parametrize(
    "finish,expected", [("tool_calls", "complete"), ("length", "incomplete"), (None, "incomplete")]
)
async def test_replay_records_wire_without_dispatch(tmp_path, monkeypatch, source, finish, expected):
    key = "fake-test-key"
    monkeypatch.setattr(
        replay, "load_source", lambda *_: (source, {"base_url": "https://provider.invalid"}, key, {})
    )
    packet = {
        "choices": [
            {
                "index": 0,
                "delta": {"tool_calls": [{"index": 0, **call('{"command":"pwd"}')}]},
                "finish_reason": finish,
            }
        ]
    }
    wire = "data: " + json.dumps(packet) + "\n\ndata: [DONE]\n\n"
    requests = []

    def respond(request):
        requests.append(request)
        assert json.loads(request.content) == source
        assert request.headers["Authorization"] == "Bearer " + key
        return httpx.Response(200, content=wire, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(replay.httpx, "AsyncClient", lambda **_: client)
    output = tmp_path / "evidence"
    args = argparse.Namespace(
        data_dir=Path("unused"),
        request_id="fixture",
        variant=["baseline"],
        repeats=1,
        timeout=1,
        output=output,
    )
    await replay.replay(args)
    assert len(requests) == 1
    summary = json.loads((output / "summary.json").read_text())[0]
    assert summary["outcome"] == expected
    assert summary["call_counts"]["missing_operation"] == 1
    assert json.loads((output / "wire-01.json").read_text())["consumed_utf8"] == wire
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["baseline_json_equal"]
    assert manifest["payload_sha256"]["baseline"] == manifest["source_payload_sha256"]
    assert all(key not in f.read_text() for f in output.rglob("*.json"))


@pytest.mark.parametrize(
    "status,wire,expected",
    [
        (503, "unavailable", "transport_error"),
        (200, "data: {broken}\n\n", "format_error"),
        (200, 'data: {"error":{"message":"interrupted","type":"server_error"}}\n\n', "provider_error"),
    ],
)
async def test_failed_sample_is_recorded_once(tmp_path, monkeypatch, source, status, wire, expected):
    monkeypatch.setattr(
        replay, "load_source", lambda *_: (source, {"base_url": "https://provider.invalid"}, "", {})
    )
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, content=wire, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(replay.httpx, "AsyncClient", lambda **_: client)
    output = tmp_path / "evidence"
    args = argparse.Namespace(
        data_dir=tmp_path, request_id="fixture", variant=["baseline"], repeats=1, timeout=1, output=output
    )
    await replay.replay(args)
    assert len(calls) == 1
    summary = json.loads((output / "summary.json").read_text())[0]
    assert summary["outcome"] == expected
    assert "call_counts" not in summary
