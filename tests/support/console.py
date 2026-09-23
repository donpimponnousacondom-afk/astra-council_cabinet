"""Shared console test helpers."""

import io

from hortator.console import OperationalConsole


def output(**kwargs):
    return OperationalConsole(stream=io.StringIO(), keys=False, color=False, **kwargs)


def read_all_evidence(console):
    for _ in range(100):
        if console.evidence.get("next") is None:
            return
        console.key("n")
    raise AssertionError("Synthetic evidence should fit within 100 bounded pages")
