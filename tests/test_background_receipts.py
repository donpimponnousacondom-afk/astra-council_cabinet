import copy
import pytest

from hortator.store import dumps
from hortator.working_set import bound_exchanges, prompt_exchanges
from support.runtime_feedback import tool


def job_batch(count, *, report=False):
    calls = []
    replies = []
    for number in range(count):
        call_id = f"job-call-{number}"
        calls.append(
            tool(
                "research_assistant",
                {
                    "operation": "start",
                    "submission_id": f"submission-{number}",
                    "task": f"Research source {number}: " + "detailed assignment " * 20,
                    "max_output_tokens": 4096,
                },
                call_id,
            )
        )
        body = {
            "job_id": f"bg_{number:020x}",
            "state": "running",
            "next": {"operation": "read_result", "job_id": f"bg_{number:020x}", "offset": 0},
            "result_id": f"result_{number}",
            "capacity": {"available_slots": count - number - 1},
        }
        if report:
            body["content"] = "saved report " * 3000
        replies.append({"role": "tool", "tool_call_id": call_id, "content": dumps(body)})
    return [
        {
            "role": "assistant",
            "reasoning_details": [{"signature": "signed-native", "data": "opaque " * 9000}],
            "tool_calls": calls,
        },
        *replies,
    ]


def index(messages):
    return next(message["_working_set_index"] for message in messages if "_working_set_index" in message)


@pytest.mark.parametrize("count", [5, 16])
def test_large_native_job_batch_keeps_every_compact_receipt(kernel, count):
    extras = job_batch(count)
    original = copy.deepcopy(extras)
    bounded, meta = bound_exchanges(extras, kernel.engine.contexts.estimate, 6000)
    assert extras == original
    assert meta["omitted_groups"] == 1
    assert kernel.engine.contexts.estimate(prompt_exchanges(bounded)) <= 6000
    saved = index(bounded)
    assert saved["omitted_jobs"] == 0
    assert len(saved["jobs"]) == count
    assert [receipt["submission_id"] for receipt in saved["jobs"]] == [
        f"submission-{number}" for number in range(count)
    ]
    assert all(receipt["state"] == "running" for receipt in saved["jobs"])
    assert all(receipt["task_truncated"] for receipt in saved["jobs"])
    assert all("content" not in receipt for receipt in saved["jobs"])
    assert "signed-native" not in dumps(prompt_exchanges(bounded))


def test_extreme_job_batch_is_bounded_and_explains_scoped_recovery(kernel):
    extras = job_batch(100)
    bounded, _ = bound_exchanges(extras, kernel.engine.contexts.estimate, 1800)
    saved = index(bounded)
    assert 0 < len(saved["jobs"]) < 100
    assert saved["omitted_jobs"] == 100 - len(saved["jobs"])
    assert "list operation" in bounded[0]["content"]
    assert "this bot/channel" in bounded[0]["content"]
    assert kernel.engine.contexts.estimate(prompt_exchanges(bounded)) <= 1800


def test_job_receipts_survive_repeated_pruning_and_track_result_offsets(kernel):
    first = job_batch(5)
    bounded, _ = bound_exchanges(first, kernel.engine.contexts.estimate, 6000)
    prior = copy.deepcopy(bounded)
    status_call = tool(
        "research_assistant",
        {"operation": "read_result", "job_id": "bg_00000000000000000000", "offset": 6000},
        "page",
    )
    second = [
        {"role": "assistant", "tool_calls": [status_call], "reasoning_details": [{"data": "opaque " * 4000}]},
        {
            "role": "tool",
            "tool_call_id": "page",
            "content": dumps(
                {
                    "job_id": "bg_00000000000000000000",
                    "state": "succeeded",
                    "offset": 6000,
                    "next": {
                        "operation": "read_result",
                        "job_id": "bg_00000000000000000000",
                        "offset": 12000,
                    },
                    "result_id": "page_evidence",
                    "content": "full original report " * 3000,
                }
            ),
        },
    ]
    continuation = [*bounded, *second]
    unchanged = copy.deepcopy(continuation)
    bounded, _ = bound_exchanges(continuation, kernel.engine.contexts.estimate, 1800)
    assert continuation == unchanged
    assert prior == unchanged[: len(prior)]
    saved = index(bounded)
    assert len(saved["jobs"]) == 5
    first_receipt = saved["jobs"][0]
    assert first_receipt["submission_id"] == "submission-0"
    assert first_receipt["state"] == "succeeded"
    assert first_receipt["read_offset"] == 6000
    assert first_receipt["next_offset"] == 12000
    assert kernel.engine.contexts.estimate(prompt_exchanges(bounded)) <= 1800


def test_cross_tool_reference_and_job_receipt_both_survive(kernel):
    extras = job_batch(5)
    extras.extend(
        [
            {"role": "assistant", "tool_calls": [tool("web_fetch", {"operation": "read_result"}, "web")]},
            {
                "role": "tool",
                "tool_call_id": "web",
                "content": dumps({"result_id": "web-result", "text": "source " * 9000}),
            },
        ]
    )
    bounded, _ = bound_exchanges(extras, kernel.engine.contexts.estimate, 1400)
    saved = index(bounded)
    assert len(saved["jobs"]) == 5
    assert any(ref.get("result_id") == "web-result" for ref in saved["references"]) or any(
        "web-result" in message.get("content", "") for message in bounded if message["role"] == "tool"
    )
    assert kernel.engine.contexts.estimate(prompt_exchanges(bounded)) <= 1400
