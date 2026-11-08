import json
from collections import Counter

import pytest

from scripts.report_shell_calls import summarize
from hortator.tool_feedback import with_usage


def row(
    arguments='{"command":"true"}', *, guidance=False, cached=None, finish="tool_calls", status="completed"
):
    schema = with_usage(
        {
            "type": "object",
            "required": ["operation"],
            "properties": {"operation": {"type": "string"}, "command": {"type": "string"}},
        }
    )
    return {
        "bot_id": "any-bot",
        "provider_id": "provider",
        "profile_id": "profile",
        "model": "any-model",
        "body": json.dumps(
            {"tools": [{"type": "function", "function": {"name": "shell", "parameters": schema}}]}
        ),
        "context": json.dumps(
            {
                "prompt_layers": [{"id": "shell_call_guidance", "sha256": "frozen-hash", "role": "system"}]
                if guidance
                else []
            }
        ),
        "response": json.dumps(
            {
                "finish_reason": finish,
                "tool_calls": []
                if arguments is None
                else [{"function": {"name": "shell", "arguments": arguments}}],
            }
        ),
        "input_tokens": 100,
        "cached_tokens": cached,
        "ttft_ms": None,
        "status": status,
    }


def test_report_preserves_denominators_unknown_cache_and_prompt_cohorts():
    groups = summarize(
        [
            row(),
            row("{}", cached=0),
            row("bad json", cached=90),
            row(None, cached=99),
            row(finish="length"),
            row(status="failed"),
            row('{"operation":"run","command":"true"}', guidance=True, cached=90),
        ]
    )
    before, after = groups
    assert before["guidance_sha256"] == "absent"
    assert before["attempts"] == 6 and before["complete_responses"] == 4
    assert before["not_complete"] == 2 and before["shell_responses"] == 3
    assert before["omission_responses"] == before["missing_operation_calls"] == 1
    assert before["nonempty_shell_objects"] == 1
    assert before["other_invalid_calls"] == before["help_calls"] == before["no_tool_responses"] == 1
    assert before["cache_reported_attempts"] == before["cache_unknown_attempts"] == 3
    assert before["reported_uncached_tokens"] == 111 and before["attempt_min_cached_share"] == 0
    assert before["attempt_median_cached_share"] == 0.9 and before["attempt_median_ttft_ms"] is None
    assert after["valid_action_calls"] == 1 and after["omission_responses"] == 0
    assert "true" not in json.dumps(groups) and "command" not in json.dumps(groups)


def test_report_excludes_requests_without_shell_and_does_not_turn_unknown_into_miss():
    no_shell = row()
    no_shell["body"] = '{"tools":[]}'
    diagnostics = Counter()
    [report] = summarize([no_shell, row(cached=150)], diagnostics=diagnostics)
    assert report["attempts"] == 1 and report["cache_unknown_attempts"] == 1
    assert report["attempt_median_cached_share"] is None
    assert diagnostics == {"scanned_generation_rows": 2, "rows_without_shell": 1}


@pytest.mark.parametrize("calls", [{}, "", False, 0])
def test_falsey_non_list_calls_are_malformed_not_no_tool(calls):
    value = row()
    value["response"] = json.dumps({"finish_reason": "stop", "tool_calls": calls})
    diagnostics = Counter()
    assert summarize([value], diagnostics=diagnostics) == []
    assert diagnostics == {"scanned_generation_rows": 1, "skipped_call_shape": 1}


@pytest.mark.parametrize("fields", [{}, {"tool_calls": None}, {"tool_calls": []}])
def test_missing_null_and_empty_call_lists_remain_valid_no_tool(fields):
    value = row()
    value["response"] = json.dumps({"finish_reason": "stop", **fields})
    diagnostics = Counter()
    [report] = summarize([value], diagnostics=diagnostics)
    assert report["complete_responses"] == report["no_tool_responses"] == 1
    assert diagnostics == {"scanned_generation_rows": 1}


def test_role_cohorts_and_attempt_metrics_include_nonstandard_or_failed_completions():
    ordinary = row(guidance=True, cached=100)
    ordinary["ttft_ms"] = 10
    failed = row(guidance=True, cached=0, status="failed", finish=None)
    failed["ttft_ms"] = 50
    unusual = row(guidance=True, cached=50, finish="custom_boundary")
    edited = row(guidance=True)
    context = json.loads(edited["context"])
    context["prompt_layers"][0]["role"] = "user"
    edited["context"] = json.dumps(context)
    system, user = summarize([ordinary, failed, unusual, edited])
    assert system["guidance_sha256"] == user["guidance_sha256"]
    assert (system["guidance_role"], user["guidance_role"]) == ("system", "user")
    assert system["attempts"] == 3 and system["complete_responses"] == 1
    assert system["attempt_median_cached_share"] == 0.5
    assert system["attempt_median_ttft_ms"] == 30 and system["ttft_reported_attempts"] == 2
    assert system["attempt_finish_reasons"] == {"tool_calls": 1, "<missing>": 1, "custom_boundary": 1}


def test_multi_call_omissions_count_response_once():
    value = row()
    response = json.loads(value["response"])
    response["tool_calls"] *= 2
    value["response"] = json.dumps(response)
    [report] = summarize([value])
    assert report["omission_responses"] == 1 and report["missing_operation_calls"] == 2
    assert report["nonempty_shell_objects"] == 2


def test_malformed_ledger_rows_are_counted_without_hiding_evaluator_errors(monkeypatch):
    rows = [row() for _ in range(4)]
    rows[0]["body"] = '{"tools":[{"type":"function","function":null}]}'
    rows[1]["context"] = '{"prompt_layers":[{}]}'
    rows[2]["response"] = "not JSON"
    diagnostics = Counter()
    [report] = summarize(rows, diagnostics=diagnostics)
    assert report["attempts"] == 1
    assert diagnostics == {
        "scanned_generation_rows": 4,
        "skipped_tool_shape": 1,
        "skipped_prompt_layer_shape": 1,
        "skipped_invalid_json": 1,
    }

    def broken(*args):
        raise ValueError("evaluator bug")

    monkeypatch.setattr("scripts.report_shell_calls.classify", broken)
    with pytest.raises(ValueError, match="evaluator bug"):
        summarize([row()])
