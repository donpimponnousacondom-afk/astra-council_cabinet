"""Shared runtime test helpers."""

import asyncio
import json

import httpx

from hortator.concurrency import join_tasks


def completion(content="Hello", silence=False):
    fn = "council_silence"
    args = {"label": "Listening"}
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call1",
                                "type": "function",
                                "function": {"name": fn, "arguments": json.dumps(args)},
                            }
                        ]
                    }
                    if silence
                    else {"content": content},
                    "finish_reason": "tool_calls" if silence else "stop",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        },
    )


async def settle(k):
    # Superseded turns are intentionally cancelled; join their finalizers while
    # still propagating unexpected exceptions from every task.
    await join_tasks(*list(k.engine.tasks.values()))
    await asyncio.sleep(0)
