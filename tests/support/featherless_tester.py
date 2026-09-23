"""Shared featherless tester test helpers."""

import featherless_tester as probe


def options(**changes):
    opts = probe.parser().parse_args(["--model", "test/model", "--yes"])
    for key, value in changes.items():
        setattr(opts, key, value)
    return opts


def model():
    return {
        "id": "test/model",
        "context_length": 32768,
        "concurrency_cost": 2,
        "availability": {"tier": "warm"},
    }
