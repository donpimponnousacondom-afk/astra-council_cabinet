"""Offline evaluator checks: never call real providers or dispatch returned tools."""

import argparse
import copy
import itertools
import json
from pathlib import Path
import sqlite3

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
    real_example = replay.classify([call('{"operation":"run","command":"pwd"}')], source["tools"])[0]
    assert real_example["category"] == "valid_action"
    assert real_example["example_command_match"]  # Human review flag, not rejection.


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
        (503, "unavailable", "http_error"),
        (0, "", "transport_error"),
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
        if status == 0:
            raise httpx.ConnectError("fixture connection failure", request=request)
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


def test_aggregate_keeps_error_incomplete_help_and_no_tool_denominators_separate(source):
    def row(outcome, calls):
        return {"variant": "baseline", "outcome": outcome, "calls": replay.classify(calls, source["tools"])}

    rows = [
        row("complete", [call('{"operation":"run","command":"pwd"}'), call('{"command":"pwd"}')]),
        row("incomplete", [call('{"command":"pwd"}')]),
        {"variant": "baseline", "outcome": "provider_error"},
        row("complete", [call("{}")]),
        row("complete", []),
    ]
    before = copy.deepcopy(rows)
    result = replay.aggregate(rows)["baseline"]
    assert result["attempted"] == 5
    assert result["complete"] == 3
    assert result["omission_completions"] == result["valid_shell_action_completions"] == 1
    assert result["shell_attempt_completions"] == result["nonempty_shell_objects"] == 2
    assert result["help_only"] == result["no_tools"] == 1
    assert result["calls_by_tool"]["shell"] == {"valid_action": 1, "missing_operation": 1, "help": 1}
    assert rows == before


def test_optional_operation_is_not_an_omission():
    tools = [
        {
            "function": {
                "name": "web_fetch",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}, "operation": {"type": "string"}},
                    "required": ["url"],
                },
            }
        }
    ]
    result = replay.classify([call('{"url":"https://example.com"}', "web_fetch")], tools)
    assert result[0]["category"] == "valid_action"


async def test_evaluator_failure_aborts_without_further_paid_calls(tmp_path, monkeypatch, source):
    monkeypatch.setattr(
        replay, "load_source", lambda *_: (source, {"base_url": "https://provider.invalid"}, "", {})
    )

    def broken_feedback(*_):
        raise ValueError("fixture evaluator defect")

    monkeypatch.setattr(replay, "feedback", broken_feedback)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "tool_calls": [call('{"command":"pwd"}')]},
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(replay.httpx, "AsyncClient", lambda **_: client)
    output = tmp_path / "evidence"
    args = argparse.Namespace(
        data_dir=tmp_path, request_id="fixture", variant=["baseline"], repeats=3, timeout=1, output=output
    )
    with pytest.raises(ValueError, match="evaluator defect"):
        await replay.replay(args)
    assert len(calls) == 1
    summary = json.loads((output / "summary.json").read_text())[0]
    assert summary["outcome"] == "evaluator_error"
    assert "calls" not in summary
    aggregate = json.loads((output / "aggregate.json").read_text())
    assert aggregate["arms"]["baseline"]["complete"] == 0
    assert aggregate["arms"]["baseline"]["attempted"] == 1


@pytest.mark.parametrize("bot", ["sun", "loki"])
def test_source_closes_connection_and_accepts_null_context(tmp_path, monkeypatch, source, bot):
    db = sqlite3.connect(tmp_path / "council.sqlite3")
    db.executescript(
        "CREATE TABLE requests(id,body,bot_id,provider_id,model,turn_id,started_at,context);"
        "CREATE TABLE entities(kind,id,body,revision); CREATE TABLE secrets(scope,value);"
    )
    db.execute(
        "INSERT INTO requests VALUES(?,?,?,?,?,?,?,?)",
        ("fixture", json.dumps(source), bot, "provider", replay.MODEL, "turn", 0, None),
    )
    db.execute(
        "INSERT INTO entities VALUES(?,?,?,?)",
        ("providers", "provider", json.dumps({"requires_key": False}), 17),
    )
    db.commit()
    db.close()
    closed = []
    original_connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        def close(self):
            closed.append(True)
            super().close()

    monkeypatch.setattr(
        replay.sqlite3,
        "connect",
        lambda *args, **kwargs: original_connect(*args, **kwargs, factory=TrackedConnection),
    )
    if bot == "sun":
        body, _, key, meta = replay.load_source(tmp_path, "fixture")
        assert body == source and key == ""
        assert meta["current_provider_revision"] == 17
        assert all(v is None for v in meta["historical_context"].values())
    else:
        with pytest.raises(ValueError, match="Sun/Zelda"):
            replay.load_source(tmp_path, "fixture")
    assert closed == [True]
