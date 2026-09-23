"""Shared provider streaming test helpers."""

import json

import httpx

from hortator.diagnostics import read_diagnostics
from support.provider import Fragments, install_client


def packet(delta=None, finish=None):
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def wire(*packets, done=True):
    text = "".join("data: " + json.dumps(p, ensure_ascii=False) + "\n\n" for p in packets)
    return (text + ("data: [DONE]\n\n" if done else "")).encode()


async def respond(kernel, data, **kwargs):
    await install_client(
        kernel,
        lambda r: httpx.Response(
            200,
            headers={"content-type": "Text/Event-Stream; charset=utf-8"},
            stream=Fragments(data),
            **kwargs,
        ),
    )


def stored(kernel):
    row = kernel.store.one("SELECT * FROM requests ORDER BY started_at DESC LIMIT 1")
    return row, read_diagnostics(kernel.store, row["id"])
