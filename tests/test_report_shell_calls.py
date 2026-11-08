import json

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
            {"prompt_layers": [{"id": "shell_call_guidance", "sha256": "frozen-hash"}] if guidance else []}
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
    assert before["reported_uncached_tokens"] == 111 and before["min_cached_share"] == 0
    assert before["median_cached_share"] == 0.9 and before["median_ttft_ms"] is None
    assert after["valid_action_calls"] == 1 and after["omission_responses"] == 0
    assert "true" not in json.dumps(groups) and "command" not in json.dumps(groups)


def test_report_excludes_requests_without_shell_and_does_not_turn_unknown_into_miss():
    no_shell = row()
    no_shell["body"] = '{"tools":[]}'
    [report] = summarize([no_shell, row(cached=150)])
    assert report["attempts"] == 1 and report["cache_unknown_attempts"] == 1
    assert report["median_cached_share"] is None
